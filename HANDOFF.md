# Mneme — Development Handoff

Point-in-time snapshot for handing development off to a new agent running on the
VPS (through Mneme itself, chat model = DeepSeek v4 pro). Everything below is
authoritative as of this writing; the code is the source of truth where they
disagree.

---

## 1. What this project is

Mneme is a **conversational memory proxy**. It is an OpenAI-compatible HTTP
proxy that sits between an LLM and persistent memory. On every turn it:

1. chunks incoming conversation/document pages,
2. embeds them (OpenRouter `qwen/qwen3-embedding-8b`),
3. stores chunks in SQLite (`mneme.db`),
4. retrieves the relevant chunks for the current query,
5. injects them into the model context before the completion call.

On top of that core it has:

- a **harness** — a structured agent run loop (plan → steps → verify) with a
  ledger, job scheduler, and profiles;
- a **decision layer** ("Jev" via the OpenRouter Decisions API) for typed
  decisions (plan/judge/safety/grading) — cloud-only and optional;
- a **canvas extension** — a shared file workspace with a CodeMirror editor,
  served on port 9090;
- a **swarm orchestrator** and a **Pi coding-agent extension**;
- a **unified setup wizard** (`scripts/mneme_setup.py`) that installs and
  generates a self-documenting `mneme.yaml`.

The proxy is the single entry point. The chat UI (`proxy/static/chat.html`) and
the API both live behind it on port 8080.

---

## 2. Design philosophy

These are the rules the project follows; a new agent should internalize them
before touching code.

- **Memory is the core.** page-read → chunk → save → recall is the heart of the
  product. Regressions here are top priority. Never "summarize" large output to
  save tokens — truncate-and-retrieve instead; verify at the deepest layer,
  never just an HTTP 200.
- **Labeling is open-vocabulary generation, not classification.** There is no
  fixed label list. Labels are generated.
- **Decision models are a separate, optional layer.** Jev / gpt-6-luna are for
  judge/safety/routing/grading decisions. They are cloud-only and must stay
  disableable so a fully-local Mneme remains possible. The label model is
  generative; the decision models are classification-style.
- **Backend is config-driven and decoupled from the chat model.** The backend
  (provider, type, key, base URL) comes from `backend.provider` / `backend.type`
  in `mneme.yaml`. Changing the chat model must NEVER change the backend. To
  change the backend you edit the config and add a key. (Model switching goes
  through `POST /models/switch`, which only changes the model.)
- **Model identity is config, not env.** Top-level `model:` / `embed_model:` /
  `label_model:` are authoritative. Templates override config: defaults < file
  < template < env.
- **Outcome-first.** Verify the code does what the user WANTED (the vision/use
  case), not merely that it executes cleanly. Runs ≠ correct.
- **Fail loud.** Scripts end with an explicit done/failed marker, never a raw
  error or a silent exit.
- **Fix-not-delete; decompose the monolith.** New features go in `proxy/mneme/`
  modules; existing features move into modules over time. Don't patch around
  `mneme_proxy.py`'s size — move logic out.
- **Root-cause, attribute first.** When something misbehaves, decide
  Mneme-vs-model before guessing. Prefer an architectural fix over the Nth
  patch. Copy a working solution; don't guess; never blame the model.
- **Grade provenance, not content.** "I don't know" is better than fabrication.
  The grading prompt reacts to connotation — feed "un-cited" not "F/fabricated".
- **Full execution.** Never dumb down a model for speed (quality > speed).
  Scope reduction is the #1 trigger for correction. Corrections apply globally.
- **Measure, don't assert.** Prefer empirical measurement over asserted
  thresholds.
- **Platform.** Linux primary; Mac mostly works; Windows = WSL2/Docker only
  (`fcntl`, `start_new_session`, and bash are Linux-only). Docker is the
  portability goal.

---

## 3. Repo layout

```
mneme/
  proxy/
    mneme_proxy.py          # the main proxy (large, ~10k lines) — flatten routes through it
    mneme/                  # modular package (the decomposition target)
      harness/              #   engine, planning, verify, ledger, jobs, profiles, skills,
                            #   commands, capabilities, chat_executor, workspace, context
      decisions.py          #   Jev DecisionClient (OpenRouter Decisions API)
      tools.py              #   tool registry / native tools
      instructions.py       #   prompt instructions
      auth.py / auth_ui.py  #   login (pw=humans, token/Bearer=machines)
      secrets_store.py      #   named secret store (${secret:NAME} refs for MCP/integrations)
      grading.py curation.py overcome.py strategies.py templates.py
      logfile.py thinking_log.py tool_trail.py mcp_client.py capability.py chatcmd.py ...
    static/                 # chat.html (chat UI), dashboard.html, vendor/ (CodeMirror)
    gateway.py              # gateway (users, auth)
  extensions/
    canvas/                 # canvas workspace (canvas_server.py + vendor/ + workspace/) :9090
    swarm/                  # swarm orchestrator
    pi/                     # Pi coding-agent extension
    gateways/               # telegram gateway
  scripts/
    mneme_setup.py          # unified install + wizard → generates mneme.yaml + start_proxy.sh
    install.sh install_mac.sh
    benchmark.py calibrate_similarity.py ...
  tests/                    # pytest suite (test_harness_*, test_proxy_auth.py, ...)
  mneme.yaml.example        # reference config (every option commented)
  model_templates.yaml      # named, known-good sampling/thinking templates
  strategies.yaml
  AGENTS.md README.md LICENSE
```

