# Hidden Capability Tasks — Baseline

Measured the current self-improvement loop (unchanged) against the capability-gap
benchmark. Backend `qwen/qwen3.6-35b-a3b` via OpenRouter. 4 environments × 3
trials, fresh DB per trial (`POST /reset`).

Run: `python3 tests/run_capability_benchmark.py --trials 3`

## Headline

| metric | value |
|---|---|
| task1 solved | 11/12 (92%) |
| task2 solved | 12/12 (100%) |
| **persistent-capability-gain** | **1/12 (8%)** |
| reused a saved tool (list_tools/read_tool) | 0/12 (0%) |
| unnecessary tool creation | 0/12 (0%) |

The loop **solves the tasks** but **does not acquire persistent capability**:
`persistent-capability-gain ≈ 0`, and the single "gain" is a weak method-transfer
signal, not a real tool reuse (see caveat below).

## Per-environment

| env | discoverable | task1 | task2 | t1 time | t2 time | reused | gain |
|---|---|---|---|---|---|---|---|
| records_avg (custom delimiter) | yes | 2/3 | 3/3 | 14.4s | 109.0s | 0/3 | 0/3 |
| binary_avg (binary struct) | no | 3/3 | 3/3 | 16.5s | 142.4s | 0/3 | 0/3 |
| timestamps_norm (dates) | yes | 3/3 | 3/3 | 74.0s | 6.3s | 0/3 | 0/3 |
| png_dims (image metadata) | yes | 3/3 | 3/3 | 36.7s | 8.4s | 0/3 | 1/3 |

## What the numbers mean

1. **No unnecessary tool building.** `unnecessary-work = 0/12`. The model does not
   compulsively write tools; it solves with `bash`/`read_file` inline. The
   "don't make building the goal" negative case holds.

2. **Ad-hoc success is not persisted.** Task 1 is solved fast (binary 16.5s mean)
   via an inline `bash` one-liner (`python3 -c "import struct; …"`). Because that
   is (a) zero failures → no recovery trigger, (b) `bash` not `write` → no saved
   tool, and (c) not a novel-procedure → nothing is written down. So task 2, which
   needs the same capability, re-derives it from scratch — binary task 2 mean
   142.4s vs 16.5s (≈8.6× slower) — and sometimes grinds (214s, 189.8s runs,
   `search_memory` calls that find an empty DB).

3. **`reused_tool = 0/12` is the clean signal.** The model never consults
   `list_tools`/`read_tool`, because nothing was ever saved to reuse. Persistence
   is the missing link, not reuse.

## Caveats (so we don't over-read)

- `method_transferred` = `task2.failures < task1.failures` is a weak proxy,
  confounded by task difficulty — task 2 is often simply easier (timestamps 74s →
  6.3s, png 36.7s → 8.4s). The single png "gain" is this noise, not real transfer.
  The trustworthy reuse signal is `reused_tool` (0/12).
- High variance: single trials swing 5s–215s. 3 trials is the floor; more would be
  better for a stable number.
- This is the loop BEFORE any persistence change. Re-run after the fix to diff.

## The actionable conclusion

To move `persistent-capability-gain` off zero, the loop must **persist a clean
ad-hoc success** — promote "this inline bash parser is a reusable capability" into
a saved tool, or save a strategy on "exercised a new capability" (not only on
"recovered from ≥2 failures"). That is a design change, not a test fix.
