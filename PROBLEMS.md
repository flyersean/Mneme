# Mneme — Issue Tracker

## Status (August 16, 2026)

`openrouter-backend` branch — the `novelty-thinking` learning/strategy layer running on a fully hosted OpenRouter backend (no Ollama, no GPU, local-machine friendly). Main LLM `deepseek/deepseek-v4-flash`, embedder `voyageai/voyage-4-lite` (1024-dim), labeler `meta-llama/llama-3.2-3b-instruct`. All three roles verified end-to-end locally: source tagging (`[source: X]`/`[guess]`), FAISS save/search, tool-calling round-trip (`search_memory` → synthesis), and the `mneme_setup_openrouter.py` wizard (key validation/save → venv → launch). `novelty-thinking` (Muse 30B on RunPod A40) remains the Ollama/local reference.

## Active Issues

### P1: No easy DB backup — strategies lost on pod termination
- **Cause:** The DB at `/workspace/mneme_chunks/mneme.db` is ephemeral. Every `rm -rf` or pod shutdown destroys all accumulated strategies and chunks.
- **Fix:** `scp -P $PORT root@$IP:/workspace/mneme_chunks/mneme.db ./` before pod termination. Future: `/backup` endpoint or periodic SCP cron.

### P2: Training weight dominance (persistent)
Models with strong training data on a topic ignore injected 2026 data. Known across all models. Epistemic framing in system prompt helps but doesn't fully solve.

### P3: LoCoMo dataset in Qwen 3.6 training data
Qwen 3.6 35B already knows LoCoMo conversations. Cannot use as a memory benchmark with this model. Custom 2026 events benchmark used instead.

### P4: Hermes-native search_memory plugin (TODO)
Hermes plugin to register `search_memory` as a native tool. ~10 lines: register tool → POST to `localhost:8080/search` → return results. Plugin goes in `~/.hermes/plugins/mneme_search/plugin.py`. No streaming issues expected (Hermes tools are synchronous).

### P5: Labeler (qwen2.5:0.5b) produces inaccurate topic labels
The 0.5B labeler misreads numbers (e.g., "180 contact tracers" labeled as "10 contact tracers"). Affects injection quality when model relies on labels rather than full message text. Consider upgrading labeler to a larger model or adding post-processing.

### P6: Harness prompt competition (persistent)
Mneme's system prompt competes with agent harness prompts (Hermes ~78KB, Pi's coding persona). The v2 prompt partially mitigates by removing Mneme's persona, but per-harness prompt templates (Option C) remain the structural fix.

### P7: Concurrent writer safety
SQLite WAL mode handles reads concurrently but writes are serialized. Multiple proxy instances sharing one DB confirmed working for reads, but concurrent writes from different instances could conflict.

### P8: Per-model config file (sampling, context, output caps, quirks)

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

**Status:** Idea only — not implemented, no code touched. `novelty-thinking` is
now the active branch (the earlier branch-decision blocker is gone); still
pending re-testing qwen3.6 + gemma4 against the recent grading/novel-procedure
changes.

### P9: Externalize all static injected prompts (hot-loadable files)

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

### P10: Hosted-model backend (OpenRouter / DeepSeek) — future feature

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

**Status:** **Implemented on `openrouter-backend`.** `MNEME_BACKEND=openrouter` routes all three call sites to OpenRouter's OpenAI-compatible API (`query_model` → `/chat/completions`, `embed` → `/embeddings`, labeler → `/chat/completions`); `MNEME_BACKEND=ollama` (default) is unchanged. Verified locally end-to-end — see P13 for the caveats that surfaced.

### P11: Frontier-model strategy distillation (future goal)

**Idea:** Use a hosted frontier model (OpenRouter / DeepSeek / Anthropic) as a
strategy *author* for the 30B local executor. Point the frontier model at the
capability edges where the 30B grades D/F, let it discover techniques —
novel-procedure detection already harvests them from the tool trace — and
inject the resulting strategies to the 30B on similar tasks. Strategies are
model-agnostic ("use the site's API endpoint" works for any executor that can
follow it), so most of the plumbing already exists.

**Two gaps that block this:**

