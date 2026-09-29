"""Background jobs — scheduled / recurring sources of runs.

    job ──(every interval_s)──▶ run ──▶ run ──▶ …

A job stores a goal (+ optional tasks/profile/budget) and an interval. The
scheduler thread creates a run whenever a job is due. Claiming a due job is a
compare-and-set on ``next_run_at``, so several proxies sharing one ledger never
fire the same tick twice. By default a job does not overlap itself: if its last
run is still active, the tick is skipped (and recorded).

Runs keep going when the user disconnects — they are already durable; jobs add
"start without a user" (recurring research, monitoring, periodic self-evaluation).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, List, Optional

from mneme.harness.ledger import TERMINAL_STATES, Ledger, LedgerError, new_id, now_iso

MIN_INTERVAL_S = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    goal        TEXT NOT NULL,
    tasks       TEXT DEFAULT 'null',
    profile     TEXT DEFAULT '',
    budget      TEXT DEFAULT '{}',
    interval_s  INTEGER NOT NULL,
    next_run_at REAL NOT NULL,
    enabled     INTEGER DEFAULT 1,
    overlap     INTEGER DEFAULT 0,
    last_run_id TEXT DEFAULT '',
    runs_count  INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    created_by  TEXT DEFAULT 'user',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    action     TEXT NOT NULL,
    run_id     TEXT DEFAULT '',
    detail     TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


class JobStore:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        with ledger._lock:
            ledger._db.executescript(_SCHEMA)
            ledger._db.commit()

    def _decode(self, row) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        d["tasks"] = json.loads(d["tasks"] or "null")
        d["budget"] = json.loads(d["budget"] or "{}")
        d["enabled"] = bool(d["enabled"])
        d["overlap"] = bool(d["overlap"])
        return d

    def create(self, name: str, goal: str, interval_s: int, *, tasks=None, profile: str = "",
               budget: Optional[dict] = None, start_in_s: float = 0, overlap: bool = False,
               created_by: str = "user") -> dict:
        if not (goal or "").strip():
            raise LedgerError("a job needs a goal")
        try:
            interval_s = int(interval_s)
        except (TypeError, ValueError):
            raise LedgerError("interval_s must be an integer")
        if interval_s < MIN_INTERVAL_S:
            raise LedgerError(f"interval_s must be >= {MIN_INTERVAL_S}")
        if tasks is not None:
            Ledger.normalize_task_specs(tasks)  # validate now, not at 3am
        job_id = new_id("job")
        ts = now_iso()
        self.ledger._write(
            "INSERT INTO jobs (job_id, name, goal, tasks, profile, budget, interval_s, next_run_at, overlap, "
            "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, (name or goal)[:120], goal.strip(), json.dumps(tasks), profile or "",
             json.dumps(budget or {}), interval_s, time.time() + float(start_in_s), 1 if overlap else 0,
             created_by, ts, ts))
        self._log(job_id, "created")
        return self.get(job_id)

    def get(self, job_id: str) -> Optional[dict]:
        return self._decode(self.ledger._one("SELECT * FROM jobs WHERE job_id=?", (job_id,)))

    def require(self, job_id: str) -> dict:
        j = self.get(job_id)
        if j is None:
            raise LedgerError(f"no such job: {job_id}")
        return j

    def list(self) -> List[dict]:
        return [self._decode(r) for r in self.ledger._all("SELECT * FROM jobs ORDER BY created_at")]

    def log(self, job_id: str, limit: int = 50) -> List[dict]:
        return [dict(r) for r in self.ledger._all(
            "SELECT * FROM job_log WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, int(limit)))]

    def _log(self, job_id, action, run_id="", detail=""):
        self.ledger._write("INSERT INTO job_log (job_id, action, run_id, detail, created_at) VALUES (?,?,?,?,?)",
                           (job_id, action, run_id, detail, now_iso()))

    def set_enabled(self, job_id: str, enabled: bool) -> dict:
        self.require(job_id)
        self.ledger._write("UPDATE jobs SET enabled=?, updated_at=? WHERE job_id=?",
                           (1 if enabled else 0, now_iso(), job_id))
        self._log(job_id, "enabled" if enabled else "disabled")
        return self.get(job_id)

    def trigger(self, job_id: str) -> dict:
        """Make a job due now (the next scheduler tick runs it)."""
        self.require(job_id)
        self.ledger._write("UPDATE jobs SET next_run_at=?, updated_at=? WHERE job_id=?",
                           (time.time() - 1, now_iso(), job_id))
        self._log(job_id, "triggered")
        return self.get(job_id)

    def claim_due(self, now: Optional[float] = None) -> List[dict]:
        """Atomically advance every due job's next_run_at; return the ones WE claimed."""
        now = time.time() if now is None else now
        claimed = []
        for j in self.list():
            if not j["enabled"] or j["next_run_at"] > now:
                continue
            nxt = now + j["interval_s"]
            cur = self.ledger._write(
                "UPDATE jobs SET next_run_at=?, updated_at=? WHERE job_id=? AND next_run_at=? AND enabled=1",
                (nxt, now_iso(), j["job_id"], j["next_run_at"]))
            if cur.rowcount == 1:
                claimed.append(self.get(j["job_id"]))
        return claimed

    def record_run(self, job_id: str, run_id: str) -> None:
        self.ledger._write("UPDATE jobs SET last_run_id=?, runs_count=runs_count+1, updated_at=? WHERE job_id=?",
                           (run_id, now_iso(), job_id))
        self._log(job_id, "run_started", run_id)

    def record_skip(self, job_id: str, detail: str) -> None:
        self.ledger._write("UPDATE jobs SET skipped=skipped+1, updated_at=? WHERE job_id=?", (now_iso(), job_id))
        self._log(job_id, "skipped", detail=detail)


class Scheduler:
    def __init__(self, engine, jobs: JobStore, tick_s: float = 15.0,
                 log: Optional[Callable[[str], None]] = None):
        self.engine, self.jobs, self.tick_s = engine, jobs, float(tick_s)
        self._log = log or (lambda m: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def tick(self, now: Optional[float] = None) -> List[str]:
        started = []
        for job in self.jobs.claim_due(now):
            last = self.engine.ledger.get_run(job["last_run_id"]) if job["last_run_id"] else None
            if last is not None and not job["overlap"] and last["status"] not in TERMINAL_STATES:
                self.jobs.record_skip(job["job_id"], f"previous run {last['run_id']} still {last['status']}")
                continue
            try:
                run = self.engine.create(job["goal"], job["tasks"], budget=job["budget"] or None,
                                         profile=job["profile"] or "", meta={"job_id": job["job_id"]},
                                         created_by=f"job:{job['job_id']}", start=True)
            except Exception as e:
                self.jobs._log(job["job_id"], "error", detail=f"{type(e).__name__}: {e}")
                continue
            self.jobs.record_run(job["job_id"], run["run_id"])
            started.append(run["run_id"])
            self._log(f"job {job['name']!r} started run {run['run_id']}")
        return started

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while not self._stop.wait(self.tick_s):
                try:
                    self.tick()
                except Exception as e:
                    self._log(f"scheduler tick failed: {type(e).__name__}: {e}")
        self._thread = threading.Thread(target=loop, name="mneme-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
