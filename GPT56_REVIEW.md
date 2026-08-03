All confirmed. Also found: `MAX_PROMPT_CHARS` is referenced on line 396 inside `query_model()` but only defined at line 782 — a module-level forward reference that works only because Python resolves globals at call time (fragile, and it means the trimming logic in `query_model` depends on definition order luck). And `classify_chunk` (line 644) iterates `msgs` which is undefined — would NameError if ever called (it's dead code, but revealing).

I have everything I need. Here is my analysis.

---

# Mneme Architecture Review — GPT-5.6 (Sol) Independent Analysis

## Scope
Read the full `dev-v2` codebase: `mneme_proxy.py` (2153 lines), `system_prompt.md`, `README.md`, `PROBLEMS.md`, `GEMINI_REVIEW.md`, `setup_pod.sh`, `restart_proxy.sh`, and git history. The Gemini review was written **without seeing the code** (it says so itself) — several of its suggestions are already irrelevant or mis-prioritized against the actual implementation. Findings below are my own, from the code.

---

## 🔴 CRITICAL — Live data-loss bug nobody has documented

**`save_chunk` is called with an empty-string `chunk_id`, so every chunk overwrites the last one.**

```python
# line 1247-1255 — _archive_single_chunk()
global _chunk_seq
with _chunk_seq_lock:
    _chunk_seq += 1
    chunk_id = f"mem_{_chunk_seq}"          # sequential ID is computed…
save_chunk("", topic_label, msgs, vec, …)   # …and then thrown away; "" is passed
```

```python
# line 498-504 — save_chunk()
INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
#   → chunk_id="" is the PRIMARY KEY. INSERT OR REPLACE with the same key
#     means SQLite keeps exactly ONE row forever.
```

Consequences:
- **SQLite `chunks` table converges to a single row** (the most recent archive). All prior history in the DB is destroyed on every flush. The README's "26/26 precision/recall tests" can only have passed because FAISS is the *de facto* store of record and it never gets deduplicated.
- **FAISS `_id_map` appends `""` on every add** (line 511). Every vector maps to the same empty ID. `_cosine_search` returns `("", score)` tuples; downstream `load_chunk("")` then returns whatever row currently holds `""` — i.e. the *latest* chunk, regardless of which vector matched. **Retrieval is effectively always returning "the last thing archived," and any semantic-match correctness is accidental.**
- The `[see also: mem_{N+1}]` next-chunk hint (line 935) is dead — real IDs never exist.
- `/detail/<chunk_id>`, `/list`, `/search` all serve the single surviving row or empty results.

This is *the* bug behind "hallucination memory loop" (P2) and much of the confusing test behavior. **Fix before anything else — every other subsystem depends on chunk identity.**

```python
# fix: thread the generated ID through
def save_chunk(chunk_id, ...):
    ...
    db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (chunk_id, topic_label, msgs_json, ...))
# and at the call site:
save_chunk(chunk_id, topic_label, msgs, vec, strategy=strategy, ...)
```
Also add a one-time migration: `UPDATE chunks SET chunk_id = 'mem_' || rowid WHERE chunk_id = ''` won't recover history (it's gone) but will at least re-key the survivor; realistically, wipe and rebuild since the current DB is corrupt by construction. Note `_load_index()` also needs to start rejecting empty IDs.

---

## 🔴 Streaming path silently bypasses the entire memory pipeline

`_chat_stream()` calls `process_chat()` — which does blocking injection via `query_model` (non-streaming) — then **re-chunks the finished string into fake 16-byte SSE deltas** (line 1990-1997). So "streaming" is fake: the client waits for the full generation, then receives it chopped up. Meanwhile the *real* streaming generator `query_model_stream()` (line 419, a genuine SSE passthrough from Ollama) **is never called by any route** — dead code.

Worse, because `_chat_stream` goes through `process_chat`, streaming requests *do* get injection and staging — but the `[STRATEGY:]` parser in the route handler only runs on the **non-streaming branch** (line 1846-1857). Streaming responses never get strategies parsed or saved. Pick one of two designs:
1. **True streaming**: use `query_model_stream`, do injection/staging up front (as `process_chat` does minus the generation), and parse `[STRATEGY:]` after the stream completes by accumulating the emitted content.
2. **Honest buffering**: drop the SSE pretense and return non-streaming for everything.

Right now you have the worst of both: latency of buffering with the complexity of SSE, plus an un-parsed strategy stream.

---

## 🟠 The `query_model` history-truncation logic is confused and doubly-applied

