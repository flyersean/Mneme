# STAGING CHECKOUT — test-repo

This is the **staging checkout** of Mneme. All development and bug fixing happens
here, on the single branch `main`, before promotion to production.

- This checkout: `/home/ubuntu/mneme/test-repo` (branch `main`)
- Tested on **instance B, port 7979**
- `/home/ubuntu/mneme/repo` is the **live production checkout** (instance A,
  port 8080). **NEVER edit `/home/ubuntu/mneme/repo`** — it is instance A's
  running code.

Git remote:
- `origin` → GitHub (`main` is the only branch). Production promotes by
  `git fetch test-repo && git merge --ff-only test-repo/main`.
