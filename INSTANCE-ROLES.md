# Instance Roles — Mneme two-instance setup

You are running on **instance A — the CODER**, port **8080**, branch **main**.

There is a second instance, **instance B — the TEST**, port **7979**, branch **dev**.

## The one rule that keeps you alive

**Never edit the code your own process runs from, and never restart yourself.**

Your process runs from `~/mneme/repo` (branch `main`). If you edit that checkout and
then restart to pick up the change, you kill your own parent process and the restart
never completes — you take yourself down. That is why B exists.

## The two checkouts

| | code checkout | branch | port | memory DB |
|---|---|---|---|---|
| A (you, coder) | `~/mneme/repo` | `main` | 8080 | `~/mneme/chunks/mneme.db` |
| B (test) | `~/mneme/test-repo` | `dev` | 7979 | `~/mneme/chunks/dev/mneme.db` |

The code is separate, the DBs are separate, the ports are separate. Nothing you do to B
can touch A.

## What you do (your job)

1. **Develop in `~/mneme/test-repo` (branch `dev`).** That is the only code you edit.
2. Commit on `dev`.
3. **Restart B to test**, detached so it doesn't block you:
   `cd ~/mneme/chunks/instances/7979 && nohup setsid bash start_proxy.sh > boot.log 2>&1 < /dev/null &`
4. Verify against port **7979**.
5. When `dev` is stable, **promote** it: merge `dev` into `main` in `~/mneme/repo`,
   then restart A (port 8080) as a deliberate, final, recoverable step.

## Hard rules

- NEVER edit, `git`-operate on, or `write` into `~/mneme/repo` (your own checkout).
- NEVER restart, kill, or stop the process on port **8080** — that is you.
- ALL changes go to `~/mneme/test-repo` and are tested on port **7979**.
- Do not touch the other instance's DB. A = `chunks/mneme.db`, B = `chunks/dev/mneme.db`.

## B's configuration (why it may differ)

- B runs `runtime.local_only: true`: the OpenRouter **Decisions API classifier** is skipped,
  and the chat model does memory labeling (the "old way" — no separate classification model).
- A currently has classification on. `local_only` is a config flag, not a branch — both
  checkouts share the same code; only the instance config differs.

## If B goes down

That is the design working. A is still alive. Fix the code in `~/mneme/test-repo`, restart B
(step 3), and carry on. Only promote to `main` once B is stable.
