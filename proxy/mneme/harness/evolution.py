"""Controlled self-improvement — proposals, levels, versioned apply, rollback.

    RUN → OBSERVE → IDENTIFY → PROPOSE → EVALUATE (tests) → APPROVE → APPLY → VERIFY → RECORD

Every change to the system is a *proposal* with a kind, a target, the new
content, the reason, the evidence (run ids), optional tests (verify checks),
and a level:

    L1 knowledge            auto-applied (facts, failure observations, lessons)
    L2 strategy / skill     auto-applied, versioned (previous version kept)
    L3 instruction / profile / config   tests must pass + explicit approval; previous kept
    L4 code                 applied ONLY to a git branch (worktree), tested there;
                            activation (merge) is a human decision — never automatic

Apply is transactional: the previous content is captured before applying, the
tests run again after applying, and a failure rolls back and records the
attempt. Nothing is overwritten without a way back; failed improvements stay
as history ("failed improvement becomes experience rather than permanent damage").
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Callable, Dict, List, Optional

from mneme.harness.ledger import Ledger, LedgerError, new_id, now_iso
from mneme.harness import verify as _verify

LEVELS = {"knowledge": 1, "strategy": 2, "skill": 2, "instruction": 3, "profile": 3,
          "config": 3, "code": 4}
STATUSES = ("proposed", "tested", "test_failed", "applied", "branch_ready", "rejected",
            "rolled_back", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    target      TEXT NOT NULL,
    level       INTEGER NOT NULL,
    status      TEXT NOT NULL,
    content     TEXT NOT NULL,
    previous    TEXT,                -- content before apply (NULL = did not exist)
    reason      TEXT DEFAULT '',
    evidence    TEXT DEFAULT '[]',
    tests       TEXT DEFAULT '[]',
    result      TEXT DEFAULT '{}',
    created_by  TEXT DEFAULT '',
    decided_by  TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, created_at);
CREATE TABLE IF NOT EXISTS evolution_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT DEFAULT '',
    data        TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS evolution_log_no_update BEFORE UPDATE ON evolution_log
BEGIN SELECT RAISE(ABORT, 'evolution_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evolution_log_no_delete BEFORE DELETE ON evolution_log
BEGIN SELECT RAISE(ABORT, 'evolution_log is append-only'); END;
"""


class Applier:
    """How one kind of object is read and changed. Subclasses override."""
    def read(self, target: str) -> Optional[str]:
        return None

    def apply(self, target: str, content: str, actor: str) -> None:
        raise NotImplementedError

    def restore(self, target: str, previous: Optional[str], actor: str) -> None:
        if previous is not None:
            self.apply(target, previous, actor)


class KnowledgeApplier(Applier):
    """L1: the proposal record IS the knowledge; optionally also sent to memory."""
    def __init__(self, sink: Optional[Callable[[str, str], None]] = None):
        self.sink = sink

    def apply(self, target, content, actor):
        if self.sink is not None:
            self.sink(target, content)

    def restore(self, target, previous, actor):
        pass  # knowledge is append-only; a wrong note is superseded by a new one


class SkillApplier(Applier):
    """Content: JSON {description, body, tools?, requires?, verify?, failure_modes?, tags?}."""
    def __init__(self, registry):
        self.registry = registry

    def read(self, target):
        s = self.registry.get(target)
        if s is None:
            return None
        return json.dumps({k: s[k] for k in ("description", "body", "tools", "requires", "strategies",
                                             "verify", "failure_modes", "tags")})

    def apply(self, target, content, actor):
        d = json.loads(content)
        extra = {k: d[k] for k in ("tools", "requires", "strategies", "verify", "failure_modes", "tags") if k in d}
        self.registry.upsert(target, d.get("description", ""), d.get("body", ""), actor=actor,
                             reason="evolution", source=actor, **extra)

    def restore(self, target, previous, actor):
        if previous is None:
            self.registry.set_active(target, False)  # did not exist before: deactivate, keep history
        else:
            self.apply(target, previous, actor)


