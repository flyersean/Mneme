# Mneme — Implementation Plan (for Kimi)

Branch: **novelty-thinking ONLY**. Do not touch `main`, `build-roadmap`, or `dev-chunks`.

## Goal

Mneme currently behaves like a rule-hoarder: it saves narrow, instance-specific
rules ("don't use a weaver", "replace luck with skill") instead of learning
transferable principles. This plan converts it from *memorization* toward
*learning* — where learning means three things:

- **Abstract** — turn an instance into a domain-agnostic principle.
- **Transfer** — apply the principle to new, unrelated domains.
- **Refine** — correct or retire principles that stop working.

It also removes the "AI code slop" that is currently working against those goals:
silent exception swallowing, regex-parsing of model output, scattered magic
truncation numbers, and fire-and-forget threads.

## Environment / how to test

- Single file of interest: `proxy/mneme_proxy.py` (≈2840 lines). Everything
  else (setup, extensions, docs) is out of scope unless a phase says otherwise.
- The proxy runs on a RunPod A40: `python3 -uB proxy/mneme_proxy.py` with
  `MNEME_MODEL=muse-glimmer:30b` (see `docs/muse-glimmer-model.md` for how that
  model is pulled and created).
- Deploy = `scp proxy/mneme_proxy.py root@<ip>:/workspace/proxy/`, then restart
  the proxy (`pkill -f mneme_proxy.py; setsid python3 -uB proxy/mneme_proxy.py ...`).
- Verify each change by (a) `python3 -c "import ast; ast.parse(open('proxy/mneme_proxy.py').read())"`
  for syntax, (b) hitting endpoints with curl, (c) watching `/tmp/mneme.log`,
  (d) querying the SQLite DB at `/workspace/mneme_chunks/mneme.db`.
- The DB is SQLite via a global `db` connection object in `mneme_proxy.py`.

Key endpoints: `POST /v1/chat/completions` (proxy chat), `POST /mode/learn`
(learning mode), `POST /mode/think` (novelty thinking mode), `GET /health`.

Line numbers below are from the current `novelty-thinking` HEAD and are
approximate — they will drift as you edit. Use function names to locate.

---

## Phase 1 — Reliability foundation (do first, low risk)

### 1.1 Kill silent exception swallowing

**Why.** `except: pass` and bare `except:` hide real failures. The
belief-evolution thread and strategy save path can fail silently forever.

**Where.** Bare `except:` at lines 241, 390, 523, 2163, 2219. `except: pass`
at 2285 (`_save_strategy` FAISS dedup) and 2303 (`_check_belief_evolution`).

**Change.** Every `except:` / `except: pass` becomes `except Exception as e:`
that logs to a module-level `_log_error(where, e)` helper. The helper appends
`timestamp | where | type | message` to `/workspace/mneme_chunks/errors.log`.
Do NOT re-raise (the proxy must keep serving) — just make every failure visible.

**Verify.** Introduce a deliberate failure (e.g. temporarily point `DB_PATH` at
a bad path) and confirm an entry appears in `errors.log` instead of vanishing.

### 1.2 Centralize all magic numbers

**Why.** Scattered `[:500]`, `[:1000]`, `[:1500]`, `[:2000]`, `[:8000]` caused
three real bugs this session (the 500-char `<<LEARN>>` detection failure, the
4500 prompt cap, the 1000-char judge seeing mid-sentence text).

**Where.** Every literal slice in `mneme_proxy.py` — see the grep hits around
lines 511, 513, 520, 522, 572, 622, 639, 690, 1335, 1541, 1621, 1721, 1732,
1743, 1753, 1757, 1765, 1773, 1881, 2036, 2078, 2091, 2115, 2149, 2216, 2320,
2325, 2340, plus `MAX_PROMPT_CHARS` (line 849) and related constants near it.

**Change.** Add a single `CONFIG` section near the top of the file (or a
`proxy/config.py` imported by `mneme_proxy.py`) with named constants:

```
MAX_QUERY_CHARS      # was [:500] on user query extraction
MAX_JUDGE_CHARS      # was [:1000] then [:3000] in _pairwise_judge
MAX_STORY_CHARS      # was [:2000] / [:1500] content truncation
MAX_MESSAGE_STORE    # was [:8000] DB_MSG_CAP
MAX_THINKING_STORE   # was [:8000]
MAX_PROMPT_CHARS     # was 4500 -> 200000, keep env override
MAX_PREVIEW_CHARS    # was [:300]/[:400] previews
```

