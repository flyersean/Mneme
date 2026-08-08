# Mneme — Issue Tracker

## Status: Strategy improvement loop complete — Phase 1-3 verified

dev-chunks branch. Tested August 3-5, 2026 on RunPod A40 with qwen2.5:7b + gemma4:26b.

## Resolved (August 2026 session)

### P0: Chunk ID collision across proxy instances [RESOLVED]
- **Cause:** `_chunk_seq` was per-process, restarting at 0. Two proxies on same DB produced identical `mem_1`, `mem_2` IDs.
- **Fix:** Timestamp-based chunk IDs (`mem_{int(time.time()*1e6)}`). Guaranteed unique across any number of proxies.
- **Verified:** 5-phase dual-model novel test — all 5 chunks survived, zero collisions.

### P0: `chunk_id=""` passed to save_chunk [RESOLVED]
- **Cause:** `_archive_single_chunk` computed correct ID but passed empty string.
- **Fix:** Thread generated `chunk_id` through to `save_chunk`.

### P0: INSERT placeholder count mismatch [RESOLVED]
- **Cause:** Schema migration added columns (session_id, indexable) but INSERT VALUES had wrong count.
- **Fix:** Aligned 15 placeholders with 15 values.

### P0: Column order misalignment [RESOLVED]
- **Cause:** INSERT VALUES order didn't match schema column order after ALTER TABLE migrations.
- **Fix:** Explicit column ordering: source, cycle, created_at, session_id, indexable.

### P1: Grade pipeline [RESOLVED]
- **Cause:** Grades parsed from model output but never written to DB.
- **Fix:** Full pipeline: model output `[GRADE: X]` → process_chat parsing → staging → archive → save_chunk → DB.
- **Verified:** Models output `[GRADE: A]`, DB shows `grade=A`.

### P1: Strategy type mismatch [RESOLVED]
- **Cause:** Strategies keyed on `problem_type` string matching — never matched in practice.
- **Fix:** Always inject top-3 strategies ranked by effective_grade, regardless of problem_type.
- **Verified:** Cross-model strategy transfer confirmed (qwen strategies injected into gemma context).

### P1: Strategy improvement loop — Phase 1: Versioning [RESOLVED]
- **Schema:** `version INT DEFAULT 1`, `parent_id TEXT`, `effective_grade REAL DEFAULT 0.0`, `use_count INT DEFAULT 0`, `success_count INT DEFAULT 0`.
- **Dedup:** FAISS cosine > 0.75 on strategy text before INSERT. Match → bump version, update text, set parent_id.
- **Verified:** "Always verify arithmetic." → v2 on second identical creation (Aug 5, pod 69.30.85.50).

### P1: Strategy improvement loop — Phase 2: Effectiveness feedback [RESOLVED]
- **Mechanism:** When model references `STRATEGY #id` in response and grades itself, apply: `new_eff = 0.7 * old + 0.3 * grade_val`, increment use_count, increment success_count on A/B.
- **Verified:** A-grade with strategy → eff bumped 0.50→0.65, used 5→6, success 2→3. F-grade → eff dropped 0.65→0.45, used 6→7, success stayed 3/6 (Aug 5).

### P1: Strategy improvement loop — Phase 3: Enriched injection + dynamic ranking [RESOLVED]
- **Enriched headers:** `STRATEGY #t1 v1 [grade:A] [eff:0.95] [used:10/9 success]` in injected context.
- **Dynamic ranking:** `ORDER BY effective_grade DESC, use_count DESC` in both get_strategies and inline queries.
- **Always-inject fallback:** build_context returns strategies even when no FAISS chunks match.
- **Parser:** Accepts `STRATEGY:` without square brackets via `re.MULTILINE`.
- **Effectiveness regex:** Matches alphanumeric `STRATEGY #id` references.
- **Verified:** All confirmed on pod Aug 5. Model echoes back `[STRATEGY: STRATEGY #t1]`.

### P2: Hallucination memory loop [MITIGATED]
- **Source-tiered indexing:** model-sourced chunks with grade C/D/F excluded from FAISS.
- **Combined trust scoring:** source + grade weights in `route_query` ranking.
- **Not fully eliminated:** models still sometimes default to training data over injected memory.

### P3: Stale DB chunks on restart [RESOLVED]
- **Cause:** WAL not checkpointed before proxy kill.
- **Fix:** SIGTERM handler with `PRAGMA wal_checkpoint(TRUNCATE)`.

