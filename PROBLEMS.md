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

None currently. See GitHub issues.
