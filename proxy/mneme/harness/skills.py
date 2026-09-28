"""Skills — reusable, versioned capabilities above individual strategies.

A skill is a named procedure: description, instructions body, the tools and
strategies it relies on, how to verify its work, known failure modes, and
composition (``requires``: other skills). Skills come from ``SKILL.md`` files
(frontmatter + markdown body — the same format as skills/swarm-creation) or
are created/updated at runtime (by the user, a run, or a self-improvement
proposal). Every change is a new version; old versions are kept.

Retrieval is deliberately cheap: lexical overlap on name/description/tags by
default, embedding cosine when an ``embed`` callable is bound. The harness
injects only the top few skills relevant to the current task.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Callable, Dict, Iterable, List, Optional

from mneme.harness.ledger import Ledger, LedgerError, now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    body        TEXT DEFAULT '',
    tools       TEXT DEFAULT '[]',
    requires    TEXT DEFAULT '[]',
    strategies  TEXT DEFAULT '[]',
    verify      TEXT DEFAULT '[]',
    failure_modes TEXT DEFAULT '[]',
    tags        TEXT DEFAULT '[]',
    version     INTEGER DEFAULT 1,
    source      TEXT DEFAULT '',        -- file:<path> | user | model | run:<id> | proposal:<id>
    created_by  TEXT DEFAULT 'user',
    active      INTEGER DEFAULT 1,
    uses        INTEGER DEFAULT 0,
    successes   INTEGER DEFAULT 0,
    failures    INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    snapshot   TEXT NOT NULL,
    actor      TEXT DEFAULT '',
    reason     TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_versions ON skill_versions(name, id);
"""
_LIST_FIELDS = ("tools", "requires", "strategies", "verify", "failure_modes", "tags")
_CONTENT_FIELDS = ("description", "body") + _LIST_FIELDS
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "use", "using", "when",
         "your", "you", "are", "any", "all", "can", "how", "what", "not", "but"}


def parse_skill_md(text: str) -> dict:
    """Split a SKILL.md into frontmatter fields + body."""
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text or "", re.S)
    if m:
        front, body = m.group(1), m.group(2)
        try:
            import yaml
            meta = yaml.safe_load(front) or {}
        except Exception:
            for line in front.splitlines():
                k, _, v = line.partition(":")
                if k.strip() and v.strip():
                    meta[k.strip()] = v.strip()
    if not isinstance(meta, dict):
        meta = {}
    meta["body"] = body.strip()
    return meta


def _tokens(text: str) -> set:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP}


