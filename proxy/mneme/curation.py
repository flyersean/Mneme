"""Memory curation — retraction, recurrence, provenance, and the decision log.

The problem this solves: a hallucinated fact, once saved and re-injected, becomes
self-reinforcing. Mneme's existing machinery (grade F labels, trust tiers,
`superseded_by`) mitigates it, but there was no way for a human OR the model to
say "this specific chunk is false" — the only destructive lever was a full
/reset.

Four mechanisms live here, all built on one principle: **judge provenance, not
truth.** Nothing in this module decides whether a claim is factually correct.
It records who asserted it, how many independent times, what it was derived
from, and whether a human disputed it.

1. RETRACTION — a chunk can be retracted (by the model, gated by a config flag)
   or flagged by the user. Retracted chunks are excluded from retrieval, or kept
   as labelled negative examples (see INJECT_RETRACTED). Reversible.

2. RECURRENCE — how many times a claim has been asserted, across independent
   turns and by whom. A single-assertion model claim is the dangerous case
   (that is the hallucinated-validation shape: the model restating its own
   earlier output). Counts are per (topic, normalized-claim) so restatements
   accumulate rather than being treated as fresh evidence.

3. PROVENANCE CHAINS — each chunk records the chunk ids it was derived from.
   This makes a SELF-CONFIRMATION LOOP mechanically detectable: a "validation"
   that cites a chunk the model itself wrote is not corroboration, it is an
   echo. That flag is the highest-value signal for the reported symptom.

   ⚠ INCOMPLETE AS SHIPPED: the storage, the diffing and detect_self_confirmation()
   are implemented and tested, but `record_provenance()` is NOT yet called from the
   archive path — so on a live proxy `derived_from` stays '[]' and the
   self-confirmation flag never fires. Wiring it up is step 1 of
   docs/provenance-and-chunk-lifecycle-plan.md. Until then, treat the mechanism as
   unavailable rather than merely unproven.

4. DECISION LOG — every retraction / flag / restore is appended to an audit
   trail with who did it and why, and can be undone. The model may PROPOSE
   (queue a candidate for review); only the user CONFIRMS. Acting and proposing
   are separate capabilities behind separate flags so the model can never
   silently delete a correct fact that contradicts it.

Schema is additive — every column is added via ALTER TABLE guarded by a
try/except, matching the existing migration style in mneme_proxy.py.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Optional

# ── Retraction states ────────────────────────────────────────────────────
# Stored in chunks.retracted (TEXT). Empty string = not retracted.
RETRACT_NONE = ""
RETRACT_USER = "user"          # the human said this is false
RETRACT_MODEL = "model"        # the model acted (only when ALLOW_MODEL_RETRACT=1)
RETRACT_AUTO = "auto"          # reserved: automated policy retraction

# A chunk in a non-empty retracted state is excluded from retrieval unless
# INJECT_RETRACTED is on, in which case it is injected as a labelled warning
# (absence is dangerous: with nothing to contradict it, the model may simply
# re-hallucinate the same fact).
_RETRACTED_STATES = (RETRACT_USER, RETRACT_MODEL, RETRACT_AUTO)

# ── Confidence / recurrence tiers ────────────────────────────────────────
# Derived, not stored: computed from recurrence counts + trust. Kept as a
# function so it stays consistent as counts change.
CONF_SINGLE = "single"       # asserted once, by a model -> treat as a claim
CONF_REPEATED = "repeated"   # asserted repeatedly, but only by the model
CONF_CORROBORATED = "corroborated"  # asserted from an independent non-model source


def _norm_claim(text: str) -> str:
    """Normalize a claim for recurrence counting.

    Aggressive on purpose: restatements differ in whitespace, punctuation, and
    casing, and we WANT those to collapse onto one counter so that repetition
    accumulates instead of looking like fresh independent evidence.
    """
    t = (text or "").lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s.]", "", t)
    return t.strip()[:400]


class CurationError(Exception):
    """Raised for invalid curation operations (unknown chunk, bad state)."""


def ensure_schema(db: sqlite3.Connection) -> None:
    """Add curation columns/tables. Idempotent; safe on existing DBs."""
    migrations = (
        # Retraction state + who/why/when (audit lives in the log table too,
        # but these make the current state queryable without a join).
        "ALTER TABLE chunks ADD COLUMN retracted TEXT DEFAULT ''",
        "ALTER TABLE chunks ADD COLUMN retracted_by TEXT DEFAULT ''",
        "ALTER TABLE chunks ADD COLUMN retracted_reason TEXT DEFAULT ''",
        "ALTER TABLE chunks ADD COLUMN retracted_at TEXT DEFAULT ''",
        # Recurrence: how many times this claim has been asserted, and by how
        # many independent non-model sources.
        "ALTER TABLE chunks ADD COLUMN assert_count INTEGER DEFAULT 1",
        "ALTER TABLE chunks ADD COLUMN independent_sources INTEGER DEFAULT 0",
        # Provenance: JSON list of chunk_ids this chunk was derived from.
        "ALTER TABLE chunks ADD COLUMN derived_from TEXT DEFAULT '[]'",
        # Self-confirmation flag: 1 when this chunk's support traces back to the
        # model's own prior output with no independent corroboration.
        "ALTER TABLE chunks ADD COLUMN self_confirm INTEGER DEFAULT 0",
        # Review queue: model-proposed retractions awaiting a human decision.
        "ALTER TABLE chunks ADD COLUMN proposed_retract TEXT DEFAULT ''",
        "ALTER TABLE chunks ADD COLUMN proposed_reason TEXT DEFAULT ''",
        # Which chat model produced this chunk. `source` is a CATEGORY ('model',
        # 'tool:x', 'page:x', 'input', 'user') and cannot answer "delete the chunks
        # gemma4 wrote" — that needs the model name, which was previously only in
        # the log line and not stored at all.
        "ALTER TABLE chunks ADD COLUMN model TEXT DEFAULT ''",
        # Chunk ids that were IN CONTEXT when this chunk was produced. Unlike
        # derived_from (which needs the model to cite what it used), the proxy
        # knows the injected set because it chose it — so this is the reliable
        # signal for "what turns were contaminated by a bad memory?".
        "ALTER TABLE chunks ADD COLUMN injected_chunk_ids TEXT DEFAULT '[]'",
        # REMOVED — the flags. 'injectable' (default) or 'removed'.
        #
        # NOT a delete. The row, content and id all stay; the flags change what
        # Mneme does with the chunk:
        #   injection                skips it, unconditionally
        #   the model's search_memory skips it
        #   /search and /list         skip it unless the caller opts in
        #   the management page       still shows it, flagged, with content
        #
        # We do NOT filter deleted rows out of the table, and we do not touch the
        # FAISS index — so this is fully reversible and a mistake costs nothing.
        # (Contrast a real delete, which leaves orphan vectors and dangling
        # strategy links; that is why purge is a separate, later problem.)
        "ALTER TABLE chunks ADD COLUMN removed TEXT DEFAULT 'injectable'",
        "ALTER TABLE chunks ADD COLUMN removed_by TEXT DEFAULT ''",
        "ALTER TABLE chunks ADD COLUMN removed_reason TEXT DEFAULT ''",
        "ALTER TABLE chunks ADD COLUMN removed_at TEXT DEFAULT ''",
    )
    for m in migrations:
        try:
            db.execute(m)
        except sqlite3.OperationalError:
            pass  # column already exists

    db.execute("""
        CREATE TABLE IF NOT EXISTS curation_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id    TEXT NOT NULL,
            action      TEXT NOT NULL,   -- retract | flag | restore | deny | confirm | propose
            actor       TEXT NOT NULL,   -- user | model | system
            reason      TEXT DEFAULT '',
            prev_state  TEXT DEFAULT '', -- for undo: what `retracted` was before
            created_at  TEXT NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_curation_chunk ON curation_log(chunk_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_curation_action ON curation_log(action)")
    db.commit()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _log(db, chunk_id: str, action: str, actor: str, reason: str = "", prev_state: str = "") -> None:
    db.execute(
        "INSERT INTO curation_log (chunk_id, action, actor, reason, prev_state, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (chunk_id, action, actor, (reason or "")[:1000], prev_state or "", _now()),
    )


def _get_chunk(db, chunk_id: str) -> Optional[dict]:
    row = db.execute(
        "SELECT chunk_id, topic_label, messages, retracted, retracted_by, "
        "retracted_reason, assert_count, independent_sources, derived_from, self_confirm, "
        "proposed_retract, proposed_reason, model, injected_chunk_ids, source, grade, "
        "trust, created_at, removed, removed_by, removed_reason, removed_at "
        "FROM chunks WHERE chunk_id=?",
        (chunk_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "chunk_id": row[0], "topic_label": row[1], "messages": row[2],
        "retracted": row[3] or "", "retracted_by": row[4] or "",
        "retracted_reason": row[5] or "", "assert_count": row[6] or 1,
        "removed": row[18] or "injectable", "removed_by": row[19] or "",
        "removed_reason": row[20] or "", "removed_at": row[21] or "",
        "independent_sources": row[7] or 0,
        "derived_from": json.loads(row[8] or "[]"), "self_confirm": bool(row[9]),
        "proposed_retract": row[10] or "", "proposed_reason": row[11] or "",
        "model": row[12] or "", "injected_chunk_ids": json.loads(row[13] or "[]"),
        "source": row[14] or "", "grade": row[15] or "C",
        "trust": row[16] or "", "created_at": row[17] or "",
    }


# ── Removed flags (management) ───────────────────────────────────────────
#
# These are the flags the memory management page drives. NOT a delete: the row,
# content and id all stay. The flag changes what Mneme does with the chunk —
# injection and the model's search_memory skip it, while the management page
# still shows it (flagged, with content) so a user can review and reverse.
#
# Deliberately separate from retract()/proposed_retract(): retraction carries an
# epistemic claim ("this is disputed"), whereas `removed` is a plain management
# flag ("stop using this"). A user may remove a chunk for reasons that have
# nothing to do with its truth (duplicate, test junk, superseded).

REMOVABLE = "injectable"
REMOVED = "removed"
_REMOVED_STATES = (REMOVABLE, REMOVED)


def set_removed(db, chunk_id, removed=True, actor="user", reason=""):
    """Set or clear the removed flag on ONE chunk. Reversible, logged.

    `removed=True`  -> 'removed'      (skip from injection + model search)
    `removed=False` -> 'injectable'   (back to normal)

    Does not touch the FAISS index or any other row, so this cannot corrupt
    retrieval — nothing is deleted, so nothing can dangle.
    """
    if actor not in ("user", "model", "system"):
        raise CurationError(f"bad actor: {actor!r}")
    chunk = _get_chunk(db, chunk_id)
    if not chunk:
        raise CurationError(f"unknown chunk: {chunk_id}")
    state = REMOVED if removed else REMOVABLE
    db.execute(
        "UPDATE chunks SET removed=?, removed_by=?, removed_reason=?, removed_at=? "
        "WHERE chunk_id=?",
        (state, actor if removed else "", (reason or "")[:1000] if removed else "",
         _now() if removed else "", chunk_id),
    )
    _log(db, chunk_id, "remove" if removed else "unremove", actor, reason,
         prev_state=chunk.get("removed", REMOVABLE))
    db.commit()
    return {"chunk_id": chunk_id, "removed": state, "previous": chunk.get("removed", REMOVABLE)}


def is_removed(chunk) -> bool:
    """True when a chunk dict is flagged removed. Tolerates missing key."""
    return (chunk or {}).get("removed", REMOVABLE) == REMOVED


def list_chunks(db, filters=None, limit=200, offset=0):
    """Query chunks for the management page. Returns rows + total count.

    Filters are ANDed; every one is optional. Kept as a plain query over the
    chunks table (no FAISS, no embeddings) so the page works even when the
    vector index is unavailable.

    Supported keys:
      keyword        substring over the messages JSON (exact-ish; SQL LIKE)
      source         e.g. 'model', 'user', 'tool:terminal', 'page:example.com'
      model          the chat model that produced it (e.g. 'gemma4')
      grade          A..F (exact)
      trust          verified / unverified / ...
      removed        'removed' | 'injectable' — omit for both
      proposed       True -> only chunks with a pending removal proposal
      self_confirm   True -> only chunks flagged as self-confirming
      uncorroborated True -> independent_sources = 0
      session        session_id
      since / until  ISO date or datetime bounds on created_at (inclusive)
      order          'newest' (default) | 'oldest'
    """
    f = dict(filters or {})
    where, args = [], []

    kw = (f.get("keyword") or "").strip()
    if kw:
        # Search content AND topic label. A user looking for a chunk thinks in
        # terms of the label shown in the UI, which is frequently not a substring
        # of the stored content (the label is derived, the content is verbatim).
        where.append("(messages LIKE ? OR topic_label LIKE ?)")
        args.extend([f"%{kw}%", f"%{kw}%"])
    if f.get("source"):
        where.append("source = ?")
        args.append(f["source"])
    if f.get("model"):
        where.append("model LIKE ?")
        args.append(f"%{f['model']}%")
    if f.get("grade"):
        where.append("grade = ?")
        args.append(f["grade"])
    if f.get("trust"):
        where.append("trust = ?")
        args.append(f["trust"])
    if f.get("session"):
        where.append("session_id = ?")
        args.append(f["session"])

    # Removed state. Default: show BOTH (this is the management view — hiding
    # removed chunks here would defeat the purpose).
    rem = f.get("removed")
    if rem in _REMOVED_STATES:
        where.append("COALESCE(NULLIF(removed,''),'injectable') = ?")
        args.append(rem)
    elif rem in ("", None) and f.get("injectable_only"):
        where.append("COALESCE(NULLIF(removed,''),'injectable') = ?")
        args.append(REMOVABLE)

    if f.get("proposed"):
        where.append("proposed_retract != '' AND proposed_retract IS NOT NULL")
    if f.get("self_confirm"):
        where.append("self_confirm = 1")
    if f.get("uncorroborated"):
        where.append("COALESCE(independent_sources,0) = 0")
    if f.get("since"):
        where.append("created_at >= ?")
        args.append(f["since"])
    if f.get("until"):
        # Inclusive of the whole day when given a bare date.
        until = f["until"]
        if len(str(until)) == 10:
            until = f"{until}T23:59:59"
        where.append("created_at <= ?")
        args.append(until)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order = "ASC" if (f.get("order") or "newest") == "oldest" else "DESC"

    total = db.execute(
        f"SELECT COUNT(*) FROM chunks {clause}", args
    ).fetchone()[0]

    rows = db.execute(
        f"SELECT chunk_id, topic_label, messages, source, model, grade, trust, "
        f"created_at, removed, removed_reason, retracted, proposed_retract, "
        f"self_confirm, COALESCE(independent_sources,0), session_id, derived_from "
        f"FROM chunks {clause} ORDER BY created_at {order}, rowid {order} "
        f"LIMIT ? OFFSET ?",
        args + [int(limit), int(offset)],
    ).fetchall()

    out = []
    for r in rows:
        out.append({
            "chunk_id": r[0], "topic_label": r[1],
            # Truncated for the list view; the detail endpoint returns full text.
            "preview": _preview(r[2]),
            "source": r[3] or "", "model": r[4] or "", "grade": r[5] or "",
            "trust": r[6] or "", "created_at": r[7] or "",
            "removed": r[8] or REMOVABLE, "removed_reason": r[9] or "",
            "retracted": r[10] or "", "proposed_retract": r[11] or "",
            "self_confirm": bool(r[12]), "independent_sources": r[13] or 0,
            "session_id": r[14] or "", "derived_from": json.loads(r[15] or "[]"),
        })
    return {"chunks": out, "count": len(out), "total": total}


def _preview(messages_json, limit=280):
    """First bit of readable text from a chunk's messages JSON."""
    try:
        msgs = json.loads(messages_json or "[]")
    except (ValueError, TypeError):
        return ""
    for m in msgs:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
            text = str(m.get("content", "")).strip()
            if text:
                text = " ".join(text.split())
                return text[:limit] + ("…" if len(text) > limit else "")
    return ""


def set_bad_chunk(db, chunk_id, bad=True, actor="user", reason=""):
    """Set or clear the "bad chunk" flag on ONE chunk.

    A question mark, not a decision: marking a chunk bad records that it is
    SUSPECTED of being wrong — nothing about what Mneme uses changes. Clearing it
    says the suspicion was unfounded. It does not touch `removed`, which is the
    separate, deliberate action that takes a chunk out of circulation.

    `actor` records WHO flagged it, which the page renders as colour:
        'model' -> amber   (the model suggested it — evaluate it)
        'user'  -> red     (you flagged it — you already made the call)

    Distinct from retract(): retraction is an epistemic claim that also changes
    injection (a dispute warning is attached). This is just a marker.
    """
    if actor not in ("user", "model"):
        raise CurationError(f"bad actor: {actor!r}")
    chunk = _get_chunk(db, chunk_id)
    if not chunk:
        raise CurationError(f"unknown chunk: {chunk_id}")
    if bad:
        db.execute(
            "UPDATE chunks SET proposed_retract=?, proposed_reason=? WHERE chunk_id=?",
            (actor, (reason or "")[:1000], chunk_id),
        )
    else:
        db.execute(
            "UPDATE chunks SET proposed_retract='', proposed_reason='' WHERE chunk_id=?",
            (chunk_id,),
        )
    _log(db, chunk_id, "bad_chunk" if bad else "bad_chunk_cleared", actor, reason,
         prev_state=chunk.get("proposed_retract", ""))
    db.commit()
    return {"chunk_id": chunk_id, "bad_chunk": actor if bad else "",
            "previous": chunk.get("proposed_retract", "")}


def set_bad_chunk_many(db, chunk_ids, bad=True, actor="user", reason=""):
    """Bulk form of set_bad_chunk. Returns per-id outcomes.

    Per-id rather than a bare count so a typo'd id cannot pass as success.
    """
    changed, failed = [], []
    for cid in (chunk_ids or []):
        try:
            set_bad_chunk(db, cid, bad=bad, actor=actor, reason=reason)
            changed.append(cid)
        except CurationError as e:
            failed.append({"chunk_id": cid, "error": str(e)})
    return {"bad_chunk": actor if bad else "", "changed": changed,
            "failed": failed, "count": len(changed)}


# ── Retraction ───────────────────────────────────────────────────────────

def retract(db, chunk_id: str, actor: str, reason: str = "") -> dict:
    """Mark a chunk retracted. Reversible via restore(). Appends to the log."""
    if actor not in ("user", "model", "system"):
        raise CurationError(f"bad actor: {actor!r}")
    chunk = _get_chunk(db, chunk_id)
    if not chunk:
        raise CurationError(f"unknown chunk: {chunk_id}")
    prev = chunk["retracted"]
    state = {"user": RETRACT_USER, "model": RETRACT_MODEL, "system": RETRACT_AUTO}[actor]
    with_actor = db.execute(
        "UPDATE chunks SET retracted=?, retracted_by=?, retracted_reason=?, retracted_at=?, "
        "proposed_retract='', proposed_reason='' WHERE chunk_id=?",
        (state, actor, (reason or "")[:1000], _now(), chunk_id),
    )
    _log(db, chunk_id, "retract", actor, reason, prev_state=prev)
    db.commit()
    return {"chunk_id": chunk_id, "retracted": state, "by": actor,
            "reason": reason, "rows": with_actor.rowcount}


def restore(db, chunk_id: str, actor: str = "user", reason: str = "") -> dict:
    """Un-retract a chunk (undo). Clears the proposal too."""
    chunk = _get_chunk(db, chunk_id)
    if not chunk:
        raise CurationError(f"unknown chunk: {chunk_id}")
    prev = chunk["retracted"]
    db.execute(
        "UPDATE chunks SET retracted='', retracted_by='', retracted_reason='', "
        "retracted_at='', proposed_retract='', proposed_reason='' WHERE chunk_id=?",
        (chunk_id,),
    )
    _log(db, chunk_id, "restore", actor, reason, prev_state=prev)
    db.commit()
    return {"chunk_id": chunk_id, "retracted": "", "restored_from": prev}


def propose_retract(db, chunk_id: str, reason: str = "", actor: str = "model") -> dict:
    """Queue a retraction for human review instead of acting.

    This is the safe default for the model: it can point at a chunk it believes
    is wrong, and the user decides. Nothing is excluded from retrieval while a
    proposal is pending — a wrong model judgement must not silently remove
    correct information.
    """
    chunk = _get_chunk(db, chunk_id)
    if not chunk:
        raise CurationError(f"unknown chunk: {chunk_id}")
    db.execute(
        "UPDATE chunks SET proposed_retract=?, proposed_reason=? WHERE chunk_id=?",
        (actor, (reason or "")[:1000], chunk_id),
    )
    _log(db, chunk_id, "propose", actor, reason, prev_state=chunk["retracted"])
    db.commit()
    return {"chunk_id": chunk_id, "proposed": True, "reason": reason}


def deny_proposal(db, chunk_id: str, reason: str = "", actor: str = "user") -> dict:
    """Reject a pending proposal — the chunk stays as-is and is not re-proposed
    immediately (the log holds the record of the decision)."""
    chunk = _get_chunk(db, chunk_id)
    if not chunk:
        raise CurationError(f"unknown chunk: {chunk_id}")
    db.execute(
        "UPDATE chunks SET proposed_retract='', proposed_reason='' WHERE chunk_id=?",
        (chunk_id,),
    )
    _log(db, chunk_id, "deny", actor, reason, prev_state=chunk["retracted"])
    db.commit()
    return {"chunk_id": chunk_id, "denied": True}


def list_proposals(db, limit: int = 50) -> list:
    """The review queue: model-proposed retractions awaiting a human call."""
    rows = db.execute(
        "SELECT chunk_id, topic_label, proposed_retract, proposed_reason, "
        "grade, trust, created_at FROM chunks WHERE proposed_retract != '' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"chunk_id": r[0], "topic_label": r[1], "proposed_by": r[2],
         "reason": r[3], "grade": r[4], "trust": r[5], "created_at": r[6]}
        for r in rows
    ]


def list_log(db, limit: int = 100, chunk_id: str = None) -> list:
    """The decision log — every action, who took it, when, and why."""
    if chunk_id:
        rows = db.execute(
            "SELECT id, chunk_id, action, actor, reason, prev_state, created_at "
            "FROM curation_log WHERE chunk_id=? ORDER BY id DESC LIMIT ?",
            (chunk_id, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, chunk_id, action, actor, reason, prev_state, created_at "
            "FROM curation_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"id": r[0], "chunk_id": r[1], "action": r[2], "actor": r[3],
         "reason": r[4], "prev_state": r[5], "created_at": r[6]}
        for r in rows
    ]


def undo_last(db, chunk_id: str, actor: str = "user") -> dict:
    """Reverse the most recent retraction/restore on a chunk.

    Convenience over restore(): finds the last log entry and puts the chunk
    back to the state it had before that action.
    """
    row = db.execute(
        "SELECT id, action, prev_state FROM curation_log WHERE chunk_id=? "
        "AND action IN ('retract','restore') ORDER BY id DESC LIMIT 1",
        (chunk_id,),
    ).fetchone()
    if not row:
        raise CurationError(f"no retraction history for {chunk_id}")
    _, action, prev_state = row
    db.execute(
        "UPDATE chunks SET retracted=?, retracted_at='' WHERE chunk_id=?",
        (prev_state or "", chunk_id),
    )
    _log(db, chunk_id, "restore", actor, f"undo of {action}", prev_state=prev_state)
    db.commit()
    return {"chunk_id": chunk_id, "retracted": prev_state or "", "undone": action}


# ── Recurrence ───────────────────────────────────────────────────────────

def bump_recurrence(db, chunk_id: str, claim_text: str, source: str) -> dict:
    """Record another assertion of `claim_text`.

    Increments the chunk's assertion count, and counts INDEPENDENT sources: a
    model restating its own earlier claim is not corroboration, so only
    non-model sources (user input, a fetched page, a tool result) move that
    counter.
    """
    chunk = _get_chunk(db, chunk_id)
    if not chunk:
        raise CurationError(f"unknown chunk: {chunk_id}")
    s = (source or "").lower()
    is_model = not (s == "user" or s.startswith("page:") or s.startswith("tool:"))
    if is_model:
        db.execute(
            "UPDATE chunks SET assert_count = COALESCE(assert_count,1) + 1 WHERE chunk_id=?",
            (chunk_id,),
        )
    else:
        db.execute(
            "UPDATE chunks SET assert_count = COALESCE(assert_count,1) + 1, "
            "independent_sources = COALESCE(independent_sources,0) + 1 WHERE chunk_id=?",
            (chunk_id,),
        )
    db.commit()
    return confidence_tier(_get_chunk(db, chunk_id))


def confidence_tier(chunk: dict) -> str:
    """Derive the confidence tier from recurrence + trust.

    Deliberately NOT a truth judgement — it is a statement about support:
      corroborated — someone other than the model has asserted this
      repeated     — the model has said it more than once (could be an echo)
      single       — asserted once; the dangerous case
    """
    if (chunk.get("independent_sources") or 0) > 0:
        return CONF_CORROBORATED
    if (chunk.get("assert_count") or 1) > 1:
        return CONF_REPEATED
    return CONF_SINGLE


# ── Provenance chains ────────────────────────────────────────────────────

def record_provenance(db, chunk_id: str, derived_from: list) -> dict:
    """Record which chunk ids this chunk was derived from / cites as support.

    Self-citations are dropped: a chunk citing itself is not evidence, and
    including it would make the lineage walk see a cycle immediately.
    """
    chunk = _get_chunk(db, chunk_id)
    if not chunk:
        raise CurationError(f"unknown chunk: {chunk_id}")
    ids = [c for c in (derived_from or [])
           if isinstance(c, str) and c and c != chunk_id]
    db.execute("UPDATE chunks SET derived_from=? WHERE chunk_id=?",
               (json.dumps(ids), chunk_id))
    db.commit()
    return {"chunk_id": chunk_id, "derived_from": ids}


def record_context(db, chunk_id: str, injected_ids: list, model: str = "") -> dict:
    """Record what was IN CONTEXT when this chunk was produced, and by which model.

    This is the reliable contamination signal. derived_from depends on the model
    citing its sources — and models paraphrase without citing constantly. The
    proxy, by contrast, KNOWS which chunks it injected, because it chose them. So
    when a hallucination is discovered three days later, this answers "which turns
    were produced with that bad memory in view?" with no model cooperation needed.

    `model` is stored so a filter like "delete chunks gemma4 wrote" is expressible
    (chunk `source` is a category, not a model name).
    """
    chunk = _get_chunk(db, chunk_id)
    if not chunk:
        raise CurationError(f"unknown chunk: {chunk_id}")
    ids = sorted({c for c in (injected_ids or [])
                  if isinstance(c, str) and c and c != chunk_id})
    db.execute("UPDATE chunks SET injected_chunk_ids=?, model=? WHERE chunk_id=?",
               (json.dumps(ids), (model or "")[:200], chunk_id))
    db.commit()
    return {"chunk_id": chunk_id, "injected_chunk_ids": ids, "model": model or ""} 


def lineage(db, chunk_id: str, max_depth: int = 10) -> dict:
    """Everything built on top of `chunk_id`, via both provenance signals.

    Two different relationships, both meaningful for damage assessment:

      cites      — this chunk's messages cited that chunk ([source: mem_X]).
                   Voluntary: the model has to have named it.
      saw        — that chunk was injected into the context that produced this one.
                   Recorded by the proxy, so it holds even when the model
                   paraphrased without citing.

    Returns the transitive closure with each node's state, so the caller can see
    the shape of the contamination (how far it spread, how bad each node is).
    A visited set bounds the walk — a cycle (A saw B, B cites A) must not loop.
    """
    root = _get_chunk(db, chunk_id)
    if not root:
        raise CurationError(f"unknown chunk: {chunk_id}")

    def _children(cid: str):
        """Direct children of cid, with how each is related."""
        out = {}
        for row in db.execute(
            "SELECT chunk_id, derived_from, injected_chunk_ids FROM chunks "
            "WHERE derived_from LIKE ? OR injected_chunk_ids LIKE ?",
            (f'%"{cid}"%', f'%"{cid}"%'),
        ).fetchall():
            ccid, df, inj = row[0], json.loads(row[1] or "[]"), json.loads(row[2] or "[]")
            rels = []
            if cid in df:
                rels.append("cites")
            if cid in inj:
                rels.append("saw")
            if rels:
                out[ccid] = rels
        return out

    seen = {chunk_id}
    frontier = [chunk_id]
    nodes = []
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        nxt = []
        for parent in frontier:
            for child, rels in _children(parent).items():
                if child in seen:
                    continue
                seen.add(child)
                c = _get_chunk(db, child)
                if not c:
                    continue
                nodes.append({
                    "chunk_id": child, "via": parent, "relation": rels,
                    "depth": depth, "topic_label": c.get("topic_label", ""),
                    "grade": c.get("grade", ""), "trust": c.get("trust", ""),
                    "retracted": c.get("retracted", ""), "model": c.get("model", ""),
                })
                nxt.append(child)
        frontier = nxt

    return {
        "chunk_id": chunk_id,
        "root": {"topic_label": root.get("topic_label", ""),
                 "grade": root.get("grade", ""),
                 "retracted": root.get("retracted", "")},
        "descendants": nodes,
        "count": len(nodes),
        "truncated": bool(frontier) and depth >= max_depth,
    }


def detect_self_confirmation(db, chunk_id: str, source: str = "") -> dict:
    """Flag a chunk whose support traces back to the model's own output.

    The reported failure shape: the model agrees with an earlier claim of its
    own, and that agreement is then stored as though it were validation. It is
    an echo, not evidence.

    A chunk is self-confirming when it cites (derived_from) a chunk that is
    itself model-generated / unverified, AND has no independent non-model
    source of its own.
    """
    chunk = _get_chunk(db, chunk_id)
    if not chunk:
        raise CurationError(f"unknown chunk: {chunk_id}")

    s = (source or "").lower()
    own_is_model = not (s == "user" or s.startswith("page:") or s.startswith("tool:"))
    own_has_independent = (chunk.get("independent_sources") or 0) > 0

    cited_model_chunks = []
    for cid in chunk.get("derived_from") or []:
        if cid == chunk_id:
            continue
        parent = _get_chunk(db, cid)
        if not parent:
            continue
        p_src = (parent.get("retracted") or "")
        parent_is_model = True  # a parent chunk is model-derived unless proven otherwise
        try:
            prow = db.execute("SELECT source, trust FROM chunks WHERE chunk_id=?", (cid,)).fetchone()
            if prow:
                ps = (prow[0] or "").lower()
                parent_is_model = not (ps == "user" or ps.startswith("page:") or ps.startswith("tool:"))
        except Exception:
            pass
        if parent_is_model:
            cited_model_chunks.append(cid)

    # Self-confirmation needs BOTH: it cites model-derived support, and nothing
    # independent backs its own claim.
    self_confirm = bool(cited_model_chunks) and not own_has_independent and own_is_model
    db.execute("UPDATE chunks SET self_confirm=? WHERE chunk_id=?",
               (1 if self_confirm else 0, chunk_id))
    db.commit()
    return {"chunk_id": chunk_id, "self_confirm": self_confirm,
            "cited_model_chunks": cited_model_chunks}


def is_retracted(chunk: dict) -> bool:
    return (chunk.get("retracted") or "") in _RETRACTED_STATES


def retraction_label(chunk: dict) -> str:
    """The injection tag for a retracted chunk (empty when not retracted).

    Retracted chunks are kept as labelled warnings rather than silently dropped:
    with no contradicting context the model may re-hallucinate the same fact.
    Mirrors the existing [G:F — FAILED ...] treatment.
    """
    state = chunk.get("retracted") or ""
    if state not in _RETRACTED_STATES:
        return ""
    by = chunk.get("retracted_by") or state
    return f"[RETRACTED by {by} — DISPUTED / DO NOT TRUST OR REPEAT]"


def confidence_label(chunk: dict) -> str:
    """Injection tag describing support (not truth)."""
    tier = confidence_tier(chunk)
    if tier == CONF_SINGLE:
        return "[SINGLE-SOURCE CLAIM — asserted once; verify before repeating]"
    if tier == CONF_REPEATED:
        return "[REPEATED BY MODEL ONLY — may be self-echo, not corroboration]"
    return ""  # corroborated needs no warning


def self_confirm_label(chunk: dict) -> str:
    if chunk.get("self_confirm"):
        return "[SELF-CONFIRMED — support traces back to model's own output]"
    return ""
