"""Tests for the swarm orchestrator's folder-IO and control-flow primitives.

Covers swap_dir (atomic inbox freeze), clear_dir (single + list), copy_dir and
move_dir (snapshot / promote), write_dir vs append_dir, get_context (single- and
multi-directory reads), folder-state if conditions (count_ge/count_lt/empty/
exists), and the max_steps safety cap. No model or proxy is involved — these are
pure filesystem + control-flow semantics.

Run:
    /home/sean/mneme/venv/bin/python tests/test_swarm_orchestrator.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extensions", "swarm"))
from swarm_orchestrator import Orchestrator  # noqa: E402


class TestFolderIO(unittest.TestCase):
    def setUp(self):
        # Skip __init__ (no config file needed to exercise these pure-IO methods).
        self.o = Orchestrator.__new__(Orchestrator)
        self.tmp = tempfile.mkdtemp()
        self.raw = os.path.join(self.tmp, "raw")

    def _write(self, name, content):
        p = os.path.join(self.raw, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)

    def test_swap_freezes_inbox_and_recreates(self):
        self._write("a.txt", "A")
        self.o.swap_dir(self.raw)
        self.assertEqual(sorted(os.listdir(self.raw + ".active")), ["a.txt"])
        self.assertEqual(os.listdir(self.raw), [])

    def test_late_write_survives_clear(self):
        self._write("a.txt", "A")
        self.o.swap_dir(self.raw)
        self._write("b.txt", "B")                       # arrives during the tick
        self.o.clear_dir(self.raw + ".active")          # consume the snapshot
        self.assertEqual(os.listdir(self.raw + ".active"), [])   # snapshot drained
        self.assertEqual(sorted(os.listdir(self.raw)), ["b.txt"])  # late write kept

    def test_next_swap_clears_stale_snapshot(self):
        self._write("a.txt", "A")
        self.o.swap_dir(self.raw)
        self._write("b.txt", "B")
        self.o.clear_dir(self.raw + ".active")
        self.o.swap_dir(self.raw)                       # next tick
        self.assertEqual(sorted(os.listdir(self.raw + ".active")), ["b.txt"])
        self.assertEqual(os.listdir(self.raw), [])

    def test_swap_missing_dir(self):
        self.o.swap_dir(self.raw)                       # raw does not exist yet
        self.assertTrue(os.path.isdir(self.raw))
        self.assertTrue(os.path.isdir(self.raw + ".active"))

    def test_clear_missing_dir_is_noop(self):
        self.o.clear_dir(os.path.join(self.tmp, "nope"))  # must not raise

    def test_reader_sees_frozen_snapshot_not_late_writes(self):
        # Full freeze/consume/re-freeze cycle from a READER's point of view:
        # a reader of the frozen snapshot must NOT see files that land in the
        # fresh inbox during the tick, and only sees them after consume+re-freeze.
        self._write("a.txt", "ORIGINAL")
        self.o.swap_dir(self.raw)                     # freeze raw -> raw.active
        self._write("b.txt", "NEW")                   # lands during the tick (in raw)
        frozen = self.o.get_context(self.raw + ".active")
        self.assertIn("ORIGINAL", frozen)
        self.assertNotIn("NEW", frozen)               # reader must NOT see late write
        self.o.clear_dir(self.raw + ".active")        # consume the snapshot
        self.o.swap_dir(self.raw)                     # next tick re-freezes raw
        refrozen = self.o.get_context(self.raw + ".active")
        self.assertIn("NEW", refrozen)                # late write now visible
        self.assertNotIn("ORIGINAL", refrozen)        # consumed snapshot gone

    def test_write_output_named_file(self):
        # write_dir with an extension is a full file path.
        self.o.write_output(os.path.join(self.tmp, "pass1", "a2_synthesis.txt"), "hello")
        p = os.path.join(self.tmp, "pass1", "a2_synthesis.txt")
        self.assertTrue(os.path.isfile(p))
        with open(p) as f:
            self.assertEqual(f.read(), "hello")

    def test_write_output_defaults_to_output_txt(self):
        # write_dir without an extension is a directory -> output.txt inside.
        self.o.write_output(os.path.join(self.tmp, "pass1"), "hello")
        p = os.path.join(self.tmp, "pass1", "output.txt")
        self.assertTrue(os.path.isfile(p))
        with open(p) as f:
            self.assertEqual(f.read(), "hello")

    def test_copy_dir_directory(self):
        # copy_dir copies a directory's CONTENTS into the destination folder.
        src = os.path.join(self.tmp, "inbox")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "story.txt"), "w") as f:
            f.write("the story")
        self.o.copy_dir(src, os.path.join(self.tmp, "buffer"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "buffer", "story.txt")))
        with open(os.path.join(self.tmp, "buffer", "story.txt")) as f:
            self.assertEqual(f.read(), "the story")

    def test_copy_dir_file(self):
        # copy_dir with a FILE source copies it into the destination under its name.
        src_file = os.path.join(self.tmp, "single.txt")
        with open(src_file, "w") as f:
            f.write("solo")
        self.o.copy_dir(src_file, os.path.join(self.tmp, "buffer"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "buffer", "single.txt")))

    def test_copy_dir_missing_source_is_noop(self):
        # A missing source must not raise and must not create the destination.
        self.o.copy_dir(os.path.join(self.tmp, "nope"), os.path.join(self.tmp, "buffer"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "buffer")))

    def test_read_multiple_dirs(self):
        # A LIST read_dir concatenates both directories into one blob, each file
        # headed with its source dir's basename so the model can tell them apart.
        os.makedirs(os.path.join(self.tmp, "buffer"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "pass1"), exist_ok=True)
        with open(os.path.join(self.tmp, "buffer", "story.txt"), "w") as f:
            f.write("ORIGINAL STORY")
        with open(os.path.join(self.tmp, "pass1", "crit.txt"), "w") as f:
            f.write("CRITIQUE")
        ctx = self.o.get_context([os.path.join(self.tmp, "buffer"), os.path.join(self.tmp, "pass1")])
        self.assertIn("ORIGINAL STORY", ctx)
        self.assertIn("CRITIQUE", ctx)
        self.assertIn("buffer/story.txt", ctx)   # disambiguating header
        self.assertIn("pass1/crit.txt", ctx)

    def test_clear_dir_list(self):
        # clear_dir with a LIST wipes several directories in one step.
        os.makedirs(os.path.join(self.tmp, "a"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "b"), exist_ok=True)
        with open(os.path.join(self.tmp, "a", "x.txt"), "w") as f:
            f.write("x")
        with open(os.path.join(self.tmp, "b", "y.txt"), "w") as f:
            f.write("y")
        self.o.clear_dir([os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "a")), [])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "b")), [])

    def test_move_dir_file(self):
        # move_dir moves a FILE into the destination folder under its own name.
        src = os.path.join(self.tmp, "draft", "story.txt")
        os.makedirs(os.path.dirname(src), exist_ok=True)
        with open(src, "w") as f:
            f.write("final text")
        self.o.move_dir(src, os.path.join(self.tmp, "published"))
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "published", "story.txt")))
        with open(os.path.join(self.tmp, "published", "story.txt")) as f:
            self.assertEqual(f.read(), "final text")

    def test_move_dir_missing_source_is_noop(self):
        # A missing source must not raise and must not create the destination.
        self.o.move_dir(os.path.join(self.tmp, "nope"), os.path.join(self.tmp, "published"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "published")))

    def test_append_output_appends(self):
        # append_dir accumulates across calls instead of overwriting.
        p = os.path.join(self.tmp, "log", "verdicts.txt")
        self.o.append_output(p, "line one\n")
        self.o.append_output(p, "line two\n")
        with open(p) as f:
            self.assertEqual(f.read(), "line one\nline two\n")

    def test_count_files(self):
        # _count_files counts non-hidden files; 0 when the dir is missing.
        os.makedirs(os.path.join(self.tmp, "pass1"), exist_ok=True)
        with open(os.path.join(self.tmp, "pass1", "a.txt"), "w") as f:
            f.write("a")
        with open(os.path.join(self.tmp, "pass1", "b.txt"), "w") as f:
            f.write("b")
        self.assertEqual(self.o._count_files(os.path.join(self.tmp, "pass1")), 2)
        self.assertEqual(self.o._count_files(os.path.join(self.tmp, "nope")), 0)

    def test_eval_folder_conditions(self):
        # Folder-state if conditions evaluate against the filesystem.
        d = os.path.join(self.tmp, "pass1")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("a")
        with open(os.path.join(d, "b.txt"), "w") as f:
            f.write("b")
        self.assertTrue(self.o._eval_folder_condition("count_ge", d, 2))
        self.assertFalse(self.o._eval_folder_condition("count_ge", d, 3))
        self.assertTrue(self.o._eval_folder_condition("count_lt", d, 3))
        self.assertFalse(self.o._eval_folder_condition("empty", d, None))
        self.assertTrue(self.o._eval_folder_condition("empty", os.path.join(self.tmp, "nope"), None))
        self.assertTrue(self.o._eval_folder_condition("exists", d, None))
        self.assertFalse(self.o._eval_folder_condition("exists", os.path.join(self.tmp, "nope"), None))

    def test_step_needs_model(self):
        # write_dir/append_dir/string-if need a model; folder actions + folder-if don't.
        self.assertTrue(self.o._step_needs_model({"write_dir": "x"}))
        self.assertTrue(self.o._step_needs_model({"append_dir": "x"}))
        self.assertTrue(self.o._step_needs_model({"if": {"condition": "contains", "value": "x"}}))
        self.assertFalse(self.o._step_needs_model({"if": {"condition": "count_ge", "dir": "pass1", "value": 2}}))
        self.assertFalse(self.o._step_needs_model({"swap_dir": "input"}))
        self.assertFalse(self.o._step_needs_model({"goto": "freeze"}))

    def test_max_steps_caps_loop(self):
        # max_steps stops an otherwise-infinite goto loop.
        import io
        import contextlib
        cfg = os.path.join(self.tmp, "loop.yaml")
        with open(cfg, "w") as f:
            f.write("max_steps: 5\nsteps:\n  - name: a\n    goto: b\n  - name: b\n    goto: a\n")
        o = Orchestrator(cfg)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            o.run()
        self.assertIn("max_steps", buf.getvalue())

    def test_full_flow_smoke(self):
        # End-to-end smoke test with a stubbed model: a freeze -> snapshot ->
        # critic -> consume -> decide(APPROVE) -> finalize -> promote run must
        # complete and land the story in published/. Catches flow-wiring bugs
        # (goto/if targets, action ordering) that unit tests miss.
        import io
        import contextlib
        workdir = os.path.join(self.tmp, "run")
        os.makedirs(os.path.join(workdir, "input"), exist_ok=True)
        with open(os.path.join(workdir, "input", "story.txt"), "w") as f:
            f.write("once upon a time")
        cfg = os.path.join(workdir, "c.yaml")
        with open(cfg, "w") as f:
            f.write(
                "max_steps: 40\n"
                "steps:\n"
                "  - name: freeze\n    swap_dir: input\n"
                "  - name: snapshot\n    copy_dir: input.active\n    copy_to: buffer\n"
                "  - name: critic\n    backend: mneme\n    port: 8080\n"
                "    read_dir: input.active\n    write_dir: pass1/c.txt\n"
                "  - name: consume\n    clear_dir: input.active\n"
                "  - name: decide\n    backend: mneme\n    port: 8080\n    read_dir: pass1\n    write_dir: pass2/v.txt\n"
                "    if:\n      condition: matches\n      value: '(?i)APPROVE'\n      then: finalize\n      else: revise\n"
                "  - name: revise\n    backend: mneme\n    port: 8080\n    read_dir: pass1\n    write_dir: input/next.txt\n    goto: cleanup\n"
                "  - name: cleanup\n    clear_dir: [buffer, pass1, pass2]\n"
                "  - name: loop\n    goto: freeze\n"
                "  - name: finalize\n    backend: mneme\n    port: 8080\n    read_dir: pass1\n    write_dir: output/story.txt\n"
                "  - name: promote\n    move_dir: output/story.txt\n    move_to: published\n"
            )
        o = Orchestrator(cfg)
        # Stub both backends to a fixed reply so no proxy/model is needed.
        o.call_mneme = lambda step, context, retries=0: "APPROVE"
        o.call_ollama = lambda step, context, retries=0: "APPROVE"
        old = os.getcwd()
        os.chdir(workdir)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                o.run()
        finally:
            os.chdir(old)
        self.assertTrue(os.path.isfile(os.path.join(workdir, "published", "story.txt")))


    def test_hot_reload_picks_up_config_edits(self):
        # Editing swarm_config.yaml on disk must reload the flow on the next step.
        cfg = os.path.join(self.tmp, "c.yaml")
        with open(cfg, "w") as f:
            f.write("steps:\n  - name: a\n    goto: b\n  - name: b\n    goto: a\n")
        os.utime(cfg, (1000000000, 1000000000))  # pin an old mtime
        o = Orchestrator(cfg)
        self.assertEqual(len(o.steps), 2)
        # Add a third step with a newer mtime so the reload is detected.
        with open(cfg, "w") as f:
            f.write("steps:\n  - name: a\n    goto: b\n  - name: b\n    goto: a\n  - name: c\n    goto: a\n")
        os.utime(cfg, (2000000000, 2000000000))
        idx = o._maybe_reload(0)
        self.assertEqual(len(o.steps), 3)
        self.assertIn("c", o.name_to_index)
        self.assertEqual(idx, 0)  # step 'a' still anchored at index 0

    def test_hot_reload_keeps_old_config_on_broken_edit(self):
        # A broken edit must NOT crash the flow — keep the last good config.
        cfg = os.path.join(self.tmp, "c.yaml")
        with open(cfg, "w") as f:
            f.write("steps:\n  - name: a\n    goto: b\n  - name: b\n    goto: a\n")
        os.utime(cfg, (1000000000, 1000000000))
        o = Orchestrator(cfg)
        with open(cfg, "w") as f:
            f.write("steps: [unclosed\n")  # malformed YAML
        os.utime(cfg, (2000000000, 2000000000))
        idx = o._maybe_reload(0)
        self.assertEqual(len(o.steps), 2)  # old flow preserved
        self.assertEqual(idx, 0)  # position unchanged

    def test_hot_reload_reanchors_by_name(self):
        # Reordering steps re-anchors the position by NAME, not index.
        cfg = os.path.join(self.tmp, "c.yaml")
        with open(cfg, "w") as f:
            f.write("steps:\n  - name: a\n    goto: b\n  - name: b\n    goto: a\n")
        os.utime(cfg, (1000000000, 1000000000))
        o = Orchestrator(cfg)
        with open(cfg, "w") as f:
            f.write("steps:\n  - name: b\n    goto: a\n  - name: a\n    goto: b\n")  # swapped
        os.utime(cfg, (2000000000, 2000000000))
        idx = o._maybe_reload(0)  # was at index 0 = step 'a'
        self.assertEqual(idx, 1)  # 'a' now lives at index 1


if __name__ == "__main__":
    unittest.main(verbosity=2)
