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
    """In-memory-ish DB with the minimal chunks table the curation code needs.

    `session_id`, `independent_sources`, `self_confirm` and `derived_from` are
    included because list_chunks() reads them — this fixture stands in for the
    real table, and a query over the real table needs the real columns. (They are
    what ensure_schema() would add on a live DB.)
    """
    path = os.path.join(tempfile.mkdtemp(prefix="mneme_cur_"), "t.db")
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY, topic_label TEXT, messages TEXT,
            grade TEXT DEFAULT 'C', trust TEXT DEFAULT '', source TEXT DEFAULT 'unknown',
            created_at TEXT,
            session_id TEXT DEFAULT 'default',
            independent_sources INTEGER DEFAULT 0,
            self_confirm INTEGER DEFAULT 0,
            derived_from TEXT DEFAULT '[]'
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

    def test_self_citation_is_dropped(self):
        """A chunk citing itself is not evidence, and would make lineage cycle."""
        db = _mkdb()
        _chunk(db, "mem_self", source="model")
        r = cur.record_provenance(db, "mem_self", ["mem_self", "mem_other"])
        self.assertEqual(r["derived_from"], ["mem_other"])


class TestRecordContext(unittest.TestCase):
    """injected_chunk_ids is the RELIABLE contamination signal: derived_from needs
    the model to cite what it used, but the proxy knows what it injected."""

    def test_context_and_model_recorded(self):
        db = _mkdb()
        _chunk(db, "mem_c")
        r = cur.record_context(db, "mem_c", ["mem_a", "mem_b"], "gemma4:latest")
        self.assertEqual(r["injected_chunk_ids"], ["mem_a", "mem_b"])
        c = cur._get_chunk(db, "mem_c")
        self.assertEqual(c["injected_chunk_ids"], ["mem_a", "mem_b"])
        self.assertEqual(c["model"], "gemma4:latest")

    def test_self_excluded_and_deduped(self):
        db = _mkdb()
        _chunk(db, "mem_c")
        r = cur.record_context(db, "mem_c", ["mem_c", "mem_a", "mem_a"], "m")
        self.assertEqual(r["injected_chunk_ids"], ["mem_a"])

    def test_unknown_chunk_raises(self):
        db = _mkdb()
        with self.assertRaises(cur.CurationError):
            cur.record_context(db, "mem_nope", [], "m")


