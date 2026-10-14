---
name: proxy-delegation
description: Delegate a self-contained task to a separate Mneme proxy (a worker) and collect the answer. Use for jobs that are independent, parallelizable, need a different model/config, or should be isolated from your own memory and context. Also covers managing (reuse vs delete) the worker proxies you create.
---

# Skill: delegating to another proxy

A "delegate proxy" is another Mneme instance on its own port. You hand it a
self-contained goal over HTTP and read the answer back. The worker is a full
proxy (memory, tools, grading), so it does the whole job, not one step.

Use it when: a task doesn't need your context (isolate it), several independent
tasks can run in parallel (one worker each), or a job wants a different model or
config than you have.

## Spin up a worker — pick a memory mode

- **Shared memory:** `python3 scripts/mneme_setup.py --add answers.json` under
  the SAME `MNEME_CHUNK_DIR`. The worker shares your DB and remembers what you
  know.
- **Isolated (default):** run the setup wizard with a DIFFERENT `MNEME_CHUNK_DIR`
  so the worker starts with empty memory. Use this unless the worker needs your
  history.

`answers.json`: `{"port": 8082, "chat_model": "<model id>"}` (add `chat_backend`,
`ctx_size` as needed). One port = one worker; never reuse a port for a different
job. Ports persist until you delete them, so track what you created.

## Delegate

Send a self-contained goal — it must stand alone: paste any facts it needs, do
not assume shared conversation context.

```bash
curl -s http://localhost:<port>/v1/chat/completions \
  -H "Authorization: Bearer $MNEME_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"default","messages":[{"role":"user","content":"<goal>"}]}'
```

The answer is `choices[0].message.content`.

## Cleanup (avoid zombie proxies)

A worker keeps running until you kill its process AND delete its folder:

1. Kill the process on that port.
2. Delete `<chunk_dir>/instances/<port>/`.

Reuse a worker (keep the folder) when you will delegate again soon — faster than
recreating. Delete it for one-off jobs. If a "delete proxy" button or surface
exists, prefer it over manual removal; otherwise remove the folder. An orphaned
proxy on a port is a leftover delegate — clean it up.