`extensions/canvas/vendor/` and `workspace/` — the vendor dir is committed
(CodeMirror + modes); `workspace/` is gitignored.

---

## 4. Branches & versions

- **`classification-models`** — the ACTIVE dev branch. Everything lives here:
  harness + canvas + Jev decision layer + backend/model decoupling + provider
  fixes. Latest commit: `cdecf7b` ("docs: add development handoff …").
- **`agent-harness`** — an older baseline (LLM-only harness + canvas, pre-Jev).
  Superseded by `classification-models`; do not start new work on it. (GitHub's
  default branch is still `agent-harness` — see §7.)

Repo locations:

- **Local**: `/home/sean/mneme/repo`, branch `classification-models`, clean
  working tree.
- **VPS**: `/home/ubuntu/mneme/repo`, branch `classification-models`, tracking
  `origin/classification-models`, clean. (Was previously tangled on
  `agent-harness`; untangled — the fetch refspec was limited to `agent-harness`
  and the stale local branch was deleted.)

(Commit hashes are point-in-time; run `git log -1` for the true latest.)

---

## 5. Runtime topology (VPS)

| Thing | Where |
|---|---|
| Proxy (API + chat UI) | `http://localhost:8080` (also `100.105.134.18:8080` via Tailscale; you RDP in and use `localhost`) |
| Canvas workspace | `http://localhost:9090` |
| Config | `/home/ubuntu/mneme/chunks/instances/8080/mneme.yaml` |
| API keys | `/home/ubuntu/mneme/env` (sourced by `start_proxy.sh`) |
| Users | `/home/ubuntu/mneme/gateway/mneme_users.yaml` |
| Data (DBs) | `/home/ubuntu/mneme/chunks/` — `mneme.db`, `harness.db`, `conversations.db` |
| Restart proxy | `cd /home/ubuntu/mneme/chunks/instances/8080 && ./start_proxy.sh` |
| Restart canvas | `python3 /tmp/relaunch_canvas.py` (or `python3 extensions/canvas/canvas_server.py --proxy-url http://localhost:8080`) |

Current identity on the VPS instance:

- backend provider: `deepseek` (base `https://api.deepseek.com`), type `openai`
- chat model: `deepseek-v4-pro`
- embed model: `qwen/qwen3-embedding-8b` (pinned to `openrouter`)
- label model: `meta-llama/llama-3.2-3b-instruct` (pinned to `openrouter`)

---

## 6. API keys

The keys already live on the VPS in `/home/ubuntu/mneme/env` and are sourced by
`start_proxy.sh`. **You do not need to hand the new agent any new keys** — they
are already present and working:

- `DEEPSEEK_API_KEY` — chat model `deepseek-v4-pro` (DeepSeek native; verified
  200 against `api.deepseek.com`).
- `OPENROUTER_API_KEY` — embed + label models (pinned to OpenRouter).

The only case where the new agent needs a key handed to it is if it runs its own
Hermes provider config separate from Mneme (e.g. a `custom:` provider pointing at
DeepSeek directly). In that case it needs the DeepSeek key (and the OpenRouter
key if it also drives its own embedding). Never commit these keys — they live in
`/home/ubuntu/mneme/env`, which is gitignored.