class TestLineage(unittest.TestCase):
    """lineage() answers: what was built on top of this chunk?"""

    def test_finds_cited_child(self):
        db = _mkdb()
        _chunk(db, "mem_p")
        _chunk(db, "mem_c")
        cur.record_provenance(db, "mem_c", ["mem_p"])
        lin = cur.lineage(db, "mem_p")
        self.assertEqual([n["chunk_id"] for n in lin["descendants"]], ["mem_c"])
        self.assertEqual(lin["descendants"][0]["relation"], ["cites"])

    def test_finds_context_child(self):
        """The case that citations miss: no citation, but the chunk was in view."""
        db = _mkdb()
        _chunk(db, "mem_p")
        _chunk(db, "mem_c")
        cur.record_context(db, "mem_c", ["mem_p"], "m")
        lin = cur.lineage(db, "mem_p")
        self.assertEqual([n["chunk_id"] for n in lin["descendants"]], ["mem_c"])
        self.assertEqual(lin["descendants"][0]["relation"], ["saw"])

    def test_both_relations_reported(self):
        db = _mkdb()
        _chunk(db, "mem_p")
        _chunk(db, "mem_c")
        cur.record_provenance(db, "mem_c", ["mem_p"])
        cur.record_context(db, "mem_c", ["mem_p"], "m")
        lin = cur.lineage(db, "mem_p")
        self.assertEqual(sorted(lin["descendants"][0]["relation"]), ["cites", "saw"])

    def test_transitive(self):
        db = _mkdb()
        for cid in ("mem_p", "mem_c", "mem_gc"):
            _chunk(db, cid)
        cur.record_provenance(db, "mem_c", ["mem_p"])
        cur.record_provenance(db, "mem_gc", ["mem_c"])
        lin = cur.lineage(db, "mem_p")
        by_id = {n["chunk_id"]: n for n in lin["descendants"]}
        self.assertIn("mem_gc", by_id)
        self.assertEqual(by_id["mem_gc"]["depth"], 2)
        self.assertEqual(by_id["mem_gc"]["via"], "mem_c")

    def test_cycle_terminates(self):
        """A→B and B→A must not loop forever."""
        db = _mkdb()
        _chunk(db, "mem_a")
        _chunk(db, "mem_b")
        cur.record_provenance(db, "mem_a", ["mem_b"])
        cur.record_provenance(db, "mem_b", ["mem_a"])
        lin = cur.lineage(db, "mem_a")
        self.assertLessEqual(lin["count"], 2)

    def test_depth_limit_reported(self):
        db = _mkdb()
        prev = None
        for i in range(8):
            cid = f"mem_{i}"
            _chunk(db, cid)
            if prev:
                cur.record_provenance(db, cid, [prev])
            prev = cid
        lin = cur.lineage(db, "mem_0", max_depth=3)
        self.assertTrue(lin["truncated"], lin)
        self.assertLessEqual(max(n["depth"] for n in lin["descendants"]), 3)

    def test_node_carries_state(self):
        """The caller needs to see how bad each descendant is."""
        db = _mkdb()
        _chunk(db, "mem_p")
        _chunk(db, "mem_c", grade="F", source="model", trust="unverified")
        cur.record_provenance(db, "mem_c", ["mem_p"])
        cur.retract(db, "mem_c", actor="user", reason="bad")
        n = cur.lineage(db, "mem_p")["descendants"][0]
        self.assertEqual(n["grade"], "F")
        self.assertEqual(n["retracted"], "user")
        self.assertEqual(n["trust"], "unverified")

    def test_unknown_root_raises(self):
        db = _mkdb()
        with self.assertRaises(cur.CurationError):
            cur.lineage(db, "mem_nope")

    def test_no_descendants_is_empty_not_error(self):
        db = _mkdb()
        _chunk(db, "mem_lonely")
        lin = cur.lineage(db, "mem_lonely")
        self.assertEqual(lin["count"], 0)
        self.assertEqual(lin["descendants"], [])


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


class TestRemovedFlag(unittest.TestCase):
    """The management flag. NOT a delete: the row and content stay, and the flag
    only changes what Mneme USES (injection + model search)."""

    def test_default_state_is_injectable(self):
        db = _mkdb()
        _chunk(db, "mem_r")
        self.assertEqual(cur._get_chunk(db, "mem_r")["removed"], "removed" if False else "injectable")

    def test_set_removed_marks_and_reports(self):
        db = _mkdb()
        _chunk(db, "mem_r")
        out = cur.set_removed(db, "mem_r", True, actor="user", reason="junk")
        self.assertEqual(out["removed"], "removed")
        self.assertEqual(out["previous"], "injectable")

    def test_content_survives_the_flag(self):
        """The whole point: nothing is deleted."""
        db = _mkdb()
        _chunk(db, "mem_r", messages='[{"role":"user","content":"keep me"}]')
        cur.set_removed(db, "mem_r", True, actor="user")
        ch = cur._get_chunk(db, "mem_r")
        self.assertIn("keep me", ch["messages"])

    def test_unflag_is_reversible(self):
        db = _mkdb()
        _chunk(db, "mem_r")
        cur.set_removed(db, "mem_r", True, actor="user", reason="x")
        cur.set_removed(db, "mem_r", False, actor="user")
        ch = cur._get_chunk(db, "mem_r")
        self.assertEqual(ch["removed"], "injectable")
        self.assertEqual(ch["removed_reason"], "")

    def test_is_removed_helper(self):
        self.assertTrue(cur.is_removed({"removed": "removed"}))
        self.assertFalse(cur.is_removed({"removed": "injectable"}))
        self.assertFalse(cur.is_removed({}))
        self.assertFalse(cur.is_removed(None))

    def test_unknown_chunk_raises_rather_than_noop(self):
        db = _mkdb()
        with self.assertRaises(cur.CurationError):
            cur.set_removed(db, "mem_nope", True)

    def test_bad_actor_rejected(self):
        db = _mkdb()
        _chunk(db, "mem_r")
        with self.assertRaises(cur.CurationError):
            cur.set_removed(db, "mem_r", True, actor="hacker")

    def test_flag_is_logged(self):
        db = _mkdb()
        _chunk(db, "mem_r")
        cur.set_removed(db, "mem_r", True, actor="user", reason="why not")
        rows = db.execute(
            "SELECT action, actor, reason FROM curation_log WHERE chunk_id='mem_r'"
        ).fetchall()
        self.assertTrue(any(r[0] == "remove" and r[1] == "user" for r in rows), rows)


