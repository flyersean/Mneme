I have a complete picture now. I've read all 2,153 lines of `mneme_proxy.py`, the system prompt, README, PROBLEMS, the Gemini review, and the Mneme skill (which contains extensive operational history). Let me write the analysis.

---

# Mneme Proxy — Independent Architectural Review

I read the full codebase (2,153 lines of `mneme_proxy.py` on `dev-v2`, system prompt, README, PROBLEMS, GEMINI_REVIEW, INSTALL). This is my own analysis; where I agree with the Gemini review I say so explicitly and add something it missed, since Gemini reviewed from a description, not the code.

## TL;DR

Mneme is a genuinely working system with real, measured results (26/26 precision tests, 30-turn stability, cross-session memory). The core idea — text injection over KV-cache surgery — is correct and well-validated. But the code has grown by accretion into a single 2,153-line module with three architectural problems that will bite hard at the next stage of growth: **(1) dead/duplicated code paths that mask real bugs, (2) global mutable state with race conditions that break under the `threaded=True` server it already runs, (3) an injection pipeline that works at current scale (~hundreds of chunks) but has O(n) scans and correctness gaps that degrade silently as memory grows.** The self-grading/strategy loop is the most novel and least safe part — it's an unverified self-attribution channel that directly feeds the hallucination loop documented as P2.

## What genuinely works well

- **Text injection as the memory mechanism.** Dumping KV-cache approaches (per the module docstring lineage from raw-k-cache) for plain text injection was the right call. It's model-agnostic, debuggable (`/tmp/injection_log.txt` shows exactly what the model saw), and survives model swaps because vectors are embedder-scoped, not model-scoped. This is the strongest architectural decision in the project.
- **The chunk+pool embedder** (`chunk_text` / `pool_embeddings`, lines 187–221) with L2-normalized centroids is clean, correct, and correctly keeps cosine-similarity compatibility with `IndexFlatIP`. Overlap clamping (`overlap = min(overlap, chunk_size // 2)`) is a small detail done right.
- **Failure-soft embedding** (`embed`, lines 234–259): returning a zero vector on any Ollama failure so archival never 500s is exactly the right resilience posture for a proxy. The zero vector simply won't match in FAISS; data is preserved in SQLite for re-embedding.
- **Hybrid search** (`_hybrid_search`, lines 318–329) is a pragmatic, honest admission that arctic-embed2's ~0.26 noise floor (documented in README Known Issues) means pure vector search misses paraphrases. Filling sparse FAISS results with keyword matches is simple and the tests show it works.
- **The debugging/ops discipline** encoded in the skill and logs (injection log, compression log, health endpoint, `/list` `/search` `/detail` introspection endpoints) is unusually good. Most memory systems are black boxes; this one is inspectable. Keep this.

## What's fragile — ranked by severity

### 1. `classify_chunk` references an undefined variable — dead code hiding a live bug

Lines 636–674: `classify_chunk` builds `text` from `messages`, then at line 644 iterates `for m in msgs:` — **`msgs` is not defined in this function's scope.** It would raise `NameError` if ever called. It's only not crashing because nothing calls it (it's superseded by `_topic_split` / `_classify_message`). Same pattern in `_archive_single_chunk` line 1222 — `for m in msgs:` there *is* defined (it's the parameter), so that one is fine, but the near-identical dead twin makes it easy to misread. This is the canonical risk of the file's structure: large stretches of superseded code (`_compress_large_tool_results_OLD`, `_advance_chunk_OLD`, `_model_loop_read_all_OLD`, `classify_chunk`, `_archive_split` + the orphaned `_segment_by_user` docstring-body at lines 1270–1286 which is **a function body with no `def`** — dead code sitting at module indent inside `_archive_single_chunk`'s return path) are still present and syntactically load-bearing in places.

**Fix:** delete the dead code, don't comment it out. The `_segment_by_user` orphan (lines 1270–1286) is unreachable code after `_archive_single_chunk`'s `return 1` — it will never run, but it makes the function look like it does something it doesn't. Then add a `python3 -c "import ast,sys; ast.parse(open('proxy/mneme_proxy.py').read())"` plus a `pyflakes` run to CI / the restart script; `NameError` like this is exactly what pyflakes catches for free.

