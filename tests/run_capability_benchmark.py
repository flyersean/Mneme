#!/usr/bin/env python3
"""
Live runner for the Hidden Capability Tasks benchmark.

For each environment × trial:
  1. POST /reset — wipe memory so no answer leaks from a warm DB.
  2. write the env's data files to a scratch dir the proxy's bash can reach (~).
  3. run task1, then task2, through the live proxy.
  4. classify + score, and print a summary line.

Usage:
  python3 tests/run_capability_benchmark.py --base http://localhost:8082 \
      --envs records_avg,binary_avg --trials 1

The outcome-first metrics (solved / class / failures / method-transferred /
persistent-capability-gain / unnecessary-work) come from capability_harness.score.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capability_harness import (  # noqa: E402
    ALL_ENVIRONMENTS, run_task, score, check_oracle,
)

SCRATCH = os.path.expanduser("~/mneme_hct")


def _reset(base_url: str) -> None:
    import urllib.request
    req = urllib.request.Request(base_url.rstrip("/") + "/reset", data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def _materialize(env, trial: int) -> None:
    """Write env data files to a scratch dir the proxy's bash (cwd=~) can reach."""
    d = os.path.join(SCRATCH, f"{env.id}_t{trial}", "data")
    os.makedirs(d, exist_ok=True)
    for name, content in env.files.items():
        mode = "wb" if isinstance(content, (bytes, bytearray)) else "w"
        with open(os.path.join(d, name), mode) as f:
            f.write(content)
    # the task text says "data/<file>"; rewrite to the absolute scratch path
    def _abs(task):
        return task.replace("data/", d + "/")
    return _abs(env.task1), _abs(env.task2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8082")
    ap.add_argument("--envs", default="", help="comma list of env ids (default: all)")
    ap.add_argument("--trials", type=int, default=1)
    args = ap.parse_args()

    envs = [mk() for mk in ALL_ENVIRONMENTS]
    if args.envs:
        want = set(args.envs.split(","))
        envs = [e for e in envs if e.id in want]

    print(f"{'env':<16} {'t':>2} {'t1':<6} {'c1':>2} {'t2':<6} {'c2':>2} "
          f"{'f1':>3} {'f2':>3} {'reuse':>5} {'method':>6} {'gain':>4} {'waste':>5}  t1s t2s")
    for env in envs:
        for t in range(args.trials):
            _reset(args.base)
            task1, task2 = _materialize(env, t)
            rr1 = run_task(args.base, task1)
            rr2 = run_task(args.base, task2)
            s = score(env, rr1, rr2)
            print(f"{env.id:<16} {t:>2} "
                  f"{str(s['task1_solved'])[:5]:<6} {s['task1_class']:<2} "
                  f"{str(s['task2_solved'])[:5]:<6} {s['task2_class']:<2} "
                  f"{s['task1_failures']:>3} {s['task2_failures']:>3} "
                  f"{str(s['reused_tool'])[:5]:>5} {str(s['method_transferred'])[:5]:>6} "
                  f"{str(s['persistent_capability_gain'])[:5]:>4} "
                  f"{str(s['unnecessary_work_1'])[:5]:>5}  "
                  f"{s['task1_elapsed']}s {s['task2_elapsed']}s", flush=True)
            if args.trials == 1 or t == 0:
                print(f"    t1 tools: {s['task1_tools']}")
                print(f"    t2 tools: {s['task2_tools']}")
                print(f"    t1 answer: {rr1.answer.strip()[:120]!r}")
                print(f"    t2 answer: {rr2.answer.strip()[:120]!r}")


if __name__ == "__main__":
    main()
