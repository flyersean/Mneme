"""Agent profiles — reusable behaviour/capability selection for a run.

A profile does not duplicate the harness; it selects within it:

    description        what the profile is for
    skills             skills pinned into every step's capability context
    grant              tool permission levels (None/absent = unrestricted, like chat)
    budget             default budget (explicit run budget keys win)
    plan               default planning behaviour (true/false; absent = auto)
    approve_each_task  every task waits for approval
    model              informational (a proxy serves one model; pick the proxy per profile)

Profiles are versioned (profile_versions keeps every spec) and can be changed
through an L3 evolution proposal.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from mneme.harness.ledger import Ledger, LedgerError, now_iso

SPEC_KEYS = {"description", "skills", "grant", "budget", "plan", "approve_each_task", "model"}

BUILTIN_PROFILES: Dict[str, dict] = {
    "default": {"description": "Unrestricted — same tool power as a chat turn."},
    "researcher": {"description": "Web research and document analysis; no shell.",
                   "grant": ["read-only", "normal", "network", "filesystem-write"],
                   "budget": {"max_steps": 40}},
    "coder": {"description": "Code changes: files, shell, tests.",
              "grant": ["read-only", "normal", "network", "filesystem-write", "shell"],
              "budget": {"max_steps": 80, "max_failures": 4}},
    "reviewer": {"description": "Read-only review and analysis.", "grant": ["read-only", "normal"],
                 "plan": False},
    "cautious": {"description": "Every task needs human approval before it runs.",
                 "approve_each_task": True},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    name       TEXT PRIMARY KEY,
    spec       TEXT NOT NULL,
    version    INTEGER DEFAULT 1,
    created_by TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    spec       TEXT NOT NULL,
    actor      TEXT DEFAULT '',
    reason     TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def validate(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise LedgerError("a profile spec must be an object")
    bad = set(spec) - SPEC_KEYS
    if bad:
        raise LedgerError(f"unknown profile keys: {sorted(bad)} (known: {', '.join(sorted(SPEC_KEYS))})")
    out = dict(spec)
    if out.get("grant") is not None:
        from mneme.harness.capabilities import normalize_grant
        try:
            out["grant"] = sorted(normalize_grant(out["grant"]))
        except ValueError as e:
            raise LedgerError(str(e))
    if out.get("budget") is not None:
        from mneme.harness.engine import merge_budget
        merge_budget(out["budget"])  # validates keys/values
    if out.get("skills") is not None and not isinstance(out["skills"], list):
        raise LedgerError("profile skills must be a list")
    return out


class ProfileStore:
    def __init__(self, ledger: Ledger, seed_builtins: bool = True):
        self.ledger = ledger
        with ledger._lock:
            ledger._db.executescript(_SCHEMA)
            ledger._db.commit()
        if seed_builtins:
            for name, spec in BUILTIN_PROFILES.items():
                if self.get(name) is None:
                    self.upsert(name, spec, actor="builtin", reason="shipped default")

    def get(self, name: str) -> Optional[dict]:
        row = self.ledger._one("SELECT * FROM profiles WHERE name=?", (name,))
        if row is None:
            return None
        d = dict(row)
        d["spec"] = json.loads(d["spec"])
        return d

    def require(self, name: str) -> dict:
        p = self.get(name)
        if p is None:
            raise LedgerError(f"no such profile: {name}")
        return p

    def list(self) -> List[dict]:
        return [{**dict(r), "spec": json.loads(r["spec"])}
                for r in self.ledger._all("SELECT * FROM profiles ORDER BY name")]

    def history(self, name: str) -> List[dict]:
        return [{**dict(r), "spec": json.loads(r["spec"])} for r in
                self.ledger._all("SELECT * FROM profile_versions WHERE name=? ORDER BY id", (name,))]

    def upsert(self, name: str, spec: dict, actor: str = "user", reason: str = "") -> dict:
        name = (name or "").strip().lower()
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise LedgerError(f"invalid profile name {name!r}")
        spec = validate(spec)
        cur = self.get(name)
        if cur is not None and cur["spec"] == spec:
            return cur
        version = (cur["version"] + 1) if cur else 1
        ts = now_iso()
        self.ledger._write(
            "INSERT INTO profiles (name, spec, version, created_by, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET spec=excluded.spec, version=excluded.version, updated_at=excluded.updated_at",
            (name, json.dumps(spec), version, actor, ts))
        self.ledger._write("INSERT INTO profile_versions (name, version, spec, actor, reason, created_at) "
                           "VALUES (?,?,?,?,?,?)", (name, version, json.dumps(spec), actor, reason, ts))
        return self.get(name)

    def apply_to(self, name: str, *, budget: Optional[dict], permissions: Optional[dict],
                 meta: Optional[dict], plan: Optional[bool]):
        """Merge a profile under a run's explicit settings (explicit always wins)."""
        spec = self.require(name)["spec"]
        budget = {**(spec.get("budget") or {}), **(budget or {})}
        permissions = dict(permissions or {})
        if permissions.get("grant") is None and spec.get("grant") is not None:
            permissions["grant"] = spec["grant"]
        meta = dict(meta or {})
        if spec.get("skills"):
            meta.setdefault("skills", spec["skills"])
        if spec.get("approve_each_task"):
            meta.setdefault("approve_each_task", True)
        meta["profile_version"] = self.require(name)["version"]
        if plan is None and spec.get("plan") is not None:
            plan = bool(spec["plan"])
        return budget, permissions, meta, plan
