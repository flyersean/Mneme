# Mneme VPS Workflow — Two Instances, Two Branches

## The idea

The VPS runs **two completely separate Mneme instances** so a self-coding agent
can edit code freely without ever taking down the known-good system:

1. **Self-code instance** (port 8080) — an autonomous agent that edits the
   codebase, commits, and pushes to its own branch. It can (and will) break
   things; that's fine, because it's sandboxed on its own branch.

2. **Standby instance** (port 8082) — a clean copy of the latest stable code,
   used to fix the system when the self-code instance breaks it.

## The branches

| Branch | What it is | Who works on it |
|---|---|---|
| `self-code` | The agent's sandbox. Pull + push freely. | The self-coding agent (8080). |
| `classification-models` | The stable, reviewed branch. | Humans + the standby fixer. |

- The self-coding agent pulls and pushes **only** `self-code`.
- Periodically, a human reviews `self-code` and merges the good changes into
  `classification-models`.
- `classification-models` is never touched by the autonomous agent.

## The repo checkouts

Both instances run the same proxy code, but from **two different checkouts** so
they can sit on different branches at the same time:

| Path | Branch | Instance | Push? |
|---|---|---|---|
| `/home/ubuntu/mneme/repo` | `self-code` | 8080 (self-code) | Yes |
| `/home/ubuntu/mneme/repo-standby` | `classification-models` | 8082 (standby) | No (read-only) |

## The instances

| Port | Role | Model | Config dir |
|---|---|---|---|
| 8080 | Self-code agent | `z-ai/glm-5.3` | `chunks/instances/8080/` |
| 8082 | Standby fixer | `deepseek/deepseek-chat` | `chunks/instances/8082/` |

Start / restart (always as the `ubuntu` user, never `sudo`):

    /home/ubuntu/mneme/chunks/instances/8080/start_proxy.sh   # self-code
    /home/ubuntu/mneme/chunks/instances/8082/start_proxy.sh   # standby

Each start script `cd`s into its own checkout, so 8080 runs the `self-code`
code and 8082 runs the `classification-models` code. They have separate memory
databases (`chunks/instances/8080` vs `8082`) and separate models.

> **Never launch via `sudo ./start_proxy.sh`.** sudo stamps `no_new_privs` on
> the process, which is inherited by every child and breaks `sudo` inside the
> agent's bash tool. Launch as `ubuntu`; verify with
> `grep NoNewPrivs /proc/<pid>/status` (expect `0`).

## The daily loop

1. The self-coding agent works on `self-code`, commits and pushes.
2. When it produces something worth keeping, merge it into `classification-models`:

       cd /home/ubuntu/mneme/repo
       git checkout classification-models && git pull
       git merge self-code            # or cherry-pick specific commits
       git push origin classification-models

3. If the self-code instance breaks the system:
   - Fix it on the **standby** instance (8082), which is on clean
     `classification-models` — the fix won't be affected by the broken agent.
   - Push the fix to `classification-models`.
   - Then repair/reset the self-code checkout as needed.

## Git identity + credentials

- Identity (repo-local, on the self-code checkout): `Mneme Agent <agent@mneme.local>`.
- Push token: stored in the **secrets store** (`<mneme-root>/secrets.yaml`, key
  `github`), read on demand by a git `credential.helper`. Only the self-code
  checkout has it wired up; the standby checkout has no credentials.
- Recommended hardening: replace the shared token with a **fine-grained PAT**
  scoped to the `self-code` branch only, so the autonomous agent can never push
  to `classification-models`.

## The agent's own instructions

The self-coding agent reads `AGENTS.md` (the "## 0. Git workflow" section) which
spells out its branch rules. Keep that section in sync with this document.
