"""Provider-transport fixes (share/provider-connection-review.md §7, critical path):

  1. Always send max_tokens on the OpenAI-compatible path (default 32000,
     OpenCode parity) + bounded reasoning.max_tokens (max_tokens/2) for
     reasoning models.
  2. Per-model first-token-timeout floors for known reasoning models (Hermes
     reasoning_timeouts.py pattern) — the 180s wall killed every glm-5.3 call
     on the POST's header read.
  3. Bounded retry with jittered exponential backoff + Retry-After, with
     structured retryability (auth/billing/context-overflow are NOT retried).
  4. Mid-stream stalls return the PARTIAL answer instead of discarding it
     (the Ollama path always preserved partials; the OpenRouter path didn't).

Run: python3 tests/test_provider_transport_fixes.py
"""

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_ptfix_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty_config.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "openai"          # force the OpenRouter path
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_CHAT_TIMEOUT"] = "5"
os.environ["MNEME_FIRST_TOKEN_TIMEOUT"] = "3"
os.environ["MNEME_RETRY_ATTEMPTS"] = "3"
os.environ["MNEME_RETRY_BACKOFF_BASE"] = "0.01"  # near-zero: fast tests
os.environ["MNEME_RETRY_BACKOFF_CAP"] = "0.02"
os.environ["MNEME_MODEL"] = "test/test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import mneme_proxy as mp  # noqa: E402

# unittest discover imports every module BEFORE running any test, and this
# repo's convention is import-time env setup. Restore the keys we touched on
# module teardown so runtime-read vars (hot-reload, first_token_timeout,
# backend) don't leak into tests that were imported before us but run after.
_ENV_KEYS_TOUCHED = ("MNEME_BACKEND", "MNEME_CHAT_TIMEOUT", "MNEME_FIRST_TOKEN_TIMEOUT",
                     "MNEME_RETRY_ATTEMPTS", "MNEME_RETRY_BACKOFF_BASE",
                     "MNEME_RETRY_BACKOFF_CAP", "MNEME_MODEL", "EMBED_MODEL",
                     "LABEL_MODEL", "MNEME_CHUNK_DIR", "MNEME_CONFIG")
_ENV_PRIOR = {k: os.environ.get(k) for k in _ENV_KEYS_TOUCHED}