class SkillRegistry:
    def __init__(self, ledger: Ledger, dirs: Iterable[str] = (), embed: Optional[Callable] = None):
        self.ledger = ledger
        self.embed = embed
        with ledger._lock:
            ledger._db.executescript(_SCHEMA)
            ledger._db.commit()
        for d in dirs or ():
            self.load_dir(d)

    # ── storage ──────────────────────────────────────────────────────────

    def _decode(self, row) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        for f in _LIST_FIELDS:
            try:
                d[f] = json.loads(d.get(f) or "[]")
            except ValueError:
                d[f] = []
        return d

    def get(self, name: str) -> Optional[dict]:
        return self._decode(self.ledger._one("SELECT * FROM skills WHERE name=?", (name,)))

    def list(self, include_inactive: bool = False) -> List[dict]:
        sql = "SELECT * FROM skills" + ("" if include_inactive else " WHERE active=1") + " ORDER BY name"
        return [self._decode(r) for r in self.ledger._all(sql)]

    def history(self, name: str) -> List[dict]:
        rows = self.ledger._all("SELECT * FROM skill_versions WHERE name=? ORDER BY id", (name,))
        out = []
        for r in rows:
            d = dict(r)
            d["snapshot"] = json.loads(d["snapshot"])
            out.append(d)
        return out

    def upsert(self, name: str, description: str, body: str = "", *, actor: str = "user",
               reason: str = "", source: str = "", **fields) -> dict:
        """Create a skill or add a new version. Unchanged content is a no-op."""
        name = (name or "").strip().lower()
        if not _NAME_RE.match(name):
            raise LedgerError(f"invalid skill name {name!r} (lowercase letters, digits, _ . -)")
        if not (description or "").strip():
            raise LedgerError("a skill needs a description")
        bad = set(fields) - set(_LIST_FIELDS)
        if bad:
            raise LedgerError(f"unknown skill fields: {sorted(bad)}")
        new = {"description": description.strip(), "body": (body or "").strip()}
        for f in _LIST_FIELDS:
            v = fields.get(f)
            new[f] = list(v) if isinstance(v, (list, tuple)) else ([v] if v else [])
        cur = self.get(name)
        ts = now_iso()
        if cur is not None:
            if all(cur.get(k) == new[k] for k in _CONTENT_FIELDS):
                return cur
            version = int(cur["version"]) + 1
            self.ledger._write(
                "UPDATE skills SET description=?, body=?, tools=?, requires=?, strategies=?, verify=?, "
                "failure_modes=?, tags=?, version=?, source=?, active=1, updated_at=? WHERE name=?",
                (new["description"], new["body"], *[json.dumps(new[f]) for f in _LIST_FIELDS],
                 version, source or cur.get("source", ""), ts, name))
        else:
            version = 1
            self.ledger._write(
                "INSERT INTO skills (name, description, body, tools, requires, strategies, verify, "
                "failure_modes, tags, version, source, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, new["description"], new["body"], *[json.dumps(new[f]) for f in _LIST_FIELDS],
                 version, source, actor, ts, ts))
        self.ledger._write(
            "INSERT INTO skill_versions (name, version, snapshot, actor, reason, created_at) VALUES (?,?,?,?,?,?)",
            (name, version, json.dumps(new), actor, reason, ts))
        return self.get(name)

    def restore_version(self, name: str, version: int, actor: str = "user") -> dict:
        snap = next((h["snapshot"] for h in self.history(name) if h["version"] == int(version)), None)
        if snap is None:
            raise LedgerError(f"skill {name} has no version {version}")
        return self.upsert(name, snap["description"], snap["body"], actor=actor,
                           reason=f"restore v{version}", **{f: snap.get(f, []) for f in _LIST_FIELDS})

    def set_active(self, name: str, active: bool) -> None:
        self.ledger._write("UPDATE skills SET active=?, updated_at=? WHERE name=?",
                           (1 if active else 0, now_iso(), name))

    def load_dir(self, path: str) -> List[str]:
        """Load every <dir>/<name>/SKILL.md (a new version only when the file changed)."""
        loaded = []
        path = os.path.expanduser(path)
        if not os.path.isdir(path):
            return loaded
        for entry in sorted(os.listdir(path)):
            f = os.path.join(path, entry, "SKILL.md")
            if not os.path.isfile(f):
                continue
            with open(f, encoding="utf-8") as fh:
                meta = parse_skill_md(fh.read())
            name = str(meta.get("name") or entry).strip().lower()
            try:
                self.upsert(name, str(meta.get("description") or ""), meta.get("body", ""),
                            actor="file", reason="loaded from disk", source=f"file:{f}",
                            **{k: meta[k] for k in _LIST_FIELDS if k in meta})
                loaded.append(name)
            except LedgerError:
                continue
        return loaded

    # ── retrieval + feedback ─────────────────────────────────────────────

    def select(self, query: str, k: int = 2, min_score: float = 0.12) -> List[dict]:
        """Top-k active skills relevant to `query` (with their `requires` expanded)."""
        skills = self.list()
        if not skills or not (query or "").strip():
            return []
        scored = []
        qv = None
        if self.embed is not None:
            try:
                qv = self.embed(query)
            except Exception:
                qv = None
        q_tok = _tokens(query)
        for s in skills:
            text = " ".join([s["name"].replace("-", " ").replace("_", " "), s["description"], " ".join(s["tags"])])
            if qv is not None:
                try:
                    sv = self.embed(text)
                    score = float(sum(a * b for a, b in zip(qv, sv)))
                except Exception:
                    score = 0.0
            else:
                s_tok = _tokens(text)
                score = len(q_tok & s_tok) / math.sqrt(max(1, len(q_tok)) * max(1, len(s_tok)))
            if score >= min_score:
                scored.append((score, s))
        scored.sort(key=lambda x: -x[0])
        picked, seen = [], set()
        for score, s in scored[:k]:
            for sk in [s] + [self.get(r) for r in s.get("requires", [])]:
                if sk and sk["name"] not in seen and sk.get("active", 1):
                    seen.add(sk["name"])
                    picked.append({**sk, "score": round(score, 3)})
        return picked

    def record_outcome(self, names: Iterable[str], success: bool) -> None:
        for n in set(names or ()):
            self.ledger._write(
                "UPDATE skills SET uses=uses+1, successes=successes+?, failures=failures+? WHERE name=?",
                (1 if success else 0, 0 if success else 1, n))
