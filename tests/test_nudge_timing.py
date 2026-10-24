"""Unit tests for the wrap-up nudge timing fix (bug b).

Pure function in proxy/mneme/overcome.py:
    wrap_up_nudge_due(round_log, tool_rounds, threshold)

round_log entries (one per tool round, in order):
    {"calls": N,   # tool calls executed that round
     "new": N,     # first-time (non-repeated) calls that round
     "ok": bool}   # no errored result, no blocked/out-of-scope result

Rules under test:
  - fires when tool_rounds >= threshold and the last round was NOT ok.
  - fires when grinding: recent rounds produce no NEW calls (no convergence).
  - is SUPPRESSED when the last round succeeded AND each of the last 2 rounds
    produced new information (still converging) even past the threshold.
  - suppression is not permanent: the same threshold crossing can fire later
    once convergence stalls.
Run: python3 tests/test_nudge_timing.py
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "proxy"))


def _load_mod():
    spec = importlib.util.spec_from_file_location(
        "mneme_overcome_test", os.path.join(ROOT, "proxy/mneme/overcome.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ok_round(calls=3, new=3):
    return {"calls": calls, "new": new, "ok": True}


def fail_round(calls=3, new=1):
    return {"calls": calls, "new": new, "ok": False}


def grind_round(calls=3, new=0):
    return {"calls": calls, "new": 0, "ok": True}


def main():
    m = _load_mod()
    T = 8
    f = m.wrap_up_nudge_due

    # 1. converging work past the threshold: last 2 rounds ok + new info -> SUPPRESSED
    log = [ok_round(4, 4), ok_round(4, 4), ok_round(2, 2)]
    assert f(log, 10, T) is False, "nudge fired while model was still converging (bug b)"

    # 2. last round errored or blocked -> fires
    log = [ok_round(4, 4), ok_round(4, 4), fail_round(3, 2)]
    assert f(log, 11, T) is True

    # 3. grinding: recent rounds have NO new information -> fires
    log = [ok_round(4, 4), grind_round(), grind_round()]
    assert f(log, 10, T) is True, "grinding (no new calls for 2 rounds) must still be nudged"

    # 4. one new-information round then a stall round -> fires
    log = [ok_round(4, 4), ok_round(3, 3), grind_round()]
    assert f(log, 11, T) is True

    # 5. below threshold -> never fires
    log = [ok_round(4, 4), ok_round(3, 2)]
    assert f(log, 6, T) is False

    # 6. ok final round but the round BEFORE it produced nothing new -> fires
    log = [grind_round(), grind_round(), ok_round(3, 3)]
    assert f(log, 9, T) is True

    # 7. suppression is not permanent: good rounds suppress, then a stall fires
    log = [ok_round(4, 4), ok_round(4, 4)]
    assert f(log, 10, T) is False
    log.append(grind_round())
    assert f(log, 11, T) is True

    # 8. only ONE recorded round: cannot show 2 rounds of new info -> fires
    assert f([ok_round(9, 9)], 9, T) is True

    # 9. blocked/out-of-scope rounds with new paths still fire even though 'new'
    log = [ok_round(4, 4), fail_round(4, 4), fail_round(4, 4)]
    assert f(log, 12, T) is True

    # 10. below threshold exactly
    assert f([ok_round(8, 8)], 7, T) is False

    print("test_nudge_timing: ALL PASS")


if __name__ == "__main__":
    main()
