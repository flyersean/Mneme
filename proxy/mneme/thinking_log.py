"""Persistent thinking/reasoning log.

Records the model's reasoning + content tokens per harness run, so a stalled,
mis-graded, or confusing run can be post-mortemed after the fact. The live
stream buffer (run_live) is in-memory and cleared on run end — this is the
durable counterpart, appended as JSON-lines.

OFF by default. Enable via config:

    debug:
      thinking_log: true            # record model reasoning/content tokens
      thinking_log_path: ""         # optional; default <chunk_dir>/thinking.log

Hot-reloadable: editing `debug.thinking_log` re-applies on the next config
reload (no restart).
"""

import json
import os
import threading
import time

_lock = threading.Lock()
_enabled = False
_path = ""
_fh = None


def configure(enabled, path):
    """Apply config: enable/disable + the log file path. Hot-reload safe."""
    global _enabled, _path, _fh
    with _lock:
        _enabled = bool(enabled)
        _path = path or ""
        if _fh is not None:
            try:
                _fh.close()
            except Exception:
                pass
            _fh = None


def record(run_id, kind, text):
    """Append one token chunk (kind: reasoning|content) or an event marker.

    No-op unless enabled and text is non-empty. Capped to the newest
    `logging.max_entries` lines (same cap as the main proxy log) so thinking.log
    can't grow without bound."""
    if not _enabled or not _path or not text:
        return
    from .logfile import capped_append
    capped_append(_path, json.dumps({"t": round(time.time(), 3), "run": run_id,
                                     "kind": kind, "text": text}))


def enabled() -> bool:
    return _enabled
