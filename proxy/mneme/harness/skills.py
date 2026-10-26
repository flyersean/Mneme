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
        self._emb_cache = {}  # (name, version) -> normalized vector, reused across selects
        self._dirs = [os.path.expanduser(d) for d in (dirs or ())]
        self._dir_sigs = {}
        with ledger._lock:
            ledger._db.executescript(_SCHEMA)
            ledger._db.commit()
        for d in self._dirs:
            self.load_dir(d)
            self._dir_sigs[d] = self._dir_sig(d)

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
        self._maybe_reload()
        return self._decode(self.ledger._one("SELECT * FROM skills WHERE name=?", (name,)))

    def list(self, include_inactive: bool = False) -> List[dict]:
        self._maybe_reload()
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
        self._emb_cache.clear()
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
                "failure_modes=?, tags=?, version=?, source=?, updated_at=? WHERE name=?",
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
        """Roll the skill's content back to an earlier version and re-activate it.

        NOTE (file-sync precedence): this changes the DB only. For a file-sourced
        skill (source starts with ``file:``) the on-disk SKILL.md still holds the
        newer content, so the next reload/restart re-syncs the file over this
        restore — a restored version is only durable for DB-only skills, or until
        the file itself is updated. Re-activating (without a version rollback) is
        the stable restore for file-sourced skills; use set_active(True) for that."""
        snap = next((h["snapshot"] for h in self.history(name) if h["version"] == int(version)), None)
        if snap is None:
            raise LedgerError(f"skill {name} has no version {version}")
        out = self.upsert(name, snap["description"], snap["body"], actor=actor,
                          reason=f"restore v{version}", **{f: snap.get(f, []) for f in _LIST_FIELDS})
        self.set_active(name, True)  # restoring a version brings the skill back into injection
        return out

    def set_active(self, name: str, active: bool) -> None:
        self.ledger._write("UPDATE skills SET active=?, updated_at=? WHERE name=?",
                           (1 if active else 0, now_iso(), name))

    def delete(self, name: str) -> bool:
        """Soft-delete: deactivate a skill (remove it from injection). The row, its
        versions, and its on-disk file are all kept — matching "delete = remove from
        injection, not from disk". Returns True if it existed."""
        if self.get(name) is None:
            return False
        self.set_active(name, False)
        return True

    def purge(self, name: str, remove_file: bool = True) -> bool:
        """Hard-delete a skill: remove the row and all versions, and (by default) its
        on-disk skill dir. The irreversible counterpart to the soft delete."""
        cur = self.get(name)
        if cur is None:
            return False
        src = cur.get("source") or ""
        self.ledger._write("DELETE FROM skill_versions WHERE name=?", (name,))
        self.ledger._write("DELETE FROM skills WHERE name=?", (name,))
        self._emb_cache.clear()
        if remove_file and src.startswith("file:"):
            path = src[len("file:"):]
            parent = os.path.dirname(path)
            if os.path.basename(parent) == name and ("/skills/" in path or "/skill-library/" in path):
                import shutil
                shutil.rmtree(parent, ignore_errors=True)
        return True

    def export(self, name: str) -> Optional[str]:
        """Materialize a skill as a SKILL.md string (YAML frontmatter + body) for
        portability, copying, or git-tracking a DB-only skill."""
        s = self.get(name)
        if s is None:
            return None
        meta = {"name": s["name"], "description": s["description"]}
        for f in _LIST_FIELDS:
            v = s.get(f) or []
            if v:
                meta[f] = list(v)
        try:
            import yaml
            front = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False).strip()
        except Exception:
            front = f"name: {s['name']}\ndescription: {s['description']}"
        return f"---\n{front}\n---\n\n{(s.get('body') or '').strip()}\n"

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
            row = self.ledger._one("SELECT source FROM skills WHERE name=?", (name,))
            was_removed = row is not None and (row["source"] or "").startswith("file-removed:")
            try:
                self.upsert(name, str(meta.get("description") or ""), meta.get("body", ""),
                            actor="file", reason="loaded from disk", source=f"file:{f}",
                            **{k: meta[k] for k in _LIST_FIELDS if k in meta})
                if was_removed:
                    # file re-created -> available again, and source points at the file once more
                    self.ledger._write("UPDATE skills SET source=?, active=1, updated_at=? WHERE name=?",
                                       (f"file:{f}", now_iso(), name))
                loaded.append(name)
            except LedgerError:
                continue
        self._sync_deleted(path)
        return loaded

    def _sync_deleted(self, path: str) -> None:
        """Deactivate file-sourced skills under `path` whose SKILL.md no longer exists.
        Soft (removed from injection; row + versions kept) — matching delete semantics.
        ``source`` is rewritten to ``file-removed:<path>`` so a re-created file
        re-activates the skill on the next load."""
        prefix = os.path.join(path, "")  # ensure a trailing separator
        for row in self.ledger._all("SELECT name, source FROM skills WHERE source LIKE 'file:%'"):
            src = row["source"] or ""
            if not src.startswith("file:"):
                continue
            fpath = src[len("file:"):]
            if fpath.startswith(prefix) and not os.path.isfile(fpath):
                self.ledger._write(
                    "UPDATE skills SET active=0, source=?, updated_at=? WHERE name=?",
                    (f"file-removed:{fpath}", now_iso(), row["name"]))

    def _dir_sig(self, path: str) -> float:
        """Max mtime of a skills dir and its <name>/SKILL.md files — changes when
        a skill is added, removed, or its SKILL.md edited (the reload trigger)."""
        if not os.path.isdir(path):
            return 0
        sig = os.path.getmtime(path)
        try:
            entries = os.listdir(path)
        except OSError:
            return sig
        for entry in entries:
            f = os.path.join(path, entry, "SKILL.md")
            if os.path.isfile(f):
                sig = max(sig, os.path.getmtime(f))
        return sig

    def _maybe_reload(self) -> None:
        """Re-scan the skills dirs when a SKILL.md (or a new/removed skill dir)
        changed on disk. File-based skills go live without a restart — the same
        mtime hot-reload pattern the config uses. ``upsert`` no-ops on unchanged
        content, so only genuinely changed files bump a version. ``_reloading``
        guards re-entry (get -> reload -> load_dir -> upsert -> get)."""
        if getattr(self, "_reloading", False):
            return
        self._reloading = True
        try:
            for d in self._dirs:
                sig = self._dir_sig(d)
                if sig != self._dir_sigs.get(d, 0):
                    self.load_dir(d)
                    self._dir_sigs[d] = sig
        finally:
            self._reloading = False

    # ── retrieval + feedback ─────────────────────────────────────────────

    def select(self, query: str, k: int = 2, min_score: float = 0.12, include_inactive: bool = False) -> List[dict]:
        """Top-k skills relevant to `query` (with their `requires` expanded).
        By default only active skills; pass include_inactive=True to search soft-deleted ones too."""
        skills = self.list(include_inactive=include_inactive)
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
                    key = (s["name"], s.get("version"))
                    sv = self._emb_cache.get(key)
                    if sv is None:
                        sv = self.embed(text)
                        if sv is not None:
                            self._emb_cache[key] = sv
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
                if sk and sk["name"] not in seen and (include_inactive or sk.get("active", 1)):
                    seen.add(sk["name"])
                    picked.append({**sk, "score": round(score, 3)})
        return picked

    def record_outcome(self, names: Iterable[str], success: bool) -> None:
        for n in set(names or ()):
            self.ledger._write(
                "UPDATE skills SET uses=uses+1, successes=successes+?, failures=failures+? WHERE name=?",
                (1 if success else 0, 0 if success else 1, n))
