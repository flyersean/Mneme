"""The run ledger — durable, append-oriented record of agent execution.

The harness must be able to answer "what happened?" without asking the model.
Everything a run does is persisted here: the run itself, its tasks, each step,
every tool call, the artifacts it produced, checkpoints, and an append-only
event stream. Summaries are derived from events; events are never rewritten
(UPDATE/DELETE on ``events`` is rejected by triggers).

Storage is its own SQLite file (default ``<db_dir>/harness.db``, beside the
shared memory DB) so that:
  - memory (``mneme.db``) stays exactly as it was — this is purely additive;
  - every proxy sharing a DB directory sees the same runs;
  - the ledger has its own connection and lock, independent of the proxy's
    shared memory connection.

Several processes may open one ledger. A run is executed by at most one
process at a time: ``claim()`` takes a lease (owner + heartbeat) atomically,
and ``orphaned_runs()`` only reports runs whose owner is provably dead (same
host, pid gone) or whose heartbeat has gone stale.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

RUN_STATES = (
    "created", "planning", "running", "waiting", "paused",
    "awaiting_approval", "verifying", "completed", "failed", "cancelled",
)
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
# States in which some process is (or should be) actively executing the run.
ACTIVE_STATES = frozenset({"planning", "running", "verifying"})

# Allowed run transitions. Deliberately permissive between the non-terminal
# states — the point is reliable execution and recovery, not ceremony. Leaving a
# terminal state is only possible through retry (failed/cancelled -> created).
_NON_TERMINAL = ("created", "planning", "running", "waiting", "paused",
                 "awaiting_approval", "verifying")
RUN_TRANSITIONS: Dict[str, frozenset] = {
    s: frozenset(set(RUN_STATES) - {s, "created"}) for s in _NON_TERMINAL
}
RUN_TRANSITIONS["completed"] = frozenset()
RUN_TRANSITIONS["failed"] = frozenset({"created"})
RUN_TRANSITIONS["cancelled"] = frozenset({"created"})

TASK_STATES = ("pending", "running", "completed", "failed", "skipped", "cancelled")
STEP_STATES = ("running", "completed", "failed", "interrupted", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    parent_run_id   TEXT DEFAULT '',
    session_id      TEXT DEFAULT '',
    goal            TEXT NOT NULL,
    status          TEXT NOT NULL,
    profile         TEXT DEFAULT '',
    model           TEXT DEFAULT '',
    workspace       TEXT DEFAULT '',
    plan            TEXT DEFAULT '{}',
    budget          TEXT DEFAULT '{}',
    usage           TEXT DEFAULT '{}',
    permissions     TEXT DEFAULT '{}',
    approval_state  TEXT DEFAULT '',
    control         TEXT DEFAULT '',     -- pending control request: pause | cancel | ''
    current_task_id TEXT DEFAULT '',
    current_step_id TEXT DEFAULT '',
    result          TEXT DEFAULT '',
    error           TEXT DEFAULT '',
    attempt         INTEGER DEFAULT 1,
    owner           TEXT DEFAULT '',     -- host:pid:engine of the executing process
    heartbeat_at    REAL DEFAULT 0,      -- epoch seconds of the owner's last heartbeat
    meta            TEXT DEFAULT '{}',
    created_by      TEXT DEFAULT 'user',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    title        TEXT NOT NULL,
    instructions TEXT DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER DEFAULT 0,
    result       TEXT DEFAULT '',
    error        TEXT DEFAULT '',
    meta         TEXT DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    started_at   TEXT DEFAULT '',
    finished_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS steps (
    step_id     TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    task_id     TEXT DEFAULT '',
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'model',
    status      TEXT NOT NULL DEFAULT 'running',
    input       TEXT DEFAULT '{}',
    output      TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    meta        TEXT DEFAULT '{}',
    started_at  TEXT NOT NULL,
    finished_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id    TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    task_id    TEXT DEFAULT '',
    step_id    TEXT DEFAULT '',
    tool       TEXT NOT NULL,
    args       TEXT DEFAULT '{}',
    result     TEXT DEFAULT '',
    status     TEXT DEFAULT '',
    elapsed_ms INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    task_id    TEXT DEFAULT '',
    step_id    TEXT DEFAULT '',
    type       TEXT NOT NULL,
    actor      TEXT DEFAULT 'harness',
    data       TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    task_id     TEXT DEFAULT '',
    step_id     TEXT DEFAULT '',
    path        TEXT NOT NULL,
    kind        TEXT DEFAULT 'file',
    sha256      TEXT DEFAULT '',
    size        INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    provenance  TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    reason        TEXT DEFAULT '',
    state         TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_status   ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_parent   ON runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_run     ON tasks(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_steps_run     ON steps(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_calls_run     ON tool_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run    ON events(run_id, event_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_ckpt_run      ON checkpoints(run_id, seq);

-- The event stream is the authoritative history: append-only.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""

_JSON_COLS = {
    "runs": ("plan", "budget", "usage", "permissions", "meta"),
    "tasks": ("meta",),
    "steps": ("input", "meta"),
    "tool_calls": ("args",),
    "events": ("data",),
    "artifacts": ("provenance",),
    "checkpoints": ("state",),
}

_RUN_UPDATABLE = {
    "parent_run_id", "session_id", "profile", "model", "workspace", "plan", "budget",
    "usage", "permissions", "approval_state", "control", "current_task_id",
    "current_step_id", "result", "error", "attempt", "owner", "heartbeat_at", "meta",
}
_TASK_UPDATABLE = {"title", "instructions", "status", "attempts", "result", "error",
                   "meta", "started_at", "finished_at"}


class LedgerError(Exception):
    """Base error for ledger operations."""


class InvalidTransition(LedgerError):
    """A run was asked to move to a state it cannot reach from its current one."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """Time-ordered, collision-resistant id: <prefix>_<ms-hex><random-hex>."""
    return f"{prefix}_{int(time.time() * 1000):x}{secrets.token_hex(4)}"


def process_owner(tag: str = "") -> str:
    """Owner string for this process: host:pid[:tag]."""
    base = f"{socket.gethostname()}:{os.getpid()}"
    return f"{base}:{tag}" if tag else base


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def owner_is_dead(owner: str) -> bool:
    """True only when we can PROVE the owner process is gone (same host, pid dead)."""
    if not owner:
        return True
    parts = owner.split(":")
    if len(parts) < 2 or parts[0] != socket.gethostname():
        return False
    try:
        return not _pid_alive(int(parts[1]))
    except ValueError:
        return False


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


class Ledger:
    """Thread-safe access to one harness ledger file."""

    def __init__(self, path: str):
        self.path = os.path.abspath(os.path.expanduser(path))
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO harness_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._db.commit()

    # ── low level ────────────────────────────────────────────────────────

    def close(self):
        with self._lock:
            self._db.close()

    def _write(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, tuple(params))
            self._db.commit()
            return cur

    def _one(self, sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchone()

    def _all(self, sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchall()

    @staticmethod
    def _decode(table: str, row: Optional[sqlite3.Row]) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        for col in _JSON_COLS.get(table, ()):
            if col in d and isinstance(d[col], str):
                try:
                    d[col] = json.loads(d[col]) if d[col] else {}
                except ValueError:
                    pass
        return d

    @staticmethod
    def _enc(v) -> str:
        return json.dumps(v if v is not None else {}, default=str)

    def schema_version(self) -> int:
        row = self._one("SELECT value FROM harness_meta WHERE key='schema_version'")
        return int(row["value"]) if row else 0

    # ── events ───────────────────────────────────────────────────────────

    def emit(self, run_id: str, type: str, data: Optional[dict] = None, *,
             task_id: str = "", step_id: str = "", actor: str = "harness") -> int:
        cur = self._write(
            "INSERT INTO events (run_id, task_id, step_id, type, actor, data, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, task_id or "", step_id or "", type, actor, self._enc(data), now_iso()),
        )
        return int(cur.lastrowid)

    def events(self, run_id: str, after_id: int = 0, limit: int = 1000,
               types: Optional[Iterable[str]] = None) -> List[dict]:
        sql = "SELECT * FROM events WHERE run_id=? AND event_id>?"
        params: list = [run_id, int(after_id)]
        if types:
            types = list(types)
            sql += f" AND type IN ({','.join('?' for _ in types)})"
            params += types
        sql += " ORDER BY event_id LIMIT ?"
        params.append(int(limit))
        return [self._decode("events", r) for r in self._all(sql, params)]

    # ── runs ─────────────────────────────────────────────────────────────

    def create_run(self, goal: str, tasks: Optional[List] = None, *, budget: Optional[dict] = None,
                   profile: str = "", model: str = "", session_id: str = "",
                   parent_run_id: str = "", permissions: Optional[dict] = None,
                   meta: Optional[dict] = None, workspace: str = "",
                   created_by: str = "user", defer_plan: bool = False) -> dict:
        """Create a run. With defer_plan the run starts with NO tasks and a
        pending plan; the engine's planner produces the tasks on first execution."""
        goal = (goal or "").strip()
        if not goal:
            raise LedgerError("a run needs a goal")
        run_id = new_id("run")
        ts = now_iso()
        self._write(
            "INSERT INTO runs (run_id, parent_run_id, session_id, goal, status, profile, model, "
            "workspace, plan, budget, usage, permissions, meta, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, parent_run_id or "", session_id or "", goal, "created", profile or "",
             model or "", workspace or "", self._enc({}), self._enc(budget or {}),
             self._enc({}), self._enc(permissions or {}), self._enc(meta or {}),
             created_by, ts, ts),
        )
        self.emit(run_id, "run_created", {"goal": goal, "budget": budget or {},
                                          "profile": profile, "parent_run_id": parent_run_id},
                  actor=created_by)
        if defer_plan:
            self.update_run(run_id, plan={"version": 0, "source": "pending", "tasks": []})
            return self.get_run(run_id)
        specs = self._normalize_tasks(goal, tasks)
        for i, spec in enumerate(specs):
            self.add_task(run_id, spec["title"], spec.get("instructions", ""), seq=i,
                          meta=spec.get("meta"))
        plan = {"version": 1, "source": "caller" if tasks else "default",
                "tasks": [s["title"] for s in specs]}
        self.update_run(run_id, plan=plan)
        self.emit(run_id, "plan_created", plan, actor=created_by)
        return self.get_run(run_id)

    @staticmethod
    def _normalize_tasks(goal: str, tasks: Optional[List]) -> List[dict]:
        if not tasks:
            return [{"title": goal[:200], "instructions": goal}]
        return Ledger.normalize_task_specs(tasks)

    @staticmethod
    def normalize_task_specs(tasks: List) -> List[dict]:
        """Validate task specs: strings, or {title|instructions, instructions?, verify?, meta?}.
        A `verify` spec is validated and stored as meta['verify']."""
        from mneme.harness.verify import VerifySpecError, normalize as _norm_verify
        out = []
        for t in tasks:
            if isinstance(t, str):
                t = t.strip()
                if t:
                    out.append({"title": t[:200], "instructions": t})
            elif isinstance(t, dict):
                title = str(t.get("title") or t.get("instructions") or "").strip()
                if not title:
                    raise LedgerError(f"task needs a title or instructions: {t!r}")
                meta = dict(t.get("meta") or {})
                if t.get("requires_approval"):
                    meta["requires_approval"] = True
                if t.get("verify") is not None:
                    try:
                        meta["verify"] = _norm_verify(t["verify"])
                    except VerifySpecError as e:
                        raise LedgerError(f"task {title[:60]!r}: {e}")
                out.append({"title": title[:200],
                            "instructions": str(t.get("instructions") or title),
                            "meta": meta})
            else:
                raise LedgerError(f"task must be a string or an object, got {type(t).__name__}")
        if not out:
            raise LedgerError("tasks list is empty")
        return out

    def get_run(self, run_id: str) -> Optional[dict]:
        return self._decode("runs", self._one("SELECT * FROM runs WHERE run_id=?", (run_id,)))

    def require_run(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        if run is None:
            raise LedgerError(f"no such run: {run_id}")
        return run

    def list_runs(self, status: Optional[Iterable[str]] = None, limit: int = 50,
                  offset: int = 0, parent_run_id: Optional[str] = None) -> List[dict]:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list = []
        if status:
            status = [status] if isinstance(status, str) else list(status)
            sql += f" AND status IN ({','.join('?' for _ in status)})"
            params += status
        if parent_run_id is not None:
            sql += " AND parent_run_id=?"
            params.append(parent_run_id)
        sql += " ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        return [self._decode("runs", r) for r in self._all(sql, params)]

    def update_run(self, run_id: str, **fields) -> None:
        bad = set(fields) - _RUN_UPDATABLE
        if bad:
            raise LedgerError(f"not updatable on runs: {sorted(bad)} (use transition() for status)")
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(self._enc(v) if k in _JSON_COLS["runs"] else v)
        cols.append("updated_at=?")
        vals.append(now_iso())
        vals.append(run_id)
        self._write(f"UPDATE runs SET {', '.join(cols)} WHERE run_id=?", vals)

    def transition(self, run_id: str, new_status: str, *, event: Optional[str] = None,
                   data: Optional[dict] = None, actor: str = "harness",
                   expect: Optional[Iterable[str]] = None, **fields) -> dict:
        """Move a run to `new_status`, validating the state machine, and record it.

        `expect` (optional) additionally requires the current status to be one of
        the given states — used for compare-and-set style control operations.
        """
        if new_status not in RUN_STATES:
            raise InvalidTransition(f"unknown run state {new_status!r}")
        with self._lock:
            run = self.require_run(run_id)
            cur = run["status"]
            if expect is not None and cur not in set(expect):
                raise InvalidTransition(f"run {run_id} is {cur}, expected one of {sorted(expect)}")
            if new_status != cur and new_status not in RUN_TRANSITIONS[cur]:
                raise InvalidTransition(f"run {run_id}: {cur} -> {new_status} is not allowed")
            sets = ["status=?", "updated_at=?"]
            vals: list = [new_status, now_iso()]
            for k, v in fields.items():
                if k not in _RUN_UPDATABLE:
                    raise LedgerError(f"not updatable on runs: {k}")
                sets.append(f"{k}=?")
                vals.append(self._enc(v) if k in _JSON_COLS["runs"] else v)
            vals.append(run_id)
            self._db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", vals)
            self._db.commit()
            payload = {"from": cur, "to": new_status}
            if data:
                payload.update(data)
            self.emit(run_id, event or f"run_{new_status}", payload, actor=actor)
            return self.get_run(run_id)

    def request_control(self, run_id: str, action: str, actor: str = "user") -> None:
        if action not in ("pause", "cancel", ""):
            raise LedgerError(f"unknown control action {action!r}")
        self.update_run(run_id, control=action)
        if action:
            self.emit(run_id, f"{action}_requested", {}, actor=actor)

    # ── ownership (one executor per run, across processes) ───────────────

    def claim(self, run_id: str, owner: str, lease_seconds: float) -> bool:
        """Atomically take ownership of a run. Succeeds when the run is unowned,
        already ours, its owner is provably dead, or its heartbeat is stale."""
        with self._lock:
            row = self._one("SELECT owner, heartbeat_at FROM runs WHERE run_id=?", (run_id,))
            if row is None:
                return False
            prev = row["owner"] or ""
            stale = (time.time() - float(row["heartbeat_at"] or 0)) > lease_seconds
            if prev and prev != owner and not stale and not owner_is_dead(prev):
                return False
            cur = self._db.execute(
                "UPDATE runs SET owner=?, heartbeat_at=?, updated_at=? "
                "WHERE run_id=? AND owner=?",
                (owner, time.time(), now_iso(), run_id, prev),
            )
            self._db.commit()
            return cur.rowcount == 1

    def heartbeat(self, run_id: str, owner: str) -> bool:
        cur = self._write("UPDATE runs SET heartbeat_at=? WHERE run_id=? AND owner=?",
                          (time.time(), run_id, owner))
        return cur.rowcount == 1

    def release(self, run_id: str, owner: str) -> None:
        self._write("UPDATE runs SET owner='', heartbeat_at=0 WHERE run_id=? AND owner=?",
                    (run_id, owner))

    def orphaned_runs(self, lease_seconds: float, exclude_owner: str = "") -> List[dict]:
        """Runs left in an active state by a process that is gone (crash/restart)."""
        out = []
        now = time.time()
        for run in self.list_runs(status=ACTIVE_STATES, limit=10_000):
            owner = run.get("owner") or ""
            if owner and owner == exclude_owner:
                continue
            stale = (now - float(run.get("heartbeat_at") or 0)) > lease_seconds
            if not owner or owner_is_dead(owner) or stale:
                out.append(run)
        return out

    # ── tasks ────────────────────────────────────────────────────────────

    def add_task(self, run_id: str, title: str, instructions: str = "", *,
                 seq: Optional[int] = None, meta: Optional[dict] = None) -> dict:
        if seq is None:
            row = self._one("SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM tasks WHERE run_id=?", (run_id,))
            seq = int(row["n"])
        task_id = new_id("task")
        ts = now_iso()
        self._write(
            "INSERT INTO tasks (task_id, run_id, seq, title, instructions, status, meta, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, run_id, seq, title, instructions or title, "pending", self._enc(meta), ts, ts),
        )
        self.emit(run_id, "task_created", {"title": title, "seq": seq}, task_id=task_id)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Optional[dict]:
        return self._decode("tasks", self._one("SELECT * FROM tasks WHERE task_id=?", (task_id,)))

    def list_tasks(self, run_id: str) -> List[dict]:
        return [self._decode("tasks", r) for r in
                self._all("SELECT * FROM tasks WHERE run_id=? ORDER BY seq", (run_id,))]

    def update_task(self, task_id: str, *, event: Optional[str] = None,
                    data: Optional[dict] = None, **fields) -> dict:
        bad = set(fields) - _TASK_UPDATABLE
        if bad:
            raise LedgerError(f"not updatable on tasks: {sorted(bad)}")
        if "status" in fields and fields["status"] not in TASK_STATES:
            raise LedgerError(f"unknown task state {fields['status']!r}")
        task = self.get_task(task_id)
        if task is None:
            raise LedgerError(f"no such task: {task_id}")
        if fields:
            cols, vals = [], []
            for k, v in fields.items():
                cols.append(f"{k}=?")
                vals.append(self._enc(v) if k in _JSON_COLS["tasks"] else v)
            cols.append("updated_at=?")
            vals += [now_iso(), task_id]
            self._write(f"UPDATE tasks SET {', '.join(cols)} WHERE task_id=?", vals)
        if event:
            self.emit(task["run_id"], event, data or {}, task_id=task_id)
        return self.get_task(task_id)

    # ── steps ────────────────────────────────────────────────────────────

    def start_step(self, run_id: str, task_id: str = "", kind: str = "model",
                   input: Optional[dict] = None) -> dict:
        row = self._one("SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM steps WHERE run_id=?", (run_id,))
        step_id = new_id("step")
        self._write(
            "INSERT INTO steps (step_id, run_id, task_id, seq, kind, status, input, started_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (step_id, run_id, task_id or "", int(row["n"]), kind, "running",
             self._enc(input), now_iso()),
        )
        self.emit(run_id, "step_started", {"kind": kind}, task_id=task_id, step_id=step_id)
        return self.get_step(step_id)

    def finish_step(self, step_id: str, status: str, *, output: str = "", error: str = "",
                    meta: Optional[dict] = None, event: Optional[str] = None) -> dict:
        if status not in STEP_STATES:
            raise LedgerError(f"unknown step state {status!r}")
        step = self.get_step(step_id)
        if step is None:
            raise LedgerError(f"no such step: {step_id}")
        self._write(
            "UPDATE steps SET status=?, output=?, error=?, meta=?, finished_at=? WHERE step_id=?",
            (status, output or "", error or "", self._enc(meta), now_iso(), step_id),
        )
        self.emit(step["run_id"], event or f"step_{status}",
                  {"error": error} if error else {}, task_id=step["task_id"], step_id=step_id)
        return self.get_step(step_id)

    def update_step_meta(self, step_id: str, extra: dict) -> None:
        step = self.get_step(step_id)
        if step is None:
            raise LedgerError(f"no such step: {step_id}")
        self._write("UPDATE steps SET meta=? WHERE step_id=?",
                    (self._enc({**(step.get("meta") or {}), **extra}), step_id))

    def get_step(self, step_id: str) -> Optional[dict]:
        return self._decode("steps", self._one("SELECT * FROM steps WHERE step_id=?", (step_id,)))

    def list_steps(self, run_id: str, task_id: Optional[str] = None) -> List[dict]:
        if task_id:
            rows = self._all("SELECT * FROM steps WHERE run_id=? AND task_id=? ORDER BY seq",
                             (run_id, task_id))
        else:
            rows = self._all("SELECT * FROM steps WHERE run_id=? ORDER BY seq", (run_id,))
        return [self._decode("steps", r) for r in rows]

    # ── tool calls ───────────────────────────────────────────────────────

    def record_tool_call(self, run_id: str, tool: str, args: Optional[dict] = None, *,
                         result: str = "", status: str = "", elapsed_ms: int = 0,
                         task_id: str = "", step_id: str = "") -> dict:
        call_id = new_id("call")
        self._write(
            "INSERT INTO tool_calls (call_id, run_id, task_id, step_id, tool, args, result, "
            "status, elapsed_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (call_id, run_id, task_id or "", step_id or "", tool, self._enc(args),
             result or "", status or "", int(elapsed_ms or 0), now_iso()),
        )
        self.emit(run_id, "tool_failed" if status == "failure" else "tool_completed",
                  {"tool": tool, "call_id": call_id, "status": status},
                  task_id=task_id, step_id=step_id)
        return self._decode("tool_calls", self._one("SELECT * FROM tool_calls WHERE call_id=?", (call_id,)))

    def list_tool_calls(self, run_id: str) -> List[dict]:
        return [self._decode("tool_calls", r) for r in
                self._all("SELECT * FROM tool_calls WHERE run_id=? ORDER BY created_at, call_id", (run_id,))]

    # ── artifacts ────────────────────────────────────────────────────────

    def add_artifact(self, run_id: str, path: str, *, kind: str = "file", description: str = "",
                     task_id: str = "", step_id: str = "", provenance: Optional[dict] = None) -> dict:
        full = os.path.abspath(os.path.expanduser(path))
        sha, size = "", 0
        if os.path.isfile(full):
            sha, size = sha256_file(full), os.path.getsize(full)
        artifact_id = new_id("art")
        prov = {"run_id": run_id, "task_id": task_id, "step_id": step_id}
        prov.update(provenance or {})
        self._write(
            "INSERT INTO artifacts (artifact_id, run_id, task_id, step_id, path, kind, sha256, size, "
            "description, provenance, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (artifact_id, run_id, task_id or "", step_id or "", full, kind, sha, size,
             description or "", self._enc(prov), now_iso()),
        )
        self.emit(run_id, "artifact_created", {"artifact_id": artifact_id, "path": full,
                                               "kind": kind, "sha256": sha},
                  task_id=task_id, step_id=step_id)
        return self._decode("artifacts", self._one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)))

    def list_artifacts(self, run_id: str) -> List[dict]:
        return [self._decode("artifacts", r) for r in
                self._all("SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at, artifact_id", (run_id,))]

    # ── checkpoints ──────────────────────────────────────────────────────

    def create_checkpoint(self, run_id: str, state: dict, reason: str = "") -> dict:
        row = self._one("SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM checkpoints WHERE run_id=?", (run_id,))
        cp_id = new_id("ckpt")
        seq = int(row["n"])
        self._write(
            "INSERT INTO checkpoints (checkpoint_id, run_id, seq, reason, state, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (cp_id, run_id, seq, reason or "", self._enc(state), now_iso()),
        )
        self.emit(run_id, "checkpoint_created", {"checkpoint_id": cp_id, "seq": seq, "reason": reason})
        return self.get_checkpoint(cp_id)

    def get_checkpoint(self, checkpoint_id: str) -> Optional[dict]:
        return self._decode("checkpoints", self._one(
            "SELECT * FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)))

    def latest_checkpoint(self, run_id: str) -> Optional[dict]:
        return self._decode("checkpoints", self._one(
            "SELECT * FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)))

    def list_checkpoints(self, run_id: str) -> List[dict]:
        rows = self._all("SELECT checkpoint_id, run_id, seq, reason, created_at FROM checkpoints "
                         "WHERE run_id=? ORDER BY seq", (run_id,))
        return [dict(r) for r in rows]

    # ── aggregate view ───────────────────────────────────────────────────

    def run_detail(self, run_id: str, include_events: bool = False) -> Optional[dict]:
        run = self.get_run(run_id)
        if run is None:
            return None
        detail = {
            "run": run,
            "tasks": self.list_tasks(run_id),
            "steps": self.list_steps(run_id),
            "tool_calls": self.list_tool_calls(run_id),
            "artifacts": self.list_artifacts(run_id),
            "checkpoints": self.list_checkpoints(run_id),
            "children": [r["run_id"] for r in self.list_runs(parent_run_id=run_id, limit=1000)],
        }
        if include_events:
            detail["events"] = self.events(run_id)
        return detail
