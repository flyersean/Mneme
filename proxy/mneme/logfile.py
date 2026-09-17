"""The proxy's own logging — an append-only, size-capped log file.

The proxy tees its stdout/stderr into a per-port log file so the log
survives restarts and re-runs regardless of how the proxy was launched (setup
wizard, start script, or by hand). The ``logging.max_entries`` config key bounds
the file:

  * unset/omit (default) — no limit
  * 0                     — logging off (no file, no tee)
  * N                     — keep only the newest N lines (oldest trimmed first)
"""

import os
import sys
import threading


class LogFile:
    """Append-only log with an optional line cap (oldest lines trimmed first)."""

    def __init__(self, path, max_entries):
        self.path = path
        self.max_entries = max_entries  # None = unlimited
        self._lock = threading.Lock()
        self._fh = open(path, "a", buffering=1, encoding="utf-8")
        self._lines = 0
        if max_entries is not None:
            self._lines = self._count_lines()
            if self._lines > max_entries:
                self._trim()

    def _count_lines(self):
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def _trim(self):
        # Rewrite the file to keep only the newest max_entries lines. The append
        # handle (O_APPEND) keeps appending at the new end afterwards, so no
        # sparse-file gap.
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            keep = lines[-self.max_entries:]
            with open(self.path, "w", encoding="utf-8") as f:
                f.writelines(keep)
            self._lines = len(keep)
        except Exception:
            self._lines = self.max_entries  # stop fighting; keep counting

    def write(self, data):
        with self._lock:
            self._fh.write(data)
            self._fh.flush()
            if self.max_entries is not None:
                self._lines += data.count("\n")
                if self._lines > self.max_entries:
                    self._trim()

    def flush(self):
        with self._lock:
            self._fh.flush()


class Tee:
    """Write to BOTH a stream and the log file. Under a detached launch the
    original stream may be closed, so stream writes are best-effort; the log
    write is the source of truth."""

    def __init__(self, stream, logfile):
        self.stream = stream
        self.logfile = logfile

    def write(self, data):
        try:
            self.logfile.write(data)
        except Exception:
            pass
        try:
            self.stream.write(data)
        except Exception:
            pass

    def flush(self):
        try:
            self.logfile.flush()
        except Exception:
            pass
        try:
            self.stream.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self.stream.isatty()
        except Exception:
            return False

    def __getattr__(self, name):
        return getattr(self.stream, name)


def read_max_entries():
    """None = unlimited (default), 0 = off, N = keep newest N lines."""
    v = os.environ.get("MNEME_MAX_LOG_ENTRIES", "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def setup_logging(log_path):
    """Tee stdout/stderr into ``log_path`` (append, size-capped).

    Returns the active LogFile, or None when logging is off or couldn't open.
    """
    max_entries = read_max_entries()
    if max_entries == 0:
        return None  # logging off
    path = log_path
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        logfile = LogFile(path, max_entries)
        sys.stdout = Tee(sys.__stdout__, logfile)
        sys.stderr = Tee(sys.__stderr__, logfile)
        return logfile
    except Exception as e:
        print(f"[LOG] cannot open {path}: {e}", flush=True)
        return None
