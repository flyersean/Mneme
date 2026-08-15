# Mneme — Issue Tracker

## Status (August 8, 2026)

v2 architecture deployed on dev-chunks. Persona-free prompt, proxy-driven strategy lifecycle, COMMAND stripping, content normalization. Testing with Hermes + Pi on RunPod A40 with Qwen 3.6 35B.

## Active Issues

### P1: No easy DB backup — strategies lost on pod termination
- **Cause:** The DB at `/workspace/mneme_chunks/mneme.db` is ephemeral. Every `rm -rf` or pod shutdown destroys all accumulated strategies and chunks.
- **Fix:** `scp -P $PORT root@$IP:/workspace/mneme_chunks/mneme.db ./` before pod termination. Future: `/backup` endpoint or periodic SCP cron.

### P2: Training weight dominance (persistent)
Models with strong training data on a topic ignore injected 2026 data. Known across all models. Epistemic framing in system prompt helps but doesn't fully solve.

### P3: LoCoMo dataset in Qwen 3.6 training data
Qwen 3.6 35B already knows LoCoMo conversations. Cannot use as a memory benchmark with this model. Custom 2026 events benchmark used instead.

### P3: Hermes-native search_memory plugin (TODO)
Hermes plugin to register `search_memory` as a native tool. ~10 lines: register tool → POST to `localhost:8080/search` → return results. Plugin goes in `~/.hermes/plugins/mneme_search/plugin.py`. No streaming issues expected (Hermes tools are synchronous).

### P4: Labeler (qwen2.5:0.5b) produces inaccurate topic labels
The 0.5B labeler misreads numbers (e.g., "180 contact tracers" labeled as "10 contact tracers"). Affects injection quality when model relies on labels rather than full message text. Consider upgrading labeler to a larger model or adding post-processing.

### P4: Harness prompt competition (persistent)
Mneme's system prompt competes with agent harness prompts (Hermes ~78KB, Pi's coding persona). The v2 prompt partially mitigates by removing Mneme's persona, but per-harness prompt templates (Option C) remain the structural fix.

### P5: Concurrent writer safety
SQLite WAL mode handles reads concurrently but writes are serialized. Multiple proxy instances sharing one DB confirmed working for reads, but concurrent writes from different instances could conflict.

### P6: Per-model config file (sampling, context, output caps, quirks)

**Problem:** Model settings are currently global — a mix of env vars and
hardcoded defaults. Different models need different settings, and applying one
model's tuning to another degrades output or breaks generation:

- **Sampling.** Muse Glimmer wants `temp=1.0, top_p=0.95, top_k=64` (its model
  card). Qwen 3.6 and Gemma 4 have different optima. As of now the Muse params
  are the *global default* in `query_model`, so every other model inherits
  Muse's tuning.
- **Context size.** A 30B fits 129K ctx on an A40, but a 7B/3B model may not,
  and a tiny labeler doesn't need it. `num_ctx` is set per-model today via
  Modelfile, not in one place.
- **Output cap (`max_tokens` / `num_predict`).** Smaller models — or any model
  on a small-context box — can runaway-generate (see the SHA-256 grind that ran
  15+ min). A per-model `max_tokens` cap is a cheap guardrail and also stops
  tiny models that don't know when to stop.

**Idea:** A single per-model config file (e.g. `models.yaml` or
`model_config.json`) keyed by model name, merged with env overrides. Sketch:

```yaml
muse-glimmer:30b:
  temperature: 1.0
  top_p: 0.95
  top_k: 64
  num_ctx: 32768        # matches our custom Modelfile
  max_tokens: 2048
  quirks: ["peg-native bug fixed in Ollama 0.32.13+ — update Ollama, not the model"]
qwen3.6-35b:
  temperature: 0.8      # TBD — needs re-testing vs the new grading code
  top_p: 0.9
  num_ctx: 129000
  max_tokens: 4096
gemma4:
  temperature: 0.7      # TBD
  # thinking→content fallback already handled in query_model
small-model-7b:
  temperature: 0.5      # small models drift at high temp
  num_ctx: 8192
  max_tokens: 512       # cap output: stops runaway + preserves context
```

The proxy resolves settings at startup (or per-request) for the active
`MNEME_MODEL`, falling back to env vars, then hardcoded defaults. This is also
the answer to "someone wants to run a much smaller model and is hitting context
size issues / runaway output": they drop a `small-model-Nb` entry with a tight
`num_ctx` + `max_tokens` and it just works.

**Status:** Idea only — not implemented, no code touched. Blocks on the branch
decision (which branch is canonical) and on re-testing qwen3.6 + gemma4 against
the recent grading/capability-edge/tool-call changes.

### P7: Externalize all static injected prompts (hot-loadable files)

**Problem:** Static prompts and system instructions that are injected on every
turn are hardcoded in `mneme_proxy.py`. Editing them means changing code,
redeploying, and restarting the proxy — which loses the in-memory staging buffer
and can force a DB clear. The Aug 15 meta-principles rewrite made this concrete:
tuning the `META_PRINCIPLES` list (to fix the "reject the obvious answer" bias
that caused 10-minute web-read grinds) required a script edit + scp + kill +
restart + DB wipe, for what should have been a one-line text change.

