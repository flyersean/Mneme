---
name: verification-before-completion
description: Prove work is actually done before claiming it — verify the DEEPEST layer (not exit 0 / HTTP 200 / "it runs"), require positive controls for absence claims, verify the verifier against deliberately-broken code, and apply the fresh-install bar. Load before declaring any fix, build, deploy, or feature complete.
tags: [verification, testing, done, deep-layer, positive-control, fresh-install, fail-loud]
---

# Skill: verification before completion

*Adapted from obra/superpowers — MIT License, Copyright (c) 2025 Jesse Vincent.*

The correctness bar is **"does it do what the user wanted"**, not "does it run without
crashing." A 200 OK, a clean exit, and green logs can all be present while the output is
silently wrong. Never claim done from the ABSENCE of errors — only from the PRESENCE of
the intended outcome.

## 1. Verify the deepest layer, never the API's 200

A successful request proves nothing about whether the thing you changed actually applied.
Find the furthest point and check there:

- A model setting (`temperature`, `repeat_penalty`, a template)? Read what the **backend
  actually received** — the sampler log, the outbound JSON, not the config that forwards it.
- A database write? Check the **row count / column values** in the live table, not the
  INSERT that was issued.
- A proxy behavior? Hit the **real endpoint** and read the body, not just the status line.
- A removed/flagged chunk? Confirm via `search_memory` that it's actually gone/hidden.

The gap between "the code reads it" and "it took effect" is exactly where silent no-ops
live. See `llm-code-slops` for the full catalog (inert settings, implemented-but-never-called
helpers, tests that assert the bug).

## 2. Absence assertions need a positive control

A check that asserts something is ABSENT passes vacuously when the precondition for it
being present was never met. Before asserting X is gone/hidden/removed, assert X was there:

```
# WRONG — passes even if search returns [] for everything (no embedder)
check("removed chunk not in search", cid not in search(marker))

# RIGHT — positive control first
pre = search(marker)
check("PRE-CONDITION: chunk WAS findable", cid in pre)
... remove ...
post = search(marker)
check("removed chunk not in search", cid not in post)
```

If the pre-condition can't be met (no embedder, no GPU, no network), the honest output is
**"unproven here"**, not a green line.

## 3. Verify the verifier — break the code on purpose

The only way to know a check catches anything is to run it against a deliberately-broken
build. Make a copy with the feature disabled, run the checker against both. If the broken
build doesn't fail, the checker is decoration. Do this BEFORE trusting the script on the
real system — a false "verified" claim in a handoff is worse than no claim.

Run the checker against a healthy system too: a checker that has only ever been run in the
broken direction is unvalidated in the direction you actually care about.

## 4. The fresh-install bar

"Works for me" is not done. The review bar for anything user-facing is: **does it work on a
FRESH install** — a clean box following the documented steps, not your warmed-up checkout.
Test against a clean clone / fresh instance, or state clearly that it wasn't.

## 5. local-green ≠ pod-green

Passing on your laptop does not mean it works where it will run. The pod/deploy update is
three steps, all required: **git pull + pod tests + live exercise.** A change that is green
locally but not exercised live on the target is unverified.

## 6. Fail loud — scripts end explicit done/failed

A verification script that ends with a raw traceback or a bare non-zero exit tells the
operator nothing. Every script must print an explicit summary and end in a definite state:

```bash
echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && echo "VERIFY: DONE" || { echo "VERIFY: FAILED"; exit 1; }
```

Never let a script "just stop". An early `return 1` with no output hides the checks it
could have produced — prefer a fallback path plus a summary that always prints.

## 7. When a positive-direction check fails, look for your own assumption

"chunk count increased" FAILED on a healthy system is usually the script's assumption, not
a product bug (e.g. the staging buffer flushes on the NEXT turn, so a single turn + poll can
never see it). State each precondition as its own check so the failure lands on the real
cause instead of triggering a hunt through correct code. Don't conclude the feature is
broken from the first red line — read the detail and ask whether the precondition was ever
established.

## Pitfalls

- **A check that fails for a HARNESS reason looks identical to one that fails for a PRODUCT
  reason.** Read the failure detail before declaring a bug.
- **`git stash` reverts your new test too.** When proving a regression test catches the bug,
  commit the test first or copy it aside before stashing the fix — otherwise the baseline
  has nothing to fail and you wrongly conclude the test is weak.
- **A green test is only evidence if you know why it's green.** A test failing with a
  confusing message that doesn't match what you changed means the harness, not the code.

## Verification checklist

1. The deepest layer was checked, not just the 200/exit-0.
2. Every absence claim has a positive control (or is reported unproven).
3. The checker was run against deliberately-broken code and caught it.
4. A fresh-install (or clean-clone) pass was demonstrated, or the gap stated.
5. Scripts end with an explicit DONE/FAILED summary, not a raw error.
