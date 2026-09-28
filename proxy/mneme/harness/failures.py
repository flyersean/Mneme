"""Failure classification — every failed step gets a category.

Categories feed metrics (which failures dominate), reflection (what to learn),
and recovery choices. Classification is deterministic from what the harness
observed (error text + step meta), never from the model's own explanation.
"""

from __future__ import annotations

CATEGORIES = ("interrupted", "verification", "unexecutable", "empty", "fabricated", "provider",
              "budget", "rejected", "executor_error", "tool", "other")


def classify(error: str, meta: dict = None) -> str:
    e = (error or "").lower()
    meta = meta or {}
    if meta.get("interrupted"):
        return "interrupted"
    if e.startswith("verification failed"):
        return "verification"
    if "cannot execute" in e:
        return "unexecutable"
    if "empty model output" in e:
        return "empty"
    if "graded f" in e:
        return "fabricated"
    if meta.get("done_reason") in ("timeout", "error") or "done_reason=timeout" in e or "done_reason=error" in e:
        return "provider"
    if e.startswith("budget exceeded"):
        return "budget"
    if e.startswith("rejected by"):
        return "rejected"
    if any(e.startswith(x) for x in ("typeerror", "valueerror", "keyerror", "attributeerror",
                                     "runtimeerror", "exception", "oserror")):
        return "executor_error"
    if any(x in e for x in ("403", "404", "429", "timed out", "blocked", "not found", "connection")):
        return "tool"
    return "other"
