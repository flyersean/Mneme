# Mneme Tool System — Native Tools + Tool Registry + Injection

## 1. Purpose

Mneme's "overcome a failure" feature lets the model escape a stuck loop by
building a tool or declaring a capability edge. But today "build a tool" only
works if the CLIENT (an agent harness like Pi) supplies `bash`/`write` execution
tools. A thin client — or no client at all — leaves the model nothing to build
with, and every failure collapses into `declare_edge`.

This spec adds three things so Mneme can stand on its own AND coexist with a
harness:

1. **Native tools** — a minimal bootstrap toolset (`bash` + `write`) owned by
   the proxy, so the model can build and run tools with no harness present.
2. **Tool registry** — a persistent store of every tool the model has built,
   which the model can find and read on demand.
3. **Tool injection** — retrieval-gated auto-injection of relevant built tools
   into context, so the model reuses instead of rebuilding.

## 2. Design principles

- **Bootstrap, not toolkit.** Native tools are exactly `bash` + `write`. Nothing
  more. Everything else the model builds. That is the whole point of "build a
  tool to escape an edge" — the escape hatch is build-it-yourself, not a shipped
  library.
- **Dual access, like memory.** Memory is already both auto-injected (retrieval)
  and actively searchable (`search_memory`). The tool registry gets the same two
  paths: passive injection + active lookup.
- **Read-only vs execution.** Read-only tools (`search_memory`, `list_tools`,
  `read_tool`) are never stripped. Execution tools (`bash`, `write`, `web_*`) are
  stripped on the hard-stop. The model can always look up the registry, even
  mid-deliberation.
- **Host-bound reality.** Native tools run on the proxy's host; passthrough tools
  run on the client's host. This is the one thing the `native_tools` flag is
  really controlling (see §8).

## 3. Config

New `tools:` section in `mneme.yaml` (and documented in `docs/config-spec.md`):

```yaml
tools:
  native: auto               # auto | on | off — proxy-owned bash+write bootstrap tools
  dir: ~/mneme/chunks/tools  # canonical directory where built tools are stored
  bash_timeout: 30           # seconds before a native bash command is killed
  inject_min_similarity: 0.75  # STRICTER than retrieval.inject_min_similarity (0.62):
                               # a tool is injected only when it CLOSELY matches the
                               # task (a near-miss script misleads more than it helps)
  inject_max: 3              # max tools auto-injected per turn
  inject_tokens: 600         # token budget for injected tool descriptions
```

Flag semantics:

