# Mneme — Issue Tracker

## Status: Working (2026-07-30)

Tested with Qwen3.6 35B + Hermes agent. All core features functional.

## ✓ Resolved

- Streaming tool_calls conversion (dict → JSON string)
- FAKE_MODEL_ID sent to Ollama (now uses real model name)
- Hermes system prompt preservation
- OpenAI/Ollama dual-format responses
- Missing enumerate in non-streaming tool_calls
- Ollama error logging
- Embedding upgrade (arctic-embed2, 1024-dim, chunk+pool)
- Hallucination guard (KNOWN/RECALLED/UNKNOWN classification)
- Injection content logging (/tmp/injection_log.txt)
- Force save endpoint (POST /save)
- Tool output classification (TEXT/STRUCTURED/SHORT routing)
- Staging buffer topic segmentation
- Save trigger in chat (<<SAVE>>)

## Needed features

- **Image input handling**: Multimodal messages with `image_url` content type
  pass through to Ollama but the memory pipeline assumes string content.
  `classify_chunk` and `build_context` need to handle list-typed content.

  **Suggested approach:** Store image references in chunk metadata.
  When `build_context` injects a chunk containing images, append
  `[IMAGE: <url> — attached to conversation below]` so the model can
  re-fetch/re-analyze the image on demand. Images themselves aren't
  embedded (arctic-embed2 is text-only) but the conversation text about
  them is fully searchable.

## Known issues (dev branch)

- **`<<SAVE>>` timeout on large conversations**: At ~46 messages / ~30K tokens,
  the save command hits HTTP 500 after ~86s. Likely model token limit or VRAM
  ceiling. Occurs on Qwen3.6 35B with large tool outputs in context.
  Not seen on short conversations. Does not affect normal recall.

- **Future test: small context models**: Test a model with a small native context window
  (e.g., 8K or 32K) to verify Hermes/Custom provider respects the model's actual limit
  vs the proxy's reported metadata. Currently the proxy reports no context size — Hermes
  defaults to 256K for custom providers. May need to expose `context_length` in model
  metadata or set `model.max_tokens` in Hermes config.