Replace every literal slice with the named constant. Do NOT change values —
this phase is pure refactor, behavior must be identical.

**Verify.** Run the full proxy once before and once after; `/health` returns,
and a chat request produces the same output. No new `[WARN]`/`[ERROR]` lines.

---

## Phase 2 — Structured output instead of regex parsing

**Why.** The single biggest reliability problem. The code asks the model for a
text format and regex-parses the reply; any deviation silently fails.

**Where** (all are `re.search`/`re.finditer`/`re.match` on model output):

| Line | Parses | Used by |
|---|---|---|
| 1737 | `[GRADE: A/B/C/D/F]` | learning mode grading |
| 1756 | `STRATEGY: ...` | learning mode strategy extraction |
| 1772 | `RULE: ...` | learning mode synthesis |
| 1815-1817 | `DIFFERENT/VALID/REASON: ...` | novelty pairwise judge |
| 1851 | `POINT: X \| CONVENTIONAL: Y` | problem decomposition |
| 2232, 2402, 2540 | `[GRADE: ...]` | grading / strategy lifecycle |

**Change.** For each, switch the model call to Ollama structured output:
pass `format=<json-schema>` (or `format: "json"` with a schema in the prompt)
in `query_model`, then `json.loads` the reply instead of regex. Add a
`query_model(..., format_schema=...)` parameter that threads through to the
`/api/chat` payload's `format` field. Keep a lenient fallback: if the reply
isn't valid JSON, fall back to the old regex parse and log a warning — never
crash the request.

**Verify.** For each changed call, send a request and confirm the structured
field is extracted; also send a deliberately malformed/edge-case prompt and
confirm it degrades to the fallback (logged) instead of producing a wrong
empty result.

---

## Phase 3 — Supervised background tasks

**Why.** `threading.Thread(daemon=True)` means errors never surface, work
silently vanishes on crash, and it races the DB (already caused the FAISS
corruption that needed `fcntl.flock`).

**Where.** `threading.Thread(daemon=True).start()` at lines 589 (belief
evolution), 1662 / 2128 / 2242 (archive_staging), 2139 (learning mode),
2542 (strategy lifecycle).

**Change.** Introduce one module-level background worker:
a `queue.Queue` plus N=2 daemon worker threads that pull `(fn, args, kwargs)`
jobs, run them, and — on any exception — write to `errors.log` via the helper
from Phase 1.1 and continue. Replace every direct
`threading.Thread(...).start()` with `_enqueue(fn, *args, **kwargs)`.

Keep the existing `fcntl.flock` / `faiss_lock()` discipline unchanged — the
worker threads still need it; do not remove locking.

**Verify.** Confirm all background work still happens (save a chunk, force
learning mode, watch the log show the enqueued job completing). Confirm a
raising job is caught and logged rather than killing the process.

---

## Phase 4 — Strategy abstraction + refinement + telemetry (the core)

This is the phase that actually changes Mneme from a rule-hoarder into a
learner. The `strategies` table already has `version`, `parent_id`,
`effective_grade`, `use_count`, `success_count` columns (from schema
migrations at lines 132-136) — they exist but are unused. No schema change
needed; just wire them up.

### 4.1 Abstract-at-save (mechanism vs example filter)

**Why.** "Don't use a weaver" is an example. "Don't reach for the most obvious
archetype" is a mechanism. Only mechanisms transfer.

**Where.** `_save_strategy` (line 2270) and `_strategy_lifecycle` (line 2305),
plus `generate_strategy` (line 713).

**Change.** Before saving any strategy text, run one extra model call:
"Rewrite this rule so it references no specific person, object, domain, or
proper noun — keep only the underlying mechanism. If it is already general,
return it unchanged." Store the result. If the abstraction step produces an
empty/garbage result, store the original and log a warning.

**Verify.** Trigger learning mode on the fantasy-story prompt. Inspect
`strategies.strategy_text` — saved strategies should read like "generate
multiple alternatives before committing to the first answer" not "replace
weavers with masons."

### 4.2 Strategy telemetry (close the loop)

**Why.** Learning is meaningless if nothing tracks whether a strategy worked
when later applied.

**Where.** `build_context` (line 994) where strategies are injected, and the
grade-detection at 2232 / 2540.