class TestChunkListing(unittest.TestCase):
    """list_chunks powers the management page's filters."""

    def _seed(self, db):
        _chunk(db, "mem_a", topic="alpha topic", source="model", grade="A",
               messages='[{"role":"assistant","content":"the price is 42"}]')
        _chunk(db, "mem_b", topic="beta topic", source="user", grade="F",
               messages='[{"role":"user","content":"pizza order note"}]')
        db.execute("UPDATE chunks SET model='gemma4', created_at='2026-08-01T10:00:00' WHERE chunk_id='mem_a'")
        db.execute("UPDATE chunks SET model='qwen3', created_at='2026-08-05T10:00:00' WHERE chunk_id='mem_b'")
        db.commit()

    def test_returns_all_by_default(self):
        db = _mkdb(); self._seed(db)
        out = cur.list_chunks(db, {})
        self.assertEqual(out["total"], 2)

    def test_keyword_matches_content(self):
        db = _mkdb(); self._seed(db)
        out = cur.list_chunks(db, {"keyword": "price"})
        self.assertEqual([c["chunk_id"] for c in out["chunks"]], ["mem_a"])

    def test_keyword_matches_topic_label(self):
        """Users search by the label they can SEE, which is often not in content."""
        db = _mkdb(); self._seed(db)
        out = cur.list_chunks(db, {"keyword": "beta topic"})
        self.assertEqual([c["chunk_id"] for c in out["chunks"]], ["mem_b"])

    def test_source_filter(self):
        db = _mkdb(); self._seed(db)
        self.assertEqual([c["chunk_id"] for c in cur.list_chunks(db, {"source": "user"})["chunks"]], ["mem_b"])

    def test_model_filter(self):
        db = _mkdb(); self._seed(db)
        self.assertEqual([c["chunk_id"] for c in cur.list_chunks(db, {"model": "gemma4"})["chunks"]], ["mem_a"])

    def test_grade_filter(self):
        db = _mkdb(); self._seed(db)
        self.assertEqual([c["chunk_id"] for c in cur.list_chunks(db, {"grade": "F"})["chunks"]], ["mem_b"])

    def test_date_range(self):
        db = _mkdb(); self._seed(db)
        out = cur.list_chunks(db, {"since": "2026-08-04", "until": "2026-08-06"})
        self.assertEqual([c["chunk_id"] for c in out["chunks"]], ["mem_b"])

    def test_bare_date_until_is_inclusive(self):
        db = _mkdb(); self._seed(db)
        out = cur.list_chunks(db, {"since": "2026-08-05", "until": "2026-08-05"})
        self.assertEqual([c["chunk_id"] for c in out["chunks"]], ["mem_b"])

    def test_removed_filter_both_states(self):
        db = _mkdb(); self._seed(db)
        cur.set_removed(db, "mem_a", True, actor="user")
        self.assertEqual([c["chunk_id"] for c in cur.list_chunks(db, {"removed": "removed"})["chunks"]], ["mem_a"])
        self.assertEqual([c["chunk_id"] for c in cur.list_chunks(db, {"removed": "injectable"})["chunks"]], ["mem_b"])
        self.assertEqual(cur.list_chunks(db, {})["total"], 2, "no filter must show BOTH")

    def test_order(self):
        db = _mkdb(); self._seed(db)
        self.assertEqual([c["chunk_id"] for c in cur.list_chunks(db, {"order": "oldest"})["chunks"]],
                         ["mem_a", "mem_b"])

    def test_uncorroborated_filter(self):
        db = _mkdb(); self._seed(db)
        out = cur.list_chunks(db, {"uncorroborated": True})
        self.assertEqual(out["total"], 2, "both start with 0 independent sources")

    def test_self_confirm_filter(self):
        db = _mkdb(); self._seed(db)
        db.execute("UPDATE chunks SET self_confirm=1 WHERE chunk_id='mem_a'"); db.commit()
        self.assertEqual([c["chunk_id"] for c in cur.list_chunks(db, {"self_confirm": True})["chunks"]], ["mem_a"])

    def test_unknown_filter_values_are_ignored_not_applied(self):
        """A bad value must not silently filter to nothing. An unrecognised
        `removed` is ignored (both shown); an impossible grade legitimately
        matches nothing, which is why the two are asserted separately."""
        db = _mkdb(); self._seed(db)
        self.assertEqual(cur.list_chunks(db, {"removed": "banana"})["total"], 2,
                         "unknown removed value must be ignored")
        self.assertEqual(cur.list_chunks(db, {"grade": "Z"})["total"], 0,
                         "an impossible grade matches nothing (correct)")

    def test_preview_is_populated(self):
        db = _mkdb(); self._seed(db)
        row = next(c for c in cur.list_chunks(db, {"grade": "A"})["chunks"])
        self.assertIn("price", row["preview"])

    def test_pagination(self):
        db = _mkdb(); self._seed(db)
        out = cur.list_chunks(db, {}, limit=1, offset=0)
        self.assertEqual(len(out["chunks"]), 1)
        self.assertEqual(out["total"], 2, "total counts all matches, not the page")


