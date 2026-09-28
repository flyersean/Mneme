"""Harness metrics — the feedback signal for self-improvement (collected, not optimized).

Everything is derived from the ledger (runs, steps, tool calls, events) plus skill
stats and evolution proposals, so it is always consistent with "what happened".
"""

from __future__ import annotations

from collections import Counter
from typing import Optional


def _avg(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 2) if xs else None


def compute(ledger, skills=None, evolution=None, limit: int = 2000) -> dict:
    runs = ledger.list_runs(limit=limit)
    by_status = Counter(r["status"] for r in runs)
    done = [r for r in runs if r["status"] == "completed"]
    finished = [r for r in runs if r["status"] in ("completed", "failed")]
    u = lambda r, k: (r.get("usage") or {}).get(k, 0) or 0
    categories, verif = Counter(), Counter()
    recovered = 0
    tool_total, tool_status = Counter(), Counter()
    for r in runs:
        evs = ledger.events(r["run_id"], types=["task_failed", "verification_passed", "verification_failed"])
        had_fail = False
        for e in evs:
            if e["type"] == "task_failed":
                categories[(e["data"] or {}).get("category", "other")] += 1
                had_fail = True
            else:
                verif[e["type"]] += 1
        if had_fail and r["status"] == "completed":
            recovered += 1
        for c in ledger.list_tool_calls(r["run_id"]):
            tool_total[c["tool"]] += 1
            if c["status"] == "success":
                tool_status[c["tool"]] += 1
    out = {
        "runs": {"total": len(runs), **dict(by_status)},
        "task_success_rate": round(len(done) / len(finished), 3) if finished else None,
        "per_completed_run": {
            "steps": _avg(u(r, "steps") for r in done),
            "model_calls": _avg(u(r, "model_calls") for r in done),
            "tool_calls": _avg(u(r, "tool_calls") for r in done),
            "replans": _avg(u(r, "replans") for r in done),
            "runtime_s": _avg(u(r, "runtime_s") for r in done),
        },
        "failure_categories": dict(categories),
        "verification": {"passed": verif["verification_passed"], "failed": verif["verification_failed"]},
        "recovered_runs": recovered,
        "tools": {t: {"calls": n, "success_rate": round(tool_status[t] / n, 3)} for t, n in tool_total.most_common()},
    }
    if skills is not None:
        out["skills"] = {s["name"]: {"version": s["version"], "uses": s["uses"],
                                     "success_rate": round(s["successes"] / s["uses"], 3) if s["uses"] else None}
                         for s in skills.list()}
    if evolution is not None:
        props = evolution.list(limit=10_000)
        out["self_improvement"] = {"proposals": len(props), **dict(Counter(p["status"] for p in props))}
    return out