Currently hardcoded (violations of this rule):
- `META_PRINCIPLES` — hardcoded list in mneme_proxy.py (the one that just bit us)
- Pi system prompt — embedded in Pi's TypeScript source (see "Hot-Swappable
  System Prompt" below)
- Strategy/preference template blocks

**Idea:** Every static prompt/instruction injected repeatedly lives in a plain
file under `prompts/` (e.g. `prompts/meta_principles.md`, `prompts/system.md`,
`prompts/preferences.md`), read once at startup, plus a `POST /admin/reload`
endpoint to re-read them without a restart (mechanism already designed in the
"Hot-Swappable System Prompt" section). Editing wording — like the
verify-don't-reject meta-principles fix — becomes `vim prompts/meta_principles.md`
+ `curl -X POST localhost:8080/admin/reload`, no redeploy, no DB clear, no chunk
loss.

**Status:** Rule documented; not implemented. `META_PRINCIPLES` is still a
hardcoded list in the script.

### P8: Hosted-model backend (OpenRouter / DeepSeek) — future feature

**Problem:** The proxy is OpenAI-compatible on the way OUT (Pi/Hermes connect to
`localhost:8080/v1`), but on the way IN it talks to Ollama's *native* API, not
an OpenAI-compatible one. `query_model` POSTs `{OLLAMA_URL}/api/chat` with an
Ollama payload (`options`, `message.thinking`, dict tool-call arguments) and
parses an Ollama response. So pointing the proxy at a hosted model is not a
one-line `base_url` change — the LLM call, the embedding call, and the labeler
call are all Ollama-native.

**Scope (3 call sites + config):**
- `query_model` → `/api/chat` (main LLM)
- `embed` → `/api/embeddings` (memory vectors, snowflake-arctic-embed2)
- labeler/topic-gen → `/api/generate` (qwen2.5:0.5b)
- `OLLAMA_URL` (line 31) + `MODEL` (line 32)

**Idea:** Add a backend adapter in `query_model` that maps Ollama-native ↔
OpenAI format, plus env config for base URL, API key, and model name
(`deepseek-chat`, `deepseek/deepseek-chat`, etc.):

- Payload: `options` (temp/top_p/top_k) → OpenAI params (top_k dropped).
- Response: `choices[0].message` → `{content, tool_calls}`; `finish_reason` →
  `done_reason`; `usage.completion_tokens` → `eval_count`.
- Tool-call arguments: OpenAI returns a JSON string, Ollama a dict → json.loads
  on the way out (reverse conversion already exists on the way in).
- `thinking`: Ollama `message.thinking` → DeepSeek `reasoning_content` /
  OpenRouter `reasoning`.

**Hybrid (recommended):** swap only the LLM; keep embeddings + labeler on local
Ollama. DeepSeek has no embeddings endpoint, and swapping the embedder would
mean re-embedding the whole FAISS store (1024-dim snowflake-arctic-embed2).
Dropping Ollama entirely is what pushes this from "low" to "moderate".

**Tuning caveat:** the grading/novelty/source-tagging pipeline is tuned to
Muse's behavior (emits `[source: X]`/`[guess]` tags, has a `thinking` field,
abliterated). A hosted `deepseek-chat` will comply differently, so the
source-tagging prompt and inline-grade thresholds likely need re-tuning to hit
the same honesty yield. The `peg-native` workaround (deliver search results as a
user message, not a tool message) was an Ollama/llama.cpp quirk and could be
removed on a hosted backend.

**Status:** Future feature — assessed, not implemented. No code touched.

## Resolved (August 8, 2026)

### v2 Architecture
- **Persona-free prompt:** No "You are..." identity — Mneme describes itself as a system
- **Proxy-driven strategy lifecycle:** Success (A/B) triggers mini-convo; failure (C/D/F) auto-creates boilerplate with FAISS dedup
- **COMMAND stripping:** `<<SAVE>>`, `<<DETAIL>>`, `<<REVISE>>` processed server-side, stripped from model context
- **Content normalization:** `_extract_text()` handles both string and array content formats (OpenAI, Pi, Hermes)

### Pi search_memory extension + streaming intercept
- **Root cause:** Pi validates tools client-side. Proxy-injected `search_memory` rejected. Pi's tool API uses `execute(toolCallId, params)` returning `{content: [{type: "text", text}], details: {}}` — the `handler` key was silently ignored.
- **Fix:** Pi extension (`extensions/pi/mneme-search-tool.ts`) registers the tool with an empty `execute()` handler. Proxy's `_chat_stream` interceptor catches tool calls server-side.
- **Verified:** Streaming mode, model calls `search_memory`, proxy returns results. Qwen 3.6 + Pi on RunPod A40.