class TestBadChunk(unittest.TestCase):
    """"Bad chunk" is a MARKER, not a decision. It records that a chunk is
    suspected-wrong and changes nothing about what Mneme uses. The page renders it
    amber when the model set it and red when the user did."""

    def test_set_by_model(self):
        db = _mkdb(); _chunk(db, "mem_b")
        out = cur.set_bad_chunk(db, "mem_b", True, actor="model", reason="contradicted later")
        self.assertEqual(out["bad_chunk"], "model")
        c = cur._get_chunk(db, "mem_b")
        self.assertEqual(c["proposed_retract"], "model")
        self.assertEqual(c["proposed_reason"], "contradicted later")

    def test_set_by_user(self):
        db = _mkdb(); _chunk(db, "mem_b")
        cur.set_bad_chunk(db, "mem_b", True, actor="user")
        self.assertEqual(cur._get_chunk(db, "mem_b")["proposed_retract"], "user")

    def test_clear_is_a_toggle(self):
        db = _mkdb(); _chunk(db, "mem_b")
        cur.set_bad_chunk(db, "mem_b", True, actor="model", reason="x")
        cur.set_bad_chunk(db, "mem_b", False, actor="user")
        c = cur._get_chunk(db, "mem_b")
        self.assertEqual(c["proposed_retract"], "")
        self.assertEqual(c["proposed_reason"], "", "reason must clear with the flag")

    def test_marker_does_not_remove(self):
        """The critical property: flagging is not removing."""
        db = _mkdb(); _chunk(db, "mem_b")
        cur.set_bad_chunk(db, "mem_b", True, actor="model")
        c = cur._get_chunk(db, "mem_b")
        self.assertEqual(c["removed"], "injectable", "flagging must not remove")
        self.assertEqual(c["retracted"], "", "flagging must not retract")

    def test_model_can_flag_but_not_pick_user(self):
        """A model-set flag must not be able to masquerade as a user decision."""
        db = _mkdb(); _chunk(db, "mem_b")
        cur.set_bad_chunk(db, "mem_b", True, actor="model")
        self.assertNotEqual(cur._get_chunk(db, "mem_b")["proposed_retract"], "user")

    def test_bad_actor_rejected(self):
        db = _mkdb(); _chunk(db, "mem_b")
        with self.assertRaises(cur.CurationError):
            cur.set_bad_chunk(db, "mem_b", True, actor="system")

    def test_unknown_chunk_raises(self):
        db = _mkdb()
        with self.assertRaises(cur.CurationError):
            cur.set_bad_chunk(db, "mem_nope", True)

    def test_bulk_reports_per_id(self):
        db = _mkdb(); _chunk(db, "mem_b")
        out = cur.set_bad_chunk_many(db, ["mem_b", "mem_nope"], bad=True, actor="user")
        self.assertEqual(out["changed"], ["mem_b"])
        self.assertEqual(len(out["failed"]), 1)

    def test_logged_with_actor(self):
        db = _mkdb(); _chunk(db, "mem_b")
        cur.set_bad_chunk(db, "mem_b", True, actor="model")
        cur.set_bad_chunk(db, "mem_b", False, actor="user")
        rows = db.execute(
            "SELECT action, actor FROM curation_log WHERE chunk_id='mem_b' ORDER BY rowid"
        ).fetchall()
        self.assertIn(("bad_chunk", "model"), rows)
        self.assertIn(("bad_chunk_cleared", "user"), rows)


