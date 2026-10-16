# Mneme VPS Workflow — Two Instances, Two Branches

## The idea

The VPS runs **two Mneme instances** from **two separate code checkouts** so the
coding agent (A) can develop and test freely without ever taking down its own
runtime:

1. **Instance A — the coder** (port 8080, branch `main`) — the autonomous agent's
   own stable runtime. It must never break, because it is what fixes everything else.

2. **Instance B — the test** (port 7979, branch `dev`) — runs the code under
   development. It can (and will) break; that's fine, because A is still alive to fix it.

## The branches

| Branch | What it is | Runs on |
|---|---|---|
| `main` | Stable, trusted code. A's runtime. | A (8080) |
| `dev`  | Development / staging code. | B (7979) |

- The coder (A) commits and pushes **only** to `dev`.
- `main` is never touched by the agent — promotion (`dev` -> `main`) is a human action.
- Both branches sync through GitHub (origin) as the shared remote.

## The repo checkouts

| Path | Branch | Instance | Pushes |
|---|---|---|---|
| `/home/ubuntu/mneme/repo`      | `main` | 8080 (coder) | No (never) |
| `/home/ubuntu/mneme/test-repo` | `dev`  | 7979 (test)  | Yes (dev only) |

Both are separate clones pointing at the same GitHub remote.

## The instances

| Port | Role | Model | Config dir |
|---|---|---|---|
| 8080 | A — coder (stable) | `z-ai/glm-5.3`       | `chunks/instances/8080/` |
| 7979 | B — test (cheap)   | `qwen/qwen3.8-flash` | `chunks/instances/7979/` |

Start / restart (always as the `ubuntu` user, never `sudo`):

    /home/ubuntu/mneme/chunks/instances/8080/start_proxy.sh   # A (coder)
    /home/ubuntu/mneme/chunks/instances/7979/start_proxy.sh   # B (test)

Each start script `cd`s into its own checkout, so 8080 runs `main` and 7979 runs
`dev`. They have **separate memory databases**: A = `chunks/mneme.db`, B = `chunks/dev/mneme.db`.
B runs `runtime.local_only: true` (no classifier; the chat model does the labeling).

> **Never launch via `sudo ./start_proxy.sh`.** sudo stamps `no_new_privs` on
> the process, which is inherited by every child and breaks `sudo` inside the
> agent's bash tool. Launch as `ubuntu`; verify with
> `grep NoNewPrivs /proc/<pid>/status` (expect `0`).

## The daily loop

1. The coder (A) develops in `~/mneme/test-repo` on `dev`, commits, pushes:

       cd ~/mneme/test-repo
       git pull --rebase origin dev
       # ... edit ...
       git add -A && git commit -m "feat: what and why"
       git push origin dev

2. Test on B (7979): restart it and verify.

       cd ~/mneme/chunks/instances/7979 && nohup setsid bash start_proxy.sh > boot.log 2>&1 < /dev/null &

3. When `dev` is stable, promote (human action, in `~/mneme/repo`):

       cd ~/mneme/repo
       git checkout main && git pull
       git merge origin/dev
       git push origin main
       # then restart A (8080) to run the new main

4. If B breaks, A is still up. Fix `dev`, restart B, carry on.

## Git identity + credentials

- Identity (repo-local): `Mneme Agent <agent@mneme.local>` (set on both checkouts).
- Push token: stored in the **secrets store** (`<mneme-root>/secrets.yaml`, key
  `github`), read on demand by a git `credential.helper`. Both checkouts have it wired up.
- Recommended hardening: replace the shared token with a **fine-grained PAT**
  scoped to `dev` only, so the agent can never push to `main`.

## The agent's own instructions

The coder reads `AGENTS.md` (the "## 0. Git workflow" section) and
`INSTANCE-ROLES.md`, which spell out its branch rules. Keep those in sync with this
document.
