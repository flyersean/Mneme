# Mneme

Conversational memory system for LLMs. Archives conversations, classifies by topic, and injects relevant past context into future sessions. Transparent proxy between your agent and Ollama.

Named after Mneme, the Greek muse of memory.

## Quick Start

```bash
git clone https://github.com/flyersean/Mneme.git /tmp/mneme
cd /tmp/mneme && git checkout dev-chunks && bash setup_pod.sh
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
| Dynamic topic labels from content (no fixed clusters) | ✓ |
| Embedding-based classification (no model call) | ✓ |
| 500-char chunk/injection alignment (full-chunk injection) | ✓ |
| Chunk+pool embedding (arctic-embed2) | ✓ |
| POST /search — FAISS search returning chunk IDs + scores | ✓ |
| GET /list — recent 50 chunks | ✓ |
| GET /detail/<id> — full chunk retrieval | ✓ |
| Memory Strategies (learned from failures, auto-injected) | ✓ |
| Topic dedup via DB topic_label | ✓ |
| save_chunk: all messages preserved (no truncation) | ✓ |
| Sliding window (32 messages) | ✓ |
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

## Endpoints

- `GET /health` — status, chunk count, model
- `POST /v1/chat/completions` — OpenAI-compatible chat
- `GET /list` — recent 50 chunks
- `POST /search` — FAISS search `{"query": "...", "top_k": 10}`
- `GET /detail/<chunk_id>` — full chunk content
- `POST /save` — force archive

## Pod Commands

```bash
bash restart_proxy.sh           # kill old proxy, start fresh
bash setup_pod.sh               # full pod setup
```

## Dependencies

- Ollama (chat model + snowflake-arctic-embed2)
- Python 3.11+, Flask, FAISS, SQLite, NumPy

## License

MIT
