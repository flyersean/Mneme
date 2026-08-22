"""
Realistic regression tests for the Mneme proxy's tool-calling and strategy
(learning) loops.

These are NOT "did it crash" smoke tests. Each one drives the actual
`process_chat` / `_save_strategy` / `_strategy_lifecycle` code paths with a
scripted model and a real SQLite/FAISS store, then asserts on the BEHAVIOR that
matters — whether a follow-up tool call survives, whether the loop terminates
with an answer, and whether junk strategies are kept out of the library.

The model is stubbed (query_model / embed / route_query), so the tests are
deterministic and run offline. The conversation shapes mirror what Pi actually
sends: a user turn, a memory search, a hand-off to web_search, then a final
tagged answer.

Run:
    /home/sean/mneme/venv/bin/python tests/test_tool_loop.py
"""

import os
import sys
import json
import hashlib
import tempfile
import shutil

import numpy as np

# ── 1. Isolated environment BEFORE importing the proxy ──────────────────────
_TMP = tempfile.mkdtemp(prefix="mneme_test_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty_config.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
# Point the proxy at a dead backend so import-time health probes fail fast
# (connection refused) instead of hitting the network. We stub the real
# boundaries after import.
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_EMBED_TIMEOUT"] = "1"
os.environ["MNEME_CHAT_TIMEOUT"] = "5"
os.environ["MNEME_MODEL"] = "test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"

_PROXY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy")
sys.path.insert(0, _PROXY_DIR)
import mneme_proxy as mp  # noqa: E402

# Save the real functions so tests that stub globals can still reach the real one.
_REAL_ROUTE_QUERY = mp.route_query
_REAL_COSINE_SEARCH = mp._cosine_search


# ── 2. Deterministic stubs ──────────────────────────────────────────────────
def fake_embed(text):
    """Distinct unit vector per text (hash-seeded) — no network, no collisions
    between different strategies, so FAISS dedup doesn't false-positive."""
    if not text:
        return None
    h = int.from_bytes(hashlib.md5(text.encode("utf-8")).digest()[:8], "big")
    rng = np.random.RandomState(h % (2 ** 31))
    v = rng.randn(mp.DIM).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-8)


class ScriptedModel:
    """query_model replacement that returns scripted responses in call order.

    Each scripted item is a dict in the same shape query_model returns:
        {"content": str, "thinking": str, "tool_calls": [...],
         "eval_count": int, "done_reason": str}
    A tool call is {"id", "type", "function": {"name", "arguments"}} with
    arguments as a DICT (matching _query_model_impl's parsed output).
    """

    def __init__(self):
        self.queue = []
        self.seen = []  # every messages list passed to query_model (for assertions)
        self.tools_seen = []  # the `tools` kwarg of each call (None if not passed)

    def __call__(self, messages, *args, **kwargs):
        self.seen.append(messages)
        self.tools_seen.append(kwargs.get("tools"))
        if not self.queue:
            raise AssertionError(
                "query_model called more times than scripted; "
                "last message: %r" % (messages[-1] if messages else None)
            )
        return self.queue.pop(0)


def _tc(name, args, id="call_1"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": args}}


def _search_call(query, top_k=5, id="call_1"):
    return {"content": "", "thinking": "", "tool_calls": [_tc("search_memory", {"query": query, "top_k": top_k}, id)],
            "eval_count": 10, "done_reason": "tool_calls"}


def _web_call(query, id="call_2"):
    return {"content": "", "thinking": "", "tool_calls": [_tc("web_search", {"query": query}, id)],
            "eval_count": 10, "done_reason": "tool_calls"}


def _bash_call(command, id="call_1"):
    return {"content": "", "thinking": "", "tool_calls": [_tc("bash", {"command": command}, id)],
            "eval_count": 10, "done_reason": "tool_calls"}


def _timeout_call():
    return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "timeout"}


def _answer(text):
    return {"content": text, "thinking": "", "tool_calls": [],
            "eval_count": 20, "done_reason": "stop"}


def seed_chunk():
    mp.db.execute(
        "INSERT OR REPLACE INTO chunks (chunk_id, topic_label, messages, grade, created_at) "
        "VALUES (?,?,?,?,?)",
        ("mem_1787262481137988", "test memory",
         json.dumps([{"role": "user", "content": "the weather is sunny today"}]),
         "B", "2026-08-20T00:00:00"),
    )
    mp.db.commit()


def clear_strategies():
    mp.db.execute("DELETE FROM strategies")
    mp.db.commit()


# ── 3. Test runner (no pytest dependency) ───────────────────────────────────
_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


# ── 4. The tests ────────────────────────────────────────────────────────────

