---
name: proxy-delegation
description: Delegate a self-contained task to a separate Mneme proxy (a worker) — as a single chat turn or a full harness run — and collect the answer. Use for jobs that are independent, parallelizable, need a different model/config, or should be isolated from your own memory and context. Also covers enumerating and managing (reuse vs delete) the worker proxies you create.
---

# Skill: delegating to another proxy

A "delegate proxy" is another Mneme instance on its own port. Hand it a
self-contained goal over HTTP and read the answer back. It is a full proxy
(memory, tools, grading, and the harness), so it does the whole job.

Use for: a task that doesn't need your context, several independent tasks in
parallel (one worker each), or a job wanting a different model/config.

## Spin up — pick a memory mode

- **Shared:** `python3 scripts/mneme_setup.py --add answers.json` under the SAME
  `MNEME_CHUNK_DIR`. The worker shares your DB and remembers what you know.
- **Isolated (default):** run setup with a DIFFERENT `MNEME_CHUNK_DIR` so the
  worker starts empty. Use this unless the worker needs your history.

`answers.json`: `{"port": 8082, "chat_model": "<model id>"}` (add `chat_backend`,
`ctx_size` as needed). One port = one worker; never reuse a port for another job.

## Enumerate workers

`GET /system/status` is the source of truth for what proxies exist on this box —
not `/health`, and not a raw `curl` of each port.

- It returns `instances` with `port`, `running`, `model`, `owner_pid`, `self`.
- `running: false` means the instance folder still exists but the process is
  down — created, not deleted. A port that shows up here is a known worker,
  alive or dead.
- ⚠ Do NOT probe up/down with `curl -o /dev/null -w %{http_code} <port>/health`:
  a gateway may forward a dead port's request to a healthy peer and return 200
  with a chat reply, so a dead port looks alive. Only `instances[].running` (a
  real probe the proxy itself makes) is trustworthy.
- The list only covers `instances/` under this proxy's own chunk dir; workers
  created under a different chunk dir are invisible here. Track what you spawn.

## Delegate — two modes (the goal must stand alone)

**Chat turn** — a single synchronous turn:

```bash
curl -s http://localhost:<port>/v1/chat/completions \
  -H "Authorization: Bearer $MNEME_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"default","messages":[{"role":"user","content":"<goal>"}]}'
```
The answer is `choices[0].message.content`.

**Harness run** — a bigger job; the worker plans → executes → verifies in the
background instead of a single turn:

```bash
curl -s -X POST http://localhost:<port>/runs \
  -H "Authorization: Bearer $MNEME_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"goal":"<goal>","start":true}'
# -> {"run":{"run_id":"...","status":"..."}}
```
Poll until the status is terminal (completed/failed):

```bash
curl -s http://localhost:<port>/runs/<run_id> -H "Authorization: Bearer $MNEME_API_TOKEN"
```
Structured (planner → tasks → verify) is the default; add `"free_form":true` for
a durable open-ended goal session. Use a harness run for multi-step work worth
planning and verifying; a chat turn for a quick single-shot question.

## Cleanup (avoid zombie proxies)

A worker keeps running until you kill its process AND delete its folder:

1. Kill the process on that port (or call the `delete_proxy` tool with the port).
2. Delete `<chunk_dir>/instances/<port>/`.

Reuse a worker (keep the folder) when you will delegate again soon — faster than
recreating. Delete it for one-off jobs. Prefer the `delete_proxy` tool or the
dashboard Remove button over manual removal. An orphaned proxy on a port is a
leftover delegate — clean it up.
