---
name: proxy-delegation
description: Delegate a self-contained task to a separate Mneme proxy (a worker) — as a single chat turn or a full harness run — and collect the answer. Use for jobs that are independent, parallelizable, need a different model/config, or should be isolated from your own memory and context. Also covers managing (reuse vs delete) the worker proxies you create.
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

1. Kill the process on that port.
2. Delete `<chunk_dir>/instances/<port>/`.

Reuse a worker (keep the folder) when you will delegate again soon — faster than
recreating. Delete it for one-off jobs. If a "delete proxy" button or surface
exists, prefer it over manual removal; otherwise remove the folder. An orphaned
proxy on a port is a leftover delegate — clean it up.