### P4: Dead code (~400 lines) [RESOLVED]
- **Removed:** `classify_chunk`, `_archive_split`, `_segment_by_user` orphan, `_compress_large_tool_results_OLD`, `_advance_chunk_OLD`, `_model_loop_read_all_OLD`, `query_model_stream`, `CLASSIFY_PROMPT`.
- **Impact:** 326 lines removed.

### P4: Handler INSERT column count [RESOLVED]
- **Cause:** Chat handler INSERT used 6 values for 11-column strategies table.
- **Fix:** Aligned all handler INSERTs with schema: version, parent_id, effective_grade, use_count, success_count.
- **Also fixed:** Missing closing quote on line 914, undefined `limit` variable in inline queries, `st` variable extraction after regex match.

## Active Issues

### P1: No easy DB backup — strategies lost on pod termination
- **Cause:** The DB at `/workspace/mneme_chunks/mneme.db` is ephemeral. Every `rm -rf` or pod shutdown destroys all accumulated strategies and chunks. We can't assume Jupyter Lab is available on the pod — many GPU pods only offer SSH.
- **Fix:** `scp -P $PORT root@$IP:/workspace/mneme_chunks/mneme.db ./` before pod termination. Future: `/backup` endpoint or periodic SCP cron.

### P2: Training weight dominance (persistent)
Models with strong training data on a topic (e.g., 2016 Kumamoto earthquake) ignore injected 2026 data. Known across all models. Epistemic framing in system prompt helps but doesn't fully solve.

### P2: Concurrent writer safety (Kimi Bug #2-3)
Single SQLite connection shared across Flask threads + background archival threads with no write lock. Rare under low concurrency but will corrupt under load. Fix: `_db_lock` or single-writer queue.

### P2: FAISS ghost vectors on REPLACE (Kimi Bug #5)
When INSERT OR REPLACE fires (legacy collision case), FAISS holds old + new vectors for same chunk_id. Fixed by timestamp IDs eliminating collisions, but _id_map doesn't deduplicate on reload.

### P3: Deterministic topic labels (Kimi Bug #6)
LLM labeler with `temperature=0.0` produces identical labels for similar content. Combined with `_merge_small_groups`, different conversations get merged into same topic group. Low impact with timestamp IDs but semantically ugly.

### P3: Streaming dead code
`_chat_stream()` buffers full response then re-chunks into 16-char SSE deltas — fake streaming. True SSE generator `query_model_stream()` exists but is unused. TTFB = full generation latency.

### P4: Single-file monolith
~2,000 lines in one module. Module-level mutable state shared across Flask threads. Would benefit from splitting into storage/embedding/routing/injection/HTTP modules.

### P1: Harness prompt competition — Mneme instructions buried [NEW — Aug 7, 2026]

**Symptom:** When Mneme is used as a backend for a standard AI harness (Hermes, Pi, OpenCode), the harness's system prompt competes with Mneme's instructions. The model follows the harness persona and ignores Mneme grading/strategy rules.

**Root cause:** Mneme controls the model through prompt engineering (`[GRADE: A-F]`, `[STRATEGY: ...]`, `<<SAVE>>`). Every harness also controls the model through its own system prompt. Two prompts = two identities. The model picks one.

**Tested:**
- **Hermes (full install):** ~78KB per turn (21K system prompt + 30 tools + 70 skills). Mneme instructions injected as separate system message. Model uses Hermes tools and personality; Mneme grading captured but model defaults to Hermes behavior.
- **Pi (lightweight):** ~600 char system prompt ("expert coding assistant"). Mneme injected as second system message. Model grades and saves chunks but defaults to coding persona. When asked about Mneme, searches the web instead of acknowledging memory instructions.

**Proposed fix paths:**
- **Option B:** Reframe Mneme as a capability, not a persona. Drop "You are a memory-aware assistant" — Mneme becomes a feature the model uses within its harness identity.
- **Option C:** Ship per-harness prompt templates (`mneme-for-pi.md`, `mneme-for-hermes.md`). Each integrates Mneme rules into the harness's existing prompt structure.

### P2: Content format compatibility — array vs string [RESOLVED — Aug 7, 2026]

**Symptom:** 500 errors when Pi (or any client sending `"content": [{"type":"text","text":"..."}]`) talks to Mneme.

**Cause:** Mneme's message handling at 3 code points assumed `content` was a plain string. OpenAI's API allows both `"content": "string"` and `"content": [{"type":"text","text":"string"}]`. Hermes sends strings; Pi sends arrays.