def tearDownModule():
    for k, v in _ENV_PRIOR.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class Recorder:
    """Stand-in for query_model that replays canned results."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, *a, **kw):
        self.calls.append(kw)
        return self.replies.pop(0) if self.replies else {"content": "x", "done_reason": "stop"}


# ─── Fix 2: reasoning-model stale-timeout floors ─────────────────────────────

class TestReasoningStaleFloor(unittest.TestCase):
    def test_known_reasoning_models_get_floor(self):
        # (model, expected floor) — slug after the provider/ prefix.
        # GLM is intentionally NOT floored (streams reasoning, 180s is plenty).
        cases = [
            ("openai/o3-mini", 300), ("openai/o3", 600), ("openai/o1", 600),
            ("deepseek/deepseek-r1", 600), ("deepseek/deepseek-v4-flash", 600),
            ("deepseek/deepseek-v4-pro", 600),
            ("qwen/qwen3-8b", 180), ("qwen/qwq-32b", 300),
            ("nvidia/nemotron-3-ultra-550b", 600),
        ]
        for model, floor in cases:
            self.assertEqual(mp._reasoning_stale_floor(model), floor, model)

    def test_non_reasoning_models_get_none(self):
        for model in ("gpt-4o", "llama-3.2-3b", "olmo-1", "z-ai/glm-5.3", "test-model", "", None):
            self.assertIsNone(mp._reasoning_stale_floor(model), model)

    def test_floor_is_longest_slug_match(self):
        # o3-mini (300) must beat the shorter o1/o3 table entries.
        self.assertEqual(mp._reasoning_stale_floor("openai/o3-mini-2025-01-31"), 300)


# ─── Fix 1: max_tokens + reasoning budget in the payload ────────────────────

class TestPayloadBudget(unittest.TestCase):
    def setUp(self):
        self._posted = {}
        self._orig_post = mp.requests.post
        mp.requests.post = self._fake_post
        self._orig_ev = (os.environ.get("MNEME_REASONING_ENABLED"),
                         os.environ.get("MNEME_REASONING_EFFORT"),
                         os.environ.get("MNEME_MAX_TOKENS"),
                         mp.OR_REASONING_BUDGET)

    def tearDown(self):
        mp.requests.post = self._orig_post
        for key, val in zip(("MNEME_REASONING_ENABLED", "MNEME_REASONING_EFFORT",
                             "MNEME_MAX_TOKENS"), self._orig_ev[:3]):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        mp.OR_REASONING_BUDGET = self._orig_ev[3]

    def _fake_post(self, url, headers=None, json=None, timeout=None, stream=None):
        self._posted = {"url": url, "json": json, "timeout": timeout, "stream": stream}

        class _R:
            status_code = 200
            encoding = "utf-8"

            def iter_lines(self, decode_unicode=False):
                yield 'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}'
                yield "data: [DONE]"

            def close(self):
                pass

            @property
            def text(self):
                return ""

        return _R()

    def _payload(self, **kw):
        msgs = kw.pop("msgs", [{"role": "user", "content": "hi"}])
        res = mp._query_openrouter(msgs, {}, **kw)
        self.assertIsInstance(res, dict)
        return self._posted["json"], res

    def test_max_tokens_always_sent(self):
        os.environ.pop("MNEME_MAX_TOKENS", None)
        os.environ.pop("MNEME_REASONING_ENABLED", None)
        os.environ.pop("MNEME_REASONING_EFFORT", None)
        payload, _ = self._payload()
        self.assertEqual(payload.get("max_tokens"), mp.OR_DEFAULT_MAX_TOKENS)

    def test_explicit_max_tokens_wins(self):
        payload, _ = self._payload(max_tokens=1234)
        self.assertEqual(payload.get("max_tokens"), 1234)

    def test_reasoning_on_sends_half_budget(self):
        os.environ["MNEME_REASONING_ENABLED"] = "1"
        payload, _ = self._payload(max_tokens=8000)
        self.assertEqual(payload.get("reasoning", {}).get("max_tokens"), 4000)

    def test_reasoning_on_budget_never_exceeds_half_even_if_explicit_larger(self):
        os.environ["MNEME_REASONING_ENABLED"] = "1"
        mp.OR_REASONING_BUDGET = "7000"
        payload, _ = self._payload(max_tokens=8000)
        self.assertEqual(payload.get("reasoning", {}).get("max_tokens"), 4000)

    def test_reasoning_budget_off_is_legacy(self):
        os.environ["MNEME_REASONING_ENABLED"] = "1"
        mp.OR_REASONING_BUDGET = "0"
        payload, _ = self._payload(max_tokens=8000)
        self.assertNotIn("max_tokens", payload.get("reasoning", {}))
        self.assertEqual(payload.get("max_tokens"), 8000)

    def test_reasoning_off_sends_enabled_false_and_no_budget(self):
        os.environ.pop("MNEME_REASONING_ENABLED", None)
        os.environ.pop("MNEME_REASONING_EFFORT", None)
        payload, _ = self._payload(max_tokens=8000)
        self.assertEqual(payload.get("reasoning"), {"enabled": False})

    def test_effort_and_budget_coexist(self):
        os.environ["MNEME_REASONING_ENABLED"] = "1"
        os.environ["MNEME_REASONING_EFFORT"] = "low"
        payload, _ = self._payload(max_tokens=8000)
        self.assertEqual(payload["reasoning"].get("effort"), "low")
        self.assertEqual(payload["reasoning"].get("max_tokens"), 4000)

    def test_floor_raises_stream_timeout(self):
        # deepseek-v4-pro floor 600 must replace the configured 3s stale timeout.
        os.environ.pop("MNEME_REASONING_ENABLED", None)
        payload, _ = self._payload(model="deepseek/deepseek-v4-pro")
        self.assertEqual(self._posted["timeout"], (mp.CONNECT_TIMEOUT, 600))

    def test_no_floor_uses_configured_ttft(self):
        os.environ.pop("MNEME_REASONING_ENABLED", None)
        payload, _ = self._payload(model="test-model")
        self.assertEqual(self._posted["timeout"], (mp.CONNECT_TIMEOUT, mp.FIRST_TOKEN_TIMEOUT))


# ─── Fix 3: retry classification + backoff ─────────────────────────────────

class TestRetryClassification(unittest.TestCase):
    def test_timeout_is_retryable(self):
        self.assertTrue(mp._provider_failure_retryable({"done_reason": "timeout"}))

    def test_timeout_with_partial_is_retryable(self):
        # Mid-stream stall with partial content — incomplete, keep best + retry.
        self.assertTrue(mp._provider_failure_retryable(
            {"done_reason": "timeout", "content": "partial answer", "eval_count": 40}))

    def test_auth_billing_not_retryable(self):
        for status in (401, 402, 403):
            self.assertFalse(mp._provider_failure_retryable(
                {"done_reason": "error", "status_code": status}))
        self.assertFalse(mp._provider_failure_retryable(
            {"done_reason": "error", "error_type": "insufficient credits"}))

    def test_429_and_5xx_retryable(self):
        for status in (429, 408, 500, 502, 503):
            self.assertTrue(mp._provider_failure_retryable(
                {"done_reason": "error", "status_code": status}), status)

    def test_429_in_error_type_retryable(self):
        self.assertTrue(mp._provider_failure_retryable(
            {"done_reason": "error", "error_type": "http_429"}))

    def test_context_overflow_not_retryable(self):
        self.assertFalse(mp._provider_failure_retryable(
            {"done_reason": "error", "status_code": 400, "error_type": "context_length_exceeded"}))

    def test_other_4xx_not_retryable(self):
        self.assertFalse(mp._provider_failure_retryable(
            {"done_reason": "error", "status_code": 400}))
        self.assertFalse(mp._provider_failure_retryable(
            {"done_reason": "error", "status_code": 422}))

    def test_unmapped_transport_error_retryable(self):
        self.assertTrue(mp._provider_failure_retryable(
            {"done_reason": "error", "error_type": "upstream_error"}))

    def test_malformed_2xx_error_retryable(self):
        self.assertTrue(mp._provider_failure_retryable(
            {"done_reason": "error", "status_code": 200}))

    def test_success_not_retryable(self):
        for dr in ("stop", "length", "tool_calls", "cancelled", ""):
            self.assertFalse(mp._provider_failure_retryable({"done_reason": dr}), dr)


class TestBackoffDelay(unittest.TestCase):
    def test_exponential_with_jitter(self):
        d1 = mp._retry_backoff_delay(1)
        d2 = mp._retry_backoff_delay(2)
        d3 = mp._retry_backoff_delay(3)
        self.assertGreaterEqual(d1, 0.0)
        self.assertLessEqual(d1, mp.RETRY_BACKOFF_BASE * 1.5 + 1e-9)
        self.assertGreaterEqual(d2, mp.RETRY_BACKOFF_BASE)
        # jitter (up to half the delay) is added AFTER the cap (Hermes shape)
        _cap3 = min(mp.RETRY_BACKOFF_BASE * 4, mp.RETRY_BACKOFF_CAP)
        self.assertLessEqual(d3, _cap3 * 1.5 + 1e-9)
        self.assertGreaterEqual(d3, min(mp.RETRY_BACKOFF_BASE * 4, mp.RETRY_BACKOFF_CAP))

    def test_retry_after_wins_and_is_capped(self):
        self.assertEqual(mp._retry_backoff_delay(1, 45.0), 45.0)
        self.assertEqual(mp._retry_backoff_delay(1, 99999.0), mp.RETRY_AFTER_CAP)

    def test_parse_retry_after(self):
        h = {"retry-after": "7"}
        self.assertEqual(mp._parse_retry_after(h), 7.0)
        h = {"retry-after-ms": "1500"}
        self.assertEqual(mp._parse_retry_after(h), 1.5)
        self.assertIsNone(mp._parse_retry_after({}))
        self.assertIsNone(mp._parse_retry_after(None))
        self.assertEqual(mp._parse_retry_after({"retry-after": "9999"}), mp.RETRY_AFTER_CAP)


# ─── Fix 3: the retry loop itself ───────────────────────────────────────────

class TestRetryLoop(unittest.TestCase):
    def setUp(self):
        self._orig_qm = mp.query_model
        mp.RETRY_ATTEMPTS = 3

    def tearDown(self):
        mp.query_model = self._orig_qm
        mp.RETRY_ATTEMPTS = 3

    def test_success_first_try(self):
        rec = Recorder([{"content": "ok", "done_reason": "stop"}])
        mp.query_model = rec
        res = mp._query_retry_timeout([{"role": "user", "content": "q"}])
        self.assertEqual(res["content"], "ok")
        self.assertEqual(len(rec.calls), 1)

    def test_transient_failure_then_success(self):
        rec = Recorder([
            {"content": "", "done_reason": "timeout", "eval_count": 0},
            {"content": "recovered", "done_reason": "stop"},
        ])
        mp.query_model = rec
        res = mp._query_retry_timeout([{"role": "user", "content": "q"}])
        self.assertEqual(res["content"], "recovered")
        self.assertEqual(len(rec.calls), 2)

    def test_all_fail_returns_best_partial(self):
        # First attempt stalls mid-stream with a partial; later attempts die
        # with nothing. The loop must return the PARTIAL, not the last empty.
        rec = Recorder([
            {"content": "useful partial answer", "done_reason": "timeout", "eval_count": 12},
            {"content": "", "done_reason": "timeout", "eval_count": 0},
            {"content": "", "done_reason": "timeout", "eval_count": 0},
        ])
        mp.query_model = rec
        res = mp._query_retry_timeout([{"role": "user", "content": "q"}])
        self.assertEqual(res["content"], "useful partial answer")
        self.assertEqual(len(rec.calls), 3)

    def test_non_retryable_error_no_retry(self):
        rec = Recorder([
            {"content": "", "done_reason": "error", "status_code": 401},
        ])
        mp.query_model = rec
        res = mp._query_retry_timeout([{"role": "user", "content": "q"}])
        self.assertEqual(res["status_code"], 401)
        self.assertEqual(len(rec.calls), 1)

    def test_attempts_respected(self):
        rec = Recorder([{"content": "", "done_reason": "timeout"}] * 5)
        mp.query_model = rec
        mp._query_retry_timeout([{"role": "user", "content": "q"}], attempts=2)
        self.assertEqual(len(rec.calls), 2)

    def test_options_max_tokens_forwarded(self):
        rec = Recorder([{"content": "ok", "done_reason": "stop"}])
        mp.query_model = rec
        mp._query_retry_timeout([{"role": "user", "content": "q"}],
                                tools=[1], options={"temperature": 0.5}, max_tokens=99)
        kw = rec.calls[0]
        self.assertEqual(kw.get("tools"), [1])
        self.assertEqual(kw.get("options"), {"temperature": 0.5})
        self.assertEqual(kw.get("max_tokens"), 99)


# ─── Fix 4: mid-stream partial preservation in _query_openrouter ───────────

class TestMidStreamPartialPreservation(unittest.TestCase):
    def setUp(self):
        self._orig_post = mp.requests.post
        mp.requests.post = self._fake_post

    def tearDown(self):
        mp.requests.post = self._orig_post

    def _fake_post(self, url, headers=None, json=None, timeout=None, stream=None):
        import requests as _rq

        class _R:
            status_code = 200
            encoding = "utf-8"

            def iter_lines(self, decode_unicode=False):
                yield 'data: {"choices":[{"delta":{"content":"The first part of "}}]}'
                yield 'data: {"choices":[{"delta":{"content":"a long answer."}}]}'
                # stream dies here: no finish event, no [DONE]
                raise _rq.exceptions.ConnectionError("simulated mid-stream stall")

            def close(self):
                pass

            @property
            def text(self):
                return ""

        return _R()

    def test_mid_stream_stall_returns_partial_not_empty(self):
        res = mp._query_openrouter([{"role": "user", "content": "hi"}], {})
        self.assertEqual(res["done_reason"], "timeout")           # incomplete
        self.assertEqual(res["content"], "The first part of a long answer.")
        self.assertGreater(len(res["content"]), 0)

    def test_clean_eof_without_terminal_event_is_incomplete(self):
        # OpenCode requireTerminalEvent: a stream that ends (clean EOF) with
        # NEITHER a finish_reason chunk NOR [DONE] died mid-response — the
        # partial must be preserved and marked retryable, not "stop".
        import requests as _rq

        def _eof_post(url, headers=None, json=None, timeout=None, stream=None):
            class _R:
                status_code = 200
                encoding = "utf-8"

                def iter_lines(self, decode_unicode=False):
                    yield 'data: {"choices":[{"delta":{"content":"half an ans"}}]}'
                    # clean EOF: no finish_reason, no [DONE]

                def close(self):
                    pass

                @property
                def text(self):
                    return ""

            return _R()

        mp.requests.post = _eof_post
        try:
            res = mp._query_openrouter([{"role": "user", "content": "hi"}], {})
        finally:
            mp.requests.post = self._orig_post
        self.assertEqual(res["done_reason"], "timeout")
        self.assertEqual(res["content"], "half an ans")

    def test_done_sentinel_without_finish_is_complete(self):
        # [DONE] alone is a terminal event — an empty-but-clean stream must
        # NOT be retried (preserves the legacy [EMPTY]-nudge behaviour).
        def _done_post(url, headers=None, json=None, timeout=None, stream=None):
            class _R:
                status_code = 200
                encoding = "utf-8"

                def iter_lines(self, decode_unicode=False):
                    yield "data: [DONE]"

                def close(self):
                    pass

                @property
                def text(self):
                    return ""

            return _R()

        mp.requests.post = _done_post
        try:
            res = mp._query_openrouter([{"role": "user", "content": "hi"}], {})
        finally:
            mp.requests.post = self._orig_post
        self.assertEqual(res["done_reason"], "stop")

    def test_no_first_token_returns_empty_timeout(self):
        import requests as _rq

        def _hang_post(url, headers=None, json=None, timeout=None, stream=None):
            class _R:
                status_code = 200
                encoding = "utf-8"

                def iter_lines(self, decode_unicode=False):
                    raise _rq.exceptions.ReadTimeout("simulated hang")
                    yield  # pragma: no cover

                def close(self):
                    pass

                @property
                def text(self):
                    return ""

            return _R()

        mp.requests.post = _hang_post
        res = mp._query_openrouter([{"role": "user", "content": "hi"}], {})
        self.assertEqual(res["done_reason"], "timeout")
        self.assertEqual(res["content"], "")
        self.assertEqual(res["eval_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
