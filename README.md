# Mneme

**Work in progress** — built by flyersean with DeepSeek and Kimi K3 (AI-assisted development).

Conversational memory system for LLMs. Archives conversations, classifies them by
topic, and injects relevant past context into future sessions. Works as a transparent
proxy between your agent and Ollama.

Named after Mneme, the Greek muse of memory.

## Quick Start

```bash
# Clone and run on any Linux pod with Ollama
git clone https://github.com/flyersean/Mneme.git /tmp/mneme
bash /tmp/mneme/setup_pod.sh
```

## Architecture

```
Agent (Hermes / any OpenAI client) → Mneme proxy (:8080) → Ollama (:11434)
                                        │
                                  SQLite + FAISS
                                  (1024-dim vectors)
```

## Features

| Feature | Status |
|---------|--------|
| Tool calling (streaming + non-streaming) | ✓ |
| Memory injection with FAISS routing | ✓ |
| Classification (I_CAN/I_NEED_TOOL) | ✓ |
| Hallucination guard (KNOWN/RECALLED/UNKNOWN) | ✓ |
| Chunk+pool embedding (arctic-embed2) | ✓ |
| Large output handling (TEXT/STRUCTURED/SHORT) | ✓ |
| Force save (`POST /save` or `<<SAVE>>` in chat) | ✓ |
| Per-message topic segmentation | ✓ |
| Multi-model support (same DB) | ✓ |

## Hermes Integration

```yaml
model:
  default: text-mneme:64k
  provider: custom
  base_url: http://localhost:8080/v1
  api_key: none
memory:
  memory_enabled: false
  user_profile_enabled: false
```

## Dependencies

- Ollama (any chat model + snowflake-arctic-embed2)
- Python 3.11+, Flask, FAISS, SQLite

## License

MIT
