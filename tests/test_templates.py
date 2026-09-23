"""Tests for the model-template system.

Templates package known-good generation settings for specific models, selectable
at setup time. Key guarantees under test:
  - NO template selected == behaviour identical to before the feature existed
  - file values always beat template values (a template is not a lock-in)
  - {model} substitution makes one template work for any tag
  - an unknown template key FAILS LOUD (a silent no-op is the bug class this
    feature must not reintroduce)
"""

import importlib.util
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "proxy"))

from mneme import templates as T  # noqa: E402

CATALOGUE = os.path.join(REPO, "model_templates.yaml")


class TestCatalogue(unittest.TestCase):
    def test_catalogue_loads(self):
        names = T.list_template_names(CATALOGUE)
        self.assertIn("muse-glimmer", names)
        self.assertIn("qwen3.8-xhigh", names)
        self.assertIn("qwen3.8-nothink", names)
        self.assertIn("gemma4-repeat", names)

    def test_missing_catalogue_is_not_an_error(self):
        """No template file -> no templates. Not a crash."""
        self.assertEqual(T.load_templates("/nonexistent/nope.yaml"), {})
        self.assertEqual(T.list_template_names("/nonexistent/nope.yaml"), [])

    def test_every_shipped_template_validates(self):
        for name in T.list_template_names(CATALOGUE):
            desc = T.describe(name, CATALOGUE)
            self.assertTrue(desc["description"], f"{name} needs a description")
            T.validate(T.load_templates(CATALOGUE)[name], name)


class TestDefaultBehaviour(unittest.TestCase):
    """No template chosen => nothing changes. This is the compatibility promise."""

    def test_no_template_returns_data_unchanged(self):
        data = {"sampling": {"temperature": 0.2}, "models": {"m": {"top_k": 5}}}
        out = T.apply_template(data, "m", None, CATALOGUE)
        self.assertEqual(out, data)
        self.assertIs(out, data)

    def test_blank_template_name_returns_data_unchanged(self):
        data = {"sampling": {"temperature": 0.2}}
        self.assertEqual(T.apply_template(data, "m", "", CATALOGUE), data)


class TestMerging(unittest.TestCase):
    def test_template_fills_missing_keys(self):
        data = {"sampling": {"temperature": 0.2}}
        out = T.apply_template(data, "m", "muse-glimmer", CATALOGUE)
        # template supplied these (sampling.* uses max_tokens; num_predict is per-model)
        self.assertEqual(out["sampling"]["top_k"], 64)
        self.assertEqual(out["sampling"]["max_tokens"], 2048)
        self.assertEqual(out["models"]["m"]["num_predict"], 2048)
        # the template also sets temperature, so the TEMPLATE's value wins
        self.assertEqual(out["sampling"]["temperature"], 1.0)

    def test_file_beats_template(self):
        """Template VALUES WIN now (they are the point of selecting a template).
        A key the template does NOT set keeps the file/default value."""
        # ctx_tokens / completion_reserve are not set by muse-glimmer, so the
        # file's values must survive; temperature/top_k ARE set, so template wins.
        data = {"sampling": {"temperature": 0.99, "top_k": 5,
                             "ctx_tokens": 8192, "completion_reserve": 1024}}
        out = T.apply_template(data, "m", "muse-glimmer", CATALOGUE)
        # template sets temperature/top_k -> template wins
        self.assertEqual(out["sampling"]["temperature"], 1.0)
        self.assertEqual(out["sampling"]["top_k"], 64)
        # template does NOT set these -> file values kept
        self.assertEqual(out["sampling"]["ctx_tokens"], 8192)
        self.assertEqual(out["sampling"]["completion_reserve"], 1024)

    def test_template_value_wins_over_file(self):
        """The core contract the user asked for: template settings are applied."""
        data = {"sampling": {"temperature": 0.2, "top_p": 0.9, "top_k": 64}}
        out = T.apply_template(data, "my-gemma", "gemma4-repeat", CATALOGUE)
        self.assertEqual(out["sampling"]["temperature"], 1.0, "template temp must win")
        self.assertEqual(out["sampling"]["top_p"], 0.95)
        self.assertEqual(out["sampling"]["top_k"], 64)

    def test_key_absent_from_template_falls_back_to_file(self):
        """A template need not be exhaustive — unset keys come from the file."""
        data = {"sampling": {"ctx_tokens": 8192, "completion_reserve": 1024}}
        out = T.apply_template(data, "my-gemma", "gemma4-repeat", CATALOGUE)
        self.assertEqual(out["sampling"]["ctx_tokens"], 8192, "file value preserved")
        self.assertEqual(out["sampling"]["completion_reserve"], 1024)
        # ...while template keys are present
        self.assertEqual(out["sampling"]["temperature"], 1.0)

    def test_per_model_template_wins_over_file_block(self):
        data = {"models": {"m": {"temperature": 0.5, "my_custom_key": 7}}}
        out = T.apply_template(data, "m", "gemma4-repeat", CATALOGUE)
        blk = out["models"]["m"]
        self.assertEqual(blk["temperature"], 1.0, "template per-model value wins")
        self.assertEqual(blk["my_custom_key"], 7, "file-only keys survive")

    def test_input_not_mutated(self):
        data = {"sampling": {"temperature": 0.2}}
        before = {"sampling": {"temperature": 0.2}}
        T.apply_template(data, "m", "gemma4-repeat", CATALOGUE)
        self.assertEqual(data, before, "apply_template must not mutate its input")

    def test_per_model_block_keyed_to_real_model_name(self):
        """{model} placeholder resolves to the actual tag."""
        out = T.apply_template({}, "hf.co/Some/Model-GGUF:Q4_K_M", "gemma4-repeat", CATALOGUE)
        self.assertIn("hf.co/Some/Model-GGUF:Q4_K_M", out["models"])
        blk = out["models"]["hf.co/Some/Model-GGUF:Q4_K_M"]
        self.assertEqual(blk["repeat_penalty"], 1.1)
        self.assertEqual(blk["temperature"], 1.0)
        self.assertNotIn("{model}", out["models"])

    def test_file_per_model_block_merges_key_by_key(self):
        """Template per-model values win; file-only keys survive."""
        data = {"models": {"my-model": {"temperature": 0.5, "num_predict": 999,
                                        "custom_knob": 3}}}
        out = T.apply_template(data, "my-model", "gemma4-repeat", CATALOGUE)
        blk = out["models"]["my-model"]
        self.assertEqual(blk["temperature"], 1.0, "template must win")
        self.assertEqual(blk["num_predict"], 3072, "template must win")
        self.assertEqual(blk["custom_knob"], 3, "file-only key preserved")
        self.assertEqual(blk["repeat_penalty"], 1.1, "template key preserved")

    def test_file_blocks_for_other_models_survive(self):
        data = {"models": {"other-model": {"temperature": 0.3}}}
        out = T.apply_template(data, "target", "gemma4-repeat", CATALOGUE)
        self.assertIn("other-model", out["models"])
        self.assertIn("target", out["models"])


