# Mneme VPS Workflow — Two Instances, One Branch

## The idea

The VPS runs **two Mneme instances** from **two separate code checkouts** so the
coding agent (A) can develop and test freely without ever taking down its own
runtime:

1. **Instance A — the coder** (port 8080) — the autonomous agent's own stable
   runtime. It must never break, because it is what fixes everything else.

2. **Instance B — the test** (port 7979) — runs the code under development. It can
   (and will) break; that's fine, because A is still alive to fix it.

## The branch

There is a single branch: `main`. It is the one true line of code and the only
branch on GitHub. The dev/prod split is **by checkout**, not by branch.

## The repo checkouts

| Path | Checkout | Instance | Role |
|---|---|---|---|
| `/home/ubuntu/mneme/repo`      | production | 8080 (coder) | Never edited — A's running code |
| `/home/ubuntu/mneme/test-repo` | staging    | 7979 (test)  | The only code you edit |

Both are separate clones of `main`. Development happens in `test-repo`, is tested
on 7979, then fast-forwarded into `repo`'s `main` and pushed to GitHub.

## The instances

| Port | Role | Model | Config dir |
|---|---|---|---|
| 8080 | A — coder (stable) | `z-ai/glm-5.3`       | `chunks/instances/8080/` |
| 7979 | B — test (cheap)   | `qwen/qwen3.8-flash` | `chunks/instances/7979/` |

Start / restart (always as the `ubuntu` user, never `sudo`):

    /home/ubuntu/mneme/chunks/instances/8080/start_proxy.sh   # A (coder)
    /home/ubuntu/mneme/chunks/instances/7979/start_proxy.sh   # B (test)

They have **separate memory databases**: A = `chunks/mneme.db`, B = `chunks/dev/mneme.db`.
B runs `runtime.local_only: true` (no classifier; the chat model does the labeling).

> **Never launch via `sudo ./start_proxy.sh`.** sudo stamps `no_new_privs` on
> the process, which is inherited by every child and breaks `sudo` inside the
> agent's bash tool. Launch as `ubuntu`; verify with
> `grep NoNewPrivs /proc/<pid>/status` (expect `0`).

## The daily loop

1. The coder (A) develops in `~/mneme/test-repo` (staging checkout), commits:

       cd ~/mneme/test-repo
       git pull --rebase origin main
       # ... edit ...
       git add -A && git commit -m "feat: what and why"

2. Test on B (7979): restart it and verify.

       cd ~/mneme/chunks/instances/7979 && nohup setsid bash start_proxy.sh > boot.log 2>&1 < /dev/null &

3. When stable, promote (human action, in `~/mneme/repo`):

       cd ~/mneme/repo
       git fetch test-repo && git merge --ff-only test-repo/main
       git push origin main
       # then restart A (8080) to run the new main

4. If B breaks, A is still up. Fix `test-repo`, restart B, carry on.

## Git identity + credentials

- Identity (repo-local): `Mneme Agent <agent@mneme.local>` (set on both checkouts).
- Push token: stored in the **secrets store** (`<mneme-root>/secrets.yaml`, key
  `github`), read on demand by a git `credential.helper`. Both checkouts have it wired up.

## The agent's own instructions

The coder reads `AGENTS.md` (the "## 0. Git workflow" section) and
`INSTANCE-ROLES.md`, which spell out its rules. Keep those in sync with this
document.
