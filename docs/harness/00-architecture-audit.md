# Phase 0 — Architecture audit (agent-harness transformation)

Audited from the code on branch `agent-harness` (commit `c4574fa`), not from the docs.
Line numbers refer to `proxy/mneme_proxy.py` at that commit unless stated otherwise.

---

## 1. Current architecture

### 1.1 Shape

```
client (Pi / chat UI / swarm / any OpenAI client)
   │  POST /v1/chat/completions
   ▼
proxy/mneme_proxy.py  (Flask, ~8.2k lines, one process = one model)
   ├── config loader       _CONFIG_ENV_MAP → env vars (L110–440), hot reload (L631)
   ├── process_chat()      THE agent loop (L5533–6350)
   ├── query_model()       provider layer: Ollama native / OpenAI-compatible (L2026–2620)
   ├── build_context()     memory + strategy retrieval → injected text (L3560)
   ├── staging/archive     StagingBuffer → topic-split chunks (L3843–4300)
   ├── grading             provenance-based A/B/C/F (mneme/grading.py)
   ├── strategy layer      _save_strategy / _strategy_lifecycle / telemetry (L6369–6690)
   └── Flask routes        UI pages, memory curation, MCP, /v1 (L6712–8040)
proxy/mneme/*.py           extracted modules (tools, curation, capability, overcome,
                           tool_trail, instructions, templates, chatcmd, mcp_client, util)
proxy/gateway.py           reverse proxy fronting many instances on one port
extensions/swarm/          HTTP-only config-driven orchestrator (serial + parallel)
scripts/mneme_setup.py     setup wizard → <db>/instances/<port>/mneme.yaml
```

The proxy module does real work **at import time**: opens the SQLite DB, runs
migrations, probes the embedder, loads FAISS, calibrates noise, materializes prompts
(L1161–1280, L8157–8183). Every test that touches the proxy works around this by
setting env vars and a dead backend before `import mneme_proxy`.

### 1.2 Traced execution path of one request

1. **Entry** — `chat_completions()` (L7230). Reads `options` (nested only) for
   per-call overrides, clears the process-wide `_cancel_event`, derives a
   `session_id`, dispatches to `process_chat` (or `_chat_stream`, which runs the
   whole turn and then *replays* it as SSE — streaming is cosmetic).
2. **Model selection** — there is none per request. The process is hard-wired to one
   model (`MODEL`, `backend.type`, `providers.<name>`). `model: "default"` in the
   request is ignored. Per-model sampling overrides come from `models.<name>` and
   model templates (`mneme/templates.py`).
3. **Command handling** — `<<DETAIL>>`, `<<SETTINGS>>`, `<<RETRIEVAL …>>`,
   `<<SAVE>>`, `<<LEARN …>>` are parsed from the last user message at the top of
   `process_chat` (L5560–5671) and stripped from history. These are the existing
   *user-typed harness controls*; they short-circuit the model.
4. **Context construction** — two injected system messages:
   - a **fixed** block after the client's system message: `system_prompt.md` via
     `_system_prompt_block()` (+ meta-principles when not memory-only, + overcome /
     build / reuse / tool-failure directives when triggered) — L5724–5775;
   - a **variable tail** before the last user message: retrieved memory +
     strategies (`build_context`) + advisory directives (saved-tool hint, explore,
     relevant built tools) — L5785–5807.
   Then the conversation is windowed to `staging_turns + context_recent_extra`
   turns under a token budget (`_recent_window`).
5. **Memory retrieval** — `build_context(query)` embeds the last user turn only,
   `route_query` (FAISS cosine, absolute floor `inject_min_similarity`, dynamic-K
   over a calibrated noise baseline), sibling expansion, grade-aware ordering,
   per-topic cap, token-budget trim, curation labels, strategy block.