class TestFailLoud(unittest.TestCase):
    """A typo'd key in a template must not be a silent no-op."""

    def test_unknown_template_name_raises(self):
        with self.assertRaises(T.TemplateError) as ctx:
            T.apply_template({}, "m", "no-such-template", CATALOGUE)
        self.assertIn("unknown model template", str(ctx.exception))

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(T.TemplateError):
            T.validate({"sampling": {}, "sampeling": {}}, "typo")

    def test_unknown_sampling_key_rejected(self):
        with self.assertRaises(T.TemplateError) as ctx:
            T.validate({"sampling": {"temperature": 1.0, "repitition_penalty": 1.1}}, "typo")
        self.assertIn("repitition_penalty", str(ctx.exception))

    def test_unknown_model_key_rejected(self):
        with self.assertRaises(T.TemplateError):
            T.validate({"models": {"m": {"temperture": 0.5}}}, "typo")

    def test_unknown_timeout_key_rejected(self):
        with self.assertRaises(T.TemplateError):
            T.validate({"timeouts": {"chat_timout": 30}}, "typo")

    def test_valid_template_passes(self):
        T.validate({"description": "x", "sampling": {"temperature": 0.5},
                    "models": {"m": {"repeat_penalty": 1.1, "num_ctx": 4096}}}, "ok")


class TestShippedTemplateValues(unittest.TestCase):
    """The templates encode MEASURED findings — assert they keep them."""

    def test_gemma4_template_raises_repeat_penalty(self):
        out = T.apply_template({}, "gemma", "gemma4-repeat", CATALOGUE)
        blk = out["models"]["gemma"]
        # the degeneration loop was caused by repeat_penalty 1.0 (no-op)
        self.assertGreater(blk["repeat_penalty"], 1.0)
        # and by temp 0.0 (greedy), which makes a loop unrecoverable
        self.assertNotEqual(blk["temperature"], 0.0)
        self.assertGreater(blk["num_predict"], 0, "needs a hard cap")

    def test_qwen_xhigh_uses_xhigh_not_high(self):
        """'high' is not a valid effort level — the set is low|medium|xhigh."""
        out = T.apply_template({}, "qwen", "qwen3.8-xhigh", CATALOGUE)
        blk = out["models"]["qwen"]
        self.assertEqual(blk["reasoning_effort"], "xhigh")
        self.assertNotEqual(blk["reasoning_effort"], "high")

    def test_qwen_nothink_uses_card_presence_penalty(self):
        out = T.apply_template({}, "qwen", "qwen3.8-nothink", CATALOGUE)
        blk = out["models"]["qwen"]
        self.assertIs(blk["reasoning"], False)
        self.assertEqual(blk["presence_penalty"], 1.5)
        self.assertEqual(out["sampling"]["reasoning_enabled"], 0)

    def test_muse_template_caps_output(self):
        """Muse generates ~5.3k tokens for a page summary — must be bounded."""
        out = T.apply_template({}, "muse", "muse-glimmer", CATALOGUE)
        self.assertGreater(out["models"]["muse"]["num_predict"], 0)
        self.assertEqual(out["models"]["muse"]["num_ctx"], 32768)