**Fix:** Applied `_extract_text()` normalization in `query_model` (message forwarding to Ollama), `process_chat` (user message extraction), and `chat_completions` (session ID generation). Verified working with Pi Aug 7.

### P3: Ollama content array rejection [RESOLVED — Aug 7, 2026]

**Symptom:** `json: cannot unmarshal array into Go struct field ChatRequest.messages.content of type string` from Ollama.

**Cause:** Array-format content was forwarded verbatim to Ollama, which only accepts string content. The `_extract_text()` normalization now happens before Ollama forwarding.

## Not Issues (verified correct)

- WAL journaling works — `db.commit()` persists correctly.
- Cycle counter increments per flush — correct.
- `/save` returns proper error codes — not swallowing failures.
- FAISS `_idx_lock` covers read + write paths — thread-safe for this code path.
- build_context IS called for every query — confirmed via entry dump (Aug 5).
- Second requests don't crash — prior "CUDA crashes" were Python NameError bugs, now fixed.

---

## Implementation Plan: Prompt + Strategy Architecture v2 (Aug 8, 2026)

### Problem Summary

The current Mneme system prompt competes with harness prompts by defining a persona ("You are a memory-aware assistant"). Strategies only capture failure — successful approaches are never saved. Model-generated strategy creation via `[STRATEGY: ...]` tags is unreliable across different model sizes. `<<COMMANDS>>` confuse models into trying to execute them.

### Design Principles

1. **Prompt has no persona.** It describes Mneme as a system the model uses, not an identity the model adopts. Works alongside any harness prompt.
2. **Proxy handles strategy lifecycle.** The model grades responses; the proxy runs mini-conversations to decide when and how to create/improve strategies. No `[STRATEGY: ...]` output format needed in the prompt.
3. **Grading measures answer quality, not Mneme usage.** A-grade from web search beats D-grade from memory. Honest uncertainty beats fabrication.
4. **Strategies capture both success and failure.** A-grade work produces "here's how to do this" strategies. C/D/F work produces "here's what went wrong" strategies. Both compete on effectiveness.

### Phase 1: New System Prompt

Replace `system_prompt.md` with a persona-free, information-only prompt (see `docs/system-prompt-v2.md`). Changes:

- No "You are..." statements. Pure system description: "Mneme is a persistent memory system..."
- No strategy creation instructions. Model only needs to know strategies exist and are auto-managed.
- No `[STRATEGY: ...]` output format. Model never writes strategy tags.
- Grading simplified to 5 clear criteria measuring answer quality.
- `<<SAVE>>` described as user action, not model command: "The user can force a save by typing <<SAVE>>. You do not need to do anything."
- `<<COMMANDS>>` stripped by proxy before reaching the model (see Phase 3).

### Phase 2: Proxy-Driven Strategy Lifecycle

After every model response, the proxy evaluates the grade and runs decision logic:

**Success path (grade A or B):**
```
Proxy runs mini-convo with model (labeler or main model):
  Q1: "You graded this response A/B. Did you use a novel or
       repeatable approach worth saving for future sessions?
       Answer yes or no."
  
  If no → stop.
  
  If yes:
    Q2: "Does this improve or replace an existing strategy? If
         yes, state the strategy ID. If no, say 'new'."
    
    Q3: "Describe the approach in 2-3 sentences. What steps did
         you take and why did they work?"
    
    Proxy saves the result as a strategy. If Q2 referenced an
    existing ID, version-bump the old strategy and recalculate
    effectiveness.
```

**Failure path (grade C, D, or F):**
```
Proxy auto-generates boilerplate strategy:
  "Previous low-grade responses on this topic suggest trying
   different approaches. Use available tools. Verify claims
   against memory. Be honest when grading."
```
No model call needed. Simple, fast, consistent.

### Phase 3: Strategy Ranking and Improvement

**Dynamic ranking (already implemented, no change needed):**
`ORDER BY effective_grade DESC, use_count DESC`

**De-ranking via silent observation (NEW):**
When a strategy is injected before a response that receives grade D or F, the strategy's effectiveness is slightly penalized even if the model did not explicitly reference its ID. This provides negative feedback for strategies that silently fail.

**Mid-session improvement (NEW):**
Model can flag a strategy for revision during normal chat by outputting `<<REVISE id:strat_XXX>>`. The proxy runs a mini-convo asking the model to propose an improved version. This gives the model agency to fix strategies it encounters mid-session without waiting for a post-response hook.