6. **Tool exposure** — `mntools.assemble_tools(client_tools)`: read-only server
   tools (search_memory, list_tools, read_tool, read_image, read_file, fetch_url,
   web_search) + native `bash`/`write` (auto = only if client lacks them) + client
   passthrough + MCP tools + curation tools. **All tools are sent every turn**;
   only *built* tools are retrieval-gated (`inject_relevant_tools`).
7. **Tool-call parsing/execution** — native function-calling first; text fallbacks
   for DSML, Gemma `tool_code`, JSON blobs, XML `<tool_call>` (L1713–1980). The
   server-side loop (L5954–6153) executes search_memory / registry / native / MCP
   calls, feeds results back (mostly as *user* messages), compacts the followup to
   the token budget, and re-queries until the model answers, a redundancy stop
   fires, or `MAX_SERVER_ROUNDS` is hit. Non-server tools pass through to the
   client (the client executes and re-sends).
8. **Strategies** — stored in `strategies`; retrieved by *linkage* to matched chunks
   (`source_chunk`) with a legacy `problem_type` fallback (`_strategy_block`,
   L3223). Saved by: grade-A `STRATEGY:` tag in the reply, novel-procedure
   detection, `_strategy_lifecycle` (D/F → "don't do" directive), `_ask_reusable_strategy`.
   Telemetry (`use_count`, `success_count`, `effective_grade`, auto-retire) closes
   the loop in `_consume_injected_strategies`.
9. **Persistence of results** — the turn is added to the in-memory `StagingBuffer`;
   the *next* request (or idle timeout / `<<SAVE>>`) flushes it into topic-split
   `chunks` rows + FAISS vectors, with provenance columns (`injected_chunk_ids`,
   `derived_from`, `self_confirm`, `trust`, `model`).
10. **Failure handling** — per-turn only: one provider retry on timeout/error, an
    empty-reply retry, "continue" nudges for shrug answers, redundancy stop, explanatory
    fallback text, grade `F`. Background jobs log to `errors.log`. Nothing
    records *that a turn failed* outside the log and the chunk grade.
11. **What survives restart** — chunks, FAISS index, strategies, preferences,
    capability_edges, tools registry, curation_log, instructions files, config.
    **Lost on restart:** the staging buffer (un-archived turns), the in-flight tool
    loop, `_tool_trace`, the cancel flag, topic-switch state, injected-strategy ids.
12. **What a "session" is** — effectively nothing. `session_id` is
    `conv_<md5(first msg)>_<time%100000>` for the *first* turn of a conversation and
    the literal `"default"` for every later turn (L7248–7254). It is stamped on
    chunks but never used to reconstruct anything. Conversation state lives in the
    client, which re-sends history each turn.
13. **What an "agent" is** — one `process_chat` call: a single bounded tool loop for
    one user turn. There is no goal that outlives a turn, no plan, no task list, no
    notion of "done" beyond "the model produced text".
14. **Experimental systems that are harness-capable** — see §2.

### 1.3 Other subsystems