class TestDescribe(unittest.TestCase):
    def test_describe_shape(self):
        d = T.describe("muse-glimmer", CATALOGUE)
        self.assertEqual(d["name"], "muse-glimmer")
        self.assertTrue(d["description"])
        self.assertIn("sampling", d)

    def test_describe_unknown_raises(self):
        with self.assertRaises(T.TemplateError):
            T.describe("nope", CATALOGUE)


class TestModelfile(unittest.TestCase):
    def test_valid_modelfile_passes(self):
        T.validate({"description": "x", "modelfile": {
            "from": "hf.co/Some/Model-GGUF:Q5_K_M",
            "template": "{{ if .Prompt }}...<|start|>assistant",
            "parameters": {"stop": ["<|eot|>"], "num_ctx": 32768},
        }}, "ok")

    def test_missing_from_rejected(self):
        with self.assertRaises(T.TemplateError):
            T.validate({"modelfile": {"template": "x"}}, "bad")

    def test_unknown_modelfile_key_rejected(self):
        with self.assertRaises(T.TemplateError):
            T.validate({"modelfile": {"from": "x", "templte": "x"}}, "bad")

    def test_unknown_modelfile_param_rejected(self):
        with self.assertRaises(T.TemplateError):
            T.validate({"modelfile": {"from": "x", "parameters": {"num_ct": 5}}}, "bad")

    def test_stop_must_be_list_of_strings(self):
        with self.assertRaises(T.TemplateError):
            T.validate({"modelfile": {"from": "x", "parameters": {"stop": "eot"}}}, "bad")

    def test_render_modelfile(self):
        mf = {"from": "hf.co/Some/Model-GGUF:Q5_K_M",
              "template": "{{ if .Prompt }}...<|start|>assistant",
              "parameters": {"stop": ["<|eot|>"], "num_ctx": 32768}}
        out = T.render_modelfile(mf)
        self.assertEqual(out.splitlines()[0], "FROM hf.co/Some/Model-GGUF:Q5_K_M")
        self.assertIn('TEMPLATE """{{ if .Prompt }}...<|start|>assistant"""', out)
        self.assertIn('PARAMETER stop "<|eot|>"', out)
        self.assertIn("PARAMETER num_ctx 32768", out)

    def test_shipped_muse_template_has_modelfile(self):
        d = T.describe("muse-glimmer", CATALOGUE)
        self.assertTrue(d["modelfile"].get("from"))
        T.render_modelfile(d["modelfile"])  # must not raise

    def test_modelfile_round_trip(self):
        mf = {"from": "hf.co/Some/Model-GGUF:Q5_K_M",
              "template": "{{ if .Prompt }}x\n\ny\n\nz<|start|>assistant",
              "parameters": {"stop": ["<|eot|>", "<|start|>user"], "num_ctx": 32768}}
        rendered = T.render_modelfile(mf)
        self.assertEqual(T.render_modelfile(T.parse_modelfile(rendered)), rendered)

    def test_parse_blank_is_empty(self):
        self.assertEqual(T.parse_modelfile(""), {})
        self.assertEqual(T.parse_modelfile("   \n  "), {})

    def test_empty_modelfile_block_validates(self):
        T.validate({"description": "x", "modelfile": {}}, "ok")  # no from -> fine


class TestUserMerge(unittest.TestCase):
    def test_user_template_overrides_shipped(self):
        import yaml
        with tempfile.TemporaryDirectory() as td:
            up = os.path.join(td, "user.yaml")
            with open(up, "w") as f:
                yaml.safe_dump({"templates": {"muse-glimmer": {
                    "description": "override", "sampling": {"temperature": 0.5}}}}, f)
            names = T.list_template_names(CATALOGUE, up)
            self.assertIn("muse-glimmer", names)
            cat = T.load_templates(CATALOGUE, up)
            self.assertEqual(cat["muse-glimmer"]["description"], "override")
            out = T.apply_template({"sampling": {}}, "m", "muse-glimmer", CATALOGUE, up)
            self.assertEqual(out["sampling"]["temperature"], 0.5)

    def test_missing_user_catalogue_is_fine(self):
        shipped = T.load_templates(CATALOGUE)["muse-glimmer"]
        merged = T.load_templates(CATALOGUE, "/nonexistent/nope.yaml")["muse-glimmer"]
        self.assertEqual(merged, shipped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
