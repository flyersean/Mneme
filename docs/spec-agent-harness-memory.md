# Mneme — Agent Harness + Memory Robustness: Spec & Build Plan

Source: GPT + Gemini reviews (both verified against the code), Sean's decisions.
Status of each item is marked. Order is "easy/correct first", risky/large last.

---

## Principles (Sean's standing constraints)

- Non-destructive: mark, don't delete. "Absence is silent" — old facts stay walkable.
- Simplify over fragment; architectural fix over Nth patch.
- Outcome-first: the task gets done, not a tool gets built.
- Copy a working solution; don't guess.
- Memory stays in the DB, not in process RAM (never-ending session is the goal).

---

## Phase 0 — Done this session

- verify.py: `_SHELL_ERROR` now matches shell-parse only (exit 2 + `/bin/sh: N:` prefix),
  not a language `SyntaxError`/`Unexpected token`. Broken code no longer passes verification.
- `edit_file` tool: surgical `old_string`→`new_string`, fails loudly on 0 or >1 matches,
  atomic write (temp + `os.replace`), returns a short diff.
- `read_file`: line-numbered output + `[path | lines S-E of N]` header.
- `plan=True`: read-only tool grant (intersect with `{read-only, network}`).
- (earlier) embed retry + `allow_fallbacks`; provider-switch base-URL fix; mission-first
  prompt order; wrap-up nudge softened to advisory; `MEMORY BUDGET`→`memory context`.

---

## Phase 1 — Quick correctness wins (independent, do next)

### 1.1 Fix `get_siblings_batch()` — sibling expansion is a no-op
- Where: `proxy/mneme_proxy.py` `get_siblings_batch()` (L5564).
- Bug: it queries `WHERE chunk_id IN (chunk_ids)` and re-groups those same chunks by topic;
  it never fetches *other* DB chunks sharing those `topic_label`s. Docstring claims otherwise.
- Fix: after collecting the topics, run a second query
  `SELECT chunk_id FROM chunks WHERE topic_label IN (topics) AND chunk_id NOT IN (chunk_ids)`
  ordered newest-first, and merge into the per-topic sibling lists (respect `MAX_SIBLINGS` cap).
- Accept: a test where a chunk has a sibling in the DB that is *not* in the retrieved set,
  and `build_context` pulls it in.

### 1.2 Replace brittle `_freeform_done` with an explicit finish signal
- Where: `proxy/mneme/harness/engine.py` `_freeform_done` (L537).
- Problem: completion is recognized only when the *last non-empty line* is literally
  `done`/`complete`/`finished`/etc. Trailing summary/code/punctuation wastes a turn.
- Fix: add a `finish_run(summary, status="completed"|"blocked")` tool (or a structured
  sentinel tag) the model calls explicitly; completion is a tool call, not text parsing.
- Accept: completion detected regardless of trailing text.

### 1.3 (Optional) `search_code` / `glob_files` / `git_diff`
- Low value: `grep`/`find`/`git diff` already work via bash. Defer unless a concrete
  pain point appears.

---

## Phase 2 — Memory mutability ("changing facts") — the core project

Goal: facts that change (chat "I switched X→Y", a file/URL/setting change, a disproved
hypothesis) resolve to the *latest* truth, without deleting history and without trusting
the model to self-curate mid-task. This is the parked "separate project."

### 2.1 Schema (in `proxy/mneme/curation.py` `ensure_schema`)
Add four columns to `chunks`:
- `superseded_by TEXT DEFAULT ''` — chunk_id of the newer chunk that replaces this fact.
- `superseded_at TEXT DEFAULT ''` — ISO timestamp.
- `superseded_reason TEXT DEFAULT ''` — one line: what changed.
- `resource_key TEXT DEFAULT ''` — deterministic identity key
  (`file:/abs/path`, `url:https://...`, `pref:name`, `cmd:...`).
- `CREATE INDEX IF NOT EXISTS idx_chunks_resource ON chunks(resource_key)`.

### 2.2 Label (in `curation.py`, next to `retraction_label`/`bad_chunk_label`)
`superseded_label(chunk)` →
`[SUPERSEDED <date> by <id> — changed: <reason>. Use read_chunk("<id>") for latest.]`

### 2.3 Tier 1 — deterministic supersession (zero LLM, ship first)
- Where: `save_chunk` (L5099) / the archive path, when the new chunk carries a `resource_key`
  (set by the producing tool: `read_file`→`file:path`, `fetch_url`→`url:...`, explicit prefs).
- Logic: if an older *active* (non-removed, non-superseded) chunk has the same `resource_key`
  and different content → set `old.superseded_by = new_id`, `superseded_at`, `superseded_reason`.
- Log to `curation_log` (`action="supersede"`, `actor="system"`).
- Accept: re-read a changed file → old snapshot shows `[SUPERSEDED]`, new snapshot is live.
  This is the immediate fix for stale code/tool/URL state.

### 2.4 `build_context` — resolve supersession chains on retrieval
- Where: `build_context` (the inject path, near the `[see also:]` footer at L6160).
- Logic: if a retrieved chunk has `superseded_by`, walk to the terminal chunk (cycle guard,
  max depth 10), then either (a) inject the terminal chunk instead, or (b) render the old one
  as a one-line historical stub + `[SUPERSEDED]` banner pointing to the latest.
