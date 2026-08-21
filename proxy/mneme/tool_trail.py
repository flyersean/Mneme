"""Deterministic tool-outcome observation + the mid-loop failure nudge.

Two sources feed one ordered trail:
  - the model's own [TOOL:SUCCESS]/[TOOL:FAILURE: reason] tags (the semantic
    "result was non-empty but wrong" cases only the model can judge), and
  - deterministic classification of tool RESULT content (the objective failures
    — a blocked scrape, an empty search, a timeout — the model often never tags,
    because from its view "a different URL is just another attempt").

From that trail the proxy nudges the model out of failure loops; the
fail->success strategy learner (still in the orchestrator) reuses it too.
"""

import re

from mneme.util import _extract_text
from mneme.instructions import _load_instruction


_TOOL_TAG_RE = re.compile(r'\[TOOL:\s*(SUCCESS|FAILURE)\s*(?::\s*([^\]]*?))?\]', re.IGNORECASE)


def _extract_tool_tags(messages, since_last_user=False):
    """Scan assistant messages for [TOOL:SUCCESS]/[TOOL:FAILURE: reason] tags.

    Returns [(status, reason), ...] in conversation order. status is 'SUCCESS'
    or 'FAILURE'. since_last_user scopes to the current task's tool-call loop.
    """
    tags = []
    start = 0
    if since_last_user:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                start = i
                break
    for m in messages[start:]:
        if m.get("role") != "assistant":
            continue
        content = _extract_text(m.get("content", "") or "")
        for match in _TOOL_TAG_RE.finditer(content):
            tags.append((match.group(1).upper(), (match.group(2) or "").strip()))
    return tags


# ─── Deterministic tool-outcome observation ─────────────────────────────
# The model tags outcomes ([TOOL:SUCCESS]/[TOOL:FAILURE]) per system_prompt.md,
# but it often never tags *objective* failures — a blocked scrape, an empty
# search, a timeout — because from its view "a different URL is just another
# attempt." This layer classifies tool RESULTS directly (the proxy already holds
# them when it stages them), so a string of failed calls mid-chain is noticed
# even when untagged. It complements, never replaces, the model's tags.

_FAILURE_MARKERS = (
    "no results found", "no results", "no matching", "nothing found",
    "0 results", "no matches", "not found", "no data",
    "blocked", "captcha", "cloudflare", "access denied", "forbidden",
    "too many requests", "rate limit", "rate-limited", "challenge",
    "timed out", "timeout", "connection refused", "connection reset",
    "connection error", "command not found", "no such file",
    "permission denied", "cancelled", "canceled",
    "403 forbidden", "404 not found", "429 too many", "502 bad gateway",
    "503 service unavailable",
    # exact strings the Pi web extensions emit when they throw (so a real
    # thrown error is caught, not just a clean "no results"/"blocked" reply)
    "web_search failed", "web_scrape failed", "fetch failed",
    "getaddrinfo", "enotfound", "eai_again", "econnrefused", "econnreset",
    "no text content found",
)


def _classify_tool_outcome(content):
    """Deterministic success/failure heuristic for a tool result string.

    Returns (status, reason) or None when it can't confidently tell. status is
    'SUCCESS' or 'FAILURE'. Conservative by design: objective failure shapes
    (empty/blocked/timeout) are classified here; the semantic "non-empty but
    wrong" case is left to the model's tags.
    """
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text:
        return ("FAILURE", "empty result")
    low = text.lower()
    for m in _FAILURE_MARKERS:
        if m in low:
            return ("FAILURE", m)
    if len(text) >= 100:
        return ("SUCCESS", "content")
    return None


def _extract_tool_outcomes(messages, since_last_user=False):
    """Classify each tool message deterministically.

    Returns [(msg_index, status, reason), ...] in conversation order, skipping
    results the heuristic can't confidently label.
    """
    events = []
    start = 0
    if since_last_user:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                start = i
                break
    for i, m in enumerate(messages[start:], start=start):
        if m.get("role") != "tool":
            continue
        cls = _classify_tool_outcome(_extract_text(m.get("content", "") or ""))
        if cls:
            events.append((i, cls[0], cls[1]))
    return events


def _extract_combined_tool_trail(messages, since_last_user=False):
    """Merge deterministic tool outcomes with the model's [TOOL:...] tags into
    one ordered trail of (status, reason), sorted by conversation position.

    Deterministic classification is authoritative for objective failure modes;
    the model's tags fill in the semantic cases only it can judge.
    """
    events = list(_extract_tool_outcomes(messages, since_last_user=since_last_user))
    start = 0
    if since_last_user:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                start = i
                break
    for i, m in enumerate(messages[start:], start=start):
        if m.get("role") != "assistant":
            continue
        content = _extract_text(m.get("content", "") or "")
        for match in _TOOL_TAG_RE.finditer(content):
            events.append((i, match.group(1).upper(), (match.group(2) or "").strip()))
    events.sort(key=lambda e: e[0])
    return [(s, r) for _, s, r in events]


def _tool_failure_nudge(messages):
    """Return a nudge when the recent tag trail shows repeated failures.

    The proxy observes the failure (it never judges success itself); the nudge
    asks the MODEL to diagnose and change approach — it does not prescribe one.
    Uses the combined trail (deterministic outcomes + model tags) so an
    untagged string of blocked/empty tool results still trips the nudge.
    Empty string when the trail looks healthy.
    """
    tags = _extract_combined_tool_trail(messages)
    if not tags:
        return ""
    streak = 0
    for status, _ in reversed(tags):
        if status == "FAILURE":
            streak += 1
        else:
            break
    if streak >= 2:
        return _load_instruction("tool_failure_nudge", vars={"count": str(streak)})
    return ""
