#!/usr/bin/env python3
"""
Live runner for the Hidden Capability Tasks benchmark.

For each environment × trial:
  1. POST /reset — wipe memory so no answer leaks from a warm DB.
  2. write the env's data files to a scratch dir the proxy's bash can reach (~).
  3. run task1, then task2, through the live proxy.
  4. classify + score.

Then print per-environment SUMMARY stats across trials (success rate, mean
failures, mean wall time, persistent-capability-gain rate), plus per-trial
detail. This is the BASELINE for the current loop — run it before changing what
Mneme saves.

Usage:
  python3 tests/run_capability_benchmark.py --base http://localhost:8082 \
      --envs records_avg,binary_avg --trials 3
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capability_harness import (  # noqa: E402
    ALL_ENVIRONMENTS, run_task, score,
)

SCRATCH = os.path.expanduser("~/mneme_hct")


def _reset(base_url: str) -> None:
    import urllib.request
    req = urllib.request.Request(base_url.rstrip("/") + "/reset", data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def _materialize(env, trial: int):
    d = os.path.join(SCRATCH, f"{env.id}_t{trial}", "data")
    os.makedirs(d, exist_ok=True)
    for name, content in env.files.items():
        mode = "wb" if isinstance(content, (bytes, bytearray)) else "w"
        with open(os.path.join(d, name), mode) as f:
            f.write(content)
    def _abs(task):
        return task.replace("data/", d + "/")
    return _abs(env.task1), _abs(env.task2)


def _fmt(b: bool) -> str:
    return "yes" if b else " no"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8082")
    ap.add_argument("--envs", default="", help="comma list of env ids (default: all)")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--verbose", action="store_true", help="print per-trial detail")
    args = ap.parse_args()

    envs = [mk() for mk in ALL_ENVIRONMENTS]
    if args.envs:
        want = set(args.envs.split(","))
        envs = [e for e in envs if e.id in want]

    print(f"== Hidden Capability Tasks baseline — {len(envs)} env x {args.trials} trials "
          f"(backend qwen/qwen3.6-35b-a3b) ==\n")

    for env in envs:
        rows = []
        for t in range(args.trials):
            _reset(args.base)
            task1, task2 = _materialize(env, t)
            rr1 = run_task(args.base, task1)
            rr2 = run_task(args.base, task2)
            s = score(env, rr1, rr2)
            rows.append(s)
            if args.verbose:
                print(f"  [{env.id} t{t}] t1={_fmt(s['task1_solved'])}/{s['task1_class']} "
                      f"t2={_fmt(s['task2_solved'])}/{s['task2_class']} "
                      f"f1={s['task1_failures']} f2={s['task2_failures']} "
                      f"reuse={_fmt(s['reused_tool'])} method={_fmt(s['method_transferred'])} "
                      f"gain={_fmt(s['persistent_capability_gain'])} "
                      f"waste={_fmt(s['unnecessary_work_1'])}/{_fmt(s['unnecessary_work_2'])} "
                      f"{s['task1_elapsed']}s/{s['task2_elapsed']}s", flush=True)
                print(f"       t1tools={s['task1_tools']}  t2tools={s['task2_tools']}")

        n = len(rows)
        t1_ok = sum(1 for r in rows if r["task1_solved"])
        t2_ok = sum(1 for r in rows if r["task2_solved"])
        gain = sum(1 for r in rows if r["persistent_capability_gain"])
        reuse = sum(1 for r in rows if r["reused_tool"])
        method = sum(1 for r in rows if r["method_transferred"])
        waste = sum(1 for r in rows if r["unnecessary_work_1"] or r["unnecessary_work_2"])
        f1 = sum(r["task1_failures"] for r in rows) / n
        f2 = sum(r["task2_failures"] for r in rows) / n
        t1s = sum(r["task1_elapsed"] for r in rows) / n
        t2s = sum(r["task2_elapsed"] for r in rows) / n

        print(f"{env.id:<16} (capability: {env.capability}, discoverable={env.discoverable})")
        print(f"  task1 solved   {t1_ok}/{n}   mean failures {f1:.1f}   mean {t1s:.1f}s")
        print(f"  task2 solved   {t2_ok}/{n}   mean failures {f2:.1f}   mean {t2s:.1f}s")
        print(f"  reused tool    {reuse}/{n}   method-transferred {method}/{n}")
        print(f"  unnecessary-work {waste}/{n}")
        print(f"  >>> persistent-capability-gain {gain}/{n}   <<<")
        print()

    print("(baseline: this is the current loop, unchanged. persistent-capability-gain")
    print(" is the headline — does a related task benefit from the previous acquisition?)")


if __name__ == "__main__":
    main()
