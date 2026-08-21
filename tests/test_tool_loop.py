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
    /home/sean/mneme-venv/bin/python tests/test_tool_loop.py
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

    def __call__(self, messages, *args, **kwargs):
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
def test_search_then_web_search_passthrough():
    """BUG 1 (regression): after a memory search, the model's follow-up web_search
    call must be FORWARDED to the client, not silently dropped.

    Old code did `result["tool_calls"] = remaining_calls`, wiping the re-query's
    web_search and grading the turn F. This fails on the old code, passes on the fix.
    """
    model = ScriptedModel()
    model.queue = [
        _search_call("mneme tool calling bug"),   # turn 1: recall memory
        _web_call("mneme tool calling bug"),      # turn 2: hand off to web_search
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1787262481137988"]

    result = mp.process_chat(
        [{"role": "user", "content": "What's the status of the Mneme tool-calling bug? Search memory."}],
        session_id="test", tools=[],
    )

    tc = result.get("tool_calls") or []
    assert len(tc) == 1, f"expected exactly 1 forwarded tool call, got {tc!r}"
    assert tc[0]["function"]["name"] == "web_search", \
        f"web_search was dropped; got {tc!r}"
    # The memory search was resolved server-side, so it must NOT leak through.
    names = [t["function"]["name"] for t in tc]
    assert "search_memory" not in names, "search_memory should be resolved server-side"
    assert result.get("_grade") == "C", f"expected deferred grade C, got {result.get('_grade')!r}"
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
def test_search_loop_exhaustion_returns_results_as_content():
    """BUG 1 (fallback): if the model keeps searching and never answers, the
    proxy must resolve the final search and return the results as CONTENT —
    not forward a search_memory to the client's empty shim (which would stall)."""
    model = ScriptedModel()
    model.queue = [
        _search_call("mneme tool calling bug"),
        _search_call("mneme tool calling bug", id="call_2"),
        _search_call("mneme tool calling bug", id="call_3"),
        _search_call("mneme tool calling bug", id="call_4"),
        _search_call("mneme tool calling bug", id="call_5"),
        # The fallback content (raw search results) has no provenance tags, so
        # the grading path makes one slow provenance-judge call on it.
        _answer("NO SPECIFIC CLAIMS"),
    ]
    mp.query_model = model
    mp.route_query = lambda q, top_k=3, with_scores=False: ["mem_1787262481137988"]

    result = mp.process_chat(
        [{"role": "user", "content": "What's the status of the Mneme tool-calling bug?"}],
        session_id="test", tools=[],
    )

    # The fallback returns the search results as content, and does NOT forward
    # a search_memory tool call (the client shim is empty).
    assert "Search results from Mneme memory:" in (result.get("content") or ""), \
        f"expected search results as content, got {result.get('content','')[:120]!r}"
    names = [t["function"]["name"] for t in (result.get("tool_calls") or [])]
    assert "search_memory" not in names, \
        f"search_memory should not be forwarded to the empty client shim: {names!r}"


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
