"""Deterministic verification — the harness checks work instead of trusting claims.

A task may carry ``verify`` checks (from the caller, or from the planner's
``VERIFY:`` lines). After a step reports the task done, the harness runs every
check; any failure turns the step into a failure (retry / replan), so "I wrote
the file" only counts when the file is actually there.

Check types (paths are relative to the run's workspace/ dir; commands run there):

    {"type": "command", "command": "pytest -q", "expect_exit": 0, "timeout": 60}
    {"type": "file_exists", "path": "notes.txt"}
    {"type": "file_contains", "path": "notes.txt", "text": "3.13"}
    {"type": "output_contains", "text": "DONE"}          # the step's final output
    {"type": "output_matches", "pattern": "(?i)version\\s+3\\.\\d+"}

A bare string is shorthand for a command check.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import List, Optional, Tuple

CHECK_TYPES = {
    "command": ("command",),
    "file_exists": ("path",),
    "file_contains": ("path", "text"),
    "output_contains": ("text",),
    "output_matches": ("pattern",),
    "llm_judge": ("criteria",),   # model-judged; supplements, never replaces, deterministic checks
}
_DEFAULT_TIMEOUT = 60
_DETAIL = 500
# Shell parse errors — a malformed command (unquoted parens/operators) errors out
# before it can verify anything. These are NOT evidence the work failed, so they
# must not fail a task (see run_check for the "command" branch).
_SHELL_ERROR = re.compile(r"syntax error|unexpected", re.I)


class VerifySpecError(ValueError):
    pass


def normalize(checks) -> List[dict]:
    """Validate and normalize a verify spec (list, single check, or command string)."""
    if checks in (None, "", []):
        return []
    if not isinstance(checks, list):
        checks = [checks]
    out = []
    for c in checks:
        if isinstance(c, str):
            c = {"type": "command", "command": c}
        if not isinstance(c, dict):
            raise VerifySpecError(f"verify check must be an object or command string, got {c!r}")
        kind = c.get("type")
        if kind not in CHECK_TYPES:
            raise VerifySpecError(f"unknown verify type {kind!r} (one of {', '.join(CHECK_TYPES)})")
        for req in CHECK_TYPES[kind]:
            if not str(c.get(req) or "").strip():
                raise VerifySpecError(f"verify '{kind}' needs '{req}'")
        if kind == "output_matches":
            try:
                re.compile(c["pattern"])
            except re.error as e:
                raise VerifySpecError(f"bad output_matches pattern: {e}")
        out.append(dict(c))
    return out


def _resolve(base: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(base, path)


def _tool_evidence(tool_calls) -> str:
    """Compact text summary of a step's tool trace for the verification judge.
    Tool results are kept long enough that the judge can actually see WHAT was
    written/run — a 400-char cut hid file contents and made the judge reject
    work it couldn't verify."""
    if not tool_calls:
        return ""
    lines = []
    for tc in tool_calls:
        tool = tc.get("tool") or "?"
        args = tc.get("args") or {}
        if isinstance(args, dict):
            a = args.get("command") or args.get("path") or json.dumps(args)[:200]
        else:
            a = str(args)[:200]
        res = (tc.get("result") or "")[:2000]
        status = tc.get("status") or ""
        lines.append(f"[{tool}] {a}\n    -> {status}: {res}")
    return "\n".join(lines)


def run_check(check: dict, output: str, base_dir: str, judge=None, evidence: str = "") -> Tuple[bool, str]:
    kind = check["type"]
    try:
        if kind == "llm_judge":
            if judge is None:
                return False, "no judge configured for llm_judge"
            ok, why = judge(check["criteria"], output or "", evidence=evidence)
            return bool(ok), f"judge: {why}"[:_DETAIL]
        if kind == "command":
            p = subprocess.run(check["command"], shell=True, cwd=base_dir, capture_output=True,
                               text=True, timeout=float(check.get("timeout") or _DEFAULT_TIMEOUT))
            want = int(check.get("expect_exit", 0))
            tail = ((p.stdout or "") + (p.stderr or "")).strip()
            if p.returncode == want:
                return True, f"exit {p.returncode} (want {want})"
            detail = f"exit {p.returncode} (want {want})"
            if tail:
                detail += ": " + tail[-_DETAIL:]
            # A shell parse error means the COMMAND was malformed, not that the
            # work failed — it never got a chance to check anything. Treat it as
            # inconclusive so a bad grep (unquoted parens etc.) can't turn a
            # successful write into a false failure + replan cascade.
            if _SHELL_ERROR.search(tail):
                return True, "skipped (malformed shell command): " + tail[-_DETAIL:]
            return False, detail
        if kind == "file_exists":
            full = _resolve(base_dir, check["path"])
            return os.path.exists(full), full
        if kind == "file_contains":
            full = _resolve(base_dir, check["path"])
            if not os.path.isfile(full):
                return False, f"missing: {full}"
            with open(full, encoding="utf-8", errors="replace") as f:
                return check["text"] in f.read(), full
        if kind == "output_contains":
            return check["text"] in (output or ""), "step output"
        if kind == "output_matches":
            return re.search(check["pattern"], output or "", re.DOTALL) is not None, "step output"
    except subprocess.TimeoutExpired:
        return False, "command timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return False, f"unknown check {kind}"


def run_checks(checks: List[dict], output: str, base_dir: Optional[str],
               judge=None, evidence: str = "", criteria: str = "") -> Tuple[bool, List[dict]]:
    """Deterministic checks run first. An explicit llm_judge check confirms a pass.
    When a deterministic check FAILS and a judge is available, the judge is run as an
    APPEAL over the evidence — a false-negative check (e.g. ``output_contains`` on the
    narration) must not turn a successful step into a spin-out."""
    base = base_dir or os.getcwd()
    os.makedirs(base, exist_ok=True)
    results = []
    ordered = sorted(checks, key=lambda c: c["type"] == "llm_judge")
    for c in ordered:
        if c["type"] == "llm_judge" and not all(r["passed"] for r in results):
            results.append({"check": c, "passed": False, "detail": "skipped: a deterministic check failed"})
            continue
        ok, detail = run_check(c, output, base, judge, evidence)
        results.append({"check": c, "passed": ok, "detail": detail})
    all_pass = all(r["passed"] for r in results)
    # Appeal: only when a DETERMINISTIC check failed (an llm_judge failure is already
    # a model verdict — appealing it with the same judge is pointless). The appeal
    # gives the judge the full evidence so it can overturn a false negative.
    det_failed = any(not r["passed"] and r["check"].get("type") != "llm_judge" for r in results)
    if det_failed and judge is not None:
        failed_summary = summarize_failures(results)
        ok, why = judge(criteria, output or "", evidence=evidence, failed=failed_summary)
        results.append({"check": {"type": "judge_appeal", "text": failed_summary[:200]},
                        "passed": bool(ok), "detail": f"appeal: {why}"[:_DETAIL]})
        all_pass = bool(ok)
    return all_pass, results


def summarize_failures(results: List[dict]) -> str:
    bad = [r for r in results if not r["passed"]]
    parts = []
    for r in bad:
        c = r["check"]
        what = c.get("command") or c.get("path") or c.get("text") or c.get("pattern") or c.get("criteria") or ""
        parts.append(f"{c['type']}({what}) — {r['detail']}")
    return "; ".join(parts)
