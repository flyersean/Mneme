---
name: systematic-debugging
description: 4-phase root cause debugging — understand a bug before fixing it. Attribute Mneme-vs-model first, build a tight repro loop, find the root cause, then fix architecturally (not a patch-on-patch). Load for any bug, test failure, crash, or "it doesn't do what I asked".
tags: [debugging, troubleshooting, root-cause, investigation, bugs, failures]
requires: [verification-before-completion]
---

# Skill: systematic debugging

*Adapted from obra/superpowers — MIT License, Copyright (c) 2025 Jesse Vincent.*

Random fixes waste time and create new bugs. Quick patches mask the root cause.

**Core principle: find the root cause BEFORE writing a fix. Symptom fixes are failure.**

## The Mneme-first rule (do this before anything else)

Before investigating the code, answer one question:

> **Is this a Mneme bug, or a model behavior?**

- If the SAME model/provider/library works fine in another system (Hermes, OpenCode,
  Jan, a direct API call) but fails in Mneme, the bug is in **Mneme's** client, payload,
  or config — **never blame the model.** Diff Mneme's actual request/behavior against the
  working system's real implementation (source defaults, config, outbound JSON) and the
  discrepancy IS the bug.
- Don't invent a bespoke fix for a "model problem." Newer high-end models all work; find
  the working, tested solution and apply it. **Copy the working solution, don't guess.**
- If a fix has been tried 2-3 times and each one reveals a new problem elsewhere, stop —
  this is an architectural problem, not a debugging problem. Fix the architecture, don't
  add a 4th patch. (See `llm-code-slops` for the "over-engineering" mirror of this.)

## The four phases

Complete each phase before moving to the next.

### Phase 1 — Root cause investigation

1. **Read the error message completely.** Stack traces, line numbers, error codes. Don't
   skip past them — they often contain the exact fix.
2. **Build a tight feedback loop.** A command that reproduces the user's exact symptom
   and goes green only when the bug is fixed. Use `bash` for this:
   ```bash
   python3 -m unittest tests.test_<name>          # or the failing script
   python3 scripts/repro_bug.py                   # or a minimal repro you write with `write`
   for i in $(seq 1 50); do python3 tests/x.py -q || break; done   # flaky bugs: raise the rate
   ```
   If it's a harness failure, `inspect_run(run_id=...)` first to read the exact error and
   the step/task that failed before retrying.
3. **Check recent changes.** `git log --oneline -10` and `git diff` via `bash` — what
   changed that could cause this?
4. **Gather evidence at component boundaries.** Mneme is a proxy over three swappable
   parts (provider, database, UI) plus the harness. For a multi-component bug, log what
   enters and exits each boundary before concluding where it breaks. A request that
   returns 200 proves nothing about whether the right thing happened (see
   `verification-before-completion`).
5. **Trace data flow to the source.** Where does the bad value originate? Fix at the
   source, not at the symptom. Use `read_file` on the relevant source and `bash` `grep`
   to trace callers.

**Stop here until you can state WHY it happens.** If you don't understand it, say "I
don't understand X" and gather more data — don't guess.

### Phase 2 — Pattern analysis

- Find similar code that WORKS in the same repo and diff against it.
- If implementing a known pattern, read the reference completely (don't skim).
- List every difference between working and broken, however small. Don't assume "that
  can't matter."

### Phase 3 — Hypothesis and test

- Form 3-5 ranked, falsifiable hypotheses. State each one's prediction: "if X is the
  cause, changing Y should make Z happen."
- Test the top hypothesis with the smallest probe, one variable at a time.
- Verified? → Phase 4. Not verified? → new hypothesis, don't stack fixes.

### Phase 4 — Implement the root-cause fix

1. Write a failing regression test first (see `test-driven-development`).
2. Make ONE fix at the root cause. No "while I'm here" changes.
3. Verify: run the regression test, then the full suite.
4. If it doesn't work: < 3 attempts → return to Phase 1 with new info; ≥ 3 → question
   the architecture, discuss before a 4th fix.

## Red flags — stop and return to Phase 1

- "Quick fix for now, investigate later"
- "Just try changing X and see"
- "It's probably X, let me fix that"
- Proposing a fix before you can state the root cause
- "One more fix attempt" (when you've already tried 2+)
- "The model is the problem" (see the Mneme-first rule — it almost never is)

## Pitfalls specific to Mneme

- **Proxied-200 illusion.** A dead port can still return 200 because a gateway forwards
  to a healthy peer. The only trustworthy signal is the source of truth field (e.g.
  `instances[].running` in `/system/status`), never `curl -w %{http_code}` on a port.
- **A setting that's wired but inert.** The config reads it, the logs show it, and it
  does nothing. Find these by checking the DEEPEST layer — what the backend actually
  received (sampler log, outbound JSON, DB row count), not the code that forwards it.
  See `llm-code-slops` for the full catalog.
- **`2>&1 | tail` masks exit codes.** A pipeline's status is the last command's. Use
  `set -o pipefail` or drop the pipe when you need the real status.
- **`pkill -f <pattern>` kills the SSH session itself** (the killer's own argv matches).
  Use the `[x]` bracket trick: `pkill -f '[p]attern'`.
- **A `/tmp` script shadowing a stdlib module** (`/tmp/json.py`, `/tmp/io.py`) produces a
  baffling "circular import" in code you never touched. Name scratch scripts distinctively
  and clean up stale ones.

## Verification checklist

1. Root cause is stated in one sentence and evidence supports it.
2. The repro loop goes red on the symptom and green on the fix.
3. The fix is at the root cause, not the symptom; one change, not several.
4. A regression test exists and failed before the fix.
5. The deepest layer was checked, not just the 200/exit-0.
