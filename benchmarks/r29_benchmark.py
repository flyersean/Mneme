"""R29 — repeatable live-model benchmark: does a REAL model complete real tasks?

The harness machinery is unit-tested with scripted models, but that proves the
machinery works, not that a real small model can DRIVE it. This benchmark runs a
set of small, deterministic coding tasks through the harness with a real model
and reports, per task: did it complete, did the deterministic verify pass, and
how many turns/tool-calls it took. That turns "the model stops early" from an
anecdote into a number, and gives a regression signal for every mission/prompt/
nudge change.

Run it against a real model by wiring the proxy's process_chat into the runner:

    from mneme_proxy import process_chat
    from benchmarks.r29_benchmark import TASKS, run_benchmark
    results = run_benchmark(process_chat)

Each task is self-contained: it names a goal, a deterministic VERIFY check (the
same file_exists/file_contains/output_contains primitives the harness already
uses), and a pass criterion. A task "passes" only if the run COMPLETED and every
verify check passed — "I wrote the file" counts only when the file is actually
there.
"""
from __future__ import annotations

import json
import os
import tempfile

# ─── Task set ──────────────────────────────────────────────────────────
# Each task: (id, goal, tasks, pass_criteria). `tasks` uses the harness's
# {"title", "verify": [...]} shape; `verify` entries are the deterministic
# primitives from mneme/harness/verify.py (command / file_exists / file_contains /
# output_contains). `pass_criteria` is a human string for the report.

TASKS = [
    {
        "id": "write_file",
        "goal": "Create a file named hello.txt in your tools directory containing exactly the text 'hello world'.",
        "tasks": [{
            "title": "write hello.txt",
            "verify": [
                {"type": "file_exists", "path": "hello.txt"},
                {"type": "file_contains", "path": "hello.txt", "text": "hello world"},
            ],
        }],
        "pass_criteria": "hello.txt exists and contains 'hello world'",
    },
    {
        "id": "fix_bug",
        "goal": "Fix the bug: the function in bug.py should return 2, but it returns 3.",
        "setup": {
            "bug.py": "def broken():\n    return 3\n",
            "test_bug.py": "from bug import broken\nassert broken() == 2, f'got {broken()}'\n",
        },
        "tasks": [{
            "title": "fix bug.py",
            "verify": [
                {"type": "command", "command": "python3 test_bug.py", "expect_exit": 0},
            ],
        }],
        "pass_criteria": "test_bug.py exits 0 (broken() returns 2)",
    },
    {
        "id": "add_function",
        "goal": "Add a function `add(a, b)` to math_util.py that returns a + b.",
        "setup": {
            "math_util.py": "def sub(a, b):\n    return a - b\n",
            "test_math.py": "from math_util import add\nassert add(2, 3) == 5\n",
        },
        "tasks": [{
            "title": "add add() to math_util.py",
            "verify": [
                {"type": "command", "command": "python3 test_math.py", "expect_exit": 0},
            ],
        }],
        "pass_criteria": "test_math.py exits 0 (add(2,3)==5)",
    },
    {
        "id": "extract_fact",
        "goal": "Determine the value of PORT in config.json and write it to a file answer.txt.",
        "setup": {
            "config.json": '{"PORT": 8081, "HOST": "localhost"}\n',
        },
        "tasks": [{
            "title": "extract PORT to answer.txt",
            "verify": [
                {"type": "file_exists", "path": "answer.txt"},
                {"type": "file_contains", "path": "answer.txt", "text": "8081"},
            ],
        }],
        "pass_criteria": "answer.txt exists and contains '8081'",
    },
    {
        "id": "refactor_rename",
        "goal": "Rename the function old_name to new_name in rename.py and update the caller in main.py.",
        "setup": {
            "rename.py": "def old_name():\n    return 'ok'\n",
            "main.py": "from rename import old_name\nprint(old_name())\n",
            "test_rename.py": "from rename import new_name\nassert new_name() == 'ok'\n",
        },
        "tasks": [{
            "title": "rename old_name -> new_name",
            "verify": [
                {"type": "command", "command": "python3 test_rename.py", "expect_exit": 0},
            ],
        }],
        "pass_criteria": "test_rename.py exits 0 (new_name exists and returns 'ok')",
    },
]


def _setup_task(workdir: str, task: dict) -> str:
    """Write the task's setup files into a temp workdir. Returns the workdir."""
    for fname, content in (task.get("setup") or {}).items():
        with open(os.path.join(workdir, fname), "w", encoding="utf-8") as f:
            f.write(content)
    return workdir


def run_benchmark(process_chat, *, budget: dict = None, log=None) -> list:
    """Run every task through the harness with the given process_chat (real model
    or scripted). Returns a list of result dicts:
      {id, status, verify_pass, turns, tool_calls, error}
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
    from mneme.harness import Ledger, RunEngine
    from mneme.harness.chat_executor import make_chat_executor

    log = log or (lambda m: None)
    results = []
    for task in TASKS:
        with tempfile.TemporaryDirectory() as td:
            _setup_task(td, task)
            ledger = Ledger(os.path.join(td, "harness.db"))
            executor = make_chat_executor(process_chat)
            eng = RunEngine(ledger, executor, runs_root=os.path.join(td, "runs"), log=log)
            rid = eng.create(task["goal"], task["tasks"], budget=budget or {})["run_id"]
            try:
                out = eng.execute(rid)
            except Exception as e:
                results.append({"id": task["id"], "status": "error", "verify_pass": False,
                                "turns": 0, "tool_calls": 0, "error": f"{type(e).__name__}: {e}"})
                continue
            # verify_pass = completed AND every task's verify passed (status reflects it)
            verify_pass = out.get("status") == "completed"
            results.append({
                "id": task["id"],
                "status": out.get("status"),
                "verify_pass": verify_pass,
                "turns": (out.get("data") or {}).get("turns", 0),
                "tool_calls": (out.get("usage") or {}).get("tool_calls", 0),
                "error": out.get("error", ""),
            })
            log(f"[BENCH] {task['id']}: status={out.get('status')} pass={verify_pass} "
                f"turns={results[-1]['turns']} calls={results[-1]['tool_calls']}")
    return results


def report(results: list) -> str:
    """One-line-per-task + a summary, for stdout or a log."""
    lines = []
    n_pass = sum(1 for r in results if r["verify_pass"])
    for r in results:
        mark = "PASS" if r["verify_pass"] else "FAIL"
        lines.append(f"  {mark}  {r['id']:<14} status={r['status']:<10} "
                     f"turns={r['turns']} calls={r['tool_calls']} {r['error']}")
    lines.append(f"--- {n_pass}/{len(results)} passed ---")
    return "\n".join(lines)


if __name__ == "__main__":
    # Default: run with a SCRIPTED process_chat (proves the runner works; use a
    # real model by wiring the proxy's process_chat in instead).
    def scripted_chat(messages, session_id="default", tools=None, **kw):
        # A scripted model that always "succeeds" by writing/echoing — NOT a real
        # capability test. Wire a real process_chat for the actual benchmark.
        return {"content": "done", "tool_calls": [], "_grade": "B", "done_reason": "stop"}

    print("R29 benchmark (scripted model — wire a real process_chat for the live test):")
    print(report(run_benchmark(scripted_chat)))