- `auto` — inject native `bash`+`write` only when the incoming request's tool
  list does NOT already include a `bash`/`write` (thin client -> inject; Pi ->
  don't). Default, so both modes "just work" with zero config.
- `on` — always inject native `bash`+`write` (standalone mode).
- `off` — never inject native (always lean on the harness).

`search_memory`, `list_tools`, `read_tool` are always available regardless of
this flag — they are read-only memory/registry tools, not bootstrap execution
tools.

## 4. Native tools (bootstrap)

Two proxy-owned tools, executed server-side (the way `search_memory` already is):

- **`bash`** — run a shell command on the PROXY host. Returns stdout+stderr and
  exit code, truncated to `caps.max_tool_forward`, killed after
  `tools.bash_timeout`.
- **`write`** — write a file on the PROXY host (path + content).

Exposed only when `native=on`, or `native=auto` and the client supplied no
`bash`/`write` of its own.

**Execution-model change:** the proxy currently only FORWARDS tool_calls to the
client. Native tools introduce a small server-side executor — a dispatch that,
when the model calls `bash`/`write` and they are native, runs them in-process
(`subprocess` for bash, `open()` for write) instead of forwarding.

## 5. Tool registry

### 5.1 Schema (`tools` table)

The `tools` table already exists (added with the overcome work). Target schema —
columns marked `NEW` are added by this feature:

| column | type | meaning |
|---|---|---|
| name | TEXT UNIQUE | tool identifier |
| description | TEXT | one-line; embedded for retrieval and shown to the model |
| problem_type | TEXT | secondary retrieval key (matches `capability_edges.problem_type`) |
| script_path | TEXT | canonical path in `tools.dir` |
| script_source | TEXT (NEW) | authoritative copy of the script (for `read_tool` + portability) |
| success_count | INTEGER | times reused successfully |
| created_at / last_used_at | TEXT | timestamps |
| embedding | BLOB (NEW) | vector of `name + " " + description` (for retrieval) |

`script_source` is authoritative; `script_path` is where it is materialized for
running. Keeping both makes `read_tool` host-agnostic and the registry
self-contained.

### 5.2 Saving a tool (`TOOL_SAVE`)

On `TOOL_SAVE: <name> :: <description> :: <path>`:

1. Read the script at `<path>` (works when `<path>` is on the proxy host — i.e.
   native `write`, or a same-host harness).
2. Copy it into `tools.dir/<name>`; set `script_path = tools.dir/<name>`; store
   the source in `script_source`.
3. Embed `name + " " + description`; insert/update the registry row.
4. If `<path>` is unreachable (cross-host harness), store the path as-is and
   leave `script_source` empty — the tool is "host-bound" (runnable only by the
   harness that built it). Flag it in the list output.

## 6. Tool access (dual)

### 6.1 Passive: retrieval-gated injection

- Reuses the query embedding already computed each turn (no extra embed call).
- Linear scan over the registry's embeddings (tiny — FAISS is overkill);
  cosine similarity.
- Inject only tools scoring >= `tools.inject_min_similarity`, sorted by score
  (problem_type match as tiebreak), capped at `inject_max` and `inject_tokens`.
- Compact payload, ~1-2 lines per tool:

      [Built tools you can reuse]
      - scrape_salary (live_data): scrape a salary from a job site — bash ~/mneme/chunks/tools/scrape_salary

- Injected at two points: (a) normal turns — so the model reuses before it even
  gets stuck; and (b) the overcome deliberation turn — so "reuse" is a visible
  option when it IS stuck.

### 6.2 Active: `list_tools` + `read_tool` (always available)

- **`list_tools [query?]`** — list the registry (all tools, or semantically
  filtered by `query`, or filtered by `problem_type`). Compact metadata: name,
  description, problem_type, script_path, success_count, last_used_at, and a
  host-bound flag.
- **`read_tool <name>`** — return the full `script_source` for one tool.

Both are server-side, read-only, and never stripped. This is the "find and read
the registry" path the model can use whenever it wants — including during the
deliberation turn.

## 7. Overcome integration

State machine, updated:

- **Normal** — full toolset: native bootstrap (if enabled) + passthrough + the
  read-only tools.
- **Deliberation** (stuck: 2 consecutive failures / 6 tool rounds) — strip
  EXECUTION tools (`bash`, `write`, `web_*`); keep read-only tools
  (`search_memory`, `list_tools`, `read_tool`). Inject the relevant-tools payload
  plus the directive.
- **Directive** now offers three decisions:

      DECISION: reuse_tool   + TOOL: <name>    (an injected/listed tool already solves this)
      DECISION: build_tool   + PLAN: ...       (build a new one)
      DECISION: declare_edge + MISSING: ...    (no capability — honest edge)

- **Reuse** — record `reuse_tool`, re-enable `bash`, inject "run `<name>` at
  `<path>` and use its output". One turn to run + answer. On a SUCCESS-classified
  result (the existing tool-trail classifier), bump `success_count`.
- **Build** — unchanged bounded loop (`BUILD_MAX_ITERATIONS = 3`): re-enable
  `bash`+`write` (native or harness, tool-source-aware), write -> bash -> observe,
  ending in `TOOL_SAVE` or exhaustion.
- **Declare** — record `confirmed` (existing).

**Tool-source-aware hard-stop:** the build/reuse turns must hand back the RIGHT
`bash`/`write` — native (if `native=on`/`auto`-no-client) or the harness's (if
`off`/Pi). Not a blanket "re-add `msg_tools`".

`_parse_deliberation` is extended to recognize `reuse_tool` alongside the
existing `build_tool` / `declare_edge`.

## 8. Execution-location caveat (why `native_tools` matters)

- native `bash`/`write`  -> proxy host
- passthrough `bash`/`write` -> client host

Same host (laptop): indistinguishable. Cross-host (proxy on RunPod, client on
laptop): native `bash` runs on the POD, harness `bash` runs on the LAPTOP. A tool
built via native `write` lives on the pod; one built via harness `write` lives on
the laptop. `list_tools`/`read_tool` mark host-bound tools so the model knows
which ones it can still run.

## 9. Security

Native `bash` = arbitrary command execution on the proxy host, reachable via the
chat API. On a single-user laptop this matches what Pi already does, so no new
exposure. On a shared or pod host, `native=auto` still won't inject when a harness
is present, and a standalone deployment should consider a sandboxed subprocess
(restricted workdir, no network, non-root). Say this explicitly in the config
comment.

## 10. Testing plan (deterministic stubs + real multi-turn)

- **Unit** — native-tool `auto`/`on`/`off` injection logic (thin client vs Pi
  tool list).
- **Unit** — registry save (`TOOL_SAVE` -> row + canonical file + embedding);
  `list_tools`/`read_tool` output shapes.
- **Unit** — retrieval gating (below threshold -> zero injection; above ->
  compact payload; `inject_max`/`inject_tokens` caps).
- **Unit** — overcome decision parsing (`reuse_tool`/`build_tool`/`declare_edge`)
  and the tool-source-aware build/reuse turn.
- **Integration (real model, scripted client)** — thin client (no bash/write) ->
  stuck scrape -> deliberation -> reuse/build -> tool actually runs via native
  bash.
- **Integration (Pi harness)** — has bash/write -> verify native tools are NOT
  duplicated (`auto`).

## 11. Implementation order

1. `tools:` config section + native `bash`/`write` executor behind `auto`/`on`/`off`.
2. Registry schema (`script_source`, `embedding`) + `TOOL_SAVE` writes canonical
   file + embeds.
3. `list_tools` + `read_tool` server-side tools.
4. Retrieval-gated injection (reuse the query embedding, linear scan).
5. Overcome: `reuse_tool` decision + tool-source-aware build/reuse turns.
6. `docs/config-spec.md` + README + instruction-sync test updates.
7. Chat interface LAST — a thin client over the API is nearly free once 1–6 land.
