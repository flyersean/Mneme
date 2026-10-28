# Mneme — Simple Handoff (for a non-engineer)

Short guide to how Mneme is set up on this VPS and the daily routine.
The old detailed agent handoff is preserved in git history if ever needed.

---

## The 3-places rule

There are only three places that matter:

1. **The repo** — `/home/ubuntu/mneme/repo`
   The **only** place where code is ever edited.
   Both running Mneme instances load their code directly from here.

2. **Production data** — `/home/ubuntu/mneme/chunks/instances/8080/` (port 8080)
   This is your **live Mneme** (memory, chat UI). It holds config and data.
   **Never hand-edit anything here.** It restarts only through
   `mneme-deploy production --yes`.

3. **Staging** — `/home/ubuntu/mneme/chunks/instances/8083/` (port 8083)
   A safe copy for trying out new code before it touches production.

Why this matters: if there is only one copy of the code (the repo), you can
never be confused about "which version is running". Running instances record
the commit they started from in a file called `DEPLOYED_COMMIT`, and
`mneme-status` uses it to tell you if anything is out of date.

---

## The two helper commands

Both live in the repo (`repo/scripts/`) with shortcuts at
`/home/ubuntu/mneme/mneme-status` and `/home/ubuntu/mneme/mneme-deploy`
(symlinks), so you can run them from any folder:

    mneme-status

"Which instance is running where, and is it up to date?"
Prints, in plain language: the repo's branch, whether anything is unsaved or
unpushed, and each running instance's port, role (production / staging /
test), and whether it matches the repo's current code.

    mneme-deploy staging        # update the staging copy (port 8083)
    mneme-deploy production --yes   # update live Mneme (port 8080) — asks for --yes on purpose

`mneme-deploy` runs quick checks on the code first, then restarts the target
instance and records which commit it's running. Staging warns but continues
if some old tests fail; production aborts unless everything passes, so the
live instance is protected. **Tip:** production restarts YOUR live Mneme on
port 8080 — expect it to be briefly down and to reconnect afterwards.

PATH note: if your shell says "command not found", run them by full path
(`/home/ubuntu/mneme/mneme-status`) or add `/home/ubuntu/mneme` to your PATH.

---

## The daily workflow

1. Edit code only in the repo (`/home/ubuntu/mneme/repo`).
2. Commit it (`git commit`), push it (`git push`).
3. Run **`mneme-deploy staging`** — updates port 8083.
4. Check it's healthy: open `http://localhost:8083/health` (or just run
   `mneme-status`).
5. If it looks good: **`mneme-deploy production --yes`** — updates port 8080.
6. Run **`mneme-status`** at the end (and anytime you're unsure) to confirm
   everything is RUNNING and up to date.

---

## Branch policy (keep it boring)

- There is **one branch** — `main`. It is the one true line of code.
- If a separate change is ever needed, make a **short-lived branch**, test it,
  and **merge it back quickly** — then delete it.
- Never leave experiment branches hanging around; they cause exactly the
  "which version is real?" confusion this setup avoids.
- (All older experiment branches — `agent-harness`, `memory-only`, `self-code`,
  `dev`, `unified_mneme`, `classification-models` — have been deleted. Only
  `main` remains.)

---

## Laptop stays a client

Your laptop does **not** hold a copy of the code or run Mneme. It just
*connects* to the VPS (browser / chat UI / API). All code, data, editing and
deploys happen on the VPS. That keeps one single source of truth and one
place to look when something is wrong.
