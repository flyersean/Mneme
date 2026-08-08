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