| Subsystem | Where | Notes |
|---|---|---|
| Provider layer | `query_model`, `_query_openrouter`, `_query_model_impl` | Ollama native + any OpenAI-compatible; OpenRouter fallbacks/routing; cancel-aware streaming; reasoning handling. Already provider-agnostic. |
| Config | `_CONFIG_ENV_MAP`, `load_config`, `_reload_sampling_if_changed` | env > yaml > default; unknown keys abort; hot reload of sampling/retrieval. |
| Memory | `chunks` + FAISS (`faiss.index`/`faiss.idmap`) | shared across proxies via `db_path`; per-instance `chunk_dir`. |
| Provenance / curation | `mneme/curation.py` | retract/flag/remove (non-destructive), `curation_log` (append-style decision log with `prev_state`), lineage via `injected_chunk_ids`/`derived_from`, self-confirmation detection. **The best existing model of "never destroy, always attribute".** |
| Grading | `mneme/grading.py` | deterministic inline `[source:]`/`[guess]` provenance grading + fake-citation check against the turn's trace; slow judge fallback. This is already *harness-side verification of model claims*. |
| Capability edges | `mneme/capability.py`, `mneme/overcome.py` | grade → per-problem-type failure map → "overcome" mode (build/reuse a tool, `DECISION:`/`PLAN:`/`TOOL_SAVE:` markers). Off in memory-only mode (default). |
| Tools | `mneme/tools.py`, `mcp_client.py` | static JSON tool defs; `tools` table = model-built tools with embedding, success_count, script_source. |
| Tag protocol | `tool_trail.py`, `grading.py`, `overcome.py` | model→harness: `[TOOL:SUCCESS]`, `[TOOL:FAILURE: why]`, `[source: X]`, `[guess]`, `STRATEGY:`, `DECISION:`, `PLAN:`, `TOOL_SAVE: a :: b :: c`. user→harness: `<<SAVE>>`, `<<LEARN>>`, `<<SETTINGS>>`, `<<RETRIEVAL>>`, `<<DETAIL>>`. |
| Learning modes | `_run_learning_mode`, `_novelty_thinking_mode` | multi-iteration exploration with a pairwise judge; fire-and-forget, results only in strategies/`learned_ideas.jsonl`. |
| Background work | `_enqueue` + 2 daemon workers | in-memory queue; jobs lost on restart. |
| Prompts | `mneme/instructions.py` | every injected prompt materialized to `instructions/{default,<model>,instance_<port>}/*.txt` — editable, layered, but **unversioned** (edit overwrites). |
| UI | `proxy/static/*.html` | dashboard hub, chat, memory manager, instructions editor, templates, ollama. |
| Gateway | `proxy/gateway.py` | a *reverse proxy*, not the gateway abstraction in the harness spec (name collision — see §7). |
| Swarm | `extensions/swarm/` | HTTP-only, filesystem-as-state, config-driven control flow; no durable run record (a crash loses the index position). |
| Tests | `tests/*.py` | standalone scripts (unittest or a custom runner), no pytest. Baseline: 19/21 suites pass; `test_mcp_client`/`test_mcp_endpoints` fail on the installed `mcp` package (fixture imports `mcp.server.mcpserver`) — environmental, pre-existing. |

---

## 2. Existing components that already satisfy parts of the design

| Harness concept | Existing piece | Coverage |
|---|---|---|
| Execute / observe loop | `process_chat` tool loop | Good for *one step*: executes, observes, compacts, stops on redundancy. |
| Deterministic observation | `tool_trail._classify_tool_outcome`, `_recent_attempts_summary` | Objective failure detection on tool results — the seed of deterministic verification. |
| Verification of claims | `grading.py` (provenance + fake-source trace check), `_verify_and_regrade` | Harness verifies model claims against what actually happened this turn. |
| Provenance | `curation.py` columns + `curation_log` + lineage | Chunk-level; not yet for strategies, prompts, tools, runs. |
| Strategy telemetry | `use_count`, `success_count`, `effective_grade`, `retired`, `version`, `parent_id`, `cost` | Real feedback loop, but `version` overwrites the row (`INSERT OR REPLACE`) — history lost. |
| Failure → experience | `_strategy_lifecycle` (D/F directive), capability edges, junk-directive filter | Failures already become stored knowledge. |
| Tool as capability | `tools` table (embedding, success_count, script_source, last_used) + retrieval-gated injection | Only for model-built tools; built-ins have no metadata beyond the JSON schema. |
| Capability selection | retrieval-gated built-tool injection, linkage strategy retrieval, per-tool enable flags | Partial; built-in tools are always all exposed. |
| Tight prompt split | fixed (cacheable) system block vs variable tail | The static/dynamic split the spec asks for already exists — Phase 4 extends it rather than inventing it. |
| User control commands | `<<…>>` commands in `chatcmd.py` | The right authority model (user-typed, not model tools). |
| Budgets | `MAX_SERVER_ROUNDS`, `BUILD_MAX_*`, token budgets, redundancy limit | Per-turn only; not per goal. |
| Workspace | `tools.dir` (bash cwd), swarm run dir | No per-run isolation. |
| Multi-agent | swarm (serial/parallel), multi-instance shared DB | Works, but outside any run model. |
| Profiles | model templates (`model_templates.yaml`, `/templates`) | Sampling/quirk bundles per model — a sub-part of an agent profile. |

