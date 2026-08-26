"""
Deterministic tests for the Hidden Capability Tasks harness (no live model).

These exercise the CLASSIFICATION and SCORING logic — the oracle, the A..G
trajectory classifier, the method-reuse probe, and the unnecessary-work detector
— with synthetic RunResults, plus the environment generators' oracles against an
independent brute-force recomputation. No network, no Mneme, no model.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from capability_harness import (  # noqa: E402
    RunResult, Environment, check_oracle, classify_single, method_reuse_probe,
    unnecessary_work, score, make_env_records, make_env_binary, make_env_timestamps,
    make_env_png_dims,
)

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


def _rr(answer, tools, results):
    return RunResult(answer=answer, tools=tools, results=results)


# ─── Oracle ──────────────────────────────────────────────────────────────────
@test
def test_check_oracle_numeric():
    assert check_oracle("the average is 5.5", "5.5") is True
    assert check_oracle("avg = 5.5000", "5.5") is True
    assert check_oracle("42 records", "42") is True
    assert check_oracle("the answer is 7", "8") is False
    assert check_oracle("I don't know", "5") is False


@test
def test_check_oracle_leading_field_number():
    # the answer names "field 3" before the actual result — a first-number grab
    # would wrongly read 3 instead of 58.7647.
    assert check_oracle("field 3 average is 58.7647 (999 / 17)", "58.7647") is True
    # and it must NOT accept a number that isn't there at all
    assert check_oracle("field 3 average is 58.7647", "42") is False


@test
def test_check_oracle_substring():
    assert check_oracle("the price is Market Price", "market price") is True
    assert check_oracle("it is Paris", "London") is False
    assert check_oracle("the symbol is Au", "Au") is True


# ─── Trajectory classification A..F ──────────────────────────────────────────
@test
def test_classify_single_direct():
    assert classify_single(_rr("5.5", [], []), "5.5") == "A"


@test
def test_classify_single_existing_tool():
    assert classify_single(_rr("5.5", ["bash"], ["parsed ok"]), "5.5") == "B"


@test
def test_classify_single_discovered():
    assert classify_single(_rr("5.5", ["web_search", "bash"], ["results", "ok"]), "5.5") == "B"


@test
def test_classify_single_gave_up():
    assert classify_single(_rr("I can't do this", ["bash"], ["command not found"]), "5.5") == "C"


@test
def test_classify_single_recognized_gap():
    assert classify_single(_rr("I need a tool to parse this format", ["bash"], ["error"]), "5.5") == "D"


@test
def test_classify_single_built_failed():
    assert classify_single(_rr("couldn't get it working", ["write", "bash"], ["written", "error"]), "5.5") == "E"


@test
def test_classify_single_built_solved():
    assert classify_single(_rr("5.5", ["write", "bash"], ["written", "ok"]), "5.5") == "F"


# ─── Method-reuse probe ──────────────────────────────────────────────────────
@test
def test_method_probe_detects_transfer():
    hard = _rr("5.5", ["bash", "bash", "web_search", "bash"],
               ["command not found", "command not found", "content", "content"])  # 2 failures
    easy = _rr("5.5", ["web_search", "bash"], ["content", "content"])             # 0 failures
    probe = method_reuse_probe(hard, easy)
    assert probe["fewer_failures"] is True
    assert probe["no_failures"] is True
    assert probe["transferred"] is True


@test
def test_method_probe_detects_no_transfer():
    hard = _rr("5.5", ["bash", "bash", "web_search", "bash"],
               ["command not found", "command not found", "content", "content"])
    still_hard = _rr("5.5", ["bash", "bash", "web_search", "bash"],
                     ["command not found", "command not found", "content", "content"])
    probe = method_reuse_probe(hard, still_hard)
    assert probe["transferred"] is False


# ─── Unnecessary-work detector ───────────────────────────────────────────────
@test
def test_unnecessary_work_flags_build_without_need():
    # built a tool (write) but had zero failures — the task was a one-liner.
    rr = _rr("5.5", ["write", "bash"], ["written", "ok"])
    assert unnecessary_work(rr, solved=True) is True


@test
def test_unnecessary_work_does_not_flag_legit_build():
    # built after failures (genuine gap) -> not flagged.
    rr = _rr("5.5", ["bash", "write", "bash"],
             ["command not found", "written", "ok"])
    assert unnecessary_work(rr, solved=True) is False


# ─── Persistent capability gain (headline metric) ────────────────────────────
@test
def test_score_persistent_gain_via_reuse():
    env = Environment(id="x", task1="t1", task2="t2", oracle1="5", oracle2="6",
                      capability="c", discoverable=True)
    rr1 = _rr("5", ["write", "bash"], ["written", "ok"])
    rr2 = _rr("6", ["list_tools", "read_tool", "bash"], ["tool", "tool", "ok"])  # reused
    s = score(env, rr1, rr2)
    assert s["task1_solved"] is True
    assert s["task2_solved"] is True
    assert s["reused_tool"] is True
    assert s["persistent_capability_gain"] is True


# ─── Environment oracles are deterministic AND correct ───────────────────────
@test
def test_records_oracle_correct():
    e = make_env_records(seed=7)
    # independent brute-force recomputation of the @@ format
    vals = []
    for line in e.files["records.dat"].splitlines():
        parts = line.split("@@")
        if len(parts) != 4:
            continue  # corrupted
        code = parts[3]
        if code == "ZX":
            vals.append(int(parts[2]))
    expect = round(sum(vals) / len(vals), 4)
    assert str(expect) == e.oracle1, f"oracle mismatch: {e.oracle1} vs {expect}"
    # deterministic: same seed -> same oracle
    assert make_env_records(seed=7).oracle1 == e.oracle1


@test
def test_binary_oracle_correct():
    import struct
    e = make_env_binary(seed=11)
    blob = e.files["metrics.bin"]
    header_ts, n = struct.unpack("<II", blob[:8])
    recs = struct.unpack("<" + "I" * n, blob[8:8 + 4 * n])
    assert str(round(sum(recs) / n, 4)) == e.oracle1
    assert len(recs) == n


@test
def test_png_is_valid_and_oracle_correct():
    import struct
    from capability_harness import _make_png, make_env_png_dims
    e = make_env_png_dims(seed=5)
    for name, oracle in (("image.png", e.oracle1), ("image2.png", e.oracle2)):
        blob = e.files[name]
        assert blob[:8] == b"\x89PNG\r\n\x1a\n", "bad PNG signature"
        # IHDR is the first chunk; width/height are bytes 16-23 (big-endian)
        assert blob[12:16] == b"IHDR", "IHDR not first chunk"
        width, height = struct.unpack(">II", blob[16:24])
        if name == "image.png":
            assert str(width) == oracle
        else:
            assert str(height) == oracle
    # also verify the second image's height matches its oracle
    blob2 = e.files["image2.png"]
    w2, h2 = struct.unpack(">II", blob2[16:24])
    assert str(h2) == e.oracle2


@test
def test_all_envs_generate_and_are_consistent():
    for mk in (make_env_records, make_env_binary, make_env_timestamps, make_env_png_dims):
        e = mk()
        assert e.id and e.task1 and e.task2 and e.oracle1 and e.oracle2
        assert e.files, f"{e.id} has no data files"
        assert mk().oracle1 == e.oracle1  # deterministic


if __name__ == "__main__":
    failed = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
