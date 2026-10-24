# DEV CHECKOUT — test-repo

This is the **dev checkout** of Mneme, on branch `dev`. All development and
bug fixing happens here.

- This checkout: `/home/ubuntu/mneme/test-repo` (branch `dev`)
- Tested on **instance B, port 7979**
- `/home/ubuntu/mneme/repo` is the **live production checkout** (instance A,
  port 8080, branch `main`). **NEVER edit `/home/ubuntu/mneme/repo`** — it is
  instance A's running code.

Git remotes:
- `origin` → `/home/ubuntu/mneme/repo` (local clone source; fetch-only in practice — do not push to it)
- `test-repo` → `/home/ubuntu/mneme/test-repo` (remote for this checkout itself; `git ls-remote test-repo` must succeed)