class TestBadChunkLabel(unittest.TestCase):
    """A flagged chunk still injects (the flag is a marker, not a decision), so it
    must ANNOUNCE that it is flagged. Without this the model sees a chunk it
    itself flagged yesterday looking exactly like a trusted one."""

    def test_flagged_chunk_gets_a_label(self):
        lab = cur.bad_chunk_label({"proposed_retract": "model"})
        self.assertTrue(lab)
        self.assertIn("FLAGGED", lab)
        self.assertIn("the model", lab)

    def test_user_flag_reads_differently(self):
        lab = cur.bad_chunk_label({"proposed_retract": "user"})
        self.assertIn("the user", lab)
        self.assertNotIn("the model", lab)

    def test_clean_chunk_has_no_label(self):
        self.assertEqual(cur.bad_chunk_label({}), "")
        self.assertEqual(cur.bad_chunk_label({"proposed_retract": ""}), "")

    def test_reason_is_included(self):
        lab = cur.bad_chunk_label({"proposed_retract": "model",
                                   "proposed_reason": "contradicts a fetched page"})
        self.assertIn("contradicts a fetched page", lab)

    def test_reason_is_truncated(self):
        lab = cur.bad_chunk_label({"proposed_retract": "model",
                                   "proposed_reason": "x" * 5000})
        self.assertLess(len(lab), 400, "a huge reason must not blow up the header")

    def test_language_is_suspicion_not_prohibition(self):
        """A flag is an unverified suspicion, possibly self-raised — it must not
        read like the stronger [RETRACTED ... DO NOT TRUST] tag."""
        lab = cur.bad_chunk_label({"proposed_retract": "model"})
        self.assertIn("treat with suspicion", lab)
        self.assertNotIn("DO NOT TRUST", lab)

    def test_distinct_from_retraction_label(self):
        """The two labels must not be confused: retraction is a decision, the flag
        is not."""
        flagged = cur.bad_chunk_label({"proposed_retract": "model"})
        retracted = cur.retraction_label({"retracted": "user", "retracted_by": "user"})
        self.assertNotEqual(flagged, retracted)
        self.assertIn("RETRACTED", retracted)
        self.assertNotIn("RETRACTED", flagged)

    def test_label_survives_a_missing_key(self):
        """Every injected chunk dict must carry the key; if one doesn't, the label
        must degrade to empty rather than raise."""
        self.assertEqual(cur.bad_chunk_label({"chunk_id": "x"}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
