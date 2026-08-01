# Mneme

Conversational memory system for LLMs. Archives conversations, classifies them by topic, and injects relevant past context into future sessions. Transparent proxy between your agent and Ollama.

Named after Mneme, the Greek muse of memory.

## Quick Start

```bash
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
| Streaming + non-streaming tool calls | ✓ |
| FAISS memory injection with routing | ✓ |
| Silent page ingestion (auto-stage + save) | ✓ |
| Topic-aware chunking (per-message classification) | ✓ |
| Descriptive topic labels from content | ✓ |
| Embedding-based classification (no model call) | ✓ |
| Chunk+pool embedding (arctic-embed2) | ✓ |
| `<<DETAIL id:chunk_id>>` retrieval | ✓ |
| `<<SAVE>>` archive trigger | ✓ |
| Force save (`POST /save`) | ✓ |
| Multi-model support (same DB) | ✓ |
| Sliding window (32 messages) | ✓ |

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

## Commands

- `<<SAVE>>` — archive current conversation
- `<<DETAIL id:chunk_id>>` — retrieve full stored chunk

## Dependencies

- Ollama (chat model + snowflake-arctic-embed2)
- Python 3.11+, Flask, FAISS, SQLite, NumPy

## License

MIT