**Improvement during success mini-convo (Phase 2, Q2):**
Already covered — the proxy asks if the new approach improves an existing strategy.

### Phase 4: <<COMMAND>> Stripping

All `<<...>>` tags in user messages are stripped by the proxy before reaching the model. This includes `<<SAVE>>`, `<<DETAIL>>`, `<<REVISE>>`, and any future commands. The proxy intercepts them and acts on them server-side. The model never sees angle-bracket commands and never tries to execute them.

Implementation: add a regex filter in `process_chat()` that removes `<<[A-Z_]+(\s+[^>]+)?>>` patterns from user message content before injection and forwarding.

### Phase 5: Prompt Injection Architecture (no change)

The current injection architecture (system prompt + memory as separate system message after harness prompt) stays. The new prompt is shorter and has no persona, so it won't compete with harness identity. Same injection point, cleaner content.

### Migration Notes

- The `[STRATEGY: ...]` regex parser in `chat_completions()` becomes dead code. Keep for backward compatibility with old sessions, but no new strategies use this path.
- Strategy table schema unchanged (version, parent_id, effective_grade, use_count, success_count). New proxy logic writes same columns.
- Grade pipeline unchanged. Grades still parsed from `[GRADE: X]` in model output.
- Mini-convos use existing `query_model()` function. No new Ollama endpoint needed.
- `<<COMMAND>>` stripping must happen AFTER command processing (proxy needs to see the tag to act) but BEFORE message forwarding to the model.

---

## Implementation Status (Aug 8, 2026)

### Completed (deployed on pod 69.30.85.95:22176)

- **Phase 1 (v2 prompt):** Persona-free prompt deployed. Pi-compatible trimmed version active. Full version saved at `docs/system-prompt-v2.md`.
- **Phase 2 (strategy lifecycle):** `_save_strategy` + `_strategy_lifecycle` functions implemented. FAISS dedup prevents boilerplate stacking. Success path (A/B) runs mini-convo; failure path (C/D/F) auto-generates boilerplate.
- **Phase 4 (COMMAND stripping):** `<<SAVE>>`, `<<DETAIL>>`, `<<REVISE>>` stripped from user messages before model sees them. Proxy intercepts and processes commands server-side.
- **Content normalization:** `_extract_text()` applied to all content access points for compatibility with array-format messages (Pi, OpenAI structured format).
- **SEARCH_MEMORY_TOOL conditional:** Only appended to tool list when client sends no tools. When client provides tools (Pi, Hermes), the harness's own tool system handles search_memory.

### In Progress

**P3: Hermes-native search_memory plugin (TODO)**

Hermes plugin to register `search_memory` as a native tool. Hermes has a Python plugin system — ~10 lines: register tool → POST to `localhost:8080/search` → return results. Plugin goes in `~/.hermes/plugins/mneme_search/plugin.py`. No streaming issues expected (Hermes tools are synchronous).

### Resolved (Aug 8, 2026)

**P3: Pi search_memory extension + streaming intercept [RESOLVED]**

- **Root cause:** Pi validates tools client-side. Proxy-injected `search_memory` was rejected. Pi's tool API uses `execute(toolCallId, params)` returning `{content: [{type: "text", text}], details: {}}` — the `handler` key was silently ignored.
- **Fix:** Pi extension (`extensions/pi/mneme-search-tool.ts`) registers the tool with an empty `execute()` handler. Proxy's `_chat_stream` interceptor catches tool calls server-side, executes search, and injects results before Pi processes the response. No hang, no conflict.
- **Verified:** Streaming mode, model calls `search_memory`, proxy returns results, model synthesizes answer. Qwen 3.6 + Pi on RunPod A40.

**P3: Proxy streaming search_memory intercept [RESOLVED]**

- **Symptom:** In streaming mode, tool calls happen mid-SSE-stream and Pi can't inject results. Model hangs waiting for tool response.
- **Fix:** `_chat_stream` detects search_memory tool calls, executes search via `route_query()`, injects results into `result["content"]`, clears `tool_calls`. Pi receives a clean response with no tool calls.

---

## Ollama KV Cache Quantization for Large Context Windows (Aug 8, 2026)

Qwen 3.6 35B on A40 (46GB VRAM) can support 129K-264K context windows by quantizing the KV cache. Without quantization, 120K context crashes with CUDA OOM (~50GB required, 46GB available).

**Modelfile for large context:**
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
