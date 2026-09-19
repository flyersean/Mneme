"""Tests for memory curation: retraction, recurrence, provenance, decision log.

The failure this guards against: a hallucinated fact gets saved, re-injected,
and becomes self-reinforcing. The model then "validates" its own earlier claim
and that validation is stored as though it were evidence. Before this, the only
way to remove bad memory was a full /reset — there was no way for a human or the
model to say "this specific chunk is false".
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))

from mneme import curation as cur  # noqa: E402


def _mkdb():
    """In-memory-ish DB with the minimal chunks table the curation code needs."""
    path = os.path.join(tempfile.mkdtemp(prefix="mneme_cur_"), "t.db")
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY, topic_label TEXT, messages TEXT,
            grade TEXT DEFAULT 'C', trust TEXT DEFAULT '', source TEXT DEFAULT 'unknown',
            created_at TEXT
        )
    """)
    cur.ensure_schema(db)
    return db


def _chunk(db, cid, topic="t", messages="[]", source="unknown", grade="C", trust=""):
    db.execute(
        "INSERT INTO chunks (chunk_id, topic_label, messages, source, grade, trust, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (cid, topic, messages, source, grade, trust, "2026-01-01T00:00:00"),
    )
    db.commit()
    return cid


class TestSchema(unittest.TestCase):
    def test_ensure_schema_is_idempotent(self):
        db = _mkdb()
        cur.ensure_schema(db)   # second call must not raise
        cur.ensure_schema(db)
        cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)").fetchall()}
        for c in ("retracted", "retracted_by", "retracted_reason", "assert_count",
                  "independent_sources", "derived_from", "self_confirm",
                  "proposed_retract"):
            self.assertIn(c, cols, f"missing column {c}")
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("curation_log", tables)

    def test_new_chunks_default_to_not_retracted(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        c = cur._get_chunk(db, "mem_1")
        self.assertEqual(c["retracted"], "")
        self.assertFalse(cur.is_retracted(c))
        self.assertEqual(c["assert_count"], 1)
        self.assertEqual(c["self_confirm"], False)


class TestRetraction(unittest.TestCase):
    def test_user_retract_marks_and_logs(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        r = cur.retract(db, "mem_1", actor="user", reason="that price is wrong")
        self.assertEqual(r["retracted"], cur.RETRACT_USER)
        c = cur._get_chunk(db, "mem_1")
        self.assertTrue(cur.is_retracted(c))
        self.assertEqual(c["retracted_by"], "user")
        self.assertIn("price is wrong", c["retracted_reason"])
        log = cur.list_log(db, chunk_id="mem_1")
        self.assertEqual(log[0]["action"], "retract")
        self.assertEqual(log[0]["actor"], "user")

    def test_restore_reverses_retraction(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        cur.retract(db, "mem_1", actor="user", reason="wrong")
        cur.restore(db, "mem_1", actor="user", reason="actually correct")
        c = cur._get_chunk(db, "mem_1")
        self.assertFalse(cur.is_retracted(c))
        self.assertEqual(c["retracted"], "")
        actions = [e["action"] for e in cur.list_log(db, chunk_id="mem_1")]
        self.assertIn("restore", actions)

    def test_undo_last_restores_previous_state(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        cur.retract(db, "mem_1", actor="user", reason="oops")
        out = cur.undo_last(db, "mem_1")
        self.assertEqual(out["undone"], "retract")
        self.assertFalse(cur.is_retracted(cur._get_chunk(db, "mem_1")))

    def test_unknown_chunk_raises(self):
        db = _mkdb()
        with self.assertRaises(cur.CurationError):
            cur.retract(db, "nope", actor="user")
        with self.assertRaises(cur.CurationError):
            cur.restore(db, "nope")
        with self.assertRaises(cur.CurationError):
            cur.propose_retract(db, "nope")

    def test_bad_actor_rejected(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        with self.assertRaises(cur.CurationError):
            cur.retract(db, "mem_1", actor="hacker")


class TestProposalQueue(unittest.TestCase):
    """Model may PROPOSE; only the user disposes. A pending proposal must never
    affect retrieval — a wrong model judgement cannot silently hide truth."""

    def test_propose_does_not_retract(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        cur.propose_retract(db, "mem_1", reason="I think this is stale")
        c = cur._get_chunk(db, "mem_1")
        self.assertFalse(cur.is_retracted(c), "a proposal must not retract")
        self.assertEqual(c["proposed_retract"], "model")

    def test_proposal_appears_in_review_queue(self):
        db = _mkdb()
        _chunk(db, "mem_1", topic="prices")
        cur.propose_retract(db, "mem_1", reason="contradicts the menu")
        q = cur.list_proposals(db)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["chunk_id"], "mem_1")
        self.assertEqual(q[0]["topic_label"], "prices")
        self.assertIn("menu", q[0]["reason"])

    def test_deny_clears_proposal_and_logs(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        cur.propose_retract(db, "mem_1", reason="suspect")
        cur.deny_proposal(db, "mem_1", reason="it is actually correct")
        self.assertEqual(cur.list_proposals(db), [])
        c = cur._get_chunk(db, "mem_1")
        self.assertEqual(c["retracted"], "", "denying must not retract")
        actions = [e["action"] for e in cur.list_log(db, chunk_id="mem_1")]
        self.assertIn("deny", actions)

    def test_confirming_a_proposal_retracts_as_user(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        cur.propose_retract(db, "mem_1", reason="suspect")
        cur.retract(db, "mem_1", actor="user", reason="confirmed false")
        c = cur._get_chunk(db, "mem_1")
        self.assertTrue(cur.is_retracted(c))
        self.assertEqual(c["retracted_by"], "user")   # attribution follows the decision
        self.assertEqual(c["proposed_retract"], "", "proposal cleared on confirm")
        self.assertEqual(cur.list_proposals(db), [])


class TestRecurrence(unittest.TestCase):
    def test_model_restatement_is_not_independent(self):
        """The hallucinated-validation shape: the model repeating itself must not
        look like corroboration."""
        db = _mkdb()
        _chunk(db, "mem_1", source="model")
        cur.bump_recurrence(db, "mem_1", "the price is $8", "model")
        cur.bump_recurrence(db, "mem_1", "the price is $8", "model")
        c = cur._get_chunk(db, "mem_1")
        self.assertEqual(c["assert_count"], 3)         # 1 initial + 2
        self.assertEqual(c["independent_sources"], 0)  # still nobody else
        self.assertEqual(cur.confidence_tier(c), cur.CONF_REPEATED)

    def test_user_assertion_counts_as_independent(self):
        db = _mkdb()
        _chunk(db, "mem_1", source="user")
        cur.bump_recurrence(db, "mem_1", "the price is $13.49", "user")
        c = cur._get_chunk(db, "mem_1")
        self.assertEqual(c["independent_sources"], 1)
        self.assertEqual(cur.confidence_tier(c), cur.CONF_CORROBORATED)

    def test_page_source_counts_as_independent(self):
        db = _mkdb()
        _chunk(db, "mem_1", source="page:example.com")
        cur.bump_recurrence(db, "mem_1", "price $13.49", "page:example.com")
        c = cur._get_chunk(db, "mem_1")
        self.assertEqual(cur.confidence_tier(c), cur.CONF_CORROBORATED)

    def test_tool_source_counts_as_independent(self):
        db = _mkdb()
        _chunk(db, "mem_1", source="tool:terminal")
        cur.bump_recurrence(db, "mem_1", "x", "tool:terminal")
        self.assertEqual(cur.confidence_tier(cur._get_chunk(db, "mem_1")), cur.CONF_CORROBORATED)

    def test_single_assertion_is_single_tier(self):
        db = _mkdb()
        _chunk(db, "mem_1", source="model")
        c = cur._get_chunk(db, "mem_1")
        self.assertEqual(cur.confidence_tier(c), cur.CONF_SINGLE)


class TestProvenance(unittest.TestCase):
    def test_provenance_recorded(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        r = cur.record_provenance(db, "mem_1", ["mem_a", "mem_b"])
        self.assertEqual(r["derived_from"], ["mem_a", "mem_b"])
        self.assertEqual(cur._get_chunk(db, "mem_1")["derived_from"], ["mem_a", "mem_b"])

    def test_self_confirmation_detected(self):
        """A model chunk citing another model chunk, with no independent source
        of its own, is a self-confirmation loop — not validation."""
        db = _mkdb()
        _chunk(db, "mem_orig", source="model")
        _chunk(db, "mem_echo", source="model")
        cur.record_provenance(db, "mem_echo", ["mem_orig"])
        out = cur.detect_self_confirmation(db, "mem_echo", source="model")
        self.assertTrue(out["self_confirm"], out)
        self.assertIn("mem_orig", out["cited_model_chunks"])
        self.assertTrue(cur._get_chunk(db, "mem_echo")["self_confirm"])

    def test_independent_source_defeats_self_confirmation(self):
        db = _mkdb()
        _chunk(db, "mem_orig", source="model")
        _chunk(db, "mem_ok", source="model")
        cur.record_provenance(db, "mem_ok", ["mem_orig"])
        cur.bump_recurrence(db, "mem_ok", "claim", "page:example.com")  # someone else agrees
        out = cur.detect_self_confirmation(db, "mem_ok", source="model")
        self.assertFalse(out["self_confirm"], "independent support must clear the flag")

    def test_citing_a_user_chunk_is_not_self_confirmation(self):
        db = _mkdb()
        _chunk(db, "mem_user", source="user")
        _chunk(db, "mem_m", source="model")
        cur.record_provenance(db, "mem_m", ["mem_user"])
        out = cur.detect_self_confirmation(db, "mem_m", source="model")
        self.assertFalse(out["self_confirm"])
        self.assertEqual(out["cited_model_chunks"], [])

    def test_non_model_chunk_is_never_self_confirming(self):
        db = _mkdb()
        _chunk(db, "mem_orig", source="model")
        _chunk(db, "mem_page", source="page:x.com")
        cur.record_provenance(db, "mem_page", ["mem_orig"])
        out = cur.detect_self_confirmation(db, "mem_page", source="page:x.com")
        self.assertFalse(out["self_confirm"])

    def test_missing_parent_does_not_crash(self):
        db = _mkdb()
        _chunk(db, "mem_m", source="model")
        cur.record_provenance(db, "mem_m", ["mem_gone"])
        out = cur.detect_self_confirmation(db, "mem_m", source="model")
        self.assertFalse(out["self_confirm"])


class TestLabels(unittest.TestCase):
    def test_retracted_label_is_explicit(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        cur.retract(db, "mem_1", actor="user", reason="false")
        label = cur.retraction_label(cur._get_chunk(db, "mem_1"))
        self.assertIn("RETRACTED", label)
        self.assertIn("DO NOT TRUST", label)

    def test_no_label_when_not_retracted(self):
        db = _mkdb()
        _chunk(db, "mem_1")
        self.assertEqual(cur.retraction_label(cur._get_chunk(db, "mem_1")), "")

    def test_single_source_warns(self):
        db = _mkdb()
        _chunk(db, "mem_1", source="model")
        self.assertIn("SINGLE-SOURCE", cur.confidence_label(cur._get_chunk(db, "mem_1")))

    def test_repeated_by_model_only_warns(self):
        db = _mkdb()
        _chunk(db, "mem_1", source="model")
        cur.bump_recurrence(db, "mem_1", "x", "model")
        label = cur.confidence_label(cur._get_chunk(db, "mem_1")).lower()
        self.assertIn("self-echo", label)
        self.assertIn("repeated", label)

    def test_corroborated_has_no_warning(self):
        db = _mkdb()
        _chunk(db, "mem_1", source="page:x")
        cur.bump_recurrence(db, "mem_1", "x", "page:x")
        self.assertEqual(cur.confidence_label(cur._get_chunk(db, "mem_1")), "")

    def test_self_confirm_label(self):
        db = _mkdb()
        _chunk(db, "mem_o", source="model")
        _chunk(db, "mem_e", source="model")
        cur.record_provenance(db, "mem_e", ["mem_o"])
        cur.detect_self_confirmation(db, "mem_e", source="model")
        self.assertIn("SELF-CONFIRMED", cur.self_confirm_label(cur._get_chunk(db, "mem_e")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
