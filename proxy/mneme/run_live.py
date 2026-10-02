"""Live run streaming buffer.

The proxy routes a harness step's model tokens (reasoning/content) and tool
calls here, keyed by run_id. `/runs/<id>/stream` drains it alongside the ledger
so the chat can show what a model is doing *right now*, not just the plan steps.

In-memory only, FIFO-capped per run. The ledger remains the persistent source
of truth for the runs page; this buffer is purely the live/recent window.
"""

import threading
from collections import deque

_lock = threading.Lock()
_bufs = {}          # run_id -> {"seq": int, "items": deque, "lock": threading.Lock}
_MAX_ITEMS = 20000  # per-run cap (FIFO eviction) — reasoning streams are chatty


def publish(run_id: str, kind: str, data: dict) -> None:
    """Publish one live item. kind: "token" (data={kind: reasoning|content, text})
    or "tool_call" (data=call dict)."""
    with _lock:
        b = _bufs.get(run_id)
        if b is None:
            b = {"seq": 0, "items": deque(), "lock": threading.Lock()}
            _bufs[run_id] = b
    with b["lock"]:
        b["seq"] += 1
        b["items"].append({"seq": b["seq"], "kind": kind, "data": data})
        while len(b["items"]) > _MAX_ITEMS:
            b["items"].popleft()


def drain(run_id: str, after_seq: int):
    """Return (items, new_after_seq) for items with seq > after_seq. Non-destructive."""
    with _lock:
        b = _bufs.get(run_id)
    if b is None:
        return [], after_seq
    with b["lock"]:
        items = [i for i in b["items"] if i["seq"] > after_seq]
        if items:
            after_seq = items[-1]["seq"]
        return items, after_seq


def forget(run_id: str) -> None:
    with _lock:
        _bufs.pop(run_id, None)
