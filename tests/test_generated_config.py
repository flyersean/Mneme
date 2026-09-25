"""The setup wizard's generated mneme.yaml must use only valid config keys — the
proxy rejects unknown keys at startup. This guards against adding a knob to the
template without registering it in _CONFIG_ENV_MAP."""

import os
import sys
import tempfile
import unittest

# ── Isolated environment BEFORE importing the proxy ──────────────────────────
_TMP = tempfile.mkdtemp(prefix="mneme_test_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty_config.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_MODEL"] = "test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"
os.environ["MNEME_ASK_REUSABLE"] = "0"
os.environ["MNEME_MEMORY_ONLY"] = "0"
os.environ["MNEME_TOPIC_SWITCH_GRACE"] = "0"
os.environ["MNEME_MAX_PER_TOPIC"] = "0"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
import yaml  # noqa: E402
import mneme_proxy as mp  # noqa: E402
import mneme_setup as setup  # noqa: E402


def _generated():
    y = setup._common_yaml("/tmp/inst", "/tmp/inst/mneme.db", 8080, "true", 64000, "false")
    y = (y.replace("@@BTYPE@@", "openai").replace("@@BPROV@@", "openrouter")
          .replace("@@MAIN@@", "test-model").replace("@@EMBED@@", "test-embed")
          .replace("@@LABEL@@", "test-label"))
    return yaml.safe_load(y)


class TestGeneratedConfig(unittest.TestCase):
    def test_all_sections_present(self):
        data = _generated()
        for sec in ("backend", "providers", "sampling", "timeouts", "storage",
                    "retrieval", "caps", "tools", "models"):
            self.assertIn(sec, data)

    def test_tools_section_complete(self):
        data = _generated()
        for t in ("native", "search_memory", "list_tools", "read_tool",
                  "read_file", "fetch_url", "web_search"):
            self.assertIn(t, data["tools"])

    def test_every_key_is_a_valid_config_key(self):
        data = _generated()
        for section, val in data.items():
            if section in mp._STRUCTURAL_SECTIONS:
                continue  # providers/models are free-form (read by provider resolution)
            if section in mp._CONFIG_ENV_MAP:
                continue  # top-level scalar key
            if section in mp._CONFIG_PASSTHROUGH_KEYS:
                continue  # handled specially, not env-mapped (e.g. model_template)
            self.assertIsInstance(val, dict, f"section '{section}' must be a mapping")
            for key in val:
                flat = f"{section}.{key}"
                self.assertIn(flat, mp._CONFIG_ENV_MAP, f"unknown config key: {flat}")

    def test_model_template_key_present_and_documented(self):
        """The generated config must carry `model_template` (blank by default),
        and the proxy must accept it as a recognised key — otherwise selecting a
        template in the wizard would produce a config the proxy rejects."""
        data = _generated()
        self.assertIn("model_template", data, "generated config should advertise the key")
        self.assertIn("model_template", mp._CONFIG_PASSTHROUGH_KEYS)


class TestModelNameLength(unittest.TestCase):
    """Ollama rejects model names longer than 80 chars ("invalid model name").
    The wizard derives names from base-model paths (HF paths, quant tags) which
    can exceed that; both derived-name helpers must cap deterministically."""

    def _long_base(self):
        return "hf.co/Blackfrost-AI/" + "SomeModel-" * 12 + "GGUF:Q4_K_M"

    def test_derived_name_stays_within_limit(self):
        name = setup._derived_model_name(self._long_base(), 128000)
        self.assertLessEqual(len(name), 80)

    def test_modelfile_name_caps_the_real_failure(self):
        # The exact case that broke on the pod: muse-glimmer applied to the
        # derived 128k context model produced an 89-char name Ollama rejected.
        chosen = "mneme-chat-hf-co-blackfrost-ai-muse-glimmer-30b-abliterated-gguf-q4-k-m-128k"
        name = setup._modelfile_model_name("muse-glimmer", chosen)
        self.assertLessEqual(len(name), 80)

    def test_capped_names_unique_and_deterministic(self):
        a = setup._modelfile_model_name("muse-glimmer", "model-" + "a" * 60)
        b = setup._modelfile_model_name("muse-glimmer", "model-" + "b" * 60)
        self.assertNotEqual(a, b)
        self.assertEqual(a, setup._modelfile_model_name("muse-glimmer", "model-" + "a" * 60))

    def test_short_name_unchanged(self):
        # Under the limit, the name must not be hashed/truncated (no change for
        # existing valid installs).
        self.assertEqual(setup._modelfile_model_name("muse-glimmer", "qwen3.8"),
                         "muse-glimmer-qwen3-8")


class TestStartScript(unittest.TestCase):
    """Generated start scripts must free their own port before starting, so a
    re-run is a clean stop-and-restart instead of a bind conflict."""

    def _has_port_free(self, path):
        with open(path) as f:
            body = f.read()
        return ("ss -ltnp" in body and "kill -9" in body and "${MNEME_PORT}" in body)

    def test_main_start_script_frees_port(self):
        d = tempfile.mkdtemp()
        p = setup.write_start_script(
            "ollama", {"model": "test-model", "embed_model": "test-embed",
                       "label_model": "test-label"}, 8080, d)
        self.assertTrue(self._has_port_free(p),
                        "start_proxy.sh must free the port before starting")

    def test_instance_start_script_frees_port(self):
        d = tempfile.mkdtemp()
        p = setup.write_instance_start_script(
            os.path.join(d, "8081"), d, 8081, "ollama", "test-model",
            "test-embed", "ollama", "test-label", "ollama", "1", "0")
        self.assertTrue(self._has_port_free(p),
                        "start_proxy_<port>.sh must free the port before starting")


if __name__ == "__main__":
    unittest.main()