### 2. Global mutable state races under the threaded server you already run

`app.run(..., threaded=True)` (line 2150) means every request handler runs concurrently. The shared state:

- `_archive_cycle` / `_chunk_seq` — correctly guarded by locks. Good.
- `db = sqlite3.connect(DB_PATH, check_same_thread=False)` (line 78) — a **single connection shared across all threads with no lock around writes**. `save_chunk` does `db.execute(...); db.commit()` (lines 498–505) from request threads *and* from the background `archive_staging` threads spawned at lines 1735, 1775, 1528. SQLite serializes at the file level with WAL, but the Python `sqlite3` connection object itself is not safe for interleaved `execute`/`commit` from multiple threads — you can interleave two threads' statements into one transaction and commit partial state. In practice with WAL + `synchronous=NORMAL` + short statements it rarely corrupts, but it *will* occasionally raise `sqlite3.OperationalError: cannot commit - no transaction is active` or silently group unrelated writes. This is also the root of **P4 (WAL loss on restart)** — the real fix isn't just a shutdown handler; it's that you have no single writer.
- `compress_large_tool_results._staged_hashes` (lines 1479–1531) — a `set` stored as a function attribute, mutated without a lock, and **grows forever** (never cleared). Two threads staging simultaneously can both pass the `h in _staged_hashes` check. Minor (dedup is best-effort) but it's also an unbounded memory leak keyed on content hashes.
- `_index` / `_id_map` — reads in `_cosine_search` take `_idx_lock`, writes in `save_chunk` take `_idx_lock`, but `_load_index` mutates `_id_map` globally at startup only, so that's fine. However `save_chunk` appends to `_id_map` *and* adds to FAISS under the lock while `route_query` is mid-search in another thread — FAISS `IndexFlatIP.search` is not thread-safe against concurrent `add`. The lock does cover both, so this is actually OK — but only because everything goes through `_idx_lock`. Keep it that way; any future "optimization" that drops the lock on the read path breaks it.

**Fix:** serialize all DB writes through one writer. Cheapest correct version: a module-level `_db_lock = threading.Lock()` wrapping every `db.execute`/`db.commit` pair. Better version: a single `queue.Queue` + one writer thread owning the connection, request threads enqueue write jobs — this also gives you a natural place to checkpoint WAL before exit (fixes P4 properly) and to batch commits. Add `threading.Lock` around `_staged_hashes` or replace it with a bounded `functools.lru_cache`-style structure / just a `set` cleared on flush.

### 3. The injection pipeline does O(n) DB scans and per-chunk JSON loads on every request

`build_context` (lines 859–985) already got the P0 batch fixes (`get_siblings_batch`, `_grade_cache`, `_chunk_cache`) per the skill notes — and it's *still* doing this per request:

- `route_query` → `_cosine_search` over the whole flat index (fine — `IndexFlatIP` is exact, O(n) but vectorized, OK to ~10⁴–10⁵ chunks).
- `get_siblings_batch` then inflates each hit to **every chunk sharing its `topic_label`** (lines 749–769). A hot topic ("conversation", "untitled", or a popular page domain) can have hundreds of siblings; `MAX_SIBLINGS = 3` caps what's *added* per chunk but `topics[topic]` materializes the full list for each.
- `_trim_chunks_cached` then calls `_estimate_tokens` per candidate chunk — word-split over up to `CHUNK_SIZE`×messages text — for potentially dozens of chunks, every request.
- `MEMORY_DISCLAIMER` + hard `context[:3000]` truncation at line 945–946 is a **second, silent budget** layered on top of `MAX_INJECTED_TOKENS`. Two caps fighting each other means the trim logic can carefully select chunks and then have the whole thing scissor-truncated mid-chunk at 3000 chars. The model sees a chunk cut off mid-sentence plus `[memory truncated to fit model context]`. This double-budget is a real correctness smell: pick one budget (tokens), enforce it in `_trim_chunks`, and drop the char scissors.

