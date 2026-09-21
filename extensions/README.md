# Extensions

Programs that add capability on top of Mneme. They live here, but they are **not part
of the proxy** — nothing in this directory is imported by `proxy/`, and no extension
imports the proxy. The only connection is the network API.

## The contract

An extension talks to a running Mneme proxy over HTTP. That's it.

```text
     ┌────────────────┐   ┌────────────────┐   ┌────────────────┐
     │  Pi extension  │   │  swarm         │   │  your tool     │
     └───────┬────────┘   └───────┬────────┘   └───────┬────────┘
             │                    │                    │
             └────────────────────┼────────────────────┘
                                  │  HTTP
                       ┌──────────┴──────────┐
                       │   Mneme proxy       │
                       │   /v1/chat/...      │
                       │   /search /list     │
                       │   /memory/...       │
                       └─────────────────────┘
```

Useful properties that fall out of an HTTP boundary:

- write it in any language
- run it on a different machine from the proxy
- it cannot break the proxy by changing, and a proxy upgrade cannot break it
- several extensions can share one proxy, or use instances sharing one memory DB

## What the API gives you

Send a normal OpenAI-shaped request to `POST /v1/chat/completions` and the proxy
applies memory retrieval, injection, the tool loop, and provenance grading for you.
That is the reason the memory layer lives in a proxy rather than a library: any client
gets the whole stack without implementing it.

Other endpoints an extension may want:

| Surface | Endpoint | Use it for |
| --- | --- | --- |
| Chat | `POST /v1/chat/completions` | Full agent turns (everything below applied proxy-side) |
| Chat (native) | `POST /api/chat` | Same, in Ollama's native shape |
| Memory search | `GET /search` | Retrieval without spending a generation |
| Recent chunks | `GET /list`, `GET /detail/<chunk_id>` | Browsing what memory contains |
| Memory management | `POST /memory/chunks/<id>/remove` | Taking a chunk out of circulation (or restoring it) |
| Bad chunks | `GET /memory/chunks?proposed=1`, `POST /memory/chunks/<id>/bad` | Chunks flagged as suspected-wrong (by the model or the user) — a marker, not a removal |
| Audit log | `GET /memory/log` | Every curation action, who, and why |
| Prompts | `GET/POST /instructions*` | Reading or editing system prompts |
| Health / models | `GET /health`, `/models` | Discovery, readiness |

See the main [README](../README.md#extensions) for the same table with more context.

## The included extensions

### `swarm/` — the reference example

A declarative agent workflow engine, and the intended template for writing your own.
Start with `swarm_orchestrator.py` and read `call_mneme()` — that method (~20 lines) is
the entire integration with Mneme. Everything else in the file is orchestration logic
that is independent of the proxy.

- [`swarm/README.md`](swarm/README.md) — what it does, plus a "writing your own
  extension" walkthrough
- [`swarm/SWARM_REFERENCE.md`](swarm/SWARM_REFERENCE.md) — field-by-field config spec
- [`swarm/swarm_config.yaml`](swarm/swarm_config.yaml) — a worked example config

### `pi/` — a minimal integration

Pi coding-agent tools that let Pi call Mneme's memory and web tools. Much smaller than
the swarm, and useful for seeing how little is required to integrate: it is a couple of
TypeScript files calling the proxy's endpoints directly.

## Writing your own

1. **Pick what the proxy should own.** Anything sent to `/v1/chat/completions` arrives
   with memory, tools, provenance, and the tool loop already applied. Don't
   reimplement those.
2. **Call it over HTTP.** No imports, no shared code, any language.
3. **Keep your own state outside Mneme.** The swarm uses the filesystem as a shared
   board between steps; that's a choice, not a requirement. Your extension can hold
   state however it likes.
4. **Use the read-only endpoints for control.** `/health` to wait for a proxy to be
   ready, `/search` to retrieve without a generation, `/memory/*` to correct bad
   memories programmatically.

Nothing needs to be registered with Mneme, and no proxy restart is required to add or
remove an extension — you just point a new client at the port.