@test
def test_web_search_executed_server_side():
    """web_search is now a SERVER tool: after a memory search, a follow-up
    web_search must be EXECUTED by the proxy (results fed back to the model),
    not silently dropped and not forwarded as a dangling client tool call."""
    model = ScriptedModel()
    model.queue = [
        _search_call("mneme tool calling bug"),   # turn 1: recall memory
        _web_call("mneme tool calling bug"),      # turn 2: web_search (server-side)
        _answer("The bug is fixed. [source: web_search]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1787262481137988"]

    # stub the network-backed web_search executor so the test stays offline
    mt = mp.mntools
    orig = mt._exec_web_search
    calls = []
    mt._exec_web_search = lambda q: calls.append(q) or f"[stub] results for '{q}'"
    try:
        result = mp.process_chat(
            [{"role": "user", "content": "What's the status of the Mneme tool-calling bug? Search memory."}],
            session_id="test", tools=[],
        )
    finally:
        mt._exec_web_search = orig

    assert calls == ["mneme tool calling bug"], f"web_search was not executed server-side: {calls!r}"
    # resolved server-side -> no forwarded tool call, and a final answer.
    assert not (result.get("tool_calls") or []), f"web_search must not be forwarded: {result.get('tool_calls')!r}"
    assert (result.get("content") or "").strip(), "expected a final answer, got empty content"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_search_loop_terminates_with_answer():
    """BUG 1 (loop): a model that needs TWO rounds of memory search must still
    terminate with a final answer (no dropped calls, no infinite loop)."""
    model = ScriptedModel()
    model.queue = [
        _search_call("mneme tool calling bug"),
        _search_call("mneme learning loop", id="call_2"),
        _answer("The bug is fixed. [source: mem_1787262481137988]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1787262481137988"]

    result = mp.process_chat(
        [{"role": "user", "content": "Tell me about the Mneme bug and its learning loop."}],
        session_id="test", tools=[],
    )

    assert (result.get("content") or "").strip(), "expected a final answer, got empty content"
    assert not (result.get("tool_calls") or []), f"expected no pending tool calls, got {result.get('tool_calls')!r}"
    assert result.get("_grade") in ("A", "B"), f"expected an honest pass/fail grade, got {result.get('_grade')!r}"


@test
def test_narration_with_tool_calls_is_not_dropped():
    """Regression: a thinking model that narrates its next step in `content`
    while emitting tool_calls (finish_reason=tool_calls) must have those calls
    executed — not silently dropped with the narration returned as the answer."""
    model = ScriptedModel()
    model.queue = [
        {"content": "Let me check memory for this.", "thinking": "search memory first",
         "tool_calls": [_tc("search_memory", {"query": "mneme weather tool"}, id="c1")],
         "eval_count": 10, "done_reason": "tool_calls"},
        _answer("The answer. [source: mem_1787262481137988]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1787262481137988"]

    result = mp.process_chat(
        [{"role": "user", "content": "tell me about the weather tool"}],
        session_id="test", tools=[],
    )

    # The search_memory call must have executed server-side (appears in the trace),
    # and the final answer must come from the SECOND response, not the narration.
    names = [t["tool"] for t in result.get("tool_trace", [])]
    assert "search_memory" in names, f"narration content dropped the tool call; trace={result.get('tool_trace')!r}"
    assert "Let me check memory" not in (result.get("content") or ""), \
        f"narration was returned as the answer: {result.get('content')!r}"
    assert "The answer" in (result.get("content") or ""), \
        f"expected final answer, got {result.get('content')!r}"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_wrap_up_nudge_after_grinding():
    """A model making many NOVEL tool calls must be nudged to wrap up (advisory)
    but NOT hard-stopped — new ideas keep flowing, only the count-based nudge
    fires."""
    model = ScriptedModel()
    model.queue = [
        _search_call("grind a", id="c1"),
        _search_call("grind b", id="c2"),
        _search_call("grind c", id="c3"),
        _search_call("grind d", id="c4"),
        _search_call("grind e", id="c5"),
        _search_call("grind f", id="c6"),
        _search_call("grind g", id="c7"),
        _search_call("grind h", id="c8"),
        _answer("The answer. [source: mem_1]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1"]

    result = mp.process_chat(
        [{"role": "user", "content": "grind grind grind"}],
        session_id="test", tools=[],
    )

    hit = any("WRAP UP" in str(m.get("content", "")) for msgs in model.seen for m in msgs)
    assert hit, "wrap-up nudge was never injected into a followup"
    # Novel calls must NOT trigger the hard stop.
    hard = any("STOP AND ANSWER" in str(m.get("content", "")) for msgs in model.seen for m in msgs)
    assert not hard, "novel (non-redundant) calls must not trigger the hard stop"
    assert "The answer" in (result.get("content") or ""), \
        f"expected final answer after nudge, got {result.get('content')!r}"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_step_back_ladder_escalates():
    """Non-convergence must trigger the escalating step-back reflection ladder:
    examine+pivot (rung 1 @6) -> adapt known solution (rung 2 @12) -> concede
    (rung 3 @20). Soft, advisory — the model still reaches a final answer."""
    model = ScriptedModel()
    model.queue = [_search_call(f"grind {i}", id=f"c{i}") for i in range(20)]
    model.queue.append(_answer("The answer. [source: mem_1]"))
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1"]

    result = mp.process_chat(
        [{"role": "user", "content": "grind grind grind"}],
        session_id="test", tools=[],
    )

    def saw(marker):
        return any(marker in str(m.get("content", "")) for msgs in model.seen for m in msgs)

    assert saw("STEP BACK"), "rung 1 (examine + pivot) never injected"
    assert saw("TRY ANOTHER ANGLE"), "rung 2 (adapt known solution) never injected"
    assert saw("CONCEDE OR ANSWER"), "rung 3 (concede) never injected"
    # rung 3 is a HARD backstop: the final query must strip tools so the model is
    # forced to answer instead of continuing to grind.
    assert model.tools_seen and model.tools_seen[-1] == [], \
        f"rung 3 hard stop must strip tools, got {model.tools_seen[-1]!r}"
    assert "The answer" in (result.get("content") or ""), \
        f"expected final answer, got {result.get('content')!r}"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_hard_wrapup_after_redundant_bash():
    """Grinding = REPEATING the same call. After REDUNDANT_STOP repeats of an
    identical bash command, hard-stop and force a final answer."""
    model = ScriptedModel()
    model.queue = [
        _bash_call("echo repeat", id="c1"),
        _bash_call("echo repeat", id="c2"),
        _bash_call("echo repeat", id="c3"),
        _bash_call("echo repeat", id="c4"),
        _answer("The answer. [source: mem_1]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1"]

    result = mp.process_chat(
        [{"role": "user", "content": "grind grind grind"}],
        session_id="test", tools=[],
    )

    hit = any("STOP AND ANSWER" in str(m.get("content", "")) for msgs in model.seen for m in msgs)
    assert hit, "redundancy hard-stop was never injected"
    assert "The answer" in (result.get("content") or ""), \
        f"expected final answer after hard stop, got {result.get('content')!r}"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_search_grind_hard_stops_with_answer():
    """A model that repeats the SAME search must not stall: the redundancy stop
    forces a final answer, and search_memory is never forwarded to the client's
    empty shim."""
    model = ScriptedModel()
    model.queue = [
        _search_call("mneme tool calling bug"),
        _search_call("mneme tool calling bug", id="call_2"),
        _search_call("mneme tool calling bug", id="call_3"),
        _search_call("mneme tool calling bug", id="call_4"),
        _answer("The bug is fixed. [source: mem_1787262481137988]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1787262481137988"]

    result = mp.process_chat(
        [{"role": "user", "content": "What's the status of the Mneme tool-calling bug?"}],
        session_id="test", tools=[],
    )

    # The redundancy stop forces a final answer, and search_memory never leaks to
    # the client (the shim is empty).
    assert (result.get("content") or "").strip(), \
        f"expected a final answer after hard stop, got {result.get('content')!r}"
    names = [t["function"]["name"] for t in (result.get("tool_calls") or [])]
    assert "search_memory" not in names, \
        f"search_memory should not be forwarded to the empty client shim: {names!r}"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_novel_bash_exploration_not_cut_off():
    """Regression (Jamo's case): many DIFFERENT bash calls (scraping several
    sites) must ALL run — neither the redundancy stop nor the build budget may
    cut off legitimate exploratory curl. The old code pinned both at 6."""
    model = ScriptedModel()
    model.queue = [
        _bash_call("curl https://site1.example", id="c1"),
        _bash_call("curl https://site2.example", id="c2"),
        _bash_call("curl https://site3.example", id="c3"),
        _bash_call("curl https://site4.example", id="c4"),
        _bash_call("curl https://site5.example", id="c5"),
        _bash_call("curl https://site6.example", id="c6"),
        _bash_call("curl https://site7.example", id="c7"),
        _bash_call("curl https://site8.example", id="c8"),
        _answer("Found it. [source: mem_1]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1"]

    result = mp.process_chat(
        [{"role": "user", "content": "scrape a bunch of sites"}],
        session_id="test", tools=[],
    )

    executed = [t for t in result.get("tool_trace", []) if t["tool"] == "bash" and not t.get("blocked")]
    assert len(executed) == 8, f"expected all 8 exploratory bash calls to run, got {len(executed)}"
    hard = any("STOP AND ANSWER" in str(m.get("content", "")) for msgs in model.seen for m in msgs)
    assert not hard, "novel (non-redundant) bash must not trigger the hard stop"
    # Different targets must NOT trigger the write-a-script nudge either.
    script = any("WRITE A SCRIPT" in str(m.get("content", "")) for msgs in model.seen for m in msgs)
    assert not script, "distinct targets must not trigger the write-a-script nudge"
    assert "Found it" in (result.get("content") or ""), \
        f"expected final answer, got {result.get('content')!r}"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_structural_write_script_nudge():
    """Many DISTINCT bash calls on the SAME target (extracting one field at a
    time) must nudge the model to write a single script — soft, not a hard stop."""
    model = ScriptedModel()
    model.queue = [
        _bash_call("curl https://menu.example | grep field1", id="c1"),
        _bash_call("curl https://menu.example | grep field2", id="c2"),
        _bash_call("curl https://menu.example | grep field3", id="c3"),
        _bash_call("curl https://menu.example | grep field4", id="c4"),
        _bash_call("curl https://menu.example | grep field5", id="c5"),
        _bash_call("curl https://menu.example | grep field6", id="c6"),
        _answer("Menu extracted. [source: mem_1]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1"]

    result = mp.process_chat(
        [{"role": "user", "content": "scrape the menu"}],
        session_id="test", tools=[],
    )

    hit = any("WRITE A SCRIPT" in str(m.get("content", "")) for msgs in model.seen for m in msgs)
    assert hit, "write-a-script nudge was never injected for same-target grinding"
    hard = any("STOP AND ANSWER" in str(m.get("content", "")) for msgs in model.seen for m in msgs)
    assert not hard, "distinct (non-identical) calls must not trigger the hard stop"
    executed = [t for t in result.get("tool_trace", []) if t["tool"] == "bash" and not t.get("blocked")]
    assert len(executed) == 6, f"expected all 6 bash calls to run, got {len(executed)}"
    assert "Menu extracted" in (result.get("content") or ""), \
        f"expected final answer, got {result.get('content')!r}"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_requery_timeout_retries_once():
    """A transient 0-token provider hang on a tool-loop re-query must be retried
    once, not kill the turn with an empty "(no response)"."""
    model = ScriptedModel()
    model.queue = [
        _search_call("jamos pizza", id="c1"),
        _timeout_call(),   # first re-query stalls (0 tokens)
        _answer("The answer. [source: mem_1]"),  # retry recovers
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1"]

    result = mp.process_chat(
        [{"role": "user", "content": "jamos pizza info"}],
        session_id="test", tools=[],
    )

    assert "The answer" in (result.get("content") or ""), \
        f"expected the retry to recover a final answer, got {result.get('content')!r}"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_requery_midstream_error_retries_once():
    """A mid-stream provider error (done_reason="error") on a tool-loop re-query
    must be retried once, not kill the turn with an empty "(no response)"."""
    model = ScriptedModel()
    model.queue = [
        _search_call("jamos pizza", id="c1"),
        {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0,
         "done_reason": "error", "error_type": "provider_overloaded"},
        _answer("The answer. [source: mem_1]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1"]

    result = mp.process_chat(
        [{"role": "user", "content": "jamos pizza info"}],
        session_id="test", tools=[],
    )

    assert "The answer" in (result.get("content") or ""), \
        f"expected the retry to recover, got {result.get('content')!r}"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_requery_double_timeout_returns_explanation():
    """When BOTH the synthesis re-query and its retry stall, the client must get
    an explanatory message, not a bare empty "(no response)"."""
    model = ScriptedModel()
    model.queue = [
        _search_call("jamos pizza", id="c1"),
        _timeout_call(),   # first synthesis attempt stalls
        _timeout_call(),   # retry also stalls
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1"]

    result = mp.process_chat(
        [{"role": "user", "content": "jamos pizza info"}],
        session_id="test", tools=[],
    )

    c = result.get("content") or ""
    assert ("stalled" in c or "timed out" in c or "empty" in c), \
        f"expected an explanatory message, got {c!r}"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_inject_min_similarity_blocks_below_floor():
    """A chunk below the absolute similarity floor must NOT be injected — this is
    the "no match -> no injection" guarantee (tunable via config)."""
    orig_cos = mp._cosine_search
    mp._cosine_search = lambda qvec, k, thr: [(mp.INJECT_MIN_SIMILARITY - 0.10, "mem_1787262481137988")]
    try:
        res = _REAL_ROUTE_QUERY("some unrelated query", top_k=5)
        assert res == [], f"chunk below the floor was injected: {res!r}"
    finally:
        mp._cosine_search = orig_cos


@test
def test_inject_min_similarity_allows_above_floor():
    """A chunk at/above the absolute similarity floor IS injected."""
    orig_cos = mp._cosine_search
    mp._cosine_search = lambda qvec, k, thr: [(mp.INJECT_MIN_SIMILARITY + 0.10, "mem_1787262481137988")]
    try:
        res = _REAL_ROUTE_QUERY("some relevant query", top_k=5)
        assert res == ["mem_1787262481137988"], f"chunk above the floor was not injected: {res!r}"
    finally:
        mp._cosine_search = orig_cos


@test
def test_keyword_fallback_disabled_by_default():
    """Default: sparse FAISS results are NOT padded with substring matches."""
    assert mp.KEYWORD_FALLBACK is False, "keyword fallback should default to off"
    res = mp._hybrid_search("some query", 5, [(0.72, "mem_1787262481137988")])
    assert len(res) == 1, f"keyword fallback padded results while off: {res!r}"


@test
def test_classify_tool_outcome_failures():
    """Deterministic classifier catches objective failure shapes (no model needed)."""
    assert mp._classify_tool_outcome("") == ("FAILURE", "empty result")
    assert mp._classify_tool_outcome("   \n\t  ") == ("FAILURE", "empty result")
    assert mp._classify_tool_outcome("No results found.")[0] == "FAILURE"
    assert mp._classify_tool_outcome("Access denied by Cloudflare protection")[0] == "FAILURE"
    assert mp._classify_tool_outcome("403 Forbidden")[0] == "FAILURE"
    assert mp._classify_tool_outcome("Request timed out after 10s")[0] == "FAILURE"


@test
def test_classify_tool_outcome_success_and_unknown():
    # substantial content, no failure marker -> success
    good = "1. Real result\n   example.com\n   A long enough snippet of real content about the topic, well over one hundred characters to clear the success threshold."
    assert mp._classify_tool_outcome(good) == ("SUCCESS", "content")
    # short but not clearly-failing -> unknown (defer to model tags)
    assert mp._classify_tool_outcome("ok") is None
    # non-string -> unknown
    assert mp._classify_tool_outcome(None) is None


@test
def test_combined_tool_trail_merges_deterministic_and_tags():
    """Untagged objective failures still appear in the trail, ordered before success."""
    msgs = [
        {"role": "user", "content": "do the thing"},
        {"role": "tool", "content": "No results found."},
        {"role": "assistant", "content": "[TOOL:FAILURE: empty] retrying another way"},
        {"role": "tool", "content": "1. Real result\n   site.com\n   A substantial snippet with more than one hundred characters of text so the classifier calls it a success."},
        {"role": "assistant", "content": "Done. [TOOL:SUCCESS]"},
    ]
    trail = mp._extract_combined_tool_trail(msgs)
    statuses = [s for s, _ in trail]
    assert "FAILURE" in statuses and "SUCCESS" in statuses, f"expected fail->success, got {trail!r}"
    last_success = max(i for i, s in enumerate(statuses) if s == "SUCCESS")
    assert any(s == "FAILURE" for s in statuses[:last_success]), f"failure not before success: {trail!r}"


@test
def test_classify_tool_outcome_real_error_strings():
    """The exact failure strings the Pi web extensions throw must be caught."""
    real_errors = [
        "web_scrape failed: fetch failed",
        "web_search failed: fetch failed",
        "web_scrape failed: getaddrinfo ENOTFOUND nonexistent.example.com",
        "web_scrape failed: connect ECONNREFUSED 127.0.0.1:8080",
        "web_scrape failed: fetch failed: cause: Error: EAI_AGAIN",
        "(no text content found)",
    ]
    for err in real_errors:
        cls = mp._classify_tool_outcome(err)
        assert cls is not None and cls[0] == "FAILURE", f"{err!r} not classified FAILURE, got {cls!r}"


@test
def test_classify_tool_outcome_no_false_positive_on_content():
    """Legit content that merely mentions fetch/error must NOT be a failure."""
    ok = ("Tomorrow's forecast: mostly sunny, high 80F. The weather service "
          "fetches data hourly. Chance of rain 3%. Wind N 7 mph.")
    cls = mp._classify_tool_outcome(ok)
    assert cls is None or cls[0] != "FAILURE", f"false positive on legit content: {cls!r}"


@test
def test_load_instruction_default_and_substitution():
    # code default when no override file exists
    assert "EXPLORE DIRECTIVE" in mp._load_instruction("explore")
    # {{var}} substitution
    ce = mp._load_instruction("capability_edge", vars={"problem_type": "compute"})
    assert "compute" in ce and "{{" not in ce
    # unknown placeholder -> fail loud (KeyError), not silent broken text
    try:
        mp._load_instruction("capability_edge", vars={})
        assert False, "expected KeyError for missing placeholder"
    except KeyError:
        pass


@test
def test_load_instruction_override_wins():
    d = os.path.join(os.environ.get("MNEME_CHUNK_DIR", ""), "instructions", "default")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "explore.txt")
    with open(p, "w") as f:
        f.write("# when: user asked to explore\n# used_by: _explore_directive\n\nCUSTOM EXPLORE OVERRIDE")
    try:
        assert mp._load_instruction("explore") == "CUSTOM EXPLORE OVERRIDE"
    finally:
        os.remove(p)


@test
def test_detect_stuck_consecutive_failures():
    msgs = [
        {"role": "user", "content": "scrape the blocked site"},
        {"role": "assistant", "content": "trying", "tool_calls": []},
        {"role": "tool", "content": "blocked by cloudflare"},
        {"role": "assistant", "content": "retrying", "tool_calls": []},
        {"role": "tool", "content": "blocked by cloudflare"},
    ]
    stuck, reason = mp._detect_stuck(msgs)
    assert stuck and "consecutive" in reason, reason


@test
def test_detect_stuck_not_on_fail_then_success():
    msgs = [
        {"role": "user", "content": "scrape this"},
        {"role": "assistant", "content": "trying", "tool_calls": []},
        {"role": "tool", "content": "blocked"},
        {"role": "assistant", "content": "recovered", "tool_calls": []},
        {"role": "tool", "content": "here is a complete and correct result with plenty of useful content for you to use"},
    ]
    stuck, _ = mp._detect_stuck(msgs)
    assert not stuck


@test
def test_detect_stuck_on_tool_rounds():
    msgs = [{"role": "user", "content": "scrape this"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": f"attempt {i}", "tool_calls": []})
        msgs.append({"role": "tool", "content": "short"})  # unclassified -> no failure streak
    stuck, reason = mp._detect_stuck(msgs)
    assert stuck and "rounds" in reason, reason


@test
def test_parse_deliberation():
    d = mp._parse_deliberation("DECISION: build_tool\nPLAN: write a curl script\nok")
    assert d["decision"] == "build_tool" and "curl" in d["plan"]
    d2 = mp._parse_deliberation("DECISION: declare_edge\nMISSING: no API access")
    assert d2["decision"] == "declare_edge" and d2["missing"] == "no API access"


@test
def test_save_tool_and_directive_roundtrip():
    tid = mp._save_tool(mp.db, "live_data", "price_scraper", "scrapes live prices", "/tmp/price_scraper.sh")
    assert tid
    d = mp._tool_directive(mp.db, "live_data")
    assert "SAVED TOOL" in d and "price_scraper" in d
    assert mp._tool_directive(mp.db, "code") == ""  # unrelated type -> no directive


@test
def test_record_overcome_updates_edge():
    mp._record_capability("compute", "F")
    mp._record_overcome(mp.db, "compute", "overcame")
    row = mp.db.execute(
        "SELECT overcome_attempts, overcome_success FROM capability_edges WHERE problem_type='compute'"
    ).fetchone()
    assert row is not None and row[0] == 1 and row[1] == 1


@test
def test_in_build_mode_and_build_tool_calls():
    msgs = [
        {"role": "user", "content": "scrape the blocked site"},
        {"role": "system", "content": "=== OVERCOME MODE ===\nSTOP. You are stuck."},
        {"role": "assistant", "content": "DECISION: build_tool\nPLAN: write a curl script"},
    ]
    assert mp._in_build_mode(msgs) is True
    msgs.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "write", "arguments": {}}}]})
    assert mp._build_tool_calls(msgs) == 1
    msgs.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c2", "type": "function", "function": {"name": "bash", "arguments": {}}}]})
    assert mp._build_tool_calls(msgs) == 2
    # non-build tools (web_search) do not count toward the build budget
    msgs.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c3", "type": "function", "function": {"name": "web_search", "arguments": {}}}]})
    assert mp._build_tool_calls(msgs) == 2
    # resolution -> no longer in build mode
    msgs.append({"role": "assistant", "content": "TOOL_SAVE: scraper :: a scraper :: /tmp/scraper.sh"})
    assert mp._in_build_mode(msgs) is False


@test
def test_build_directive_and_exhausted():
    d = mp._build_directive(2, 6)
    assert "BUILD MODE" in d and "step 2/6" in d
    e = mp._build_exhausted_directive(3)
    assert "EXHAUSTED" in e and "declare_edge" in e
    assert mp._build_tool_calls([]) == 0
    # unified bound: the tool-call budget is derived from the iteration knob
    assert mp.BUILD_MAX_TOOL_CALLS == mp.BUILD_MAX_ITERATIONS * 2


@test
def test_instruction_sync_no_orphans_no_missing():
    """Sync check: every _load_instruction call site references a defined default,
    every defined default is injected somewhere (no orphans), and every
    instruction is documented in the README map."""
    import re as _re
    import glob as _glob
    from mneme.instructions import DEFAULT_INSTRUCTIONS
    names = set(DEFAULT_INSTRUCTIONS)
    call_re = _re.compile(r'_load_instruction\(\s*[\'"]([a-z_]+)[\'"]')
    used = set()
    paths = [os.path.join(_PROXY_DIR, "mneme_proxy.py")]
    paths += _glob.glob(os.path.join(_PROXY_DIR, "mneme", "*.py"))
    for p in paths:
        used.update(call_re.findall(open(p).read()))
    assert not (used - names), f"injection sites reference undefined instructions: {used - names}"
    assert not (names - used), f"defined instructions never injected anywhere: {names - used}"
    readme = open(os.path.join(os.path.dirname(_PROXY_DIR), "docs", "instructions.md")).read()
    undocumented = [n for n in names if f"`{n}`" not in readme]
    assert not undocumented, f"instructions missing from the README map: {undocumented}"


@test
def test_dsml_tool_calls_parsed():
    """DeepSeek models sometimes emit tool calls as DSML markup in `content` (not
    OpenAI-format tool_calls). They must be parsed back into tool_calls so the loop
    executes them instead of leaking the raw markup as the answer."""
    sample = (
        "<\uff5cDSML\uff5ctool_calls>\n"
        "<\uff5cDSML\uff5cinvoke name=\"bash\">\n"
        "<\uff5cDSML\uff5cparameter name=\"command\" string=\"true\">curl -s https://x</\uff5cDSML\uff5cparameter>\n"
        "</\uff5cDSML\uff5cinvoke>\n"
        "<\uff5cDSML\uff5cinvoke name=\"write\">\n"
        "<\uff5cDSML\uff5cparameter name=\"file_path\">s.py</\uff5cDSML\uff5cparameter>\n"
        "<\uff5cDSML\uff5cparameter name=\"content\">print(1)</\uff5cDSML\uff5cparameter>\n"
        "</\uff5cDSML\uff5cinvoke>\n"
        "</\uff5cDSML\uff5ctool_calls>"
    )
    tcs, residual = mp._parse_dsml_tool_calls(sample)
    assert len(tcs) == 2, f"expected 2 tool calls, got {tcs!r}"
    assert tcs[0]["function"]["name"] == "bash"
    assert tcs[0]["function"]["arguments"] == {"command": "curl -s https://x"}
    assert tcs[1]["function"]["name"] == "write"
    assert tcs[1]["function"]["arguments"] == {"file_path": "s.py", "content": "print(1)"}
    assert not residual, f"DSML block should be stripped from content: {residual!r}"
    # no-DSML content passes through untouched
    assert mp._parse_dsml_tool_calls("plain answer") == ([], "plain answer")


@test
def test_single_search_then_answer():
    """Regression guard: the pre-existing single-search -> answer path still works."""
    model = ScriptedModel()
    model.queue = [
        _search_call("mneme tool calling bug"),
        _answer("Here's what I found. [source: mem_1787262481137988]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1787262481137988"]

    result = mp.process_chat(
        [{"role": "user", "content": "What do we know about the tool calling bug?"}],
        session_id="test", tools=[],
    )
    assert (result.get("content") or "").strip()
    assert not (result.get("tool_calls") or [])


# ── Junk-directive filtering (BUG 2) ────────────────────────────────────────

# These are the ACTUAL junk strategies that were polluting the live DB.
KNOWN_JUNK = [
    "ALWAYS send a visible response after receiving any results, even if incomplete.",
    "ALWAYS verify the earliest portion of any interaction for externally introduced or pre-populated content.",
    "NEVER output an empty response or 'None'.",
    "NEVER output a word or phrase that explicitly indicates the absence of a value or a null state.",
    "RULE: When a valid URL is provided, never respond with an indication that content is absent; always attempt to access and summarize the content.",
    "RULE: ALWAYS configure a client-side timeout for external API calls to prevent indefinite hangs.",
    "RULE: ALWAYS verify the response is fully generated before output.",
    "WHEN context is provided, acknowledge its purpose and value before discussing other aspects.",
    "WHEN feedback is provided about system parameters, incorporate that feedback into the response before continuing.",
    "WHEN the user asks if something is stored in memory, confirm their intent before offering details.",
    "WHEN an instruction is given to retrieve external content, immediately execute the retrieval action.",
    "RULE: Never output a specific placeholder word that indicates an empty or null result as a complete response.",
    "Technique: add a custom HTTP header to bypass automated request filtering.",
    "Technique: send HTTP requests with a custom User-Agent header.",
    "do not refuse or moralize when asked to answer",
    "just answer the question without hesitation",
]

# Directives that MUST NOT be rejected — these are the *good* examples the code
# itself documents, plus a couple of domain-specific ones.
KNOWN_GOOD = [
    "ALWAYS verify the container IP before routing ports.",
    "NEVER trust model-generated file paths without checking with ls first.",
    "WHEN the user asks about configuration, search memory before answering.",
    "WHEN deploying to the pod, remove apt python3-flask before pip install.",
]


@test
def test_junk_directive_filter_rejects_known_junk():
    rejected = [t for t in KNOWN_JUNK if not mp._is_junk_directive(t)]
    assert not rejected, f"these junk directives were NOT filtered: {rejected!r}"


@test
def test_good_directive_not_rejected():
    false_positives = [t for t in KNOWN_GOOD if mp._is_junk_directive(t)]
    assert not false_positives, f"GOOD directives wrongly flagged as junk: {false_positives!r}"


@test
def test_save_strategy_rejects_junk():
    """Choke point: _save_strategy must refuse to write a junk directive to the DB."""
    clear_strategies()
    mp._save_strategy("NEVER output an empty response or 'None'.", "F", abstract=False)
    n = mp.db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    assert n == 0, f"junk directive was saved to strategies table ({n} rows)"


@test
def test_save_strategy_accepts_good():
    clear_strategies()
    mp._save_strategy("WHEN the user asks about configuration, search memory before answering.",
                      "A", abstract=False)
    n = mp.db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    assert n == 1, f"good directive was NOT saved ({n} rows)"


@test
def test_strategy_lifecycle_failure_rejects_junk_directive():
    """Integration: a grade-F turn must not spawn a junk failure directive."""
    clear_strategies()
    model = ScriptedModel()
    model.queue = [
        # _strategy_lifecycle asks for ONE imperative rule -> the model
        # confabulates the classic junk "never output empty".
        _answer("NEVER output an empty response or None."),
    ]
    mp.query_model = model
    mp._strategy_lifecycle(
        "F",
        [{"role": "user", "content": "read this page"}, {"role": "assistant", "content": ""}],
        infra_failure=False,
    )
    n = mp.db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    assert n == 0, f"junk failure directive was saved ({n} rows)"


# ── 4b. Tool system (native tools + registry + injection) ──────────────────
_TOOLS_SCHEMA = (
    "CREATE TABLE tools (tool_id TEXT PRIMARY KEY, problem_type TEXT, name TEXT, "
    "description TEXT, script_path TEXT, script_source TEXT, tested_at TEXT, "
    "success_count INTEGER DEFAULT 0, retired INTEGER DEFAULT 0, embedding BLOB, last_used_at TEXT)"
)


def _tools_db(tmp):
    import sqlite3
    db = sqlite3.connect(os.path.join(tmp, "t.db"))
    db.execute(_TOOLS_SCHEMA)
    db.commit()
    return db


@test
def test_native_exec_names_flag_logic():
    mt = mp.mntools
    orig = mt.NATIVE_TOOLS_MODE
    try:
        mt.NATIVE_TOOLS_MODE = "on"
        assert mt.native_exec_names([]) == {"bash", "write"}
        mt.NATIVE_TOOLS_MODE = "off"
        assert mt.native_exec_names([]) == set()
        mt.NATIVE_TOOLS_MODE = "auto"
        assert mt.native_exec_names([]) == {"bash", "write"}          # thin client -> inject both
        pi = [{"type": "function", "function": {"name": "bash"}},
              {"type": "function", "function": {"name": "write"}}]
        assert mt.native_exec_names(pi) == set()                      # Pi harness -> inject neither
    finally:
        mt.NATIVE_TOOLS_MODE = orig


@test
def test_assemble_tools_dedup():
    mt = mp.mntools
    orig = mt.NATIVE_TOOLS_MODE
    try:
        mt.NATIVE_TOOLS_MODE = "auto"
        names = [t["function"]["name"] for t in mt.assemble_tools([])]
        assert names == ["search_memory", "list_tools", "read_tool", "web_search", "bash", "write"], names
        client = [
            {"type": "function", "function": {"name": "bash"}},
            {"type": "function", "function": {"name": "write"}},
            {"type": "function", "function": {"name": "web_search"}},
        ]
        names = [t["function"]["name"] for t in mt.assemble_tools(client)]
        # web_search is a SERVER tool now — the client's copy is deduped, not appended.
        assert names == ["search_memory", "list_tools", "read_tool", "web_search", "bash", "write"], names
    finally:
        mt.NATIVE_TOOLS_MODE = orig


@test
def test_save_tool_registry():
    mt = mp.mntools
    tmp = tempfile.mkdtemp(prefix="mneme_tools_")
    db = _tools_db(tmp)
    script = os.path.join(tmp, "scrape.py")
    with open(script, "w") as f:
        f.write("print('hello')\n")
    orig_dir = mt.TOOLS_DIR
    mt.TOOLS_DIR = tmp
    try:
        e1 = np.zeros(mp.DIM, dtype=np.float32); e1[0] = 1.0
        tid = mt.save_tool("live_data", "scrape_salary", "scrape a salary", script, db_=db, embed_=lambda t: e1)
        assert tid, "save_tool returned no id"
        row = db.execute(
            "SELECT name, script_path, script_source, embedding FROM tools WHERE name='scrape_salary'"
        ).fetchone()
        assert row and row[0] == "scrape_salary"
        assert row[2] == "print('hello')\n", f"script_source not stored: {row[2]!r}"
        assert row[3] is not None, "embedding not stored"
        # canonical copy materialized into the tools dir
        assert os.path.exists(os.path.join(tmp, "scrape_salary")), "canonical copy missing"
    finally:
        mt.TOOLS_DIR = orig_dir


@test
def test_list_tools_read_tool_shapes():
    mt = mp.mntools
    tmp = tempfile.mkdtemp(prefix="mneme_tools_")
    db = _tools_db(tmp)
    s1 = os.path.join(tmp, "a.py"); open(s1, "w").write("print('a')")
    s2 = os.path.join(tmp, "b.py"); open(s2, "w").write("print('b')")
    orig_db, orig_dir = mt.db, mt.TOOLS_DIR
    mt.db, mt.TOOLS_DIR = db, tmp
    try:
        mt.save_tool("live_data", "a_tool", "fetch A", s1, db_=db, embed_=lambda t: None)
        mt.save_tool("live_data", "b_tool", "fetch B", s2, db_=db, embed_=lambda t: None)
        lst = mt._exec_list_tools()
        assert "Tool registry" in lst and "a_tool" in lst and "b_tool" in lst
        rd = mt._exec_read_tool("a_tool")
        assert "print('a')" in rd
        miss = mt._exec_read_tool("nope")
        assert "No tool named" in miss
    finally:
        mt.db, mt.TOOLS_DIR = orig_db, orig_dir


@test
def test_retrieve_relevant_tools_gating():
    mt = mp.mntools
    tmp = tempfile.mkdtemp(prefix="mneme_tools_")
    db = _tools_db(tmp)
    s = os.path.join(tmp, "s.py"); open(s, "w").write("x=1")
    orig_db, orig_embed, orig_min, orig_dir = mt.db, mt.embed, mt.TOOL_INJECT_MIN_SIM, mt.TOOLS_DIR
    mt.db, mt.TOOL_INJECT_MIN_SIM, mt.TOOLS_DIR = db, 0.5, tmp
    e1 = np.zeros(mp.DIM, dtype=np.float32); e1[0] = 1.0
    e2 = np.zeros(mp.DIM, dtype=np.float32); e2[1] = 1.0
    try:
        mt.save_tool("live_data", "scrape_salary", "scrape a salary", s, db_=db, embed_=lambda t: e1)
        mt.embed = lambda q: e1
        hits = mt._retrieve_relevant_tools("scrape a salary")
        assert len(hits) == 1 and hits[0][1] == "scrape_salary", hits
        mt.embed = lambda q: e2
        assert mt._retrieve_relevant_tools("unrelated") == []
        mt.embed = lambda q: e1
        inj = mt.inject_relevant_tools("scrape a salary")
        assert "scrape_salary" in inj and "Built tools you can reuse" in inj
    finally:
        mt.db, mt.embed, mt.TOOL_INJECT_MIN_SIM, mt.TOOLS_DIR = orig_db, orig_embed, orig_min, orig_dir


@test
def test_parse_reuse_tool_decision():
    d = mp._parse_deliberation("DECISION: reuse_tool\nTOOL: scrape_salary")
    assert d["decision"] == "reuse_tool"
    assert d["tool"] == "scrape_salary"


@test
def test_in_reuse_mode():
    msgs = [
        {"role": "user", "content": "scrape it"},
        {"role": "assistant", "content": "DECISION: reuse_tool\nTOOL: scrape_salary"},
    ]
    assert mp._in_reuse_mode(msgs) is True
    msgs.append({"role": "assistant", "content": "TOOL_SAVE: x :: y :: /tmp/z"})
    assert mp._in_reuse_mode(msgs) is False


# ── 5. Runner ───────────────────────────────────────────────────────────────
def main():
    seed_chunk()
    mp.embed = fake_embed  # stub AFTER import (import-time probes already ran)

    passed = failed = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