**Fix:** 
- Store a per-chunk precomputed `token_cost` column (or `char_len`) at `save_chunk` time; `_trim_chunks` becomes a pure sort-and-sum over integers — no text re-derivation per request. This is a one-column migration + computing cost once at write time.
- Cap `get_siblings_batch` results in SQL (`LIMIT` per topic) rather than materializing full topic lists.
- Unify the budgets: delete the `context[:3000]` hard cut, make `MAX_INJECTED_TOKENS` the single source of truth, and let `_trim_chunks_cached` guarantee the output fits. If the 3000-char cap exists because of the documented Qwen-35B ~4,600-char CUDA crash (README Known Issues), then encode *that* as `MAX_PROMPT_CHARS` enforced on the final assembled prompt in `query_model` — where you already have `total = sum(...)` at line 395 — not as a second scissors inside `build_context`.

### 4. The self-grading / strategy loop is an unverified self-attribution channel (P2's real engine)

The README and PROBLEMS document the hallucination memory loop (P2): model says something wrong → archived → re-injected → model "confirms" its own hallucination. The Gemini review's source-tiering suggestion (only index user/page/tool chunks; model chunks only if GRADE: A) is directionally right but **trusts the model's own grade as the gate** — and the grade is assigned by the same model that hallucinated. A hallucinating model will grade its hallucination B or A. Lines 1847–1857 parse `[STRATEGY: ...]` out of model output and insert it with grade hardcoded `"A"` and `problem_type="model"` — which is *also* the exact mismatch behind P3 (strategies typed "model" never match injection queries typed "other"/"error"). So the strategy system currently: (a) can't be retrieved reliably (P3), and (b) when it is retrieved, carries an unearned A grade.

**Fix (beyond echoing Gemini):**
- The meaningful trust signal isn't the model's grade, it's **provenance**: `source='user'` and `source='page:...'` chunks are ground truth-ish; `source='model'` chunks are claims. You already store `source` on every chunk (line 501) — use it in `route_query`/`build_context` as a ranking feature, not just a label. Concretely: in `combined()` (line 732), add a source prior, e.g. `source_boost = {"user": 0.15, "page": 0.10, "tool": 0.05, "model": -0.10, "unknown": 0.0}`. Model-sourced chunks have to *win* on similarity to be injected; user/page chunks get the benefit of the doubt. This directly blunts the hallucination echo without trusting self-grades.
- For strategies: drop `problem_type` matching entirely for retrieval (it's the P3 bug and a rigid schema in a system that everywhere else chose vectors over schemas). Embed `strategy_text` into the same FAISS index (they're short — single `_embed_single` call), tag them in the chunks table or a parallel index, and retrieve top-k by query similarity at injection time. This subsumes Gemini's "strategy vectorization" point and deletes the `problem_type` plumbing rather than patching it.
- Parse grades but treat them as *hints*, and never let a model-sourced chunk be re-injected on the strength of its own grade alone.

### 5. `query_model`'s "smarter truncation" silently rewrites conversation semantics

Lines 375–398: when `len(non_sys) > 4`, you keep `sys_msgs + first_user + recent[-4:]`. This drops the middle of the conversation **without telling the model**. For a memory system whose entire premise is continuity, silently amputating turns 2..n-2 means the model can contradict something said 5 turns ago and the injection system is expected to paper over it — but injection is keyed on the *last 3 user messages* (line 1705), so the dropped middle turns are exactly the ones *not* covered by either the live context or reliably by injection. `MAX_MSG_CHARS = 800` per-message truncation (line 376) similarly mid-sentence-cuts tool outputs the model may have needed verbatim (code, error traces).

**Fix:** make truncation explicit in the prompt (a `[earlier turns omitted — see memory]` marker is one line) so the model knows its context is gappy; and prefer dropping whole oldest *turns* over `[:800]` mid-message cuts. The CUDA-driven `MAX_MSG_CHARS` cap is a hardware workaround — fine — but it belongs in one place (`query_model`) with a logged warning, which you partially have at line 397.