class CallableApplier(Applier):
    """Wraps read/write callables — e.g. instructions (_load_instruction / save_instruction)."""
    def __init__(self, read: Callable[[str], Optional[str]], write: Callable[[str, str], None]):
        self._read, self._write = read, write

    def read(self, target):
        return self._read(target)

    def apply(self, target, content, actor):
        self._write(target, content)


class CodeApplier(Applier):
    """L4: apply a unified diff on a NEW branch in a separate git worktree. The main
    working tree is never touched; merging is left to a human."""
    def __init__(self, repo_root: str, worktrees_dir: Optional[str] = None):
        self.repo = os.path.abspath(repo_root)
        self.worktrees = worktrees_dir or os.path.join(tempfile.gettempdir(), "mneme-evolve")

    def _git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True, timeout=120)

    def worktree(self, pid: str) -> str:
        return os.path.join(self.worktrees, pid)

    def prepare(self, pid: str, diff: str) -> str:
        wt = self.worktree(pid)
        if os.path.isdir(wt):
            return wt
        os.makedirs(self.worktrees, exist_ok=True)
        r = self._git("worktree", "add", "-b", f"evolve/{pid}", wt, "HEAD")
        if r.returncode != 0:
            raise LedgerError(f"git worktree add failed: {r.stderr.strip()[:300]}")
        patch = os.path.join(wt, ".mneme-evolve.patch")
        with open(patch, "w", encoding="utf-8") as f:
            f.write(diff if diff.endswith("\n") else diff + "\n")
        r = self._git("apply", "--whitespace=nowarn", patch, cwd=wt)
        os.remove(patch)
        if r.returncode != 0:
            self.discard(pid)
            raise LedgerError(f"patch does not apply: {r.stderr.strip()[:300]}")
        self._git("add", "-A", cwd=wt)
        r = self._git("-c", "user.name=mneme-evolution", "-c", "user.email=mneme@localhost",
                      "commit", "-qm", f"evolve: proposal {pid}", cwd=wt)
        if r.returncode != 0:
            self.discard(pid)
            raise LedgerError(f"commit failed: {(r.stderr or r.stdout).strip()[:300]}")
        return wt

    def discard(self, pid: str) -> None:
        self._git("worktree", "remove", "--force", self.worktree(pid))
        shutil.rmtree(self.worktree(pid), ignore_errors=True)
        self._git("branch", "-D", f"evolve/{pid}")

    def apply(self, target, content, actor):
        raise LedgerError("code proposals are applied via prepare() on a branch, never in place")


