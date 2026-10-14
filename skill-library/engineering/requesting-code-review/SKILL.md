---
name: requesting-code-review
description: Use when completing tasks, implementing major features, or before merging to verify work meets requirements — launch a background review run to catch issues before they cascade
tags: [code-review, review, quality, pre-merge, verification, workflow, git]
requires: [receiving-code-review]
---

Adapted from obra/superpowers — MIT License, Copyright (c) 2025 Jesse Vincent.

# Requesting Code Review

Launch a code-reviewer background run to catch issues before they cascade. The reviewer gets precisely crafted context for evaluation — never your session's history.

**Core principle:** Review early, review often.

## When to Request Review

**Mandatory:**
- After each task in run-driven development
- After completing a major feature
- Before merge to main

**Optional but valuable:**
- When stuck (fresh perspective)
- Before refactoring (baseline check)
- After fixing a complex bug

## How to Request

**1. Get git SHAs:**

```bash
BASE_SHA=$(git rev-parse HEAD~1)  # or: git merge-base origin/main HEAD
HEAD_SHA=$(git rev-parse HEAD)
```

**2. Read the reviewer template:**

```python
read_file("skills/requesting-code-review/code-reviewer.md")
```

Fill its placeholders:
- `{DESCRIPTION}` — brief summary of what you built
- `{PLAN_OR_REQUIREMENTS}` — what it should do
- `{BASE_SHA}` — starting commit
- `{HEAD_SHA}` — ending commit

**3. Launch the reviewer as a background run:**

```python
start_run(goal=<filled template>, free_form=True)
```

**4. Check the verdict:**

```python
inspect_run(run_id)
```

**5. Act on feedback:**
- Fix Critical issues immediately
- Fix Important issues before proceeding
- Note Minor issues for later
- Push back if the reviewer is wrong (with reasoning) — see `receiving-code-review`

## Example

```
[Just completed Task 2: Add verification function]

You: Let me request code review before proceeding.

BASE_SHA=$(git log --oneline | grep "Task 1" | head -1 | cut -d' ' -f1)
HEAD_SHA=$(git rev-parse HEAD)

start_run(goal=<template filled with:
  DESCRIPTION: Added verifyIndex() and repairIndex() with 4 issue types
  PLAN_OR_REQUIREMENTS: Task 2 from docs/plans/deployment-plan.md
  BASE_SHA: a7981ec
  HEAD_SHA: 3df7661>)

[Run returns]:
  Strengths: Clean architecture, real tests
  Issues:
    Important: Missing progress indicators
    Minor: Magic number (100) for reporting interval
  Assessment: Ready to proceed

You: [Fix progress indicators]
[Continue to Task 3]
```

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "I'll just review the diff myself instead of launching a reviewer" | You're the coordinator — reviewing the diff inline burns the context window you need to keep driving the work. Launch a reviewer run: the diff and the evaluation live in its context, and only the findings come back to you. |
| "The reviewer needs my whole session history to understand the change" | Hand it precisely crafted context, never your session's history. That keeps the reviewer on the work product, not your thought process. |

## Pitfalls / Red Flags

**Never:**
- Skip review because "it's simple"
- Ignore Critical issues
- Proceed with unfixed Important issues
- Argue with valid technical feedback

**If the reviewer is wrong:**
- Push back with technical reasoning
- Show code/tests that prove it works
- Request clarification

## Verification

A review request is complete when:
1. `start_run` returned a `run_id` for the reviewer run.
2. `inspect_run` shows the reviewer produced Strengths + Issues (Critical/Important/Minor) + Assessment.
3. Every Critical and Important issue is either fixed and re-verified, or explicitly pushed back with technical reasoning recorded in the run thread.