- Keep the old chunk searchable when the user explicitly asks about history.
- Accept: a fact changed 3× → search lands on the oldest, injected content is the newest.

### 2.5 Tier 2 — async semantic reconciler (last; the risky piece)
- Where: a background worker like `_reembed_loop` (L12444) — never blocks a turn.
- Trigger: a new chunk has active neighbors with FAISS similarity ≥ 0.80.
- Model: separate `reconciler_model` config knob (NOT the 0.5b labeler — 3B–7B local or a
  cheap API model). One small structured call per high-overlap pair.
- Output schema (strict JSON):
  `{ "relation": "supersedes|adds_to|unrelated", "confidence": 0.0, "what_changed": "..." }`
- Write `superseded_by` only when `relation=="supersedes"` AND `confidence>=0.8` AND
  `what_changed` non-empty. Log to `curation_log`.
- Prompt guard: "return supersedes only if the new chunk replaces/invalidates the old claim,
  NOT if it describes a temporary test, a different environment, or an additional detail."
- UI: an "Unsupersede" undo action (column flip + curation_log entry).

### 2.6 Edge cases to cover
- Temporary override vs permanent change → `adds_to`, not `supersedes`.
- Partial update inside a big chunk → `superseded_reason` names the part that changed.
- Chain hopping → always resolve to the terminal chunk (A→E), not the next hop.

---

## Phase 3 — Agent execution improvements (medium)

### 3.1 Observation masking (replace transcript flattening)
- Where: `_compact_followup` (mneme_proxy.py L7932); the free-form loop's transcript collapse
  in `chat_executor.py`.
- Problem: prior turns are flattened into a ~700-char/turn one-liner in the *system* prompt,
  which mangles tracebacks/line numbers (amnesia) and mutates the system prompt every turn
  (0% KV-cache reuse).
- Fix: keep the system prompt static for the run; carry a real multi-turn message list; on
  budget pressure mask old tool-result bodies to one-line stubs
  (`[read_file proxy/... L1-200: 200 lines omitted — re-read if needed]`) while keeping the
  assistant `tool_calls` intact; drop oldest only as a last resort. Put dynamic metadata
  (budget/note) in a trailing *user* message, never in system.
- Accept: multi-turn runs keep exact error context; prefix cache hit on consecutive turns.

### 3.2 TurnContext out of module globals
- Where: turn-scoped globals in `mneme_proxy.py` (`_cancel_local`, `_stream_local`, injected
  chunk ids, staging).
- Fix: move into a `TurnContext` dataclass (or `contextvars.ContextVar`) passed explicitly.
- Why: removes the `threading.Lock` serialization in `chat_executor.py` and enables safe
  concurrent/parallel runs (swarm) later.
- Accept: concurrent chat + harness runs do not race.

### 3.3 Git worktree isolation for coding runs
- Where: `harness/engine.py` + `profiles.py`.
- Fix: profile flag `isolate_git: true` → create a dedicated `git worktree` (or `stash create`
  shadow ref) before step 1; `git diff --stat` surfaced in the UI; `/revert <run_id>` discards
  so a failed run leaves the main tree untouched.
- Accept: a coding run that edits 5 files can be reverted with one command.

---

## Phase 4 — Evaluation + scale (deferred; bigger projects)

### 4.1 Live-model benchmark (R29) — the feedback loop
- Build a repeatable task set (fix-a-bug, add-a-test, refactor-a-function) each with a
  deterministic `VERIFY` check, run against real models (deepseek-flash, gemma, etc.).
- Purpose: turn "the model stops early / confabulates a budget" from anecdote into a
  regression number, gating every future mission/prompt/nudge change.

### 4.2 FAISS approximate index (~1M chunks)
- `IndexFlatIP` is exact brute force (full scan). Fine at 100k; migrate to
  `IndexIVFFlat` (train once) or HNSW when approaching ~1M. Drop-in swap in `_cosine_search`.
- Topic steering (optional): `topic_label` is already indexed — a `WHERE topic_label IN (...)`
  pre-filter feeding a smaller search is the "only look at relevant topics" lever; the graph
  table is *not* required for this.

---

## Explicitly skipped (Sean's decisions)

- **Tool-output quarantine** — keep raw tool outputs in memory. Staleness is handled by the
  Phase 2 supersession mechanism, not by not-saving.
- **`chunk_links` graph table** — the `superseded_by` column covers mutability, `topic_label`
  covers topic navigation, and FAISS IVF covers scale. A full graph table is over-engineering
  for these three needs.

---

## Suggested execution order

Phase 1 (1.1 → 1.2) → Phase 2 Tier 1 (2.1 → 2.2 → 2.3 → 2.4) → Phase 3 (3.1) →
Phase 2 Tier 2 (2.5) last → Phase 4 as separate projects.

Phase 2 Tier 1 delivers the "changing facts" fix with zero LLM cost and is the highest-value
memory improvement; Tier 2 (semantic reconciler) is the only item that can make memory
*actively wrong*, so it ships last and gated behind a real reconciler model.