### Proxy streaming search_memory intercept
- **Symptom:** In streaming mode, tool calls happen mid-SSE-stream and Pi can't inject results.
- **Fix:** `_chat_stream` detects search_memory tool calls, executes search via `route_query()`, injects results, clears `tool_calls`.

### Custom 2026 Events benchmark
- 20 questions across needle/temporal/trick/cross types. 12/20 (60%) accuracy with Qwen 3.6 35B (32K).
- Judge: gpt-4o-mini via OpenRouter. Runner: `benchmarks/locomo_runner.py`

### Setup scripts
- `setup.sh`: one-line pod installer with interactive wizard
- `scripts/mneme_setup.py`: model/context/interface/embedding picker
- `scripts/mneme_connect.py`: local SSH tunnel + agent launcher (Hermes/Pi)

---

## Ollama KV Cache Quantization for Large Context Windows

Qwen 3.6 35B on A40 (46GB VRAM) can support 129K-264K context windows by quantizing the KV cache.

**Modelfile:**
```
FROM fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest
PARAMETER num_ctx 129000
```

**Required environment variables for Ollama:**
```
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
```

With `q8_0` and flash attention, ~35GB total on A40 (22GB weights + ~13GB KV cache). Leaves ~11GB headroom.

---

## Implementation Plan (Phases 1-4)

Complete, deployed, and verified.

- **Phase 1 (v2 prompt):** Persona-free prompt deployed. Pi-compatible trimmed version active. Full version at `docs/system-prompt-v2.md`.
- **Phase 2 (strategy lifecycle):** `_save_strategy` + `_strategy_lifecycle` implemented. FAISS dedup prevents boilerplate stacking.
- **Phase 4 (COMMAND stripping):** `<<SAVE>>`, `<<DETAIL>>`, `<<REVISE>>` stripped from user messages. Proxy intercepts server-side.
- **Phase 3 (Hermes tool):** SEARCH_MEMORY_TOOL appended to Hermes tool list. Works natively (Hermes doesn't validate tools).
- **Content normalization:** All content access points use `_extract_text()` for array/string compatibility.
- **Streaming intercept:** `_chat_stream` server-side tool call handling for streaming clients.

---

## Hot-Swappable System Prompt (Pi + Mneme)

### Problem

Pi's system prompt (~2KB) is embedded in its TypeScript source. Mneme prepends its own prompt BEFORE Pi's. The model sees:

```
=== MNEME INSTRUCTIONS ===
[Mneme memory prompt — currently persona-free]

[Pi's harness prompt — coding agent, tools, guidelines]
[Conversation messages]
```

This works but makes iteration on the COMBINED behavior difficult — two separate prompts, only one editable.

### Pi's Current System Prompt

Extracted and saved as `docs/pi-system-prompt.md`. Critical sections:
- Identity: "expert coding assistant operating inside pi"
- Tools: read, bash, edit, write (+ custom extensions)
- Guidelines: file ops, edit precision, conciseness
- Doc references: extensions, themes, skills, SDK

### Merge Strategy

**Approach:** Keep Pi's prompt intact. Edit Mneme's `system_prompt.md` to complement it.

- Pi handles: tool usage, coding guidelines, file operations
- Mneme handles: memory grading, save/detail commands, retrieval instructions
- Don't repeat Pi's instructions in Mneme's prompt — Pi already handles that

**Why not merge into one file:** Pi's prompt is critical for harness operation. Changing it breaks tool calling and coding behavior. Mneme's prompt sits ABOVE Pi's and controls memory behavior. Separation of concerns.

### Hot-Swap Implementation

The proxy reads `system_prompt.md` once at startup. Add `/admin/reload` endpoint for hot-swap:

**Option A: Reload endpoint** (recommended)
Add `POST /admin/reload` endpoint that re-reads `system_prompt.md` into memory. No proxy restart, no chunk loss.
```python
@app.route("/admin/reload", methods=["POST"])
def reload_prompt():
    global SYSTEM_PROMPT
    with open(PROMPT_PATH) as f:
        SYSTEM_PROMPT = f.read()
    return {"ok": True, "size": len(SYSTEM_PROMPT)}
```

**Option B: Per-request file read**
Re-read `system_prompt.md` on every request. Simplest but adds ~1ms stat+read overhead. No endpoint needed.

**Option C: Watch file with inotify**
Use `watchdog` or raw `inotify` to auto-reload when the file changes. Overkill for this use case.

### Recommended Flow

1. Implement Option A (reload endpoint)
2. Add to setup script output: `curl -X POST localhost:8080/admin/reload` after editing prompt
3. User workflow: `vim /workspace/proxy/system_prompt.md` → `curl -X POST localhost:8080/admin/reload` → next chat uses new prompt
4. No restart, no chunk loss, instant feedback loop

---

## Strategy Roadmap

See `docs/strategy-roadmap.md` for the full analysis of memory strategies,
temporal threading, strategy extraction as directives, and implementation
priority stack. Key takeaway: strategy directives (prescriptive rules) should
inject above memory (passive records) with higher epistemic weight.