**Change.** When a strategy is injected into context, record its `strategy_id`.
When the turn's resulting grade is parsed, increment `use_count` and, if grade
is A/B, increment `success_count`; recompute `effective_grade =
success_count / max(use_count, 1)`. Store the injected-strategy IDs for the
turn in a module-level variable set at injection time and consumed at grade time.

**Verify.** Run a few chat turns that trigger strategy injection. Confirm
`use_count` and `success_count` increment in the DB and `effective_grade`
changes.

### 4.3 Strategy refinement (belief evolution for strategies)

**Why.** A learner corrects and retires failing rules; it doesn't just add.

**Where.** The grade-consumption point in 4.2, and `_strategy_lifecycle`.

**Change.** If an injected strategy's turn grades C/D/F, increment a failure
counter (or lower `effective_grade`). When `effective_grade` drops below a
threshold (e.g. 0.25) with `use_count >= 5`, mark the strategy `superseded`
— reuse the same flagging idea as `_check_belief_evolution` for chunks (add a
`superseded_by` / `retired` column via the existing migration block at line
127-138). Superseded strategies are excluded from `build_context` injection.

**Verify.** Manually demote a strategy's `effective_grade` in the DB below
threshold, run a chat turn, confirm it is no longer injected.

---

## Phase 5 — Permanent meta-principles + objective measurement

### 5.1 Permanent meta-principle injection

**Why.** A small set of always-relevant "thinking" directives should always be
present, not similarity-gated. This is how the system emulates a mindset
rather than a memory.

**Where.** `build_context` (line 994) and/or the system prompt assembly in
`process_chat`.

**Change.** Add a hardcoded `META_PRINCIPLES` list (3-5 short imperative lines,
e.g. "The first answer is the mode — generate an alternative before committing.",
"State the conventional answer, then find the assumption that makes it
conventional."). Inject them as a fixed SYSTEM directive block on every turn,
independent of memory retrieval. Keep them short and constant (they should not
consume the dynamic token budget).

**Verify.** Confirm the block appears in `sys_dump.txt` (the debug dump at
`/workspace/sys_dump.txt`) on every turn regardless of query.

### 5.2 Objective measurement replaces self-grading

**Why.** A mode-collapsed model grades its own modal output leniently. Self-
report is a hint; distance and pairwise comparison are the signal.

**Where.** The `[GRADE: ...]` self-report parsing at 1737, 2232, 2402, 2540.

**Change.** Where grading currently trusts the self-reported grade, add an
embedding-distance check: compare the answer's embedding against a baseline
(empty-query or prior-turn) and, when distance is near zero but the self-grade
is A/B, treat the grade as suspect and log it. Do not fully replace the
self-grade yet — augment it and log discrepancies. (Full replacement is a
larger follow-up.)

**Verify.** Run a repetitive query twice; confirm the log notes when a
suspiciously-identical answer self-grades A.

---

## Phase 6 — Cross-domain merging + implicit learning (deferred)

Mark these as **deferred** in the plan; do not implement in the first pass.
They are high-value but high-risk and depend on Phases 1-5 stabilizing.

- **6.1 Cross-domain principle merging** — when the same mechanism appears in
  two problem types, merge into one principle. Requires embedding-clustering
  strategies by their `strategy_text` and merging near-duplicates.
- **6.2 Implicit learning** — at session end, extract one general principle and
  dedupe against the store, so learning accumulates without an explicit
  `<<LEARN>>` / `/mode/think` trigger.

---

## Acceptance criteria summary

1. `errors.log` exists and captures failures that were previously silent.
2. All magic truncation numbers are named constants in one place.
3. No regex-parsing of model output remains (except as a logged fallback).
4. All background work runs through one supervised worker; a raising job is
   logged, not fatal.
5. Saved strategies reference mechanisms, not specific examples.
6. `use_count` / `success_count` / `effective_grade` update in the DB.
7. A strategy demoted below threshold stops being injected.
8. Meta-principles appear in context every turn.
9. Suspicious self-grades (identical output graded A) are logged.
10. All of the above verified on the running pod, on `novelty-thinking`,
    pushed to origin.

## Working notes / gotchas

- Line numbers drift as you edit; locate by function name.
- The DB is a global `db` (sqlite3) opened at import; schema migrations run at
  lines 127-138 — add any new column there, wrapped in `try/except OperationalError`.
- The proxy must never 500 from a bad model reply — every model-output parse
  needs a fallback.
- Keep `faiss_lock()` / `fcntl.flock` discipline intact in the Phase 3 worker.
- Do not change values in Phase 1.2 (pure refactor).
