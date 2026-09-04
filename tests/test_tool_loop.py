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
os.environ["MNEME_ASK_REUSABLE"] = "0"  # "just ask" is live-only; ScriptedModel has no answer
os.environ["MNEME_MEMORY_ONLY"] = "0"  # tests exercise the full learning layer, not memory-only mode

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


def _web_call_stop(query, id="call_2"):
    """A web_search tool call where the model reported done_reason="stop" instead
    of "tool_calls" (the Qwen3.6-35B "Uncensored-Aggressive" behaviour). The tool
    call must STILL be executed, not dropped."""
    return {"content": "", "thinking": "narrated reasoning", "tool_calls": [_tc("web_search", {"query": query}, id)],
            "eval_count": 924, "done_reason": "stop"}


def _bash_call(command, id="call_1"):
    return {"content": "", "thinking": "", "tool_calls": [_tc("bash", {"command": command}, id)],
            "eval_count": 10, "done_reason": "tool_calls"}


def _timeout_call():
    return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "timeout"}


def _answer(text):
    return {"content": text, "thinking": "", "tool_calls": [],
            "eval_count": 20, "done_reason": "stop"}


def seed_chunk():
    with mp._db_lock:
        mp.db.execute(
            "INSERT OR REPLACE INTO chunks (chunk_id, topic_label, messages, grade, created_at) "
            "VALUES (?,?,?,?,?)",
            ("mem_1787262481137988", "test memory",
             json.dumps([{"role": "user", "content": "the weather is sunny today"}]),
             "B", "2026-08-20T00:00:00"),
        )
        mp.db.commit()