## 3. Components to extend

- **`process_chat`** → becomes the *step executor* of the harness (one model turn +
  its server-side tool loop). Do not fork it; wrap it.
- **`tool_trail` / `grading`** → the start of the verification layer (Phase 5).
- **`strategies` table** → add versions (history rows instead of `INSERT OR REPLACE`),
  provenance (`created_by`, `derived_from`, `validated_by run`), prerequisites,
  verification method, success/failure per run (Phase 3).
- **`tools` table + `mntools` static defs** → one tool-metadata registry (risk,
  permission level, cost, verification, examples) covering built-ins, MCP and
  model-built tools (Phase 3).
- **`instructions.py`** → version prompt files instead of overwriting (Phase 6).
- **`curation_log`** → generalize the pattern (actor, reason, prev_state) into a
  system-evolution log for every modifiable object.
- **`chatcmd.py` `<<…>>` commands** → the harness control commands (`/status`,
  `/runs`, …) should reuse this authority model (Phase 8).
- **Dashboard** → add runs views rather than a new UI (Phase 8).
- **Swarm** → drive child runs of a parent run (Phase 10).

## 4. Components that should eventually be deprecated

- `retrieval.route_threshold`, `retrieval.classify_threshold` (documented as dead).
- The legacy `problem_type`-keyed strategy fallback once linkage/run-based retrieval
  covers it; `_classify_problem_type` keyword taxonomy as the primary capability key.
- The *duplicated* grade-A `STRATEGY:` save path inside `chat_completions`
  (L7268–7318) — it re-implements `_save_strategy` inline with a hard-coded
  `problem_type="model"` (which the startup migration then rewrites to `other`).
- The second "effectiveness" update in `chat_completions` (L7325–7350) — a different
  formula (EMA) on the same columns as `_consume_injected_strategies`, matched with
  `LIKE %id%`.
- Import-time side effects in `mneme_proxy.py` (move into an explicit `startup()`),
  once tests no longer rely on them.
- The pseudo `session_id` derivation — replaced by real sessions mapped to runs.
- Fire-and-forget `_enqueue` for anything that must survive restart (→ jobs, Phase 9).

## 5. Database / storage changes required

Phase 1 (this change) — **additive only**, in a separate file:

- New SQLite file `harness.db` next to the shared memory DB (`<db_dir>/harness.db`,
  override `harness.db_path`). Tables: `runs`, `tasks`, `steps`, `tool_calls`,
  `events` (append-only, enforced by triggers), `artifacts`, `checkpoints`,
  `harness_meta` (schema version).
- New directory `<db_dir>/runs/<run_id>/{input,workspace,artifacts,logs,checkpoints}`.
- No change to `mneme.db`, FAISS, or any existing table.

Later phases (not done here):

- `strategies` → `strategy_versions` history table + provenance columns (Phase 3).
- `skills`, `skill_versions`, `tool_meta` tables (Phase 3).
- `prompt_versions` / `profile_versions` / a general `evolution_log` (Phase 6).
- `jobs` table (Phase 9).
- Link columns from memory to runs (`chunks.run_id`) so memory extracted from a run
  is attributable (Phase 3/6).

## 6. Smallest viable Phase 1

1. `proxy/mneme/harness/ledger.py` — durable Run/Task/Step/ToolCall/Event/Artifact/
   Checkpoint store; explicit state machine; append-only events; ownership lease +
   heartbeat so several proxies can share one ledger without stealing each other's
   runs.