class Evolution:
    def __init__(self, ledger: Ledger, appliers: Optional[Dict[str, Applier]] = None,
                 auto_apply_max_level: int = 2, log: Optional[Callable[[str], None]] = None):
        self.ledger = ledger
        self.appliers: Dict[str, Applier] = dict(appliers or {})
        self.appliers.setdefault("knowledge", KnowledgeApplier())
        self.auto_apply_max_level = int(auto_apply_max_level)
        self._log = log or (lambda m: None)
        with ledger._lock:
            ledger._db.executescript(_SCHEMA)
            ledger._db.commit()

    # ── records ──────────────────────────────────────────────────────────

    def _decode(self, row) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        for k in ("evidence", "tests", "result"):
            try:
                d[k] = json.loads(d.get(k) or ("[]" if k != "result" else "{}"))
            except ValueError:
                pass
        return d

    def get(self, pid: str) -> Optional[dict]:
        return self._decode(self.ledger._one("SELECT * FROM proposals WHERE proposal_id=?", (pid,)))

    def require(self, pid: str) -> dict:
        p = self.get(pid)
        if p is None:
            raise LedgerError(f"no such proposal: {pid}")
        return p

    def list(self, status: Optional[str] = None, kind: Optional[str] = None, limit: int = 100) -> List[dict]:
        sql, params = "SELECT * FROM proposals WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [self._decode(r) for r in self.ledger._all(sql, params)]

    def history(self, pid: str) -> List[dict]:
        rows = self.ledger._all("SELECT * FROM evolution_log WHERE proposal_id=? ORDER BY id", (pid,))
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    def changes_to(self, kind: str, target: str) -> List[dict]:
        """Why is this object the way it is? Every proposal that touched it, newest first."""
        rows = self.ledger._all("SELECT * FROM proposals WHERE kind=? AND target=? ORDER BY created_at DESC",
                                (kind, target))
        return [self._decode(r) for r in rows]

    def _set(self, pid: str, status: str, actor: str, action: str, data: Optional[dict] = None, **fields):
        cols, vals = ["status=?", "updated_at=?"], [status, now_iso()]
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(json.dumps(v) if k in ("result", "evidence", "tests") else v)
        vals.append(pid)
        self.ledger._write(f"UPDATE proposals SET {', '.join(cols)} WHERE proposal_id=?", vals)
        self.ledger._write("INSERT INTO evolution_log (proposal_id, action, actor, data, created_at) "
                           "VALUES (?,?,?,?,?)", (pid, action, actor, json.dumps(data or {}), now_iso()))

    # ── lifecycle ────────────────────────────────────────────────────────

    def propose(self, kind: str, target: str, content: str, *, reason: str = "",
                evidence: Optional[List[str]] = None, tests=None, created_by: str = "model",
                level: Optional[int] = None) -> dict:
        if kind not in LEVELS:
            raise LedgerError(f"unknown proposal kind {kind!r} (one of {', '.join(LEVELS)})")
        if kind not in self.appliers:
            raise LedgerError(f"no applier configured for {kind!r} on this harness")
        if not (target or "").strip() or content is None:
            raise LedgerError("a proposal needs a target and content")
        lvl = max(LEVELS[kind], int(level or 0))   # a proposal may raise its level, never lower it
        try:
            tests = _verify.normalize(tests)
        except _verify.VerifySpecError as e:
            raise LedgerError(f"bad tests: {e}")
        pid = new_id("prop")
        ts = now_iso()
        self.ledger._write(
            "INSERT INTO proposals (proposal_id, kind, target, level, status, content, reason, evidence, "
            "tests, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, kind, target, lvl, "proposed", content, reason, json.dumps(evidence or []),
             json.dumps(tests), created_by, ts, ts))
        self.ledger._write("INSERT INTO evolution_log (proposal_id, action, actor, data, created_at) "
                           "VALUES (?,?,?,?,?)", (pid, "proposed", created_by,
                                                  json.dumps({"kind": kind, "target": target, "level": lvl}), ts))
        if lvl <= self.auto_apply_max_level:
            return self.approve(pid, actor="auto")
        return self.get(pid)

    def _run_tests(self, p: dict, base_dir: Optional[str]) -> tuple:
        if not p["tests"]:
            return True, []
        return _verify.run_checks(p["tests"], p["content"], base_dir or tempfile.mkdtemp(prefix="mneme-evo-"))

    def test(self, pid: str, actor: str = "harness") -> dict:
        p = self.require(pid)
        if p["status"] not in ("proposed", "tested", "test_failed"):
            raise LedgerError(f"proposal {pid} is {p['status']} — cannot test")
        base = None
        if p["kind"] == "code":
            try:
                base = self.appliers["code"].prepare(pid, p["content"])
            except LedgerError as e:
                self._set(pid, "test_failed", actor, "test_failed", {"error": str(e)}, result={"error": str(e)})
                return self.get(pid)
        ok, detail = self._run_tests(p, base)
        self._set(pid, "tested" if ok else "test_failed", actor, "tested" if ok else "test_failed",
                  {"passed": ok}, result={"tests": detail, "passed": ok, "worktree": base or ""})
        return self.get(pid)

    def approve(self, pid: str, actor: str = "user") -> dict:
        p = self.require(pid)
        if p["status"] not in ("proposed", "tested"):
            raise LedgerError(f"proposal {pid} is {p['status']} — cannot approve")
        if p["level"] >= 3 and actor == "auto":
            raise LedgerError(f"level {p['level']} proposals need an explicit approval")
        if p["level"] >= 3 and p["tests"] and p["status"] != "tested":
            p = self.test(pid, actor=actor)
            if p["status"] != "tested":
                return p
        if p["kind"] == "code":
            if not p["tests"]:
                self._set(pid, "test_failed", actor, "refused", {"error": "code proposals require tests"})
                return self.get(pid)
            wt = (p.get("result") or {}).get("worktree") or self.appliers["code"].worktree(pid)
            self._set(pid, "branch_ready", actor, "branch_ready", {"branch": f"evolve/{pid}", "worktree": wt},
                      decided_by=actor)
            self._log(f"proposal {pid}: code ready on branch evolve/{pid} (merge manually)")
            return self.get(pid)
        applier = self.appliers[p["kind"]]
        previous = applier.read(p["target"])
        try:
            applier.apply(p["target"], p["content"], f"proposal:{pid}")
        except Exception as e:
            self._set(pid, "failed", actor, "apply_failed", {"error": f"{type(e).__name__}: {e}"},
                      previous=previous, decided_by=actor)
            return self.get(pid)
        ok, detail = self._run_tests(p, None)
        if not ok:
            applier.restore(p["target"], previous, f"rollback:{pid}")
            self._set(pid, "rolled_back", actor, "post_apply_verification_failed", {"tests": detail},
                      previous=previous, decided_by=actor, result={"tests": detail, "passed": False})
            return self.get(pid)
        self._set(pid, "applied", actor, "applied", {"target": p["target"]}, previous=previous,
                  decided_by=actor, result={"tests": detail, "passed": True})
        return self.get(pid)

    def reject(self, pid: str, actor: str = "user", reason: str = "") -> dict:
        p = self.require(pid)
        if p["status"] not in ("proposed", "tested", "test_failed", "branch_ready"):
            raise LedgerError(f"proposal {pid} is {p['status']} — cannot reject")
        if p["kind"] == "code":
            self.appliers["code"].discard(pid)
        self._set(pid, "rejected", actor, "rejected", {"reason": reason}, decided_by=actor)
        return self.get(pid)

    def rollback(self, pid: str, actor: str = "user") -> dict:
        p = self.require(pid)
        if p["status"] != "applied":
            raise LedgerError(f"proposal {pid} is {p['status']} — only applied proposals roll back")
        self.appliers[p["kind"]].restore(p["target"], p["previous"], f"rollback:{pid}")
        self._set(pid, "rolled_back", actor, "rolled_back", {})
        return self.get(pid)


# ── run hooks (the observe → propose half of the loop) ──────────────────────

def observe_run(engine, run: dict) -> None:
    """on_finish hook: every failure a run hit becomes an L1 knowledge record."""
    evo = getattr(engine, "evolution", None)
    if evo is None:
        return
    failed = [e for e in engine.ledger.events(run["run_id"], types=["task_failed", "verification_failed"])]
    if not failed and run["status"] == "completed":
        return
    lines = [f"Run goal: {run['goal'][:300]}", f"Outcome: {run['status']}"
             + (f" — {run['error'][:300]}" if run.get("error") else "")]
    for e in failed[:8]:
        d = e["data"] or {}
        if e["type"] == "task_failed":
            lines.append(f"- task {d.get('title', '?')!r} failed [{d.get('category', 'other')}]: "
                         f"{(d.get('error') or '')[:200]}")
    if run["status"] == "completed" and failed:
        lines.append("The run recovered from these failures and completed.")
    evo.propose("knowledge", f"run:{run['run_id']}", "\n".join(lines),
                reason="failure observation", evidence=[run["run_id"]], created_by="harness")