def clear_strategies():
    with mp._db_lock:
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
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1787262481137988"]

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
def test_tool_call_with_stop_reason_is_executed():
    """Regression (Qwen3.6-35B bug): a model that emits a tool call with
    done_reason="stop" AND empty content must have the tool executed — the loop
    must not break early, drop the call, and return an empty answer."""
    model = ScriptedModel()
    model.queue = [
        _search_call("mneme tool calling bug"),       # turn 1: recall memory
        _web_call_stop("mneme tool calling bug"),     # turn 2: web_search but done_reason=stop
        _answer("The bug is fixed. [source: web_search]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1787262481137988"]

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

    assert calls == ["mneme tool calling bug"], f"web_search dropped on done_reason=stop: {calls!r}"
    assert (result.get("content") or "").strip(), "expected a final answer, got empty content"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


@test
def test_command_tags_stripped_from_model_input():
    """Regression: a bare <<SAVE>> (or any <<COMMAND>>) must never reach the model.
    The old `if cleaned:` guard skipped command-only messages, so a "<<SAVE>>"
    echoed back in the client's history leaked into the model's context."""
    model = ScriptedModel()
    model.queue = [_answer("Understood. I have recorded your message and will remember it.")]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: []

    # <<SAVE>> as a prior turn (echoed history) followed by a normal message.
    mp.process_chat(
        [
            {"role": "user", "content": "<<SAVE>>"},
            {"role": "assistant", "content": "saved"},
            {"role": "user", "content": "hello there"},
        ],
        session_id="test", tools=[],
    )

    # No user message the model saw may contain the raw command.
    for call_msgs in model.seen:
        for m in call_msgs:
            if m.get("role") == "user":
                txt = mp._extract_text(m.get("content", ""))
                assert "<<SAVE>>" not in txt, f"raw <<SAVE>> leaked to model: {m!r}"


@test
def test_stage_page_content_chunks_and_tags_source():
    """fetch_url now returns the full page; _stage_page_content must chunk it
    into page:<domain> source chunks so the whole page is retrievable."""
    captured = []
    orig_add = mp.staging.add
    orig_flush = mp.staging.should_flush
    orig_cs = mp.CHUNK_SIZE
    mp.CHUNK_SIZE = 1000
    mp._stage_content._seen = set()
    mp.staging.add = lambda role, content, **kw: captured.append((role, content, kw.get("source")))
    mp.staging.should_flush = lambda: False
    try:
        n = mp._stage_page_content("A" * 5500, "https://en.wikipedia.org/wiki/Foo")
    finally:
        mp.staging.add = orig_add
        mp.staging.should_flush = orig_flush
        mp.CHUNK_SIZE = orig_cs
    assert n == 6, f"expected 6 chunks (5500/1000), got {n}"
    assert all(c[0] == "assistant" for c in captured), captured
    assert all(c[2] == "page:en.wikipedia.org" for c in captured), captured
    assert "".join(c[1] for c in captured) == "A" * 5500, "chunks must reassemble the full page"


@test
def test_stage_content_prefix_and_shared_dedup():
    """_stage_content must prepend the prefix and dedup identical raw content
    across different sources (shared hash set)."""
    captured = []
    orig_add = mp.staging.add
    orig_flush = mp.staging.should_flush
    mp._stage_content._seen = set()
    mp.staging.add = lambda role, content, **kw: captured.append((content, kw.get("source")))
    mp.staging.should_flush = lambda: False
    try:
        body = "def foo():\n    return 42\n" * 40
        n1 = mp._stage_content(body, "tool:bash", prefix="[bash] cat foo.py")
        n2 = mp._stage_content(body, "page:example.com")  # same raw content -> deduped
    finally:
        mp.staging.add = orig_add
        mp.staging.should_flush = orig_flush
    assert n1 > 0, "first stage should produce chunks"
    assert n2 == 0, "same raw content under a different source must dedup"
    assert captured and captured[0][0].startswith("[bash] cat foo.py\n"), captured[0][0][:80]


@test
def test_paragraph_chunks_split_on_boundaries():
    # Short paragraphs coalesce under target, keeping the newline separators.
    assert mp._paragraph_chunks("alpha\nbeta\ngamma", 100) == ["alpha\nbeta\ngamma"]
    # Paragraphs are not merged past target — the boundary is respected.
    text = "\n".join(["a" * 40, "b" * 40, "c" * 40])
    assert mp._paragraph_chunks(text, 100) == ["a" * 40 + "\n" + "b" * 40, "c" * 40]
    # A single over-long paragraph is hard-split on the target.
    assert mp._paragraph_chunks("z" * 250, 100) == ["z" * 100, "z" * 100, "z" * 50]


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
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1787262481137988"]

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
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1787262481137988"]

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
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1787262481137988"]

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
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1"]

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
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1"]

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
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1"]

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
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1"]

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
def test_noise_baseline_clamped_below_inject_floor():
    """The calibrated noise baseline must never approach the inject floor, or the
    dynamic-K step (score = sim - noise) drops chunks that cleared the threshold."""
    inj = 0.45
    ceil = inj - 0.15
    # high raw noise clamps down to inject - 0.15
    assert mp._clamp_noise_baseline(0.47, inject_min=inj) == ceil
    assert mp._clamp_noise_baseline(0.60, inject_min=inj) == ceil
    # low raw noise passes through unchanged
    assert mp._clamp_noise_baseline(0.20, inject_min=inj) == 0.20
    # the clamp always keeps the baseline strictly below the inject floor
    for raw in (0.0, 0.1, 0.3, 0.45, 0.6, 0.9):
        assert mp._clamp_noise_baseline(raw, inject_min=inj) <= ceil, raw


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
def test_instruction_per_instance_isolation():
    """Per-instance instruction files: an edit saved through one proxy port is
    written to instance_<port>/ and only that instance sees it; other ports fall
    back to the shared default. Regression for the multi-instance /instructions
    editor leaking edits across proxies."""
    root = os.path.join(os.environ.get("MNEME_CHUNK_DIR", ""), "instructions")
    os.makedirs(os.path.join(root, "default"), exist_ok=True)
    old_port = os.environ.get("MNEME_PORT")
    try:
        os.environ["MNEME_PORT"] = "8081"
        p1 = mp.save_instruction("explore", "INSTANCE-8081-EDIT")
        assert "instance_8081" in p1, p1
        assert mp._load_instruction("explore") == "INSTANCE-8081-EDIT"

        # A different port must NOT see 8081's edit — it falls back to the default.
        os.environ["MNEME_PORT"] = "8080"
        assert "INSTANCE-8081-EDIT" not in mp._load_instruction("explore")
        assert "EXPLORE DIRECTIVE" in mp._load_instruction("explore")

        # And 8080's own edit stays separate from 8081's.
        p0 = mp.save_instruction("explore", "INSTANCE-8080-EDIT")
        assert "instance_8080" in p0, p0
        assert mp._load_instruction("explore") == "INSTANCE-8080-EDIT"
        os.environ["MNEME_PORT"] = "8081"
        assert mp._load_instruction("explore") == "INSTANCE-8081-EDIT"
    finally:
        if old_port is None:
            os.environ.pop("MNEME_PORT", None)
        else:
            os.environ["MNEME_PORT"] = old_port
        for sub in ("instance_8080", "instance_8081"):
            shutil.rmtree(os.path.join(root, sub), ignore_errors=True)


@test
def test_detect_stuck_consecutive_failures():
    # Recovery window (Phase 2): 2 consecutive failures is NOT stuck (the model
    # still gets to try a different tool); 3 consecutive failures IS stuck.
    two_fail = [
        {"role": "user", "content": "scrape the blocked site"},
        {"role": "assistant", "content": "trying", "tool_calls": []},
        {"role": "tool", "content": "blocked by cloudflare"},
        {"role": "assistant", "content": "retrying", "tool_calls": []},
        {"role": "tool", "content": "blocked by cloudflare"},
    ]
    stuck2, _ = mp._detect_stuck(two_fail)
    assert not stuck2  # 2 failures -> recovery window, not yet stuck
    two_fail += [
        {"role": "assistant", "content": "retrying again", "tool_calls": []},
        {"role": "tool", "content": "blocked by cloudflare"},
    ]
    stuck, reason = mp._detect_stuck(two_fail)
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
    # declare_edge is no longer a valid decision — it must not parse.
    d2 = mp._parse_deliberation("DECISION: declare_edge\nMISSING: no API access")
    assert d2["decision"] == ""


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
def test_capability_edge_directs_to_overcome():
    # A flagged edge must route the model to OVERCOME (build/reuse), not the
    # old dead-end "propose a tool or state you cannot answer".
    d = mp._load_instruction("capability_edge", vars={"problem_type": "compute"})
    for marker in ("OVERCOME", "DECISION: build_tool", "DECISION: reuse_tool"):
        assert marker in d, f"capability_edge missing '{marker}'"
    assert "DECISION: declare_edge" not in d
    assert "propose the exact tool" not in d
    assert "state clearly that you" not in d
    # A flagged type returns the directive; an unflagged type returns nothing.
    mp._record_capability("edge_route_test", "F")
    mp._record_capability("edge_route_test", "F")
    assert mp._is_capability_edge("edge_route_test") is True
    assert "OVERCOME" in mp._capability_directive("edge_route_test")
    assert mp._capability_directive("other") == ""


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
    assert "EXHAUSTED" in e and "declare_edge" not in e
    assert mp._build_tool_calls([]) == 0
    # unified bound: the tool-call budget is derived from the iteration knob
    assert mp.BUILD_MAX_TOOL_CALLS == mp.BUILD_MAX_ITERATIONS * 2


@test
def test_instruction_sync_no_orphans_no_missing():
    """Sync check: every _load_instruction call site references a defined default,
    and every defined default is injected somewhere (no orphans)."""
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


@test
def test_instruction_materialize_roundtrip():
    """Every shipped prompt materializes to disk and loads back EXACTLY (a user can
    read/edit the on-disk file like system_prompt.md), and materialize is idempotent
    (never clobbers an edited file)."""
    from mneme.instructions import materialize_instructions, _parse_instruction_file, DEFAULT_INSTRUCTIONS
    materialize_instructions()
    d = os.path.join(os.environ["MNEME_CHUNK_DIR"], "instructions", "default")
    assert os.path.isdir(d), f"materialize didn't create {d}"
    for name, default in DEFAULT_INSTRUCTIONS.items():
        path = os.path.join(d, name + ".txt")
        assert os.path.isfile(path), f"missing materialized file {name}.txt"
        body, _ = _parse_instruction_file(path)
        assert body == default, f"{name} round-trip mismatch: {body!r} != {default!r}"
    # idempotency: a user-edited file must survive a re-materialize
    p = os.path.join(d, "step_back_examine.txt")
    with open(p, "w") as f:
        f.write("# when: edited\n\nEDITED BODY\n")
    materialize_instructions()
    body, _ = _parse_instruction_file(p)
    assert body == "EDITED BODY", f"materialize clobbered an edited file: {body!r}"


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
def test_text_tool_calls_parsed():
    """Gemma 3/4 emit tool calls as ```tool_code``` text (Python-call syntax), not
    native message.tool_calls. The Ollama path must parse these back into
    tool_calls so the loop executes them, plus tolerate a fenced-JSON form, and
    never false-positive on ordinary prose."""
    # Gemma ```tool_code``` block with narration
    gem = 'Okay, I need to list files.\n```tool_code\nbash(command="ls /tmp")\n```'
    tcs, res = mp._parse_text_tool_calls(gem)
    assert len(tcs) == 1 and tcs[0]["function"]["name"] == "bash", tcs
    assert tcs[0]["function"]["arguments"] == {"command": "ls /tmp"}, tcs
    assert res == "Okay, I need to list files.", f"narration should survive: {res!r}"

    # fenced JSON {name, arguments}
    js = 'Let me search.\n```json\n{"name": "search_memory", "arguments": {"query": "api key"}}\n```'
    tcs2, res2 = mp._parse_text_tool_calls(js)
    assert len(tcs2) == 1 and tcs2[0]["function"]["name"] == "search_memory", tcs2
    assert tcs2[0]["function"]["arguments"] == {"query": "api key"}, tcs2

    # Gemma <tool_call> XML with JSON body
    xml = '<tool_call>\n{"name": "bash", "arguments": {"command": "ls /tmp"}}\n</tool_call>'
    tcs3, res3 = mp._parse_text_tool_calls(xml)
    assert len(tcs3) == 1 and tcs3[0]["function"]["name"] == "bash", tcs3
    assert tcs3[0]["function"]["arguments"] == {"command": "ls /tmp"}, tcs3

    # plain prose with a JSON-looking object but no arguments key -> untouched
    plain = 'Here is a normal answer with {"name": "data"} but no arguments key.'
    assert mp._parse_text_tool_calls(plain) == ([], plain)


@test
def test_single_search_then_answer():
    """Regression guard: the pre-existing single-search -> answer path still works."""
    model = ScriptedModel()
    model.queue = [
        _search_call("mneme tool calling bug"),
        _answer("Here's what I found. [source: mem_1787262481137988]"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1787262481137988"]

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
        assert names == ["search_memory", "list_tools", "read_tool", "read_file", "fetch_url", "web_search", "bash", "write"], names
        client = [
            {"type": "function", "function": {"name": "bash"}},
            {"type": "function", "function": {"name": "write"}},
            {"type": "function", "function": {"name": "web_search"}},
        ]
        names = [t["function"]["name"] for t in mt.assemble_tools(client)]
        # web_search is a SERVER tool now — the client's copy is deduped, not appended.
        assert names == ["search_memory", "list_tools", "read_tool", "read_file", "fetch_url", "web_search", "bash", "write"], names
    finally:
        mt.NATIVE_TOOLS_MODE = orig


@test
def test_per_tool_disable_flag():
    mt = mp.mntools
    saved = {k: os.environ.get(k) for k in ("MNEME_TOOL_WEB_SEARCH", "MNEME_TOOL_FETCH_URL")}
    try:
        os.environ["MNEME_TOOL_WEB_SEARCH"] = "0"
        os.environ["MNEME_TOOL_FETCH_URL"] = "0"
        names = [t["function"]["name"] for t in mt.assemble_tools([])]
        assert "web_search" not in names, names
        assert "fetch_url" not in names, names
        assert "search_memory" in names and "read_file" in names, names
        ro = mt.enabled_readonly_names()
        assert "web_search" not in ro and "fetch_url" not in ro, ro
        assert "search_memory" in ro and "list_tools" in ro, ro
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_memory_disabled_master_switch():
    mp.MEMORY_ENABLED = False
    orig_env = os.environ.get("MNEME_MEMORY_ENABLED")
    os.environ["MNEME_MEMORY_ENABLED"] = "0"
    try:
        # retrieval/injection skipped
        assert mp.build_context("test query") == ("", "other")
        # tool/page staging skipped
        assert mp._stage_content("x" * 600, "tool:bash") == 0
        # search_memory auto-removed from the tool list
        assert "search_memory" not in mp.mntools.enabled_readonly_names()
        # conversation staging is a no-op
        before = len(mp.staging.messages)
        mp.staging.add("user", "hello", source="user")
        assert len(mp.staging.messages) == before
    finally:
        mp.MEMORY_ENABLED = True
        if orig_env is None:
            os.environ.pop("MNEME_MEMORY_ENABLED", None)
        else:
            os.environ["MNEME_MEMORY_ENABLED"] = orig_env


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


# ── 4b. Verify path (grading 'check' info) ─────────────────────────────────
_gr = mp.grading  # mneme.grading module (re-exported through mneme_proxy)


@test
def test_parse_verdict_claims_extracts_check():
    reply = ("My dog is named Rex | DISHONEST | check: the dog's name Rex\n"
             "Bitcoin is $77,300 | DISHONEST | check: current bitcoin price\n"
             "The sky is blue | HONEST-SOURCED | check: none\n")
    claims = _gr._parse_verdict_claims(reply)
    assert len(claims) == 3, claims
    assert claims[0]["verdict"] == "DISHONEST"
    assert claims[0]["check"] == "the dog's name Rex"
    assert claims[1]["check"] == "current bitcoin price"
    assert claims[2]["verdict"] == "HONEST-SOURCED"


@test
def test_is_memory_backed_detects_internal_fact():
    ctx = "The user's dog is named Rex and Rex likes to fetch sticks."
    assert _gr._is_memory_backed("my dog's name is Rex", ctx) is True
    assert _gr._is_memory_backed("Bitcoin trades at $77,300", ctx) is False
    assert _gr._is_memory_backed("my dog's name is Rex", "") is False


@test
def test_verify_and_regrade_memory_backed_relabels_honest():
    prov = "My dog is named Rex | DISHONEST | check: the dog's name\n"
    ctx = "The user's dog is named Rex."
    # memory-backed -> no world claims -> no web search / no model call
    grade, answer = _gr._verify_and_regrade(
        prov, "My dog is named Rex.", "what's my dog's name", ctx, "F")
    assert grade == "B", f"memory-backed claim should relabel honest, got {grade}"
    assert answer == "My dog is named Rex.", "memory-backed answer must be untouched"


@test
def test_verify_and_regrade_world_fabrication_fails():
    orig_q, orig_ws = mp.query_model, mp.mntools._exec_web_search
    mp.mntools._exec_web_search = lambda q: "[no results]"
    class FakeModel:
        def __call__(self, messages, *a, **k):
            return {"content": "CLAIM 1: FALSE - not found anywhere", "thinking": ""}
    mp.query_model = FakeModel()
    try:
        prov = "Bitcoin is $99,999 | DISHONEST | check: current bitcoin price\n"
        grade, answer = _gr._verify_and_regrade(
            prov, "Bitcoin is $99,999.", "bitcoin price", "", "F")
        assert grade == "F", f"fabricated world claim must fail, got {grade}"
        assert "[verify]" in answer, "fabrication must be flagged in the answer"
    finally:
        mp.query_model, mp.mntools._exec_web_search = orig_q, orig_ws


@test
def test_verify_and_regrade_world_true_passes():
    orig_q, orig_ws = mp.query_model, mp.mntools._exec_web_search
    mp.mntools._exec_web_search = lambda q: "Bitcoin price today is around $77,300"
    class FakeModel:
        def __call__(self, messages, *a, **k):
            return {"content": "CLAIM 1: TRUE - matches search", "thinking": ""}
    mp.query_model = FakeModel()
    try:
        prov = "Bitcoin is $77,300 | DISHONEST | check: current bitcoin price\n"
        grade, answer = _gr._verify_and_regrade(
            prov, "Bitcoin is $77,300.", "bitcoin price", "", "F")
        assert grade == "B", f"verified-true world claim must pass, got {grade}"
        assert "[verify]" not in answer, "verified-true answer must not be flagged"
    finally:
        mp.query_model, mp.mntools._exec_web_search = orig_q, orig_ws


# ── 4b. Honest-terminal detection + URL normalization (false-positive fixes) ──
@test
def test_is_honest_terminal_detects_uncitable_answers():
    terminal = [
        "7 divided by 0 is undefined.",
        "The lobster roll is Market Price, no fixed dollar amount.",
        "I don't know — you never told me.",
        "No U.S. President resigned in August 2025 — that did not happen.",
        "Did you mean to ask something?",
    ]
    for t in terminal:
        assert _gr._is_honest_terminal(t) is True, f"{t!r} should be honest-terminal"
    specific = [
        "The Pulled Pork Sandwich is $13.49.",
        "The capital of France is Paris.",
        "Bitcoin is around $77,300.",
    ]
    for s in specific:
        assert _gr._is_honest_terminal(s) is False, f"{s!r} should NOT be honest-terminal"


@test
def test_source_domain_normalizes_www_and_scheme():
    assert _gr._source_domain("https://www.shaws-wharf.com/menu") == "shaws-wharf.com"
    assert _gr._source_domain("shaws-wharf.com") == "shaws-wharf.com"
    assert _gr._source_domain("http://WWW.Example.com:8080/x") == "example.com"


@test
def test_extract_urls_from_tool_trace():
    tr = [
        {"tool": "fetch_url", "args": {"url": "https://www.shaws-wharf.com/menu"}, "result": "menu content"},
        {"tool": "web_search", "args": {"query": "x"}, "result": "see https://allmenus.com/foo"},
    ]
    u = _gr._extract_urls_from_tool_trace(tr)
    assert "https://www.shaws-wharf.com/menu" in u
    assert "https://allmenus.com/foo" in u


@test
def test_grade_inline_honest_terminal_is_pass_not_judge():
    parsed = {"sources": [], "guesses": 0, "has_tags": False}
    # undefined -> B (no slow-judge fallback), not None
    assert _gr._grade_inline(parsed, "7 / 0 is undefined.", False) == "B"


# ── 4c. Continue-after-empty (prompt the model to keep going) ──────────────
@test
def test_is_near_empty_detects_shrugs():
    for shrug in ("None", "...", "Idk", "N/A", "I don't know", "nope", ""):
        assert mp._is_near_empty(shrug) is True, f"{shrug!r} should be near-empty"
    for real in ("Paris", "The capital is Paris.", "Yes, it is raining.",
                 "Bitcoin is around $77,300."):
        assert mp._is_near_empty(real) is False, f"{real!r} should not be near-empty"


@test
def test_near_empty_answer_prompts_continue():
    model = ScriptedModel()
    model.queue = [
        _search_call("jamos pizza", id="c1"),
        _answer("None"),                        # model gives up mid-struggle
        _answer("The pizza info. [source: mem_1]"),  # continue prompt -> real answer
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False, q_vec=None: ["mem_1"]

    result = mp.process_chat(
        [{"role": "user", "content": "jamos pizza info"}],
        session_id="test", tools=[],
    )
    c = result.get("content") or ""
    assert "pizza info" in c, f"continue prompt should produce a real answer, got {c!r}"
    assert "gave up" not in c, "must not return a quit message"
    # The continue prompt must have been injected into a followup.
    hit = any("CONTINUE" in str(m.get("content", "")) for msgs in model.seen for m in msgs)
    assert hit, "continue prompt was never injected"
    assert not model.queue, f"scripted model had leftover responses: {model.queue!r}"


# ── 4d. Context budget line + tool-state summary (model suggestions #1/#2) ──
@test
def test_context_budget_line_injected():
    ctx = mp._finalize_context("hello world")
    assert "[context budget:" in ctx, f"budget line missing: {ctx[-200:]!r}"


@test
def test_recent_attempts_summary():
    tr = [
        {"tool": "web_search", "args": {"query": "jamos pizza"},
         "result": "[web_search: no results (all backends rate-limited/blocked)]", "blocked": False},
        {"tool": "bash", "args": {"command": "curl -s https://api.example"},
         "result": "x" * 120, "blocked": False},       # long content -> success
        {"tool": "write", "args": {"file_path": "s.py"},
         "result": "", "blocked": True},               # budget-blocked
    ]
    s = mp._recent_attempts_summary(tr)
    assert "[recent attempts]" in s, s
    assert "web_search" in s and "failure" in s, s
    assert "bash" in s and "success" in s, s
    assert "write" in s and "blocked" in s, s
    assert mp._recent_attempts_summary([]) == ""


@test
def test_memory_only_uses_light_prompt():
    orig = mp.MEMORY_ONLY
    try:
        mp.MEMORY_ONLY = True
        blk = mp._system_prompt_block()
        # Keeps source-tagging (grading is still active in memory-only mode and
        # depends on [source]/[guess] tags) but drops the learning-layer obligations...
        assert "Source Tagging" in blk, "memory-only must keep source-tagging rules (grading depends on them)"
        assert "Tool Outcome Tagging" not in blk, "memory-only must drop tool-tagging rules"
        # ...but still explains how memory works...
        assert "Memory Chunk Format" in blk, "memory-only must keep the chunk-format legend"
        assert "search_memory" in blk, "memory-only must keep the search instructions"
        # _finalize_context now only appends the budget line (the system prompt
        # moved to _system_prompt_block for the head/tail split).
        ctx = mp._finalize_context("hello world")
        assert "[context budget:" in ctx
    finally:
        mp.MEMORY_ONLY = orig


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