2. `proxy/mneme/harness/engine.py` — a run executor: iterates tasks, runs steps via a
   pluggable executor, checkpoints after every step, enforces budgets, honours
   pause/cancel between steps, and recovers orphaned runs after a crash/restart.
   `checkpoint / resume / pause / cancel / retry` are all implemented here.
3. `proxy/mneme/harness/workspace.py` — per-run workspace abstraction.
4. `proxy/mneme/harness/chat_executor.py` — adapter that runs one task step through the
   existing `process_chat` (reuse, not replacement) and records its tool trace as
   tool calls.
5. `proxy/mneme/harness/http.py` — `/runs` API, registered on the existing Flask app.
6. Config: `harness.*` keys; everything default-on but inert until a run is created.

Planning (Phase 2) is deliberately **not** in Phase 1: a run's task list is supplied by
the caller (or defaults to one task = the goal).

## 7. Risks and compatibility concerns

- **Import-time side effects.** The harness must not require importing
  `mneme_proxy`; it is a standalone package bound to the proxy at startup (same
  pattern as `mneme/capability.py`, `mneme/tools.py`).
- **Global per-turn state in `process_chat`** (`_cancel_event`, `_last_injected_ids`,
  `_INJECTED_STRATEGY_IDS`, `staging`). A harness step and a chat request running
  concurrently can cross-contaminate these. Mitigation in Phase 1: harness steps are
  serialized through one lock; the chat UI's Stop button also stops an in-flight
  harness step (documented). Real fix: move turn state into a per-call context (Phase 2).
- **Cancellation is cooperative.** Pause/cancel take effect at step boundaries; an
  in-flight model call finishes first.
- **At-least-once steps.** A step interrupted by a crash is re-executed on resume. Tool
  side effects (bash/write) are not idempotent, so auto-resume is **off** by default;
  interrupted runs come back as `paused` with a `run_interrupted` event.
- **Shared DB across proxies.** The ledger lives beside the shared memory DB so every
  proxy sees every run; the lease/heartbeat prevents two processes executing one run.
  SQLite WAL on network filesystems remains unsupported (as for `mneme.db`).
- **Naming collision.** `proxy/gateway.py` is a reverse proxy; the spec's "Gateway"
  (CLI/Web/Telegram adapters) is a different thing. Phase 9 should name its interface
  distinctly (e.g. `mneme/harness/gateways/`) and keep the reverse proxy as is.
- **Security.** Runs execute `bash`/`write` with the proxy's privileges. The proxy has
  no auth and binds 127.0.0.1 by default; `/runs` inherits that. Permissions/approvals
  are Phase 5–7 work; until then a run has exactly the tool power of a chat turn.
- **Existing bug found during the audit:** the non-streaming, non-`/v1` branch of
  `chat_completions` (`/api/chat/completions`, `/chat/completions`) built its response
  and never returned it (Flask 500). Fixed alongside Phase 1 with a regression test.

## 8. Tests to add before modifying core execution behaviour

Phase 1 does not change `process_chat`. Before Phase 2 touches it, add
characterization tests for:

1. The static/dynamic injection split: exactly one fixed Mneme system block, one tail
   block before the last user message, no double injection across tool rounds.
2. Tool-loop invariants: every emitted call is either executed server-side or passed
   through; redundancy stop terminates; `MAX_SERVER_ROUNDS` terminates with content.
3. Grade outcomes for: empty reply, provider timeout, fake citation, honest `[guess]`.
4. Command stripping (`<<…>>`) from history and the short-circuit commands.
5. Staging → archive timing (a fact saved on turn N is retrievable on turn N+1).
6. Cancel flag semantics (Stop between rounds ends the turn with `[Stopped by user.]`).

Most of (2)–(5) already exist in `tests/test_tool_loop.py` (88 cases); (1) and (6) are
the gaps. Phase 1 adds its own suites: `test_harness_ledger.py`,
`test_harness_engine.py` (including a real kill-and-restart subprocess test) and
`test_harness_proxy.py` (the `/runs` API against the real Flask app with a stubbed
`process_chat`).