**Non-provider secrets** (MCP server tokens, webhook keys, third-party service
credentials) go in the general-purpose **Secrets** store, not the env file or the
config. Admin-only UI at `/secrets` (dashboard nav → Secrets); values live in a
chmod-600 `<mneme-root>/secrets.yaml`, and config consumers reference them with
`${secret:NAME}` (e.g. an MCP server's `env: GITHUB_TOKEN: ${secret:gh-token}`).

---

## 7. Known issues / TODOs

1. **`backend.type` is overridden at startup.** `start_proxy.sh` hardcodes
   `export MNEME_BACKEND="openrouter"` (or `"ollama"`), which wins over the
   config's `backend.type` (env > config precedence). So the config's `type:`
   is currently ignored and `/providers` reports `backend: openrouter` even when
   the provider is `deepseek`. Cosmetic today (openrouter/openai share the same
   request path), but it contradicts "backend read off config". Noted as a
   `TODO(fix later)` comment in both `mneme.yaml` and `mneme.yaml.example`.
   Fix = drop the export from `scripts/mneme_setup.py` and the generated start
   script, then re-sync the `MNEME_BACKEND` global from `os.environ` after
   config load (else it silently falls back to `ollama`).
2. **GitHub's default branch is still `agent-harness`** (`origin/HEAD`). Harmless
   day-to-day (the VPS now tracks `classification-models` directly), but switch
   it to `classification-models` in the repo settings if you want a clean
   default. Also: a **`memory-only` branch** exists on origin — uninvestigated;
   check whether it's stale or meant to be the default.
3. **DeepSeek `deepseek-flash` is a reasoning model** — it returns
   `content: ""` with the answer in `reasoning_content`. The streaming path
   already falls back `content = thinking`, but double-check any non-streaming
   path.
4. **Disk growth.** Memory chunks accumulate; GC and dedupe matter.
5. **Canvas** was single-threaded; converted to `ThreadingHTTPServer` this
   session (a slow chat turn used to block the whole canvas).

---

## 8. Next steps / suggested plan for the move

1. **Give the new agent a dedicated Hermes profile on the VPS** with
   `/home/ubuntu/mneme/repo` as its working directory, so `AGENTS.md` and the
   repo context load automatically.
2. **Point the new agent's model at DeepSeek v4 pro through Mneme.** The Mneme
   proxy is OpenAI-compatible — configure the agent with a custom provider
   pointing at `http://localhost:8080/v1` and model `deepseek-v4-pro`. That way
   the agent gets the model AND memory injection in one endpoint. (Alternatively
   point it at DeepSeek directly and use Mneme purely for memory.)
3. **Develop in `/home/ubuntu/mneme/repo`**; the running proxy/canvas use the
   same repo, so `git pull` + restart is the deploy. Restart is safe — memory is
   in SQLite, not RAM. (Deploy idiom: `cd .../8080 && rm -f restart.log &&
   (setsid ./start_proxy.sh > ./restart.log 2>&1 </dev/null &)`.)
4. **Test before/after:** `python3 -m pytest tests/` in the repo. Note these
   suites are known-failing and pre-existing (do not treat as regressions):
   `test_generated_config` (5 failures), `test_tool_loop` (13 session-kwarg
   mock failures), `test_harness_proxy` (2 mock-planner drift failures).
5. **Keep the decision layer optional** so a local (no-cloud) Mneme still works.

Deferred work (carried over from earlier sessions, not started):

- tool-call safety gate (`noul`) before destructive/out-of-scope bash — the
  highest-value Jev use after the judge wiring;
- model-invoked `decide` tool + prompt guidance;
- retrieval relevance re-rank and grading/routing decisions;
- self-managed `todo` tool;
- delete the planner/task-graph/free-form duplication once the harness
  replacement is proven;
- Docker portability.

---

## 9. Session history worth knowing (what was just done)

- Fixed `/providers/key` returning 400 for catalog providers (deepseek/openai/
  …) — it only looked in `CONFIG_DATA["providers"]`, not `PROVIDER_CATALOG`.
- Fixed the "empty response from DeepSeek" bug — `/providers/key` now refreshes
  the cached `OR_API_KEY` when the saved key belongs to the active provider
  (previously only for OpenRouter), so a key saved after activation no longer
  leaves the chat request 401-ing on a stale key.
- Decoupled the backend from the chat model: the chat page's model picker now
  switches only the model via `POST /models/switch`; the provider/backend
  switcher was removed from the UI. Backend changes are config + key only.
- Pinned `embed_provider`/`label_provider` to `openrouter` so switching the chat
  provider to DeepSeek no longer repoints the embed/label models at DeepSeek.
- Canvas: CodeMirror editor (same viewer as the YAML config pages), collapsible
  panes, fixed reversed resize sliders, Send→Stop button + thinking indicator,
  threaded server, taller chat input.
- Untangled the VPS repo: fixed the fetch refspec (was limited to
  `agent-harness`), fetched `classification-models`, checked it out (tracking
  `origin/classification-models`), and deleted the stale local `agent-harness`
  branch. The VPS now cleanly tracks the active dev branch.
- Added a general-purpose **secret store** (`proxy/mneme/secrets_store.py`): a
  chmod-600 `<mneme-root>/secrets.yaml` for arbitrary keys/passwords (MCP
  tokens, webhook keys, etc.), an admin-only `/secrets` page (list/add/delete
  with masked values), and `${secret:NAME}` reference resolution wired into MCP
  server `env`/`args`. Provider API keys remain in the env file.
- Added a **`start_run` tool** — the chat model can start a background harness
  run (structured or free-form) and check it via `inspect_run`. Bound via
  `mntools.engine`; gated by `tools.start_run`. Added a **`POST /decide`**
  endpoint so extensions call the classifier/decision models (Jev) over HTTP,
  and gave the **swarm** a `decide` step (classifier) + `run` step (background
  harness run) + `MNEME_API_TOKEN` auth on its proxy calls.
- The VPS agent (z-ai/glm-5.3) self-edits on its own branch
  `feature/agent-save-tool` (a `save_tool` registry-write tool + a stream
  repetition loop-guard + the no_new_privs/sudo doc). That branch is pushed to
  GitHub; merge it into `classification-models` after review.