Two different truncation schemes fight in the same function:

- Lines 375-393: per-message 800-char cap + "keep system + first user + last 4 messages"
- Line 396: `if total > MAX_PROMPT_CHARS` — but `MAX_PROMPT_CHARS` isn't **defined until line 782**, 400 lines *after* this function. It works only because Python resolves globals at call time. If anyone moves `MAX_PROMPT_CHARS` below the `if __name__ == "__main__"` guard or into a config loader, `query_model` NameErrors at runtime.
- The `MAX_PROMPT_CHARS=4500` comment says "model crashes above ~4600 chars" — this is a **CUDA OOM workaround for one specific GPU/model combo hardcoded as a global constant**. On the A40 with a 35B Q4 model, 4500 chars is tiny — the README claims 58K-char conversations, which is impossible if this path were actually exercised on the archive side. (It isn't, because archiving bypasses `query_model`'s trimming via direct `embed()` calls. So the constraint is real for the *chat* path only, and it's strangling it: system prompt ~900 chars + injection cap 3000 chars + history trimmed to 4 messages × 800 chars ≈ the model sees almost nothing.)

Concrete fixes:
- Move `MAX_PROMPT_CHARS` (and all tunables) to the config block at the top — or into a `MNEME_*` env-var-driven config object, which PROBLEMS.md already wants ("Config file for user-tunable params").
- The 800-char `MAX_MSG_CHARS` truncation **destroys tool results** before the model sees them, which is likely a bigger contributor to "indirect prompting fails" than the embedding gap blamed in the README. Tool results should be *summarized* (the `compress_tool_output` machinery exists!) rather than beheaded at 800 chars. Wire `compress_tool_output` into `query_model`'s trim path instead of blind slicing.
- The duplicated "if len(non_sys) > 4 … else msgs = sys_msgs + non_sys" then "if total > MAX: strip to last 3" discards the first-user-message task context it just carefully preserved. Pick one invariant: system + task anchor + last-N turns, then enforce a char budget *within that selection*, not by throwing the anchor away under pressure.

---

## 🟠 Dead code carrying a live NameError

`classify_chunk()` (line 636) iterates `for m in msgs:` — `msgs` is **not defined in scope** (the parameter is `messages`). It's never called (classification was replaced by `_generate_topic_label`/`_llm_topic_label`), so the NameError never fires, but it's a trap for anyone who re-enables it. Delete it or fix it. Similarly `_archive_split`, `_archive_single`, `_segment_by_user` (orphaned docstring at line 1270), `_compress_large_tool_results_OLD`, `_advance_chunk_OLD`, `_model_loop_read_all_OLD`, `CLASSIFY_PROMPT`, and `query_model_stream` are all dead — roughly **400 of 2153 lines (~19%) are corpses**. They obscure the live dataflow and are the reason `MAX_PROMPT_CHARS`-style forward references sneak in. Split into modules (storage/embeddings/routing/injection/HTTP) as PROBLEMS.md already suggests — but do it *after* fixing the chunk_id bug, since a refactor now would bake the corruption into a prettier shape.

---

## 🟡 Injection pipeline: real strengths, and one silent contradiction

**What works well — genuinely good ideas here:**
- **Noise-floor calibration** (`_calibrate_noise`, line 690) — embedding random strings at startup to find the FAISS similarity floor, then subtracting `BASELINE_NOISE` in `route_query`. This is a pragmatic, empirical fix for arctic-embed2's ~0.26 noise floor, and it's *measured* rather than guessed. Good pattern.
- **Chunk+pool embedding** (`chunk_text`/`pool_embeddings`) — overlapping 4000-char windows with mean-pooling for long documents, L2-normalized for IndexFlatIP cosine. Correct and standard.
- **Hybrid search with method tagging** — FAISS-first, keyword `LIKE` fallback, results labeled `"faiss"` vs `"keyword"`, keyword matches scored 0.0 so they never outrank semantic hits. Clean.
- **Batch SQL in `build_context`** (grades, chunk bodies, siblings) — the commit history shows a real P0 perf fix here ("fix P0 build_context thrashing"). Good instincts.
- **Source inference chain** (`_infer_source`) — page:domain → tool:name → conversation/user/model. Simple, useful metadata for the P2 source-tiering fix.

**The silent contradiction:** `build_context` enforces `MAX_INJECTED_TOKENS = 2048` via `_trim_chunks_cached`, and *then* hard-truncates the assembled context at **3000 chars** (line 945). 2048 tokens ≈ 8000+ chars by the file's own `_estimate_tokens` (1.3 tok/word ≈ 6 chars/word → ~1.2 chars/token... actually the estimator is ~0.22 tokens/char, so 2048 tokens ≈ 9200 chars). The 3000-char guillotine fires *long before* the token budget does, making the whole grade-aware trim machinery decorative. Worse, the truncation slices **mid-chunk** — the model can receive half a sentence and a dangling `[see also: mem_…]`. Fix: enforce the budget *once*, in characters, derived from the measured CUDA limit, and drop chunks whole rather than slicing the string. Also: `_estimate_tokens` is used for budget decisions but the real constraint is characters — stop converting, just count chars.

**Minor:** `MEMORY_DISCLAIMER` says "reference only, not instruction" while `system_prompt.md` says "Memory takes priority over training data… you MUST grade F [on conflict]." Gemini flagged this as needing stronger epistemic framing; the actual problem is the two documents **contradict each other**. Resolve to one policy: memory is authoritative context for facts about past events, never instructions for behavior — and say exactly that in both places.

---

## 🟡 Concurrency & persistence

- **Single shared SQLite connection** (`check_same_thread=False`) with no write lock, while Flask runs `threaded=True` and archiving happens on daemon threads. WAL mode tolerates concurrent readers but writers still serialize; two simultaneous `archive_staging` threads can interleave `_next_cycle()` and `save_chunk` in ways that scramble cycle ordering (the cycle counter is locked, but the *DB writes between cycles* are not transactional as a group). Add a module-level write lock around the flush path, or use a connection-per-thread pool.
- **P4 (WAL loss on restart) is real and the fix is exactly what Gemini said**: `fuser -k 8080/tcp` in `restart_proxy.sh` sends SIGKILL with no checkpoint. But Gemini's `pkill -15` suggestion is only half the fix — the proxy also needs a SIGTERM handler that checkpoints WAL (`PRAGMA wal_checkpoint(TRUNCATE)`) *and* flushes the staging buffer (currently, unflushed staging content dies silently on restart). FAISS is rebuildable from SQLite via `_load_index()`, so once the chunk_id bug is fixed, FAISS persistence is optional — but the **staging buffer is the only copy** of the last ≤6 turns.
- **No graceful handling of Ollama being down at startup**: `_calibrate_noise` calls `embed` → returns zero vectors → `BASELINE_NOISE` defaults to 0.20. Fine. But `_load_index()` will happily load the corrupt empty-ID vectors (once fixed, empty). Startup is survivable; restarts after Ollama crashes mid-session will fail every `embed` with a 60s timeout each — `_embed_single`'s timeout should be ~5s with the outer `embed()` catching it, not 60s blocking a request thread.

---

## 🟡 Self-grading / strategy loop — the architecture's boldest idea, and its least instrumented

The GRADE/STRATEGY feedback loop is the most original part of Mneme: the model grades itself, the proxy parses `[STRATEGY: …]` on C-or-worse, stores it, and re-injects "PROVEN STRATEGIES" later. That closed loop is worth keeping. But:

- **The regex `\[STRATEGY:\s*(.+?)\]` (line 1848) stops at the first `]`** — a strategy containing `]` (e.g. "use `list[0]` indexing") silently truncates. Use `\[STRATEGY:\s*(.+?)\]\s*$` with `re.DOTALL` or a line-anchored pattern like the grade one.
- **Grades are parsed… nowhere.** `grade` on chunks defaults to `'C'` at insert and is *only* ever set by heuristic `generate_strategy` paths — the model's `[GRADE: x]` output is never extracted from responses in the route handler. So the "grade-aware trim" (`_trim_chunks_cached` sorts A→F) is sorting a column where everything is C. **The entire grade-priority subsystem is currently a no-op.** Parse `[GRADE: ([ABCDF])]` alongside the strategy regex and `UPDATE chunks SET grade=?` for the assistant chunk staged that turn. Until then, delete the sort or admit it's decorative.
- P3 (strategy type mismatch): Gemini's "inject top-3 recent regardless of problem_type" is fine as a stopgap, but the better fix given you already have an embedder: **embed strategies into the same FAISS index as a separate namespace** (`strat_*` IDs, excluded from sibling expansion), so retrieval is semantic rather than type-string matching. That subsumes Gemini's "mini FAISS index" without a second index to maintain.
- P2 (hallucination loop): the code already has `source` on every chunk — the missing piece is exactly Gemini's `indexable` gate, but I disagree with gating on GRADE: A since grades are currently unpopulated (see above). Gate on **source** instead: `user`/`page:*`/`tool:*` → indexable; `model`/`conversation` → stored in SQLite for history, excluded from FAISS. That breaks the echo loop deterministically, no dependence on the model grading itself honestly. If/when grade parsing works, you can additionally promote A-graded model chunks.

---

## 🟢 What's genuinely good (keep these)

1. **Noise-floor auto-calibration at startup** — empirical, self-tuning, documented. Rare to see in hobby RAG.
2. **The staging-buffer decoupling** — ingestion (fast, on the request path) separated from archival (slow: embed + label + insert, on a daemon thread). Correct instinct for latency; just needs the flush-on-shutdown hook.
3. **Session IDs returned in the response body** (`session_id` field) — cross-session labeling already works. Gemini's X-Session-ID header echo is nice but the body field is already there; the client (Hermes) just needs to send it back. The heuristic "≤1 user message = new conversation" (line 1834) is fragile — prefer an explicit client-supplied ID with fallback to the hash.
4. **Hybrid keyword fallback with method labels** — pragmatic and debuggable.
5. **The test methodology in the README** (phases 1-7) is unusually honest — it records *failures* ("Indirect memory prompting: FAISS fails") rather than only wins. Keep that culture.

---

## Recommended fix order

| # | Fix | Effort | Impact |
|---|-----|--------|--------|
| 1 | Thread `chunk_id` through `save_chunk`; rebuild DB | 1 hr | 🔴 stops active data loss |
| 2 | Gate FAISS indexing on `source != model/conversation` | 30 min | 🔴 kills hallucination loop (P2) |
| 3 | Parse `[GRADE:]` from responses; populate `chunks.grade` | 1 hr | un-no-ops grade-aware trim |
| 4 | SIGTERM handler: flush staging + WAL checkpoint; `pkill -15` in restart script | 1 hr | P4 |
| 5 | Single char-based injection budget; drop mid-chunk 3000-char slice | 1 hr | memory actually reaches the model |
| 6 | Fix strategy regex; inject strategies via FAISS namespace (or top-N recent) | 1 hr | P3 |
| 7 | Decide streaming: real SSE via `query_model_stream` or honest buffering | 2 hr | latency + strategy parsing on stream |
| 8 | Move `MAX_PROMPT_CHARS` et al to top config; delete ~400 lines of dead code | 2 hr | maintainability |
| 9 | Replace 800-char tool-result beheading with `compress_tool_output` | 2 hr | likely the real "indirect prompting" fix |
| 10 | Split into modules | 1 day | only after 1-9 |

**Do not** spend time on: RRF score fusion (Gemini's suggestion — the noise-subtraction already works and hybrid fill is rare), IndexIVFFlat (corpus is tiny; FlatIP is correct), or purpose-trained embedders (premature until the pipeline above is sound).

---

## Summary for parent agent

- **Reviewed** the full Mneme codebase (`/tmp/mneme`, branch `dev-v2`): `mneme_proxy.py` (2153 lines), system prompt, README, PROBLEMS, Gemini review, setup/restart scripts, git history.
- **Found a critical undocumented bug**: `_archive_single_chunk` passes `chunk_id=""` to `save_chunk`; `INSERT OR REPLACE` on the `""` primary key means SQLite keeps only the newest chunk while FAISS appends `""` forever — retrieval always returns the latest archive regardless of query. Explains P2 and invalidates prior test results. Root-cause fix identified with code.
- **Found grades are never parsed**: `[GRADE: x]` from the model is never extracted, so the grade-aware trim subsystem is a no-op (everything sorts as 'C'). Strategy regex truncates on `]`.
- **Streaming is fake** (buffered then re-chunked into SSE) and bypasses strategy parsing; the true streaming generator is dead code.
- **Injection budget contradiction**: 2048-token trim then a 3000-char mid-chunk guillotine makes the trim decorative; the 800-char tool-result truncation (not embeddings) is likely the real "indirect prompting" failure cause. `MAX_PROMPT_CHARS` is defined 400 lines after use.
- **Agreed selectively with Gemini** (source tiering, graceful shutdown, strategy vectorization) but re-prioritized: gating on `source` not grade, and noted RRF/IVF are premature. ~19% of the file is dead code, some carrying live NameErrors.
- **Delivered a ranked 10-item fix list** with effort estimates. Top 4 fixes (~4 hrs total) stop data loss, kill the hallucination loop, and un-break grading.
- **No files modified** — analysis only, as requested.