### 6. Smaller but real issues

- **`session_id` generation is buggy** (lines 1833–1839): `h` is only assigned inside `if user_count <= 1:`, then used in the expression on line 1839 which is evaluated regardless. If `user_count > 1`, `h` is unbound → `NameError` on the ternary's evaluation path... actually no — Python evaluates the condition first, so `f"conv_{h}_..."` is only evaluated when `user_count <= 1` is true, and in that branch `h` *was* set. So it doesn't crash — but it's a write-only variable pattern that's one refactor away from a crash, and it means **every multi-turn request gets `session_id="default"`** — so the much-touted cross-session labeling only ever tags the *first* turn of each conversation. Everything after turn 1 lands in the shared "default" session. That substantially undercuts the Phase-7 multi-session claims. **Fix:** accept an `X-Session-ID` / `session_id` field from the client (Gemini's echo-back point is right), hash the *conversation's first message + a client-supplied id*, and persist it — not regenerate-per-request from message count.
- **`_calibrate_noise` runs 3 random-string embeddings + FAISS searches at startup** (lines 690–708) and its result feeds `BASELINE_NOISE`, which is then *also* reassigned at line 2143 — fine — but the fallback constant `0.20` at line 55 is documented as being "overridden at startup," so the constant is misleading. Minor.
- **N+1 in `/search`** (line 2116): one `db.execute` per result row. Batch with `IN (...)` like you did elsewhere.
- **`_keyword_search` LIKE patterns** (line 303): unescaped `%`/`_` in user query words become LIKE wildcards. Minor correctness/perf issue; escape with `ESCAPE '\'`.
- **No WAL checkpoint / graceful shutdown** (P4): agreed with Gemini, but the cleaner fix is the single-writer-queue from point 2 — the writer thread can checkpoint on a timer and on SIGTERM. `signal.signal(signal.SIGTERM, ...)` → set an `Event`, writer loop notices, runs `PRAGMA wal_checkpoint(TRUNCATE)`, persists FAISS (`faiss.write_index`) — note you currently **rebuild FAISS from SQLite on every startup** (`_load_index`), so a clean SQLite checkpoint alone is sufficient; you don't strictly need to persist the FAISS index.
- **`import re` inside hot paths** (lines 614, 955, 1184, 1487, 1710): module-level `re` is already imported at line 22; the shadowing local imports are leftovers. Harmless but noisy.
- **`except:` bare clauses** (lines 178, 338, 1747) swallow everything including `KeyboardInterrupt`. Tighten to `except Exception`.

## Recommended sequencing

1. **Hygiene (hour):** delete dead code (`classify_chunk`, all `_OLD` variants, the orphaned `_segment_by_user` body, `_archive_split`), remove shadow imports, add pyflakes to the restart script. This alone would have caught the `msgs` NameError.
2. **Concurrency (half day):** `_db_lock` around writes now; single-writer queue + SIGTERM checkpoint as the real fix for P4. Lock `_staged_hashes` and bound it.
3. **Injection correctness (half day):** `token_cost` column computed at write time; single token budget (kill the `context[:3000]` scissors); cap siblings in SQL; make truncation emit an explicit omission marker.
4. **Trust model (the important one):** source-prior in `combined()` ranking; strategy retrieval via embedding (deletes P3's schema mismatch); treat grades as hints not gates. This is the fix that addresses P2's root cause rather than its symptom.
5. **Sessions:** client-echoed session IDs so multi-turn conversations actually retain their session (currently broken after turn 1).

## Files

- No files modified — review only, per instructions. Analysis above references exact line numbers in `/tmp/mneme/proxy/mneme_proxy.py` (dev-v2 branch, 2,153 lines).

One caveat: I'm reviewing `dev-v2` (the checked-out branch). The README/skill describe `dev-chunks` as the live branch; where they diverge (e.g. silent staging thresholds, `save_chunk` truncation) some line numbers may shift, but the structural findings — monolith, dead code, shared-connection concurrency, double budgets, self-grade trust — apply to the architecture as a whole, not any one branch.