1. **The "great" grade is author-relative, not executor-relative.** Today the
   model that discovers a strategy is the same one that reuses it, so "novel
   and it worked" is a valid signal. With separate author/executor roles, a
   frontier model emits many techniques that are trivial for it but impossible
   for a 30B (parallel tool calls, multi-step self-verification, "write a
   script to do X"). Those would be saved as "great" and injected to the 30B,
   which flounders — and the D/F extraction may write anti-strategies that
   fight the frontier strategies (feedback loop). Fix: validate each frontier
   strategy against the 30B before injecting; grade = "did it lift the 30B's
   grade", not "did the frontier model invent it".

2. **Cost is the wrong metric for an executor.** Cost is tool-result bytes. For
   a 30B executor the real cost is cognitive load / step count — how reliably
   the small model can follow the recipe. Add a difficulty/steps axis.

**Framing:** frontier model as a one-shot or periodic offline strategy author
(not a live replacement — see P10 for the backend swap), run only against weak
edges. Pay frontier tokens once per domain, reuse for free at inference.

**Validation test:** pick one failing capability edge, run the frontier model on
N tasks, capture strategies, then A/B the 30B with vs. without each strategy.
The transfer yield (fraction that improve the 30B's grade) is the go/no-go
number. Prior: high for procedural/web/tool techniques, near zero for
reasoning-heavy capability.

**Status:** Idea only — not implemented. Depends on P10 (hosted-model backend
for the author side) plus executor-relative validation.

### P12: Bare `<<COMMAND>>` leaks through to the model (e.g. `<<SAVE>>`)

**Symptom:** In Pi, a bare `<<SAVE>>` reaches the model as a message instead of
being intercepted by the proxy. The model interprets it as an instruction to
save the conversation and writes files about it. Muse-specific: the system
prompt documents `<<SAVE>>` (line 71-73, "you do not need to do anything"), and
a stronger model ignores that line while Muse acts on the leaked token.

**Root cause — two bugs in `process_chat` command handling:**

1. Detection and stripping target different messages. `<<SAVE>>` is detected in
   `full_user_msg` (the last *user* message, ~line 2899), but stripped from
   `messages[-1]` (the last message, *any* role, ~line 2929). If the
   conversation ends with a non-user message (trailing tool result, assistant
   echo), the strip writes the wrong message and the bare `<<SAVE>>` survives.

2. The fallback strip loop refuses to strip a bare command. The `if cleaned:`
   guard (~line 2950) skips any message whose stripped result is empty — a
   message that is exactly `<<SAVE>>` (or `<<DETAIL ...>>`, `<<REVISE>>`) is
   never cleaned by the fallback, so it can't catch what bug 1 missed.

**Diagnostic:** check the proxy log for `[SAVE] Triggered by user`. Present →
   detection + archive worked and the leak is purely in the strip path; absent
   → `<<SAVE>>` was never detected (content-extraction/regex mismatch).

**Fix (not applied):** strip from the message that actually contains the command
   (not `messages[-1]`), and drop the `if cleaned:` guard so bare commands are
   removed (writing an empty user message is fine).

### P13: OpenRouter backend caveats (surfaced during local build/test)

**Problem:** The hosted-backend swap (P10) works, but four issues surfaced during local testing:

1. **Labeler must be non-thinking.** A thinking model (deepseek-v4-flash) with
   `max_tokens=15` burns its budget on reasoning and returns `content: null` for the
   trivial "3-5 word label" task, so `_llm_topic_label` fell back to the heuristic every
   time. Fixed by using `meta-llama/llama-3.2-3b-instruct` (non-thinking) plus a
   defensive `(msg.get("content") or "")` guard. Any future labeler must be non-thinking.

2. **Embedding endpoint transient timeouts.** One `voyage-4-lite` embed call hit a 60s
   `ReadTimeout` during a busy turn (concurrent main-model + embed + label calls). The
   fail-loud path handled it correctly (`pending_embed=1`, no dead vector), but a
   one-shot retry (or a higher timeout) would smooth this over. Not a blocker; watch for
   recurrence.

3. **`format_schema` mapping is best-effort.** The Ollama `format` field is mapped to
   OpenAI `response_format={"type":"json_schema",...}` but is untested — it's only used
   on a few novelty/grading paths, not the main memory path. May need the schema shape
   adjusted for OpenRouter.

4. **Grading thresholds are Muse-tuned.** deepseek-v4-flash emitted `[guess]`/`[source]`
   tags correctly (grading works), but the inline-grade thresholds and pass/fail
   distribution were tuned for Muse 30B. Re-tune if the grade mix looks off.

**Status:** Known caveats, documented. Item 1 fixed; 2-4 open.

## Resolved (August 15, 2026)

### Novel-procedure detection + cost-ranked strategies
- **Root cause:** "great" grade only fired on "crossed a previously-flagged capability edge", so a genuinely novel successful technique (curl + User-Agent header, MediaWiki API) was graded "pass" and dropped — the strategy table only filled with failure-derived directives.
- **Fix:** detect a working non-standard technique from the tool trace (custom header, API endpoint, method override), grade it "great", save as a strategy with a `cost` column (tool-result size). `get_strategies` + `build_context` rank grade-first then cheaper-wins.

### Embedding path fails loudly (was silent zero-vector)
- **Root cause:** `embed()` returned a zero vector on any failure; the chunk was stored but never matched in FAISS, with no signal. A 768-vs-1024 dim mismatch would crash FAISS with a confusing error.
- **Fix:** `embed()` returns None on failure; `save_chunk()` stores `pending_embed=1` (no dead vector); a startup job re-embeds pending chunks; `_embedding_health_check()` probes the embedder and reports dim mismatch at startup. New columns: `pending_embed`, `embed_model`, `dim`.

### Setup script fixes
- UnboundLocalError `prev` when the DB exists but `setup_config.json` was lost (restored/copied) — fixed by initializing `prev = {}`.
- "Keep current" offered `unknown (current)` when config was missing — now only offered when a real model was configured.

### Empty-DB instruction injection
- The empty-DB early-return path skipped `system_prompt.md` injection, so a fresh pod never saw the Source Tagging instruction. All return paths now route through `_finalize_context`.

### Synthesis after search_memory
- Re-query the model with search results so it produces a tagged answer instead of echoing raw hits. Results delivered as a USER message (not a `tool` role) to avoid the Muse Modelfile's peg-native grammar bug.

### Save-pipeline fixes
- `_generate_topic_label` NameError (`_tlre` → `re`) that crashed `archive_staging`.
- Idle staging flush was dead code (checked after `staging.add()` reset `last_activity`); fixed by flushing before adding.

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
