# Mneme Skill Library — Catalog

Two install tiers:

- **Default (`skills/`)** — ships with Mneme and is loaded automatically. A few core skills.
- **Optional (`skill-library/`)** — NOT loaded by default. Install one on demand with
  `scripts/install-skill.sh <name> [proxy-port]` (copies it into `skills/`; pass the port
  to make it live without a restart).

License note: every skill below is MIT. Skills ported from obra/superpowers carry the
copyright line "Copyright (c) 2025 Jesse Vincent" in their SKILL.md; skills authored for
Mneme are covered by this repo's MIT LICENSE. Do not add a skill here without recording
its license and attribution.

## Default install (`skills/`)

| Skill | What it does | License |
|---|---|---|
| skill-creator | How to write a good Mneme skill (SKILL.md shape, retrieval hook, pitfalls) | MIT (this repo) |
| find-and-install-skills | Find a missing skill and install it (library first, then the web) | MIT (this repo) |
| systematic-debugging | 4-phase root-cause debugging, Mneme-tuned (attribute Mneme-vs-model first) | MIT — obra/superpowers |
| verification-before-completion | Prove work is done: deepest-layer checks, positive controls, fresh-install bar | MIT — obra/superpowers |
| swarm-creation | Design, validate and run a Mneme swarm | MIT (this repo) |

## Optional library (`skill-library/`)

### engineering/

| Skill | What it does | License |
|---|---|---|
| brainstorming | Draw out what the user actually wants before building | MIT — obra/superpowers |
| writing-plans | Turn a spec into an actionable implementation plan | MIT — obra/superpowers |
| executing-plans | Execute a plan task-by-task as the implementer | MIT — obra/superpowers |
| test-driven-development | RED-GREEN-REFACTOR; tests before implementation code | MIT — obra/superpowers |
| requesting-code-review | Dispatch a review before merging to catch issues early | MIT — obra/superpowers |
| receiving-code-review | Evaluate review feedback technically, not performatively | MIT — obra/superpowers |
| subagent-driven-development | Execute independent tasks by delegating to background runs | MIT — obra/superpowers |
| dispatching-parallel-agents | Fan out 2+ independent tasks in parallel | MIT — obra/superpowers |
| using-git-worktrees | Isolate feature work with git worktrees | MIT — obra/superpowers |
| finishing-a-development-branch | Integrate completed work: merge, push, review, clean up | MIT — obra/superpowers |

### meta/

| Skill | What it does | License |
|---|---|---|
| writing-skills | Deep guide to authoring effective skills (persuasion, testing skills) | MIT — obra/superpowers |

All "obra/superpowers" entries are adapted from https://github.com/obra/superpowers,
MIT License, Copyright (c) 2025 Jesse Vincent. The full attribution line is in each
skill's SKILL.md body.
