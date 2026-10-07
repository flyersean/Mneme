"""
Mneme — Model-agnostic memory through text injection.

Architecture:
  Hermes/JAN → Flask proxy (:8080) → Ollama (:11434)
  
Storage: SQLite + FAISS (binary vectors, not JSON files)
Routing: Model-generated topic labels + FAISS similarity
Injection: Raw text chunks framed as memory, not instruction

Key patterns from raw-k-cache preserved:
  - Grade-aware recall ordering (A→F, same GRADE_PRIORITY)
  - Topic grouping with sibling loading
  - Two-pass dedup routing
  - CLASSIFY_THRESHOLD = 0.78 (same as KV version)
  - Staging buffer with auto-archive
  - Self-consistency grading (A/B/C/F)

Dependencies: ollama, requests, numpy, faiss-cpu
"""

import json, os, re, sqlite3, sys, threading, time, uuid, struct, queue, ast, shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import numpy as np
import requests
import subprocess

from mneme.util import _extract_text, _log_error, _split_content, _image_bytes_from_block, _image_token_estimate, _to_ollama_messages, _sniff_mime, _mime_to_ext
from mneme.logfile import setup_logging
from mneme.tool_trail import (
    _TOOL_TAG_RE,
    _extract_tool_tags,
    _FAILURE_MARKERS,
    _classify_tool_outcome,
    _extract_tool_outcomes,
    _extract_combined_tool_trail,
    _tool_failure_nudge,
    _recent_attempts_summary,
)
from mneme.instructions import _load_instruction, materialize_instructions, list_instructions, save_instruction, _instructions_dir, _live_instruction_path
from mneme.overcome import (
    _detect_stuck,
    _overcome_directive,
    _parse_deliberation,
    _in_build_mode,
    _build_tool_calls,
    _build_directive,
    _build_exhausted_directive,
    _in_reuse_mode,
    _reuse_directive,
    _reuse_tool_info,
    _synthesize_nudge,
    _hard_wrapup_directive,
    _write_script_nudge,
    _step_back_directive,
    _save_tool,
    _record_overcome,
    _tool_directive,
    _handle_overcome_reply,
    BUILD_MAX_ITERATIONS,
    BUILD_MAX_TOOL_CALLS,
    MAX_SERVER_ROUNDS,
)
import mneme.capability as capability
from mneme.capability import (
    _record_capability,
    _is_capability_edge,
    _capability_directive,
    _classify_problem_type,
)
import mneme.grading as grading
from mneme.grading import (
    _extract_provenance,
    _grade_from_provenance,
    _parse_inline_provenance,
    _grade_inline,
    _extract_mem_ids,
    _extract_urls_from_toolcalls,
    _extract_urls_from_messages,
    _extract_urls_from_tool_trace,
    _source_domain,
    _has_fake_source,
    _has_specific_claims,
    _is_honest_terminal,
    _verify_and_regrade,
)
import mneme.tools as mntools
import mneme.curation as curation
import mneme.templates as _templates
import mneme.chatcmd as _chatcmd
import mneme.strategy_history as _strat_hist
import mneme.run_live as _run_live
import mneme.thinking_log as _think_log

# ─── Config file loading ────────────────────────────────────────
# A single config file (YAML or JSON) holds every tunable. Loaded BEFORE the
# constants below so config values flow into them via env-var defaults.
# Precedence: environment variable > config file > built-in default.
# See mneme.yaml.example for the full schema.

CONFIG_PATH: Optional[str] = None
CONFIG_DATA: Dict = {}           # raw sections for runtime lookup (providers, models)
_PROVIDER_HEADERS: Dict = {}     # extra headers from the active provider block
_OR_FALLBACK_MODELS: list = []   # OpenRouter model fallbacks (the `models` array)
_OR_PROVIDER_PREF: Dict = {}     # OpenRouter provider routing prefs (ignore/order/...)
_OR_STREAM: bool = True          # OpenRouter stream toggle (non-streaming enables OR failover)
# The raw OpenRouter key, preserved when the CHAT model runs on a DIFFERENT
# provider. `_resolve_provider()` repoints OPENROUTER_API_KEY at the chat
# provider's key; embed/label that still run on OpenRouter need the ORIGINAL
# OpenRouter key, so we stash it here before the repoint and _aux_key() reads it.
# Defined BEFORE load_config() so _resolve_provider's assignment isn't clobbered.
_AUX_OR_KEY: str = ""

# Last-known connectivity for the chat provider, embedder, and labeler. Updated on
# each real request (chat turn, embedding, topic-label) and surfaced via /status so
# the chat header can show a green/red dot per model without doing its own probes.
_PROVIDER_STATUS = {
    "chat":  {"ok": None, "error": "", "at": ""},
    "embed": {"ok": None, "error": "", "at": ""},
    "label": {"ok": None, "error": "", "at": ""},
}


def _set_status(kind: str, ok: bool, error: str = ""):
    st = _PROVIDER_STATUS.get(kind)
    if not st:
        return
    st["ok"] = bool(ok)
    st["error"] = (error or "")[:200]
    st["at"] = datetime.now(timezone.utc).isoformat()


def _record_chat_status(result):
    """Update the chat provider's status from a query_model result dict."""
    if not isinstance(result, dict):
        return
    content = result.get("content") or ""
    tool_calls = result.get("tool_calls") or []
    done = result.get("done_reason") or ""
    ok = bool(content) or bool(tool_calls) or done in ("stop", "length", "tool_calls", "max_tokens")
    err = result.get("error") or ("" if ok else (done or "no response"))
    _set_status("chat", ok, err)

# Flat map: "section.key" -> env var. Only keys listed here are honored from the
# file; anything else fails loud (typo guard).
_CONFIG_ENV_MAP = {
    "backend.type": "MNEME_BACKEND",
    "backend.provider": "MNEME_PROVIDER",
    "backend.ollama_url": "MNEME_OLLAMA_URL",
    "sampling.temperature": "MNEME_TEMPERATURE",
    "sampling.top_p": "MNEME_TOP_P",
    "sampling.top_k": "MNEME_TOP_K",
    "sampling.ctx_tokens": "MNEME_CTX_TOKENS",
    "sampling.completion_reserve": "MNEME_COMPLETION_RESERVE",
    "sampling.max_tokens": "MNEME_MAX_TOKENS",
    "sampling.reasoning_enabled": "MNEME_REASONING_ENABLED",
    "sampling.reasoning_effort": "MNEME_REASONING_EFFORT",
    "timeouts.chat_timeout": "MNEME_CHAT_TIMEOUT",
    "timeouts.ollama_chat_timeout": "MNEME_OLLAMA_CHAT_TIMEOUT",
    "timeouts.first_token_timeout": "MNEME_FIRST_TOKEN_TIMEOUT",
    "timeouts.stale_chunk_timeout": "MNEME_STALE_CHUNK_TIMEOUT",
    "timeouts.novelty_timeout": "MNEME_NOVELTY_TIMEOUT",
    "timeouts.embed_timeout": "MNEME_EMBED_TIMEOUT",
    "timeouts.label_timeout": "MNEME_LABEL_TIMEOUT",
    "timeouts.edge_failures": "MNEME_EDGE_FAILURES",
    "timeouts.edge_ratio": "MNEME_EDGE_RATIO",
    "storage.chunk_dir": "MNEME_CHUNK_DIR",
    "storage.port": "MNEME_PORT",
    "storage.db_path": "MNEME_DB_PATH",
    "storage.inject_system": "MNEME_INJECT_SYSTEM",
    "storage.memory_only": "MNEME_MEMORY_ONLY",
    "storage.staging_turns": "MNEME_STAGING_TURNS",
    "storage.staging_idle": "MNEME_STAGING_IDLE",
    "storage.context_recent_extra": "MNEME_CONTEXT_RECENT_EXTRA",
    "storage.belief_evolution": "MNEME_BELIEF_EVOLUTION",
    # Memory curation (retraction / recurrence / provenance).
    #   curation.allow_model_retract      — model may retract directly (destructive)
    #   curation.allow_model_propose      — model may queue a retraction for human review
    #   curation.allow_model_remove       — model may remove a chunk (stop using it)
    #   curation.inject_retracted         — inject retracted chunks as labelled warnings
    #                                       instead of dropping them (absence lets the
    #                                       model re-hallucinate the same fact)
    "curation.allow_model_retract": "MNEME_ALLOW_MODEL_RETRACT",
    "curation.allow_model_propose": "MNEME_ALLOW_MODEL_PROPOSE",
    "curation.allow_model_remove": "MNEME_ALLOW_MODEL_REMOVE",
    "curation.inject_retracted": "MNEME_INJECT_RETRACTED",
    "curation.recurrence_labeling": "MNEME_RECURRENCE_LABELING",
    "storage.memory_enabled": "MNEME_MEMORY_ENABLED",
    "storage.inject_enabled": "MNEME_INJECT_ENABLED",
    "retrieval.max_injected_tokens": "MNEME_MAX_INJECTED_TOKENS",
    "retrieval.route_threshold": "MNEME_ROUTE_THRESHOLD",
    "retrieval.classify_threshold": "MNEME_CLASSIFY_THRESHOLD",
    "retrieval.baseline_noise": "MNEME_BASELINE_NOISE",
    "retrieval.inject_min_similarity": "MNEME_INJECT_MIN_SIMILARITY",
    "retrieval.strategy_min_similarity": "MNEME_STRATEGY_MIN_SIMILARITY",
    "retrieval.keyword_fallback": "MNEME_KEYWORD_FALLBACK",
    "retrieval.age_decay_days": "MNEME_AGE_DECAY_DAYS",
    "retrieval.max_siblings": "MNEME_MAX_SIBLINGS",
    "retrieval.topic_switch_sim": "MNEME_TOPIC_SWITCH_SIM",
    "retrieval.topic_switch_grace": "MNEME_TOPIC_SWITCH_GRACE",
    "retrieval.novel_inject_floor": "MNEME_NOVEL_INJECT_FLOOR",
    "retrieval.max_per_topic": "MNEME_MAX_PER_TOPIC",
    "retrieval.max_chunk_words": "MNEME_MAX_CHUNK_WORDS",
    "retrieval.max_chunk_size": "MNEME_MAX_CHUNK_SIZE",
    "retrieval.page_max_chunk_size": "MNEME_PAGE_MAX_CHUNK_SIZE",
    "caps.max_history_messages": "MNEME_MAX_HISTORY_MESSAGES",
    "caps.db_msg_cap": "MNEME_DB_MSG_CAP",
    "caps.compress_threshold": "MNEME_COMPRESS_THRESHOLD",
    "caps.compress_max_tok": "MNEME_COMPRESS_MAX_TOK",
    "caps.max_tool_forward": "MNEME_MAX_TOOL_FORWARD",
    "caps.tool_followup_tokens": "MNEME_TOOL_FOLLOWUP_TOKENS",
    "caps.max_server_rounds": "MNEME_MAX_SERVER_ROUNDS",
    "caps.chunk_size": "MNEME_CHUNK_SIZE",
    "tools.native": "MNEME_NATIVE_TOOLS",
    "tools.dir": "MNEME_TOOLS_DIR",
    "tools.bash_timeout": "MNEME_TOOLS_BASH_TIMEOUT",
    "tools.inject_min_similarity": "MNEME_TOOL_INJECT_MIN_SIMILARITY",
    "tools.inject_max": "MNEME_TOOL_INJECT_MAX",
    "tools.inject_tokens": "MNEME_TOOL_INJECT_TOKENS",
    "tools.search_memory": "MNEME_TOOL_SEARCH_MEMORY",
    "tools.list_tools": "MNEME_TOOL_LIST_TOOLS",
    "tools.read_tool": "MNEME_TOOL_READ_TOOL",
    "tools.read_file": "MNEME_TOOL_READ_FILE",
    "tools.fetch_url": "MNEME_TOOL_FETCH_URL",
    "tools.web_search": "MNEME_TOOL_WEB_SEARCH",
    "tools.inspect_run": "MNEME_TOOL_INSPECT_RUN",
    "runtime.hot_reload": "MNEME_HOT_RELOAD",
    # Agent harness (durable runs) — see docs/harness/.
    "harness.enabled": "MNEME_HARNESS",
    "harness.db_path": "MNEME_HARNESS_DB",
    "harness.runs_dir": "MNEME_RUNS_DIR",
    "harness.auto_resume": "MNEME_HARNESS_AUTO_RESUME",
    "harness.lease_seconds": "MNEME_HARNESS_LEASE",
    "harness.reflect": "MNEME_HARNESS_REFLECT",
    "harness.auto_apply_level": "MNEME_HARNESS_AUTO_APPLY_LEVEL",
    "harness.scheduler": "MNEME_HARNESS_SCHEDULER",
    "harness.scheduler_tick": "MNEME_HARNESS_SCHEDULER_TICK",
    "logging.max_entries": "MNEME_MAX_LOG_ENTRIES",
    # top-level backward-compat keys (old flat env-var names)
    "model": "MNEME_MODEL",
    "embed_model": "EMBED_MODEL",
    "embed_dim": "EMBED_DIM",
    "label_model": "LABEL_MODEL",
    "embed_provider": "EMBED_PROVIDER",
    "label_provider": "LABEL_PROVIDER",
    # Backend transports — pin a role to "openai" or "ollama" when it differs
    # from the chat backend (e.g. chat hosted, embedder on local Ollama).
    "embed_backend": "MNEME_EMBED_BACKEND",
    "label_backend": "MNEME_LABEL_BACKEND",
    "ollama_url": "MNEME_OLLAMA_URL",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "openrouter_base_url": "OPENROUTER_BASE_URL",
}

_STRUCTURAL_SECTIONS = {"providers", "models", "mcp_servers", "filesystem", "debug"}

# Repo root — used to locate model_templates.yaml (shipped alongside the proxy).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Top-level scalar keys that are handled specially rather than mapped to env.
# `model_template` selects a named settings bundle from model_templates.yaml.
_CONFIG_PASSTHROUGH_KEYS = {"model_template"}


def _config_scalar(v) -> str:
    if v is True:
        return "1"
    if v is False:
        return "0"
    return str(v)


def _find_config_path():
    for i, a in enumerate(sys.argv):
        if a == "--config" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith("--config="):
            return a.split("=", 1)[1]
    if os.environ.get("MNEME_CONFIG"):
        return os.environ["MNEME_CONFIG"]
    for name in ("mneme.yaml", "mneme.json"):
        p = os.path.join(os.path.expanduser("~/mneme/chunks"), name)
        if os.path.exists(p):
            return p
    return None


def _parse_config_file(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # optional dependency
        except ImportError:
            raise SystemExit(
                f"[CONFIG] {path} is YAML but PyYAML is not installed. "
                f"pip install pyyaml  (or use a .json config file)"
            )
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise SystemExit(f"[CONFIG] {path}: top level must be a mapping, got {type(data).__name__}")
    return data


def _apply_config(data: Dict, path: str):
    for section, val in data.items():
        if section in _STRUCTURAL_SECTIONS:
            CONFIG_DATA[section] = val or {}
            continue
        if section in _CONFIG_ENV_MAP:               # top-level scalar key
            env = _CONFIG_ENV_MAP[section]
            if os.environ.get(env) is None and val is not None:
                os.environ[env] = _config_scalar(val)
            continue
        if section in _CONFIG_PASSTHROUGH_KEYS:      # handled specially (e.g. model_template)
            CONFIG_DATA[section] = val
            continue
        if not isinstance(val, dict):
            raise SystemExit(f"[CONFIG] {path}: section '{section}' must be a mapping")
        for key, v in val.items():
            flat = f"{section}.{key}"
            env = _CONFIG_ENV_MAP.get(flat)
            if env is None:
                raise SystemExit(
                    f"[CONFIG] {path}: unknown key '{flat}' (typo?) — see mneme.yaml.example"
                )
            if os.environ.get(env) is None and v is not None:
                os.environ[env] = _config_scalar(v)


# Curated catalog of popular providers for the model switcher's provider -> model
# flow. `base_url` + `key_env` let the proxy query a provider's model list and
# route to it without a hand-written config block; the provider choice + model are
# persisted by POST /providers/activate. All are OpenAI-compatible except Ollama.
PROVIDER_CATALOG = {
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "kind": "openai"},
    "openai":     {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY", "kind": "openai"},
    "anthropic":  {"label": "Anthropic", "base_url": "https://api.anthropic.com/v1", "key_env": "ANTHROPIC_API_KEY", "kind": "openai"},
    "google":     {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GOOGLE_API_KEY", "kind": "openai"},
    "deepseek":   {"label": "DeepSeek", "base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY", "kind": "openai"},
    "groq":       {"label": "Groq", "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "kind": "openai"},
    "mistral":    {"label": "Mistral", "base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY", "kind": "openai"},
    "xai":        {"label": "xAI", "base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY", "kind": "openai"},
    "together":   {"label": "Together AI", "base_url": "https://api.together.xyz/v1", "key_env": "TOGETHER_API_KEY", "kind": "openai"},
    "routeway":   {"label": "Routeway", "base_url": "https://api.routeway.ai/v1", "key_env": "ROUTEWAY_API_KEY", "kind": "openai"},
    "featherless": {"label": "Featherless", "base_url": "https://api.featherless.ai/v1", "key_env": "FEATHERLESS_API_KEY", "kind": "openai"},
    "ollama":     {"label": "Ollama (local)", "base_url": "http://localhost:11434", "key_env": "", "kind": "ollama"},
    "vllm":       {"label": "vLLM (local)", "base_url": "http://localhost:8000/v1", "key_env": "", "kind": "openai"},
    "llamacpp":   {"label": "llama.cpp (local)", "base_url": "http://localhost:8080/v1", "key_env": "", "kind": "openai"},
}


def _resolve_provider():
    """Resolve the active OpenAI-compatible provider's connection details into
    the flat env vars the code reads (base URL, API key, model names)."""
    global _PROVIDER_HEADERS, _OR_FALLBACK_MODELS, _OR_PROVIDER_PREF, _OR_STREAM, _AUX_OR_KEY
    backend_type = os.environ.get("MNEME_BACKEND", "ollama")
    if backend_type not in ("openai", "openrouter"):
        # Ollama: the model is authoritative under the top-level `model:` key
        # (read by _apply_config). Older configs only stored it under
        # providers.<name>.model — fall back to that so a pre-upgrade config
        # still resolves its model instead of the built-in default.
        if os.environ.get("MNEME_MODEL") is None:
            _prov = (CONFIG_DATA.get("providers") or {}).get("openrouter") or {}
            if _prov.get("model"):
                os.environ["MNEME_MODEL"] = _config_scalar(_prov["model"])
        return
    name = os.environ.get("MNEME_PROVIDER", "openrouter")
    prov = (CONFIG_DATA.get("providers") or {}).get(name) or {}
    if not prov and name in PROVIDER_CATALOG:
        # Catalog fallback: a provider chosen via the switcher without a
        # hand-written config block still resolves its base_url + key_env.
        _cat = PROVIDER_CATALOG[name]
        prov = {"base_url": _cat.get("base_url", ""), "api_key_env": _cat.get("key_env", "")}
    if not prov:
        return  # providers not configured — rely on env vars directly (back-compat)

    def _set(env, pkey):
        if os.environ.get(env) is None and prov.get(pkey):
            os.environ[env] = _config_scalar(prov[pkey])

    _set("OPENROUTER_BASE_URL", "base_url")
    _set("MNEME_MODEL", "model")
    _set("EMBED_MODEL", "embed_model")
    _set("LABEL_MODEL", "label_model")
    api_key_env = prov.get("api_key_env")
    if api_key_env:
        k = os.environ.get(api_key_env)
        if k:
            # OPENROUTER_API_KEY is the generic "chat key" carrier. When the chat
            # provider is a DIFFERENT vendor (routeway/deepseek/…), the OpenRouter
            # key is still needed by embed/label if they run on OpenRouter — so
            # preserve it before repointing the carrier at the chat key.
            if api_key_env != "OPENROUTER_API_KEY":
                _AUX_OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
            os.environ["OPENROUTER_API_KEY"] = k
    _PROVIDER_HEADERS = prov.get("headers") or {}
    # OpenRouter-specific reliability (only applied to OpenRouter requests, so
    # other OpenAI-compatible providers are unaffected): model fallbacks (the
    # `models` array — walked in order if every provider for the primary model
    # fails) and provider routing prefs (ignore/order/preferred_max_latency).
    _OR_FALLBACK_MODELS = prov.get("fallback_models") or []
    _OR_PROVIDER_PREF = prov.get("provider") or {}
    # stream toggle: streaming (default) gives a fast first-token hang detector;
    # non-streaming lets OpenRouter buffer + transparently fail over a mid-stream
    # stall. Config-only so flipping it later is a one-line edit, not a code change.
    _OR_STREAM = bool(prov.get("stream", True))


CONFIG_LOAD_ATTEMPTS = 6
CONFIG_LOAD_RETRY_DELAY = 0.5  # seconds — tolerate a setup script still writing the file


def _resolve_user_templates_path(data=None):
    """Path to the user-authored model-template catalogue.

    The module-level ``USER_TEMPLATES_PATH`` is defined BELOW ``load_config()``'s
    call site (it depends on DB_PATH, which is only known after the config load
    has exported ``storage.db_path``). Referencing that global inside
    ``load_config`` is therefore a NameError whenever a model template is
    selected — the startup crash this resolves. This helper recomputes the same
    path from the parsed config (or env) at the point the template merge needs
    it. Kept in sync with the module-level definition: env override first, then
    the directory of ``storage.db_path``, then a legacy co-location next to the
    chunk dir.
    """
    p = os.environ.get("MNEME_TEMPLATES_FILE")
    if p:
        return p
    db_path = os.environ.get("MNEME_DB_PATH")
    if not db_path and isinstance(data, dict):
        db_path = (data.get("storage") or {}).get("db_path")
    if db_path:
        db_dir = os.path.dirname(os.path.abspath(os.path.expanduser(str(db_path)))) or "."
    else:
        # Legacy install (no storage.db_path): co-locate next to the chunk dir,
        # matching the module-level DB_PATH fallback (CHUNK_DIR/mneme.db).
        chunk = os.environ.get("MNEME_CHUNK_DIR")
        if not chunk and isinstance(data, dict):
            chunk = (data.get("storage") or {}).get("chunk_dir")
        db_dir = os.path.abspath(os.path.expanduser(str(chunk or ".")))
    return os.path.join(db_dir, "templates.yaml")


def load_config():
    global CONFIG_PATH
    # The setup wizard writes mneme.yaml and launches the proxy back-to-back, so
    # at import time the file can be absent or half-written. Retry briefly so a
    # racing setup can't leave the proxy running on stale/partial config (which
    # silently flips reasoning on and makes thinking models runaway-timeout).
    last_err = None
    for _ in range(CONFIG_LOAD_ATTEMPTS):
        path = _find_config_path()
        if not path:
            time.sleep(CONFIG_LOAD_RETRY_DELAY)
            continue
        try:
            data = _parse_config_file(path)
        except Exception as e:
            # Partial write: the YAML/JSON is still streaming to disk.
            last_err = e
            time.sleep(CONFIG_LOAD_RETRY_DELAY)
            continue
        CONFIG_PATH = path
        # Model template (if selected) merges in as a DEFAULTS LAYER beneath the
        # file, so any template value can still be overridden in mneme.yaml.
        # No template -> data is returned unchanged (behaviour identical to
        # before templates existed).
        _tpl_name = (data.get("model_template") or "").strip() if isinstance(data, dict) else ""
        if _tpl_name:
            try:
                data = _templates.apply_template(
                    data, os.environ.get("MNEME_MODEL", ""), _tpl_name,
                    _templates.default_templates_path(REPO_ROOT),
                    _resolve_user_templates_path(data),
                )
                print(f"  [TEMPLATE] applied {_tpl_name!r} (file values still win)", flush=True)
            except _templates.TemplateError as e:
                # Fail loud: a bad template must not silently fall back to
                # defaults, or the user would think the template was applied.
                raise SystemExit(f"[CONFIG] {e}")
        _apply_config(data, path)
        # Guard against a wrong instance identity. The log path (and prompt/tool
        # dirs) are keyed off $MNEME_CHUNK_DIR; if that env points at a DIFFERENT
        # instance's dir while MNEME_PORT is this instance's port, the proxy
        # silently loads the other instance's config and writes its log into that
        # instance's proxy.log (the "everything lands in 8080's log" symptom).
        # There is no independent source of truth to self-correct, so flag the
        # mismatch loudly instead of failing silently.
        _st = data.get("storage") or {}
        _cfg_port = _st.get("port")
        _env_port = os.environ.get("MNEME_PORT")
        if _cfg_port is not None and _env_port and str(_cfg_port) != str(_env_port):
            print(f"  [CONFIG] ⚠ PORT MISMATCH — env MNEME_PORT={_env_port} but this config "
                  f"(storage.port={_cfg_port}) is for another instance. Logging may be going to "
                  f"the wrong proxy.log ({_st.get('chunk_dir') or path}/proxy.log). Check MNEME_CHUNK_DIR.",
                  flush=True)
        _cfg_chunk = _st.get("chunk_dir")
        _env_chunk = os.environ.get("MNEME_CHUNK_DIR")
        if _cfg_chunk and _env_chunk:
            try:
                _chunk_mismatch = (os.path.abspath(os.path.expanduser(str(_cfg_chunk)))
                                   != os.path.abspath(os.path.expanduser(str(_env_chunk))))
            except Exception:
                _chunk_mismatch = str(_cfg_chunk) != str(_env_chunk)
            if _chunk_mismatch:
                print(f"  [CONFIG] ⚠ CHUNK_DIR MISMATCH — env MNEME_CHUNK_DIR={_env_chunk} but config "
                      f"storage.chunk_dir={_cfg_chunk}. The log follows $MNEME_CHUNK_DIR.", flush=True)
        _resolve_provider()
        # Expand ~ in the chunk dir (config files are the natural place to fix the
        # pod-path default /workspace/mneme_chunks on a laptop).
        cd = os.environ.get("MNEME_CHUNK_DIR")
        if cd and cd.startswith("~"):
            os.environ["MNEME_CHUNK_DIR"] = os.path.expanduser(cd)
        print(f"  [CONFIG] loaded {path}", flush=True)
        return
    if last_err is not None:
        print(f"  [CONFIG] gave up loading config after {CONFIG_LOAD_ATTEMPTS} attempts ({last_err}) — running on defaults", flush=True)


# ─── Config ────────────────────────────────────────────────────
# Sampling knobs that may be hot-reloaded live (edit mneme.yaml -> the next
# request picks them up without a proxy restart). Keys are mneme.yaml
# `sampling.*` names; values are the env vars the code reads at request time.
_SAMPLING_ENV_MAP = {
    "temperature": "MNEME_TEMPERATURE",
    "top_p": "MNEME_TOP_P",
    "top_k": "MNEME_TOP_K",
    "ctx_tokens": "MNEME_CTX_TOKENS",
    "completion_reserve": "MNEME_COMPLETION_RESERVE",
    "max_tokens": "MNEME_MAX_TOKENS",
    "reasoning_enabled": "MNEME_REASONING_ENABLED",
    "reasoning_effort": "MNEME_REASONING_EFFORT",
}

# Retrieval keys that are safe to change on a live proxy. These were previously
# read ONCE at import into module constants, so editing them in mneme.yaml did
# nothing until a restart — while the config header implied sampling/models were
# live and said nothing about retrieval. inject_min_similarity is the single
# most-tuned knob in the whole config, so silently ignoring an edit to it was the
# worst case. Now refreshed on mtime change like sampling.
_RETRIEVAL_ENV_MAP = {
    "max_injected_tokens": "MNEME_MAX_INJECTED_TOKENS",
    "inject_min_similarity": "MNEME_INJECT_MIN_SIMILARITY",
    "strategy_min_similarity": "MNEME_STRATEGY_MIN_SIMILARITY",
    "max_siblings": "MNEME_MAX_SIBLINGS",
    "age_decay_days": "MNEME_AGE_DECAY_DAYS",
    "max_per_topic": "MNEME_MAX_PER_TOPIC",
    "topic_switch_sim": "MNEME_TOPIC_SWITCH_SIM",
    "novel_inject_floor": "MNEME_NOVEL_INJECT_FLOOR",
    "keyword_fallback": "MNEME_KEYWORD_FALLBACK",
}
# Timeout keys — same story. A long-running turn that the user shortens by
# editing chat_timeout expects the change to apply to the NEXT turn.
_TIMEOUT_ENV_MAP = {
    "chat_timeout": "MNEME_CHAT_TIMEOUT",
    "ollama_chat_timeout": "MNEME_OLLAMA_CHAT_TIMEOUT",
    "first_token_timeout": "MNEME_FIRST_TOKEN_TIMEOUT",
    "stale_chunk_timeout": "MNEME_STALE_CHUNK_TIMEOUT",
}
# Env vars the user exported BEFORE the config file loaded stay pinned: hot-reload
# will never override them (preserves the documented env > file precedence).
_USER_PINNED_ENV = {env for env in _SAMPLING_ENV_MAP.values() if env in os.environ}
# Storage feature-gates (memory_only / memory_enabled) join the live-reload path:
# hot-reload re-reads them from the config unless the user exported them manually
# before startup (preserves the documented env > file precedence).
_STORAGE_ENV_MAP = {
    "memory_only": "MNEME_MEMORY_ONLY",
    "memory_enabled": "MNEME_MEMORY_ENABLED",
    "inject_enabled": "MNEME_INJECT_ENABLED",
}
_USER_PINNED_STORAGE_ENV = {env for env in _STORAGE_ENV_MAP.values() if env in os.environ}


def _apply_thinking_log():
    """Read debug.thinking_log / thinking_log_path and apply to the log module."""
    _dbg = CONFIG_DATA.get("debug") or {}
    _path = _dbg.get("thinking_log_path") or os.path.join(
        os.environ.get("MNEME_CHUNK_DIR") or ".", "thinking.log")
    _think_log.configure(_dbg.get("thinking_log"), _path)


load_config()
mntools.reload_config()  # tools.py is imported before load_config(); refresh its env-derived knobs
# Apply the filesystem scope to the native tools (bash/write/read_file) from
# config filesystem.model_scope / filesystem.browser_root.
_fs = CONFIG_DATA.get("filesystem") or {}
mntools.set_scope(model_scope=_fs.get("model_scope"), browser_root=_fs.get("browser_root"))
_apply_thinking_log()

# Connect to configured MCP servers (non-blocking; they finish connecting in the
# background and their tools appear in assemble_tools on the next request).
_mcp_cfgs = CONFIG_DATA.get("mcp_servers") or []
if _mcp_cfgs:
    mntools.get_manager().reconcile(_mcp_cfgs)
    print(f"  [MCP] configured {len(_mcp_cfgs)} server(s): {[c.get('name') for c in _mcp_cfgs]}", flush=True)

OLLAMA_URL  = os.environ.get("MNEME_OLLAMA_URL", "http://localhost:11434")
MODEL       = os.environ.get("MNEME_MODEL", "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest")

# Hot-reload master switch. Read ONCE at startup (never re-read on config mtime):
# when false, config + prompts + swarm_config are frozen and any change requires a
# restart. This is setup-only — the proxy cannot toggle it on the fly, so a
# coding/studying agent that edits its own files can't re-enable live edits.
HOT_RELOAD = os.environ.get("MNEME_HOT_RELOAD", "1") == "1"

# Backend: "ollama" (native API) | "openai"/"openrouter" (OpenAI-compatible, hosted).
# "openrouter" is an alias for "openai" — OpenRouter is just an OpenAI-compatible
# aggregator. Provider connection details come from the config `providers:` block
# or env vars (OPENROUTER_BASE_URL / OPENROUTER_API_KEY / model names).
MNEME_BACKEND = os.environ.get("MNEME_BACKEND", "ollama")
OR_API_KEY    = os.environ.get("OPENROUTER_API_KEY", "")
OR_BASE_URL   = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


def _aux_key(ke: str) -> str:
    """Resolve an auxiliary model's API key from its provider's key_env.

    When the aux provider is OpenRouter but the CHAT provider is a different
    vendor, OPENROUTER_API_KEY has been repointed at the chat key — so we return
    the preserved OpenRouter key instead. Otherwise read the env var directly."""
    if not ke:
        return ""
    if ke == "OPENROUTER_API_KEY" and _AUX_OR_KEY:
        return _AUX_OR_KEY
    return os.environ.get(ke, "")


def _backend_is_openai() -> bool:
    return MNEME_BACKEND in ("openai", "openrouter")


def _aux_backend(override_env: str) -> str:
    """Transport for an auxiliary model (embed/label/judge). An explicit override
    env (e.g. MNEME_EMBED_BACKEND) wins; otherwise follow the main backend
    (MNEME_BACKEND); otherwise local Ollama. Read at call time — not import — so
    it reflects the config-loaded backend, not the import-time default."""
    return (os.environ.get(override_env)
            or os.environ.get("MNEME_BACKEND")
            or MNEME_BACKEND)


def _aux_conn(kind: str) -> dict:
    """Resolve the connection for an auxiliary model (`kind` = 'embed' | 'label').

    Pinned to a specific provider via the top-level `embed_provider` /
    `label_provider` config key (a PROVIDER_CATALOG name, or a `providers.<name>`
    block). When unset, falls back to the chat backend's connection — backward
    compatible with the old "embedder/labeler share the chat base_url" behaviour.

    Returns {"base_url", "key", "kind", "headers"} with kind ∈ {"openai","ollama"}.
    Read at call time so it reflects the config loaded by _resolve_provider."""
    prov_name = os.environ.get("EMBED_PROVIDER" if kind == "embed" else "LABEL_PROVIDER", "") or ""
    if prov_name:
        cat = PROVIDER_CATALOG.get(prov_name)
        if cat:
            if cat.get("kind") == "ollama":
                return {"base_url": OLLAMA_URL, "key": "", "kind": "ollama", "headers": {}}
            ke = cat.get("key_env", "")
            key = _aux_key(ke)
            return {"base_url": cat.get("base_url", "") or "", "key": key, "kind": "openai", "headers": {}}
        prov = (CONFIG_DATA.get("providers") or {}).get(prov_name) or {}
        if isinstance(prov, dict) and prov.get("base_url"):
            ke = prov.get("api_key_env", "")
            key = _aux_key(ke)
            return {"base_url": prov.get("base_url", "") or "", "key": key,
                    "kind": "openai", "headers": prov.get("headers") or {}}
    # No explicit aux provider — follow the chat backend (existing behaviour).
    if _aux_backend(f"MNEME_{kind.upper()}_BACKEND") in ("openai", "openrouter"):
        return {"base_url": OR_BASE_URL, "key": OR_API_KEY, "kind": "openai",
                "headers": _PROVIDER_HEADERS or {}}
    return {"base_url": OLLAMA_URL, "key": "", "kind": "ollama", "headers": {}}


def _or_headers() -> dict:
    """Headers for the OpenAI-compatible backend. Provider-specific extra headers
    (e.g. OpenRouter's HTTP-Referer/X-Title) come from config `providers.<name>.headers`;
    OpenRouter attribution headers are added only when talking to OpenRouter."""
    # Accept-Encoding: identity — requests defaults to gzip/deflate/br, and
    # OpenRouter's Stealth provider returns a gzip body that decodes to whitespace
    # (the request then hangs until the read timeout). Forcing identity makes
    # OpenRouter return plain JSON, like curl does, and avoids the hang.
    h = {"Authorization": f"Bearer {OR_API_KEY}", "Content-Type": "application/json",
         "Accept-Encoding": "identity"}
    if _PROVIDER_HEADERS:
        h.update(_PROVIDER_HEADERS)
    elif OR_BASE_URL.startswith("https://openrouter.ai"):
        h.update({"HTTP-Referer": "https://localhost/mneme", "X-Title": "Mneme"})
    return h


CHUNK_DIR   = os.environ.get("MNEME_CHUNK_DIR", "/workspace/mneme_chunks")
PORT        = int(os.environ.get("MNEME_PORT", "8080"))
# Log is per-port (proxy-<port>.log) so two proxies that accidentally share a
# chunk dir can't merge their logs into one file. Override with MNEME_LOG_PATH.
_LOG_PATH   = os.environ.get("MNEME_LOG_PATH") or os.path.join(CHUNK_DIR, f"proxy-{PORT}.log")
setup_logging(_LOG_PATH)  # tee stdout/stderr into the per-port log (append, size-capped)
# Content-addressed image GC. The image store (CHUNK_DIR/images/<sha256>.<ext>)
# is keyed by bytes; a file whose hash is referenced by NO chunk is junk (an
# ingest whose chunk never archived). GRACE skips recently-written files so a
# just-ingested image mid-archive is never deleted; INTERVAL is the periodic
# sweep cadence (0 = startup sweep only).
IMAGE_GC_GRACE    = int(os.environ.get("MNEME_IMAGE_GC_GRACE", "3600"))
IMAGE_GC_INTERVAL = int(os.environ.get("MNEME_IMAGE_GC_INTERVAL", "1800"))
INJECT_SYSTEM = os.environ.get("MNEME_INJECT_SYSTEM", "1")  # "0" to skip Mneme instructions injection
MEMORY_ONLY = os.environ.get("MNEME_MEMORY_ONLY", "1") == "1"  # "1" = memory-only mode: no strategy/learning (no strategy save/injection, no novel-procedure, no capability-edge/overcome, no belief evolution, no learning mode). Keeps memory retrieval + grading + the full tool loop. Code default is "1" (conservative); the setup wizard writes the branch-appropriate storage.memory_only into the config — agent-harness -> false (full build), main -> true (memory-only) — and that config value overrides this default.
MEMORY_ENABLED = os.environ.get("MNEME_MEMORY_ENABLED", "1") == "1"  # master switch: "0" disables ALL memory — no retrieval/injection (build_context), no staging/archiving (conversation + tool results), and search_memory auto-off. Run through the proxy with tools only (system prompt + tool loop stay).
INJECT_ENABLED = os.environ.get("MNEME_INJECT_ENABLED", "1") == "1"  # "0" = save-only mode: skip memory retrieval/injection (build_context returns no context), but turns are still staged/archived so the work is saved and search_memory + /search still work. Master switch MEMORY_ENABLED gates BOTH injection AND saving; this flag only gates injection.

# ── Memory curation flags ────────────────────────────────────────────────
# Retraction lets a specific chunk be marked false (and later restored) instead
# of wiping the whole DB. Model authority is split deliberately:
#   allow_model_propose (ON by default) — the model may QUEUE a chunk for human
#       review. Safe: nothing is excluded from retrieval while pending.
#   allow_model_retract (OFF by default) — the model may retract directly. This
#       is destructive, so it is opt-in: a model that is confidently wrong would
#       otherwise delete the correct facts that contradict it.
ALLOW_MODEL_PROPOSE = os.environ.get("MNEME_ALLOW_MODEL_PROPOSE", "1") == "1"
ALLOW_MODEL_RETRACT = os.environ.get("MNEME_ALLOW_MODEL_RETRACT", "0") == "1"
# Remove (vs flag): the model may set the `removed` management flag on a chunk
# directly, taking it out of injection + search. OFF by default — flagging is
# the safe default, removal is opt-in (a confidently-wrong model could otherwise
# hide the facts that contradict it). Reversible: the row is kept and the user
# can restore it from the management page.
ALLOW_MODEL_REMOVE = os.environ.get("MNEME_ALLOW_MODEL_REMOVE", "0") == "1"
# Inject retracted chunks as labelled warnings instead of dropping them. Absence
# is dangerous: with nothing to contradict it the model may re-hallucinate the
# same fact. Mirror of the existing [G:F — FAILED ...] treatment.
INJECT_RETRACTED = os.environ.get("MNEME_INJECT_RETRACTED", "1") == "1"
# Flag self-confirmation loops in the injected header. (Recurrence/support-tier
# labelling was removed — the assert_count/independent_sources counters were
# never wired to the archive path, so the "single/repeated/corroborated" tiers
# could not fire; only self-confirmation, which IS populated, remains.)
RECURRENCE_LABELING = os.environ.get("MNEME_RECURRENCE_LABELING", "1") == "1"

_db_path   = os.environ.get("MNEME_DB_PATH")
if _db_path:
    DB_PATH = os.path.expanduser(_db_path)
else:
    # Legacy fallback for pre-storage.db_path installs. The DB dir is meant to be
    # independent of the proxy's config/log dir (storage.db_path is the real
    # switch; the setup wizard always sets it). Warn rather than silently co-locate.
    DB_PATH = os.path.join(CHUNK_DIR, "mneme.db")
    print(f"  [CONFIG] ⚠ storage.db_path not set — DB defaulting to {DB_PATH}. "
          f"Set storage.db_path for a shared/external DB.", flush=True)
DB_DIR     = os.path.dirname(DB_PATH) or "."

# User-authored model templates live OUT of the repo (survive git pull), next to
# the shared DB so every instance sees the same saved templates. The shipped
# catalogue stays in the repo (REPO_ROOT/model_templates.yaml) and is merged
# beneath user templates (user wins on a name clash).
USER_TEMPLATES_PATH = os.environ.get("MNEME_TEMPLATES_FILE") or os.path.join(DB_DIR, "templates.yaml")


def _write_user_templates(cat: Dict) -> None:
    """Persist the user template catalogue (overwrites the file)."""
    import yaml
    d = os.path.dirname(USER_TEMPLATES_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(USER_TEMPLATES_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump({"templates": cat}, f, sort_keys=False, allow_unicode=True)


# Sampling defaults (per-model overrides live in config `models:`)
OLLAMA_TEMP    = float(os.environ.get("MNEME_TEMPERATURE", "0.3"))

# ─── Live config hot-reload ───────────────────────────────────
# The full config is read once at startup, but generation knobs (temperature /
# top_p / top_k / max_tokens / num_predict) and the storage feature-gates
# (memory_only / memory_enabled) are re-read from the SAME per-instance mneme.yaml
# whenever its mtime changes, so a running swarm can tune them without a restart.
# `sampling` + `models` + `storage.memory_*` are refreshed; provider/model identity
# and structural settings (backend/port/db path) are still restart-only.
_CONFIG_MTIME = 0.0


def _reload_sampling_if_changed():
    global _CONFIG_MTIME, OLLAMA_TEMP, MEMORY_ONLY, MEMORY_ENABLED, INJECT_ENABLED
    if not HOT_RELOAD:
        return  # locked — config changes take effect only after a restart
    path = CONFIG_PATH
    if not path or not os.path.exists(path):
        return
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    if mtime == _CONFIG_MTIME:
        return
    _CONFIG_MTIME = mtime
    try:
        data = _parse_config_file(path)
    except Exception as e:
        print(f"  [CONFIG] hot-reload parse failed ({e}) — keeping current settings", flush=True)
        return
    # Per-model overrides are read from CONFIG_DATA at request time, so refreshing
    # this section is enough to make `models:` changes live.
    #
    # IMPORTANT: re-apply the model template here. The raw file may have an empty
    # (or absent) `models:` block while the template supplied per-model values —
    # assigning the raw section directly would silently DISCARD the template's
    # settings on the first hot-reload, so they applied at boot and then vanished
    # (observed: repeat_penalty fell back to 1.000 after the first request).
    if "models" in data or "model_template" in data:
        _tpl_name = (data.get("model_template") or "").strip()
        _sect = data.get("models") or {}
        if _tpl_name:
            try:
                _merged = _templates.apply_template(
                    data, MODEL, _tpl_name, _templates.default_templates_path(REPO_ROOT),
                    USER_TEMPLATES_PATH)
                _sect = _merged.get("models") or _sect
            except _templates.TemplateError as e:
                print(f"  [CONFIG] template re-apply failed: {e}", flush=True)
        CONFIG_DATA["models"] = _sect
        if "model_template" in data:
            CONFIG_DATA["model_template"] = data.get("model_template") or ""
    if "filesystem" in data:
        CONFIG_DATA["filesystem"] = data.get("filesystem") or {}
        _fs = CONFIG_DATA.get("filesystem") or {}
        mntools.set_scope(model_scope=_fs.get("model_scope"), browser_root=_fs.get("browser_root"))
    if "debug" in data:
        CONFIG_DATA["debug"] = data.get("debug") or {}
        _apply_thinking_log()
    # Scalar sampling keys -> refresh env (respecting user-pinned env overrides).
    sampling = data.get("sampling") or {}
    changed = []
    for key, env in _SAMPLING_ENV_MAP.items():
        if env in _USER_PINNED_ENV:
            continue
        if key in sampling and sampling[key] is not None:
            os.environ[env] = _config_scalar(sampling[key])
            changed.append(key)
    if "temperature" in sampling and sampling["temperature"] is not None \
            and "MNEME_TEMPERATURE" not in _USER_PINNED_ENV:
        OLLAMA_TEMP = float(sampling["temperature"])
    # Storage feature-gates (memory_only / memory_enabled) — re-read live so the
    # strategy/learning layer can be toggled from the config without a restart.
    storage = data.get("storage") or {}
    for cfg_key, env in _STORAGE_ENV_MAP.items():
        if env in _USER_PINNED_STORAGE_ENV:
            continue
        if cfg_key in storage and storage[cfg_key] is not None:
            os.environ[env] = _config_scalar(storage[cfg_key])
            changed.append(f"storage.{cfg_key}")
    # Retrieval + timeout keys — previously import-time-only, so editing them in
    # the file did nothing until a restart (the "I changed inject_min_similarity
    # and nothing happened" trap). Refresh the env vars, then re-read the module
    # constants from them below so the next request uses the new values.
    for _section, _map in (("retrieval", _RETRIEVAL_ENV_MAP), ("timeouts", _TIMEOUT_ENV_MAP)):
        _block = data.get(_section) or {}
        for cfg_key, env in _map.items():
            if env in _USER_PINNED_ENV:
                continue
            if cfg_key in _block and _block[cfg_key] is not None:
                os.environ[env] = _config_scalar(_block[cfg_key])
                changed.append(f"{_section}.{cfg_key}")
    _refresh_runtime_constants()
    # Re-read the flags (the loop above may have just updated their env vars).
    MEMORY_ONLY = os.environ.get("MNEME_MEMORY_ONLY", "1") == "1"
    MEMORY_ENABLED = os.environ.get("MNEME_MEMORY_ENABLED", "1") == "1"
    INJECT_ENABLED = os.environ.get("MNEME_INJECT_ENABLED", "1") == "1"
    # MCP servers — reconcile the running set with the config (hot add/remove).
    if "mcp_servers" in data:
        CONFIG_DATA["mcp_servers"] = data["mcp_servers"] or []
        mntools.get_manager().reconcile(CONFIG_DATA["mcp_servers"])
        changed.append("mcp_servers")
    print(f"  [CONFIG] hot-reloaded sampling/models/storage ({', '.join(changed) or 'models-only'})", flush=True)


def _force_config_reload() -> bool:
    """Re-read the config file and re-derive runtime constants, ignoring mtime.

    Used by the <<RETRIEVAL>> command so a change applies to the very next
    message rather than waiting for the mtime poll. Returns True on success.
    """
    global _CONFIG_MTIME
    try:
        _CONFIG_MTIME = 0.0          # defeat the mtime short-circuit
        _reload_sampling_if_changed()
        return True
    except Exception as e:
        _log_error("force_config_reload", e)
        return False


def _persist_model(model: str) -> bool:
    """Persist the active chat model to the top-level `model:` key in the config
    file so a switch survives a restart. Uses a targeted single-line replace so
    the hand-maintained comments/formatting elsewhere in the file are untouched.
    The top-level `model:` is the authoritative source of truth (it maps to
    MNEME_MODEL before the provider-level `_set` fallback runs)."""
    if not CONFIG_PATH or not os.path.isfile(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        new_text, n = re.subn(r'(?m)^model:\s*.*$', f'model: "{model}"', text, count=1)
        if n == 0:
            print("  [MODEL-SWITCH] no top-level `model:` line to replace", flush=True)
            return False
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception as e:
        print(f"  [MODEL-SWITCH] persist failed: {type(e).__name__}: {e}", flush=True)
        return False


def _env_file_path() -> str:
    """Path to the env file the start script sources (provider keys live there,
    never in the config). Shared across instances at <mneme-root>/env."""
    return os.path.join(os.path.dirname(DB_DIR), "env")


def _persist_env_key(env_file: str, key_name: str, key_value: str) -> bool:
    """Add/update `KEY=value` in the env file, preserving other lines. The key
    value itself is never logged — only the variable name."""
    try:
        lines = []
        if os.path.isfile(env_file):
            with open(env_file, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        pat = re.compile(rf'^\s*(?:export\s+)?{re.escape(key_name)}\s*=')
        kept = [ln for ln in lines if not pat.match(ln)]
        kept.append(f"{key_name}={key_value}")
        tmp = env_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
        os.replace(tmp, env_file)
        os.chmod(env_file, 0o600)
        return True
    except Exception as e:
        print(f"  [PROVIDER-KEY] persist failed: {type(e).__name__}: {e}", flush=True)
        return False


def _persist_backend_provider(name: str) -> bool:
    """Persist the active backend provider to the `backend.provider:` line (2-space
    indent under `backend:`) so a provider switch survives a restart."""
    if not CONFIG_PATH or not os.path.isfile(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        new_text, n = re.subn(r'(?m)^  provider:.*$', f'  provider: {name}', text, count=1)
        if n == 0:
            return False
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception as e:
        print(f"  [PROVIDER] persist backend.provider failed: {type(e).__name__}: {e}", flush=True)
        return False


def _persist_model_scope(path: str) -> bool:
    """Persist `filesystem.model_scope` to the config file (2-space indent under
    `filesystem:`) so a scope change survives a restart. Targeted single-line
    replace, preserving every other line's formatting/comments."""
    if not CONFIG_PATH or not os.path.isfile(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        new_text, n = re.subn(r'(?m)^  model_scope:\s*.*$', f'  model_scope: "{path}"', text, count=1)
        if n == 0:
            print("  [FS-SCOPE] no `  model_scope:` line to replace", flush=True)
            return False
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception as e:
        print(f"  [FS-SCOPE] persist failed: {type(e).__name__}: {e}", flush=True)
        return False


# ── "Open with system app" commands (file browser) ─────────────────
# Per-kind launcher commands, persisted to a small JSON sidecar beside the config
# so a user can override the system default (all default to `xdg-open`, which asks
# the OS's default-app table). Kinds: folder, text, browser, other.
_OPEN_CMDS_DEFAULT = {"folder": "xdg-open", "text": "xdg-open", "browser": "xdg-open", "other": "xdg-open"}


def _open_cmds_path() -> str:
    base = os.path.dirname(CONFIG_PATH) if CONFIG_PATH else (DB_DIR or ".")
    return os.path.join(base, "open_commands.json")


def _load_open_commands() -> dict:
    cmds = dict(_OPEN_CMDS_DEFAULT)
    try:
        with open(_open_cmds_path(), "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                cmds.update({k: v for k, v in loaded.items() if k in _OPEN_CMDS_DEFAULT})
    except Exception:
        pass
    return cmds


def _save_open_commands(cmds: dict) -> bool:
    try:
        with open(_open_cmds_path(), "w", encoding="utf-8") as f:
            json.dump({k: (cmds.get(k) or _OPEN_CMDS_DEFAULT[k]).strip() or _OPEN_CMDS_DEFAULT[k]
                       for k in _OPEN_CMDS_DEFAULT}, f, indent=2)
        return True
    except Exception as e:
        print(f"  [FS-OPEN] save open_commands failed: {type(e).__name__}: {e}", flush=True)
        return False


def _persist_backend_type(t: str) -> bool:
    """Persist `backend.type:` (2-space indent under `backend:`), used when the
    switcher toggles between an OpenAI-compatible backend and Ollama."""
    if not CONFIG_PATH or not os.path.isfile(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        new_text, n = re.subn(r'(?m)^  type:.*$', f'  type: {t}', text, count=1)
        if n == 0:
            return False
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception as e:
        print(f"  [PROVIDER] persist backend.type failed: {type(e).__name__}: {e}", flush=True)
        return False


def _extensions_root() -> str:
    """Directory scanned for extension manifests (extensions/<dir>/extension.yaml)."""
    return os.path.join(REPO_ROOT, "extensions")


def _extensions_runtime_dir() -> str:
    """Per-proxy runtime state for extensions (pidfiles, logs, saved env config)."""
    d = os.path.join(CHUNK_DIR, "extensions_runtime")
    os.makedirs(d, exist_ok=True)
    return d


def _load_extension_manifests():
    """Scan extensions/*/extension.yaml. Directories WITHOUT a manifest are
    skipped — they remain 'run in the terminal' extensions (the standard is
    opt-in, not enforced)."""
    import yaml
    root = _extensions_root()
    out = []
    if not os.path.isdir(root):
        return out
    for entry in sorted(os.listdir(root)):
        ext_dir = os.path.join(root, entry)
        mp = os.path.join(ext_dir, "extension.yaml")
        if not os.path.isfile(mp):
            continue
        try:
            with open(mp, "r", encoding="utf-8") as f:
                m = yaml.safe_load(f.read()) or {}
        except Exception:
            continue
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or entry).strip()
        out.append({
            "name": name,
            "dir": entry,
            "path": ext_dir,
            "description": m.get("description") or "",
            "command": m.get("command") or [],
            "args": m.get("args") or [],
            "config": m.get("config") or [],
            "config_file": m.get("config_file") or None,
            "health": m.get("health") or None,
        })
    return out


def _ext_pidfile(name):
    return os.path.join(_extensions_runtime_dir(), name + ".pid")


def _ext_logfile(name):
    return os.path.join(_extensions_runtime_dir(), name + ".log")


def _ext_envfile(name):
    return os.path.join(_extensions_runtime_dir(), name + ".env")


def _ext_is_running(name):
    p = _ext_pidfile(name)
    if not os.path.isfile(p):
        return False
    try:
        pid = int(open(p, "r", encoding="utf-8").read().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _ext_read_env(name):
    out = {}
    p = _ext_envfile(name)
    if os.path.isfile(p):
        for line in open(p, "r", encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _ext_write_env(name, values):
    p = _ext_envfile(name)
    lines = []
    for k, v in (values or {}).items():
        if v is None or v == "":
            continue
        lines.append(f"{k}={v}")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def _ext_spawn(manifest):
    """Launch an extension process (its `command` + `args`, with {port}/{url}
    substituted) from its own directory, inheriting the saved env config."""
    cmd = list(manifest.get("command") or [])
    args = [a.replace("{port}", str(PORT)).replace("{url}", f"http://localhost:{PORT}")
            for a in (manifest.get("args") or [])]
    full = cmd + args
    env = os.environ.copy()
    for k, v in _ext_read_env(manifest["name"]).items():
        env[k] = v
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Cap the extension log at launch: trim any existing file to the newest
    # `logging.max_entries` lines before the subprocess reopens it in append
    # mode. Extension stdout is a raw subprocess stream (can't be line-capped
    # live without a reader thread), so bounding at start keeps repeated
    # start/stop cycles from growing these logs without limit.
    from mneme.logfile import _trim_file, read_max_entries
    _cap = read_max_entries()
    if _cap:
        _trim_file(_ext_logfile(manifest["name"]), _cap)
    logf = open(_ext_logfile(manifest["name"]), "a", encoding="utf-8")
    proc = subprocess.Popen(full, cwd=manifest["path"], env=env,
                            stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    with open(_ext_pidfile(manifest["name"]), "w") as f:
        f.write(str(proc.pid))
    return proc.pid


def _ext_kill(name):
    pid = None
    p = _ext_pidfile(name)
    if os.path.isfile(p):
        try:
            pid = int(open(p, "r", encoding="utf-8").read().strip())
        except Exception:
            pid = None
    if pid:
        try:
            os.kill(pid, 15)
            for _ in range(10):
                time.sleep(0.3)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                os.kill(pid, 9)
        except ProcessLookupError:
            pass
        except Exception:
            pass
        # Reap the child so it doesn't linger as <defunct> (the proxy is the parent).
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
    try:
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass
    return True


def _settings_snapshot() -> Dict:
    """The EFFECTIVE settings, for the <<SETTINGS>> report.

    Reports resolved values (after template + file + env), plus which template
    is active, so the user sees what is actually in force rather than what they
    believe they configured. This is the answer to "I changed a setting and
    nothing happened" — compare this output against the file.
    """
    _model_cfg = (CONFIG_DATA.get("models") or {}).get(MODEL, {}) or {}
    snap = {
        "model": {
            "model": MODEL,
            "backend": os.environ.get("MNEME_BACKEND", "?"),
            "embed_model": EMBED_MODEL,
            "label_model": LABEL_MODEL,
            "config_path": CONFIG_PATH or "(none)",
            "hot_reload": "on" if HOT_RELOAD else "LOCKED",
        },
        "sampling": {
            "temperature": OLLAMA_TEMP,
            "top_p": os.environ.get("MNEME_TOP_P", "0.95"),
            "top_k": os.environ.get("MNEME_TOP_K", "64"),
            "ctx_tokens": os.environ.get("MNEME_CTX_TOKENS", "65536"),
            "max_tokens": os.environ.get("MNEME_MAX_TOKENS", "(unset)"),
        },
        "thinking": {
            "reasoning_enabled": os.environ.get("MNEME_REASONING_ENABLED", "0"),
            "reasoning_effort": os.environ.get("MNEME_REASONING_EFFORT", "(unset)"),
            "per_model_reasoning": _model_cfg.get("reasoning", "(unset)"),
        },
        "retrieval": {
            "inject_min_similarity": INJECT_MIN_SIMILARITY,
            "strategy_min_similarity": STRATEGY_MIN_SIMILARITY,
            "max_injected_tokens": MAX_INJECTED_TOKENS,
            "max_per_topic": MAX_PER_TOPIC,
            "max_siblings": globals().get("MAX_SIBLINGS", "(n/a)"),
            "topic_switch_sim": TOPIC_SWITCH_SIM,
            "novel_inject_floor": NOVEL_INJECT_FLOOR,
            "keyword_fallback": KEYWORD_FALLBACK,
            "age_decay_days": globals().get("AGE_DECAY_DAYS", "(n/a)"),
        },
        "timeouts": {
            "chat_timeout": globals().get("CHAT_TIMEOUT"),
            "ollama_chat_timeout": globals().get("OLLAMA_CHAT_TIMEOUT"),
            "first_token_timeout": globals().get("FIRST_TOKEN_TIMEOUT"),
            "stale_chunk_timeout": globals().get("STALE_CHUNK_TIMEOUT"),
        },
        "storage": {
            "memory_enabled": MEMORY_ENABLED,
            "memory_only": MEMORY_ONLY,
            "inject_enabled": INJECT_ENABLED,
            "inject_system": INJECT_SYSTEM,
            "staging_turns": os.environ.get("MNEME_STAGING_TURNS", "1"),
            "chunk_dir": os.environ.get("MNEME_CHUNK_DIR", "?"),
            "db_path": DB_PATH,
        },
        "curation": {
            "allow_model_propose": ALLOW_MODEL_PROPOSE,
            "allow_model_retract": ALLOW_MODEL_RETRACT,
            "allow_model_remove": ALLOW_MODEL_REMOVE,
            "inject_retracted": INJECT_RETRACTED,
            "recurrence_labeling": RECURRENCE_LABELING,
        },
        "template": (CONFIG_DATA.get("model_template") or "") or None,
    }
    # Per-model overrides in force for the active model (these beat sampling.*).
    if _model_cfg:
        snap["per_model_overrides"] = {k: v for k, v in _model_cfg.items()}
    return snap


# ─── Multi-pass compression config ───
MAX_HISTORY_MESSAGES = int(os.environ.get("MNEME_MAX_HISTORY_MESSAGES", "32"))  # trim conversation to keep predict budget free
CHUNK_SIZE   = int(os.environ.get("MNEME_CHUNK_SIZE", "3000"))  # chars per chunk (was defined twice: 2000 then 3000 — collapsed)
DB_MSG_CAP   = int(os.environ.get("MNEME_DB_MSG_CAP", "8000"))  # chars per message stored in SQLite (full content)
COMPRESS_THRESHOLD = int(os.environ.get("MNEME_COMPRESS_THRESHOLD", "500"))  # chars — tool results larger than this get staged
MAX_TOOL_FORWARD = int(os.environ.get("MNEME_MAX_TOOL_FORWARD", "12000"))  # chars — cap on a tool result forwarded to the model (head+tail window)
TOOL_FOLLOWUP_TOKENS = int(os.environ.get("MNEME_TOOL_FOLLOWUP_TOKENS", "10000"))  # tokens — cap on the accumulated tool-loop followup; older results compacted away before re-query
COMPRESS_MODEL     = MODEL   # use same model for compression
COMPRESS_MAX_TOK   = int(os.environ.get("MNEME_COMPRESS_MAX_TOK", "2048"))  # max tokens for compression response

# Staging: archive after N user turns or idle seconds
STAGING_TURNS  = int(os.environ.get("MNEME_STAGING_TURNS", "1"))  # swarm default: flush every turn
STAGING_IDLE   = int(os.environ.get("MNEME_STAGING_IDLE", "120"))

# Recent-context window: the tool loop keeps the last (staging_turns + this many)
# USER TURNS of the conversation instead of re-injecting the full transcript. The
# staging buffer holds up to staging_turns turns that aren't yet persisted to the DB,
# so the window must be at least that deep to avoid a retrieval gap; the extra is a
# tunable margin for continuity. Default 14 gives a 15-turn window with staging_turns
# 1 (safe for a 32K context; raise for larger models, lower for <8K).
CONTEXT_RECENT_EXTRA = int(os.environ.get("MNEME_CONTEXT_RECENT_EXTRA", "14"))

# Routing thresholds (same as KV version)
CLASSIFY_THRESHOLD = float(os.environ.get("MNEME_CLASSIFY_THRESHOLD", "0.78"))
ROUTE_THRESHOLD    = float(os.environ.get("MNEME_ROUTE_THRESHOLD", "0.08"))  # tunable: raise for stricter matching, lower for more recall
BASELINE_NOISE     = float(os.environ.get("MNEME_BASELINE_NOISE", "0.20"))  # fallback — overridden at startup by _calibrate_noise()
# Absolute injection floor: a chunk is injected only if its raw cosine similarity
# is >= this value; below it, nothing is injected. **This is embedder-dependent**
# — every embedding model has its own similarity scale (noise floor vs relevant
# band), so the default below is a starting point, NOT a universal constant.
# Measure your own: embed some obviously-relevant and obviously-irrelevant
# queries and set this just above the noise floor. Known scales:
#   voyage-4-lite            noise ~0.48, relevant ~0.70-0.72  -> 0.62 works
#   snowflake-arctic-embed2  noise ~0.32, relevant ~0.40-0.64  -> 0.45 is the
#     default here; 0.62 silently drops most relevant matches.
INJECT_MIN_SIMILARITY = float(os.environ.get("MNEME_INJECT_MIN_SIMILARITY", "0.45"))
# Strategy-only floor (below the memory floor). A chunk below INJECT_MIN_SIMILARITY
# does NOT inject as memory, but if it sits at/above this floor its LINKED
# strategies still inject — strategies are meant to generalize (same-concept,
# medium similarity) where memory is same-topic (high similarity). MUST stay below
# INJECT_MIN_SIMILARITY, and is just as embedder-dependent (same-concept band
# differs per model: voyage-4-lite ~0.43-0.62; snowflake-arctic-embed2 ~0.33-0.45).
STRATEGY_MIN_SIMILARITY = float(os.environ.get("MNEME_STRATEGY_MIN_SIMILARITY", "0.40"))
# ─── Topic-switch handling ───────────────────────────────────
# When a large DB is dominated by one topic (e.g. a story that ran for hours),
# starting a NEW topic is hard: the old topic's chunks score "moderately similar"
# (both are prose) and keep injecting, steering the model back. These knobs detect
# a topic switch (current turn far from recent turns) and harden injection for a
# short grace window so the new topic can establish itself. Set any to 0 to disable.
TOPIC_SWITCH_SIM   = float(os.environ.get("MNEME_TOPIC_SWITCH_SIM", "0.45"))   # cosine vs recent turns: below this = a switch
TOPIC_SWITCH_GRACE = int(os.environ.get("MNEME_TOPIC_SWITCH_GRACE", "2"))      # turns to harden injection after a switch
NOVEL_INJECT_FLOOR = float(os.environ.get("MNEME_NOVEL_INJECT_FLOOR", "0.60")) # raised injection floor during the grace window
MAX_PER_TOPIC      = int(os.environ.get("MNEME_MAX_PER_TOPIC", "3"))           # cap on injected chunks per topic_label


def _refresh_runtime_constants():
    """Re-read the retrieval/timeout constants from env after a hot-reload.

    These live as module globals for hot-path speed (read per request), so a
    config edit only takes effect once they are re-derived. Called from
    _reload_sampling_if_changed() after the env vars have been refreshed, and
    also used by the <<SETTINGS>> chat command's sibling <<RETRIEVAL>> setter.
    """
    global INJECT_MIN_SIMILARITY, STRATEGY_MIN_SIMILARITY, MAX_INJECTED_TOKENS
    global TOPIC_SWITCH_SIM, TOPIC_SWITCH_GRACE, NOVEL_INJECT_FLOOR
    global MAX_PER_TOPIC, KEYWORD_FALLBACK
    global AGE_DECAY_DAYS, MAX_SIBLINGS
    global MAX_HISTORY_MESSAGES, CHAT_TIMEOUT, OLLAMA_CHAT_TIMEOUT, FIRST_TOKEN_TIMEOUT, STALE_CHUNK_TIMEOUT
    INJECT_MIN_SIMILARITY = float(os.environ.get("MNEME_INJECT_MIN_SIMILARITY", "0.45"))
    STRATEGY_MIN_SIMILARITY = float(os.environ.get("MNEME_STRATEGY_MIN_SIMILARITY", "0.40"))
    MAX_INJECTED_TOKENS = int(os.environ.get("MNEME_MAX_INJECTED_TOKENS", "6000"))
    TOPIC_SWITCH_SIM = float(os.environ.get("MNEME_TOPIC_SWITCH_SIM", "0.45"))
    TOPIC_SWITCH_GRACE = int(os.environ.get("MNEME_TOPIC_SWITCH_GRACE", "2"))
    NOVEL_INJECT_FLOOR = float(os.environ.get("MNEME_NOVEL_INJECT_FLOOR", "0.60"))
    MAX_PER_TOPIC = int(os.environ.get("MNEME_MAX_PER_TOPIC", "3"))
    KEYWORD_FALLBACK = os.environ.get("MNEME_KEYWORD_FALLBACK", "0") == "1"
    try:
        AGE_DECAY_DAYS = float(os.environ.get("MNEME_AGE_DECAY_DAYS", "7"))
    except (TypeError, ValueError):
        AGE_DECAY_DAYS = 7.0
    try:
        MAX_SIBLINGS = int(os.environ.get("MNEME_MAX_SIBLINGS", "3"))
    except (TypeError, ValueError):
        MAX_SIBLINGS = 3
    try:
        MAX_HISTORY_MESSAGES = int(os.environ.get("MNEME_MAX_HISTORY_MESSAGES", "32"))
    except (TypeError, ValueError):
        MAX_HISTORY_MESSAGES = 32
    try:
        CHAT_TIMEOUT = int(os.environ.get("MNEME_CHAT_TIMEOUT", "300"))
        OLLAMA_CHAT_TIMEOUT = int(os.environ.get("MNEME_OLLAMA_CHAT_TIMEOUT", "300"))
        FIRST_TOKEN_TIMEOUT = int(os.environ.get("MNEME_FIRST_TOKEN_TIMEOUT", "45"))
        STALE_CHUNK_TIMEOUT = int(os.environ.get("MNEME_STALE_CHUNK_TIMEOUT", "20"))
    except (TypeError, ValueError):
        pass

# Keyword fallback: when FAISS returns fewer than top_k hits, pad the result list
# with SQLite LIKE-substring matches. OFF by default — substring hits carry no
# semantic score and pollute context (e.g. "tool" matches "Paramotor Tool").
KEYWORD_FALLBACK = os.environ.get("MNEME_KEYWORD_FALLBACK", "0") == "1"
# Stopwords excluded from keyword search. Substring-matching on common function
# words ("is", "me", "what", "just", "tell", "plus" ...) hits nearly every chunk
# and turns a semantic miss into arbitrary injections (e.g. "2+2" matching "is").
_KEYWORD_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "for", "with",
    "of", "to", "in", "on", "at", "by", "from", "as", "is", "are", "was", "were",
    "be", "been", "am", "do", "does", "did", "have", "has", "had", "will", "would",
    "can", "could", "should", "may", "might", "must", "not", "no", "so", "very",
    "just", "only", "also", "too", "about", "than", "there", "here", "what",
    "which", "who", "when", "where", "why", "how", "this", "that", "these", "those",
    "it", "its", "he", "she", "they", "them", "we", "you", "i", "me", "my", "your",
    "our", "their", "his", "her", "some", "any", "all", "each", "every", "more",
    "most", "other", "such", "into", "over", "under", "again", "tell", "get", "give",
    "take", "make", "let", "see", "look", "plus", "minus",
}
AGE_DECAY_DAYS     = float(os.environ.get("MNEME_AGE_DECAY_DAYS", "7"))  # recency half-life in days — newer chunks get a bonus

# Network timeouts (seconds). CHAT_TIMEOUT is the anti-grind guardrail for
# foreground/strategy calls on OpenAI-style backends (fast hang recovery);
# OLLAMA_CHAT_TIMEOUT is longer because cold-start model loading legitimately
# delays the first token; NOVELTY_TIMEOUT is for slow multi-iteration
# exploration (novelty/learning modes, 26B+ local models).
# 300s (5 min) lets the reasoning model think through long chains without the
# grind-guard aborting mid-think; the model stays resident (keep_alive=-1) so
# there's no reload latency to soak the budget.
CHAT_TIMEOUT = int(os.environ.get("MNEME_CHAT_TIMEOUT", "300"))
OLLAMA_CHAT_TIMEOUT = int(os.environ.get("MNEME_OLLAMA_CHAT_TIMEOUT", "300"))
FIRST_TOKEN_TIMEOUT = int(os.environ.get("MNEME_FIRST_TOKEN_TIMEOUT", "45"))
# Two-phase stream timeout (splits the old single 180s "stale" budget):
#   FIRST_TOKEN_TIMEOUT — no-bytes budget BEFORE the first token arrives. 45s
#       fails fast on a hung/cold provider while tolerating a slow reasoning
#       warm-up (observed max successful response ~31s).
#   STALE_CHUNK_TIMEOUT — no-bytes budget BETWEEN chunks once streaming has
#       started. Tight (20s): a reasoning model streams continuously, so a real
#       mid-stream deadlock is caught fast instead of burning 180s.
STALE_CHUNK_TIMEOUT = int(os.environ.get("MNEME_STALE_CHUNK_TIMEOUT", "20"))
CONNECT_TIMEOUT = 15  # TCP+TLS connect timeout for OpenAI-style calls
NOVELTY_TIMEOUT = int(os.environ.get("MNEME_NOVELTY_TIMEOUT", "600"))


def _main_chat_timeout() -> int:
    """Timeout for the FOREGROUND chat turn. Ollama gets the longer cold-start
    budget (OLLAMA_CHAT_TIMEOUT); OpenAI-style backends get the anti-grind
    guardrail (CHAT_TIMEOUT). Previously the main turn hardcoded CHAT_TIMEOUT,
    so Ollama users with a slow cold start timed out at the hosted-model budget."""
    return OLLAMA_CHAT_TIMEOUT if not _backend_is_openai() else CHAT_TIMEOUT
EMBED_TIMEOUT = int(os.environ.get("MNEME_EMBED_TIMEOUT", "60"))
LABEL_TIMEOUT = int(os.environ.get("MNEME_LABEL_TIMEOUT", "30"))
# Buffered (stream:false) read timeout. OpenRouter only fails over on an explicit
# 5xx/429 — a provider that accepts-then-hangs stalls the client indefinitely.
# This short timeout fails fast on that hang so OUR retry can recover (a fresh
# request usually lands on a healthy provider in ~1-3s), instead of burning
# CHAT_TIMEOUT (300s) per hang. Override with MNEME_NON_STREAM_TIMEOUT.
NON_STREAM_TIMEOUT = int(os.environ.get("MNEME_NON_STREAM_TIMEOUT", "60"))

# ─── Provider retry policy (replaces the single immediate retry) ──────────
# A transient provider failure (no first token, mid-stream stall, 429/5xx) is
# re-hit instantly today — the same overloaded window, on a fresh TCP+TLS
# handshake. Hermes (jittered_backoff) and OpenCode (Schedule.exponential +
# jitter) both back off between attempts; the log shows the instant retry
# failing identically. Bounded attempts, exponential backoff with jitter,
# Retry-After honored when the provider sends it (capped). Env-overridable.
RETRY_ATTEMPTS     = int(os.environ.get("MNEME_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF_BASE = float(os.environ.get("MNEME_RETRY_BACKOFF_BASE", "2.0"))
RETRY_BACKOFF_CAP  = float(os.environ.get("MNEME_RETRY_BACKOFF_CAP", "30.0"))
RETRY_AFTER_CAP    = 600.0  # seconds — reject pathological retry-after values (Hermes caps at 600)
# Default output budget for the OpenAI-compatible path when nothing else
# resolves (caller, models.<model>.max_tokens, MNEME_MAX_TOKENS). OpenCode's
# default is 32k (DEFAULT_MAX_TOKENS); Mneme currently sends NO cap, handing
# mandatory-reasoning models an unbounded output+thinking budget (measured:
# 28,771 reasoning tokens / 75s on GLM-5.3).
OR_DEFAULT_MAX_TOKENS = int(os.environ.get("MNEME_OR_MAX_TOKENS", "32000"))
# Thinking budget for reasoning models on the OpenAI-compatible path, as
# reasoning.max_tokens. Default "auto" = max_tokens/2 (OpenCode's
# fitThinkingBudget rule: thinking counts against the output limit, so a
# budget near it leaves the answer no room). "0"/"off" disables (pre-fix
# behaviour). An explicit number wins (still capped at max_tokens/2).
OR_REASONING_BUDGET = os.environ.get("MNEME_REASONING_BUDGET", "auto")

# ─── Reasoning-model stale-timeout floors (mirrors Hermes reasoning_timeouts.py) ────
# Stale-timeout floor for known reasoning models (mirrors Hermes'
# agent/reasoning_timeouts.py). A stream is "stale" when it produces no bytes
# for the first-token timeout (FIRST_TOKEN_TIMEOUT, default 45s); the floor is
# applied as max(default, floor). It exists for models that emit a LONG
# hidden-thinking block before their first byte (o-series, deepseek-r1,
# nemotron, etc.). GLM is deliberately NOT listed: it streams its reasoning,
# so the first byte lands in ~1-5s and 45s is plenty — the earlier 600s glm
# floor only made provider hangs cost 10 min each. Matched start-of-slug
# (after stripping any "provider/" prefix), longest slug first.
_REASONING_STALE_FLOORS = (
    ("nemotron-3-ultra", 600), ("nemotron-3-super", 600), ("nemotron-3-nano", 300),
    ("deepseek-r1", 600), ("deepseek-reasoner", 600), ("deepseek-v4-flash", 600), ("deepseek-v4-pro", 600),
    ("qwq-32b", 300), ("qwen3", 180),
    ("o1", 600), ("o1-mini", 600), ("o1-pro", 600), ("o1-preview", 600),
    ("o3", 600), ("o3-pro", 600), ("o3-mini", 300), ("o4-mini", 300),
    ("claude-opus-4", 240), ("claude-sonnet-5", 180), ("claude-sonnet-4.5", 180), ("claude-sonnet-4.6", 180),
    ("grok-4-fast-reasoning", 300), ("grok-4.20-reasoning", 300), ("grok-4.5", 300), ("grok-4-fast-non-reasoning", 180),
)

# Models whose endpoints MANDATE reasoning — sending `reasoning.enabled:false`
# makes the provider 400 with "Reasoning is mandatory for this endpoint". For
# these, a no_reasoning call can't disable thinking; it is bounded with a small
# budget instead. Slug-anchored, longest-first (same convention as the floors).
_MANDATORY_REASONING_MODELS = ("glm-5.3", "glm-5", "glm-4.7", "glm-4.6")

# Reasoning budget applied to a mandatory-reasoning model when a call asks for
# NO reasoning (no_reasoning=True) — e.g. the terse yes/no self-report in
# _ask_reusable_strategy. Small enough to keep the call cheap, big enough not to
# truncate a legitimate thought. Capped at max_tokens/2 by the caller.
_MANDATORY_REASONING_MIN_BUDGET = int(os.environ.get("MNEME_MANDATORY_REASONING_BUDGET", "2000"))

# ─── Truncation limits (Phase 1.2 — names only, values unchanged) ───
MAX_QUERY_CHARS      = 500    # user query extraction for memory routing
MAX_JUDGE_CHARS      = 8000   # pairwise judge baseline/candidate excerpt (must cover full answers)
MAX_STORY_CHARS      = 2000   # generic content truncation for prompts
MAX_STORY_CHARS_ALT  = 1500   # secondary content truncation (belief/thinking paths)
MAX_MESSAGE_STORE    = 8000   # per-message char cap when storing in SQLite
MAX_THINKING_STORE   = 8000   # thinking field char cap in SQLite
# MAX_PROMPT_CHARS is defined below near build_context (env-overridable)
MAX_PREVIEW_CHARS    = 300    # short inline previews
MAX_MSG_TEXT_CHARS   = 800    # per-message excerpt inside comparison/summary prompts
MAX_SEMANTIC_FRAG    = 5000   # per-message fragment when building embedding text
MAX_LABEL_INPUT      = 2000   # chars fed to the topic-label model
MAX_DETAIL_CHARS     = 20000  # chars returned by the <<DETAIL>> endpoint
MAX_ABSTRACT_INPUT   = 400    # short excerpt of one message
MAX_ARCHIVE_FRAG     = 200    # per-message fragment for outcome/type heuristics

# Save-cycle counter — incremented on every staging flush AND manual <<SAVE>>
_archive_cycle = 0
_archive_cycle_lock = threading.Lock()
_chunk_seq = 0
_chunk_seq_lock = threading.Lock()
# Pending strategy->source_chunk links: strategies saved with no source_chunk
# (recovery / DON'T-DO / novel — the turn's chunk is archived asynchronously
# AFTER the save is enqueued) are queued here and linked to the next archived
# chunk in _archive_single_chunk.
_pending_strategy_links = []
# Chunk ids that went into the current turn's injected context (set by
# build_context, read by the archive path to stamp injected_chunk_ids). Same
# turn-scoped-global pattern as _pending_strategy_links above.
_last_injected_ids = []
_pending_links_lock = threading.Lock()
# Single sqlite connection shared by the main thread + 2 background workers
# (check_same_thread=False). Writes must be serialized: an unguarded commit()
# racing another thread's commit on the same connection raises
# "cannot commit - no transaction is active". Wrap every write+commit pair
# (save_chunk, _save_strategy, _archive_single_chunk) in this lock.
_db_lock = threading.RLock()


def _db_write_retry(fn, retries=3, backoff=0.5):
    """Run fn() (which performs DB writes) under _db_lock, then commit, retrying
    on SQLite 'locked'/'busy' so a transient cross-process collision can't drop a
    write. Shared-DB deployments (one DB, many proxies) hit genuine cross-process
    write contention; the connect-level busy timeout handles most of it, and this
    retry catches the rest. Returns fn's result."""
    last = None
    for attempt in range(retries + 1):
        try:
            with _db_lock:
                result = fn()
                db.commit()
            return result
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                last = e
                try:
                    db.rollback()
                except Exception:
                    pass
                print(f"  [DB] write locked — attempt {attempt + 1}/{retries + 1}", flush=True)
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    raise last


def _seed_chunk_seq():
    global _chunk_seq
    try:
        row = db.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(chunk_id, 5) AS INTEGER)), 0) FROM chunks WHERE chunk_id LIKE 'mem_%'"
        ).fetchone()
        if row and row[0]:
            _chunk_seq = row[0]
            print(f"  [STARTUP] chunk_seq seeded to {_chunk_seq}", flush=True)
    except Exception:
        pass

def _next_cycle() -> int:
    global _archive_cycle
    with _archive_cycle_lock:
        _archive_cycle += 1
        return _archive_cycle

def _current_cycle() -> int:
    with _archive_cycle_lock:
        return _archive_cycle

# ─── Error Log (Phase 1.1) ─────────────────────────────────────
# Every previously-silent except: now funnels here. Failures stay visible
# without killing the proxy.
ERROR_LOG_FILE = os.path.join(CHUNK_DIR, "errors.log")

# _log_error was extracted to mneme/util.py (imported at top of this file).

os.makedirs(CHUNK_DIR, exist_ok=True)
os.makedirs(DB_DIR, exist_ok=True)

# ─── Structured-output helper (Phase 2) ─────────────────────────
# Parses model reply as JSON; on failure falls back to a regex parse and logs
# a warning. Never raises — returns (data_dict, used_fallback).

def _parse_structured(reply: str, schema_hint: str, fallback_re: str = None,
                      fallback_group: int = 1):
    """Try json.loads(reply); if that fails and fallback_re given, try regex.
    Returns (parsed_dict, used_fallback). On total failure returns ({}, True)."""
    try:
        data = json.loads(reply.strip())
        if isinstance(data, dict):
            return data, False
        # Model sometimes wraps in a list
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0], False
        return {"value": data}, False
    except Exception:
        pass
    if fallback_re:
        m = re.search(fallback_re, reply, re.IGNORECASE | re.MULTILINE)
        if m:
            print(f"  [WARN] Structured output failed, regex fallback used for {schema_hint}", flush=True)
            return {schema_hint: m.group(fallback_group)}, True
    print(f"  [WARN] Structured output failed and no fallback matched for {schema_hint} — reply[:200]={reply[:200]!r}", flush=True)
    return {}, True

# ─── Supervised background workers (Phase 3) ────────────────────
# One queue + N daemon threads. Jobs are (fn, args, kwargs); any exception is
# written to errors.log via _log_error and the loop continues. Replaces
# fire-and-forget threading.Thread(daemon=True) which swallowed every failure.

_BG_QUEUE: "queue.Queue" = queue.Queue()
_BG_N_WORKERS = 2
_bg_started = False
_bg_start_lock = threading.Lock()

# ── Turn cancellation (the chat UI "Stop" button) ────────────────
# A single process-wide event. The /cancel endpoint sets it; the foreground
# generation loops (Ollama + OpenRouter streaming) poll it between chunks and
# abort, so a runaway "thinking" turn can be stopped without restarting the
# proxy. Cleared at the start of each chat request.
_cancel_event = threading.Event()
# Per-thread cancel scope. A harness step runs process_chat with its OWN event
# (set by the run's pause/cancel), so the chat UI's Stop button (the global
# event) no longer stops a harness step, and a run's pause/cancel no longer
# stops a chat turn. Threads without a scope use the global event as before.
_cancel_local = threading.local()


def _turn_cancel_event() -> threading.Event:
    return getattr(_cancel_local, "event", None) or _cancel_event


# ── Token streaming (the chat UI's live updates) ───────────────────
# Per-thread token sink: the chat SSE endpoint installs a callback here while a
# turn is in flight; the provider streaming loop calls _emit_token for each
# content/reasoning piece as it arrives. Threads without a sink (harness steps,
# background workers) emit to nothing.
_stream_local = threading.local()


def _emit_token(kind: str, text: str) -> None:
    cb = getattr(_stream_local, "sink", None)
    if cb:
        try:
            cb(kind, text)
        except Exception:
            pass


def _scoped_process_chat(messages, cancel_event=None, tool_grant=None, **kw):
    """process_chat for a harness step: a private cancel event, and (optionally) a
    permission grant — tools whose permission level is not granted are neither
    offered to the model nor executed. Also routes the model's live tokens to
    the run's stream buffer so the chat can watch the step unfold."""
    _cancel_local.event = cancel_event or threading.Event()
    _cancel_local.tool_grant = set(tool_grant) if tool_grant is not None else None
    _sid = kw.get("session_id") or ""
    _run_id = _sid[4:] if _sid.startswith("run:") else None
    if _run_id:
        def _sink(kind, text):
            _run_live.publish(_run_id, "token", {"kind": kind, "text": text})
            _think_log.record(_run_id, kind, text)
        _stream_local.sink = _sink
    try:
        return process_chat(messages, **kw)
    finally:
        _cancel_local.event = None
        _cancel_local.tool_grant = None
        if _run_id:
            _stream_local.sink = None


def _turn_tool_ok(name: str) -> bool:
    grant = getattr(_cancel_local, "tool_grant", None)
    if grant is None:
        return True
    from mneme.harness.capabilities import allowed
    return allowed(name, grant)

def _bg_worker():
    while True:
        try:
            fn, args, kwargs = _BG_QUEUE.get()
        except Exception as e:
            _log_error("bg_worker:get", e)
            continue
        try:
            fn(*args, **kwargs)
        except Exception as e:
            _log_error(f"bg_worker:{getattr(fn, '__name__', fn)}", e)
        finally:
            try:
                _BG_QUEUE.task_done()
            except Exception:
                pass

def _start_bg_workers():
    global _bg_started
    with _bg_start_lock:
        if _bg_started:
            return
        for i in range(_BG_N_WORKERS):
            t = threading.Thread(target=_bg_worker, name=f"mneme-bg-{i}", daemon=True)
            t.start()
        _bg_started = True


def _gc_loop():
    """Periodic content-addressed image GC (interval + grace from env)."""
    if IMAGE_GC_INTERVAL <= 0:
        return
    while True:
        time.sleep(IMAGE_GC_INTERVAL)
        try:
            _gc_images()
        except Exception as e:
            _log_error("gc_images:loop", e)


def _start_gc_loop():
    t = threading.Thread(target=_gc_loop, name="mneme-gc", daemon=True)
    t.start()

def _enqueue(fn, *args, **kwargs):
    """Submit a background job. Workers are started lazily on first enqueue."""
    _start_bg_workers()
    _BG_QUEUE.put((fn, args, kwargs))

# ─── Database ──────────────────────────────────────────────────

class _LockedConnection(sqlite3.Connection):
    """A sqlite3 Connection whose commit() holds the shared write lock.

    This one connection is shared by the request thread + background workers. An
    unguarded commit() racing another thread's commit raises "cannot commit - no
    transaction is active" (SQLite has no active transaction for THIS thread).

    The codebase guards its hot-path writers with _db_lock, but ~30 call sites
    (tool registry, capability edges, preferences, ...) commit directly. Rather
    than depend on every present and future call site remembering, make commit()
    itself atomic. _db_lock is an RLock, so this is safe inside code that already
    holds it — no deadlock.

    (Found by the provenance work: the archive thread's new writes used to
    destabilise unrelated tests, because extra commits widened the race window for
    every unguarded site. Subclassing is necessary because a Connection's
    attributes are read-only — you cannot monkey-patch db.commit.)
    """

    def commit(self, *a, **kw):
        with _db_lock:
            return super().commit(*a, **kw)


db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=60,
                     factory=_LockedConnection)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")
capability.db = db  # bind the extracted capability module's db handle
mntools.db = db      # bind the tool system's db handle

db.executescript("""
    CREATE TABLE IF NOT EXISTS chunks (
        chunk_id    TEXT PRIMARY KEY,
        topic_label TEXT NOT NULL,
        messages    TEXT NOT NULL,          -- JSON array of {role, content}
        thinking    TEXT DEFAULT '',
        strategy    TEXT DEFAULT '',
        vector      BLOB,                   -- 1024 × float32 = 4096 bytes
        grade       TEXT DEFAULT 'C',
        consensus   REAL DEFAULT 0.0,
        outcome     TEXT DEFAULT '',
        problem_type TEXT DEFAULT 'other',
        source      TEXT DEFAULT 'unknown',
        cycle       INTEGER DEFAULT 0,
        created_at  TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS strategies (
        strategy_id   TEXT PRIMARY KEY,
        problem_type  TEXT NOT NULL,
        strategy_text TEXT NOT NULL,
        source_chunk  TEXT,
        grade         TEXT DEFAULT 'B',
        created_at    TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS preferences (
        pref_key   TEXT PRIMARY KEY,
        pref_value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS capability_edges (
        problem_type TEXT PRIMARY KEY,
        attempts     INTEGER DEFAULT 0,
        failures     INTEGER DEFAULT 0,   -- D/F grades
        last_grade   TEXT DEFAULT '',
        flagged      INTEGER DEFAULT 0,   -- 1 = known capability edge
        updated_at   TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS tools (
        tool_id       TEXT PRIMARY KEY,
        problem_type  TEXT NOT NULL,
        name          TEXT NOT NULL,
        description   TEXT DEFAULT '',
        script_path   TEXT DEFAULT '',
        tested_at     TEXT DEFAULT '',
        success_count INTEGER DEFAULT 0,
        retired       INTEGER DEFAULT 0
    );
    
    CREATE INDEX IF NOT EXISTS idx_chunks_topic ON chunks(topic_label);
    CREATE INDEX IF NOT EXISTS idx_chunks_type  ON chunks(problem_type);
    CREATE INDEX IF NOT EXISTS idx_strategies_type ON strategies(problem_type);
    CREATE INDEX IF NOT EXISTS idx_tools_type ON tools(problem_type);
""")

# ─── Schema migrations for existing DBs ─────────────────────────
for migration in (
    "ALTER TABLE chunks ADD COLUMN source TEXT DEFAULT 'unknown'",
    "ALTER TABLE chunks ADD COLUMN cycle INTEGER DEFAULT 0",
    "ALTER TABLE chunks ADD COLUMN session_id TEXT DEFAULT 'default'",
    "ALTER TABLE chunks ADD COLUMN indexable INTEGER DEFAULT 1",
    "ALTER TABLE strategies ADD COLUMN version INTEGER DEFAULT 1",
    "ALTER TABLE strategies ADD COLUMN parent_id TEXT DEFAULT ''",
    "ALTER TABLE strategies ADD COLUMN effective_grade REAL DEFAULT 0.0",
    "ALTER TABLE strategies ADD COLUMN use_count INTEGER DEFAULT 0",
    "ALTER TABLE strategies ADD COLUMN success_count INTEGER DEFAULT 0",
    "ALTER TABLE chunks ADD COLUMN superseded_by TEXT DEFAULT ''",
    "ALTER TABLE strategies ADD COLUMN retired INTEGER DEFAULT 0",
    "ALTER TABLE strategies ADD COLUMN superseded_by TEXT DEFAULT ''",
    "ALTER TABLE strategies ADD COLUMN cost INTEGER DEFAULT 0",
    "ALTER TABLE chunks ADD COLUMN pending_embed INTEGER DEFAULT 0",
    "ALTER TABLE chunks ADD COLUMN embed_model TEXT DEFAULT ''",
    "ALTER TABLE chunks ADD COLUMN dim INTEGER DEFAULT 0",
    "ALTER TABLE capability_edges ADD COLUMN overcome_attempts INTEGER DEFAULT 0",
    "ALTER TABLE capability_edges ADD COLUMN overcome_success INTEGER DEFAULT 0",
    "ALTER TABLE capability_edges ADD COLUMN tool_id TEXT DEFAULT ''",
    "ALTER TABLE tools ADD COLUMN script_source TEXT DEFAULT ''",
    "ALTER TABLE tools ADD COLUMN embedding BLOB",
    "ALTER TABLE tools ADD COLUMN last_used_at TEXT DEFAULT ''",
    "ALTER TABLE strategies ADD COLUMN outcome TEXT DEFAULT 'SUCCESS'",
    "ALTER TABLE chunks ADD COLUMN trust TEXT DEFAULT ''",
):
    try:
        db.execute(migration)
    except sqlite3.OperationalError:
        pass  # column already exists
db.commit()
# Memory curation schema (retraction / recurrence / provenance / decision log).
# Additive + idempotent, same style as the migrations above.
curation.ensure_schema(db)
# Strategy version history + provenance (harness Phase 3) — additive.
_strat_hist.ensure_schema(db)
# Backfill: mark failure-derived strategies as FAILURE so they inject under the
# "do NOT do this" header rather than as success examples. Only matches the old
# "FAILURE on:"/"TRUNCATED on:" text — new failures set outcome at insert time.
try:
    db.execute("UPDATE strategies SET outcome='FAILURE' WHERE strategy_text LIKE 'FAILURE on:%' OR strategy_text LIKE 'TRUNCATED on:%'")
    # Older code saved strategies with problem_type='model' placeholder, which
    # never matches a query's classified type (so they stay inert). Left as-is —
    # 'other' is deprecated as a tag (not searchable), so we no longer fold into it.
    # Reformat old-format failure text to the negative directive, so existing
    # rows match what generate_strategy now produces for new failures. Idempotent:
    # once rewritten the text no longer starts with "FAILURE on:".
    for _sid, _txt in db.execute("SELECT strategy_id, strategy_text FROM strategies WHERE strategy_text LIKE 'FAILURE on:%'").fetchall():
        _body = _txt[len("FAILURE on: "):].replace(". Retry with different approach.", "").strip()
        db.execute("UPDATE strategies SET strategy_text=? WHERE strategy_id=?",
                   (f"Do NOT repeat what failed here: {_body}. Instead, try a different approach.", _sid))
    db.commit()
except sqlite3.OperationalError:
    pass

# ─── Shipped + user strategies (durable files) ─────────────────
# Loaded from strategies.yaml (ships with the repo) + strategies.user.yaml
# (per-instance) with INSERT OR IGNORE, so the curated playbooks survive both
# restarts and a DB reset. See mneme/strategies.py.
_SHIPPED_STRATEGIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies.yaml")
_USER_STRATEGIES_PATH = os.path.join(CHUNK_DIR, "strategies.user.yaml") if CHUNK_DIR else ""
import mneme.strategies as _strat
_strat.load_shipped(db, (_SHIPPED_STRATEGIES_PATH, _USER_STRATEGIES_PATH))

# ─── Grade Priority (same as raw-k-cache) ──────────────────────
GRADE_PRIORITY = {"A": 3, "B": 2, "C": 1, "F": 0}
DEFAULT_GRADE   = "C"

# ─── Phase 5.1: Permanent meta-principles ──────────────────────
# A small fixed set of always-relevant thinking directives, injected every
# turn independent of memory retrieval. Deliberately short and constant —
# NOT counted against the dynamic MAX_INJECTED_TOKENS budget.
META_PRINCIPLES = [
    "Answer directly and concisely. A straightforward answer is usually correct — do not reject an answer merely because it came quickly.",
    "Run one quick sanity check before committing: is there a concrete reason this is wrong? If not, give the answer and stop.",
    "Only explore alternatives when the question is genuinely ambiguous, explicitly asks for options, or the sanity check found a real flaw. Do not brainstorm as a default ritual.",
    "Prefer the mechanism over the example — name the underlying rule, not just the surface detail.",
    "State your confidence honestly: if unsure, say so plainly instead of padding the answer with extra reasoning.",
]

def grade_priority(chunk_id: str) -> int:
    row = db.execute("SELECT grade FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
    return GRADE_PRIORITY.get(row[0], GRADE_PRIORITY[DEFAULT_GRADE]) if row else 1

# ─── FAISS Index ───────────────────────────────────────────────

# snowflake-arctic-embed2 produces 1024-dim embeddings (nomic-embed-text was 768).
# NOTE: existing vectors in the DB are 768-dim and incompatible. Wipe
# mneme/chunks/mneme.db (or run a migration) before starting with
# the new embedder, otherwise FAISS will reject add/search on shape mismatch.
# EMBED_MODEL is env-overridable so a DB can move between machines with
# different embedders (the startup health check re-embeds mismatched chunks).
EMBED_MODEL = os.environ.get("EMBED_MODEL", "snowflake-arctic-embed2")
# Embedding dimension. MRL models (Qwen3-Embedding, bge-m3) can emit a truncated
# dim, so the FAISS index dimension and the requested output dim are the same knob.
try:
    DIM = int(os.environ.get("EMBED_DIM", "1024"))
except (TypeError, ValueError):
    DIM = 1024
try:
    import faiss
    _index = faiss.IndexFlatIP(DIM)          # inner product (cosine on norm'd vectors)
    _id_map: List[str] = []                  # index position → chunk_id
    FAISS_OK = True
except ImportError:
    _index = None; _id_map = []; FAISS_OK = False
    print("[mokv] FAISS not available — install faiss-cpu", flush=True)

_idx_lock = threading.Lock()

# Multi-writer FAISS: disk persistence + file locking
FAISS_INDEX_FILE = os.path.join(DB_DIR, "faiss.index")
FAISS_IDMAP_FILE = os.path.join(DB_DIR, "faiss.idmap")
FAISS_LOCK_FILE   = os.path.join(DB_DIR, "faiss.lock")

import fcntl

class faiss_lock:
    """Context manager for fcntl file lock around FAISS operations.
    Kernel-enforced — released on process death, no stale locks."""
    def __init__(self):
        self._fd = None
    def __enter__(self):
        self._fd = open(FAISS_LOCK_FILE, "w")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self
    def __exit__(self, *args):
        if self._fd:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()

def _save_index():
    """Save FAISS index + id_map to disk. Caller must hold faiss_lock."""
    if FAISS_OK and _index is not None:
        faiss.write_index(_index, FAISS_INDEX_FILE)
    with open(FAISS_IDMAP_FILE, "w") as f:
        json.dump(list(_id_map), f)

def _load_index_from_disk():
    """Load FAISS index + id_map from disk. Caller must hold faiss_lock."""
    global _id_map, _index
    if os.path.exists(FAISS_INDEX_FILE) and FAISS_OK:
        _index = faiss.read_index(FAISS_INDEX_FILE)
    else:
        _index = faiss.IndexFlatIP(DIM) if FAISS_OK else None
    if os.path.exists(FAISS_IDMAP_FILE):
        with open(FAISS_IDMAP_FILE) as f:
            _id_map = json.load(f)
    else:
        _id_map = []

def _load_index():
    """Rebuild FAISS index from SQLite (fallback if disk files missing)."""
    global _id_map
    rows = db.execute(
        "SELECT chunk_id, vector FROM chunks WHERE vector IS NOT NULL AND (superseded_by = '' OR superseded_by IS NULL)"
    ).fetchall()
    with faiss_lock():
        _id_map.clear()
        if FAISS_OK and _index is not None:
            _index.reset()
        for cid, blob in rows:
            vec = _blob_to_vec(blob)
            if vec is not None and vec.shape[0] == DIM:
                if FAISS_OK and _index is not None:
                    _index.add(vec.reshape(1, -1))
                _id_map.append(cid)
            elif vec is not None:
                print(f"  [HEALTH] skipping {cid}: dim {vec.shape[0]} != {DIM} (embedder changed?)",
                      flush=True)
        _save_index()
    print(f"[mokv] FAISS loaded {len(_id_map)} vectors", flush=True)

# ─── Vector Helpers ────────────────────────────────────────────

def _vec_to_blob(vec: np.ndarray) -> bytes:
    """1024 float32 → 4096 bytes."""
    return vec.astype(np.float32).tobytes()

def _blob_to_vec(blob: bytes) -> Optional[np.ndarray]:
    try:
        return np.frombuffer(blob, dtype=np.float32).copy()
    except Exception as e:
        _log_error("_blob_to_vec", e)
        return None

# ─── Embedding: chunk + pool for long text ─────────────────────

# arctic-embed2 token limit is 8192 TOKENS, not chars. Dense text (Wikipedia
# references/citations/URLs) tokenizes ~2-3 tokens/char, so a 4000-char window
# can blow past 8192 and Ollama returns 500 "input length exceeds the context
# length", silently leaving the chunk pending_embed. 2000 chars is safely under
# even for token-dense content (≈4-6k tokens). Overlap keeps sentence context.
CHUNK_CHARS    = int(os.environ.get("MNEME_CHUNK_CHARS", "2000"))
CHUNK_OVERLAP  = 200

def chunk_text(text: str,
               chunk_size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping windows.

    Returns [text] unchanged when it fits in a single window. Overlap is
    clamped to half the chunk size so stepping always moves forward.
    """
    if not text:
        return [""]
    if len(text) <= chunk_size:
        return [text]
    overlap = min(overlap, chunk_size // 2)
    step = chunk_size - overlap
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        chunks.append(text[start:start + chunk_size])
        if start + chunk_size >= n:
            break
        start += step
    return chunks

def pool_embeddings(vectors: List[np.ndarray]) -> np.ndarray:
    """Mean-pool a list of embedding vectors into a single centroid.

    The result is L2-normalized so it stays compatible with the FAISS
    IndexFlatIP cosine-similarity convention used elsewhere.
    """
    if not vectors:
        return np.zeros(DIM, dtype=np.float32)
    stacked = np.stack(vectors)
    centroid = stacked.mean(axis=0).astype(np.float32)
    return centroid / (np.linalg.norm(centroid) + 1e-8)

def _embed_single(text: str) -> np.ndarray:
    """Embed one chunk. Uses the embedder's own connection (embed_provider, or the
    chat backend as a fallback) — so the embed model can live on a different
    provider than the chat model. Raises on failure."""
    conn = _aux_conn("embed")
    if conn["kind"] == "openai":
        _h = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
        if conn["key"]:
            _h["Authorization"] = f"Bearer {conn['key']}"
        _h.update(conn["headers"])
        r = requests.post(
            f"{conn['base_url']}/embeddings",
            headers=_h,
            json={"model": EMBED_MODEL, "input": text, "dimensions": DIM},
            timeout=EMBED_TIMEOUT,
        )
        r.raise_for_status()
        v = np.array(r.json()["data"][0]["embedding"], dtype=np.float32)
    else:
        r = requests.post(
            f"{conn['base_url']}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text, "dimensions": DIM},
            timeout=EMBED_TIMEOUT,
        )
        r.raise_for_status()
        v = np.array(r.json()["embedding"], dtype=np.float32)
    # MRL truncation safety net: if the provider ignores `dimensions` and returns
    # the native-dim vector, keep the first DIM (pad if it somehow returns fewer).
    if v.shape[0] != DIM:
        v = v[:DIM] if v.shape[0] > DIM else np.pad(v, (0, DIM - v.shape[0]))
    return v / (np.linalg.norm(v) + 1e-8)

def embed(text: str):
    """Embed text via snowflake-arctic-embed2 with chunk+pool for long input.

    - Short text (<= CHUNK_CHARS): single embedding call.
    - Long text: split into overlapping windows, embed each, mean-pool to
      a single 1024-dim centroid.
    - On ANY failure (empty text, Ollama down, model missing, bad JSON) returns
      None — NOT a zero vector. A zero vector was silently unretrievable; None
      is an explicit "not embedded" signal so callers can mark the chunk
      pending_embed instead of storing a dead vector that never matches.
    """
    if not text or not text.strip():
        return None
    try:
        chunks = chunk_text(text)
        if len(chunks) == 1:
            _v = _embed_single(chunks[0])
            _set_status("embed", True)
            return _v
        vecs = [_embed_single(c) for c in chunks]
        pooled = pool_embeddings(vecs)
        print(f"  [EMBED] chunked {len(text)} chars into {len(chunks)} windows "
              f"-> pooled centroid", flush=True)
        _set_status("embed", True)
        return pooled
    except Exception as e:
        print(f"  [EMBED][ERROR] {type(e).__name__}: {e} — returning None (pending_embed)",
              flush=True)
        _set_status("embed", False, f"{type(e).__name__}: {e}")
        return None


mntools.embed = embed  # bind the tool system's embed function


# ─── Memory curation hooks ───────────────────────────────────────────────
# The flag tool is a MARKER, not an action: the model can raise a suspicion, the
# user decides. This is the only mode — there is no confirm/deny flow, and no path
# by which the model removes anything.
#
# The tool NAME and the RESULT TEXT both have to say this, because both feed the
# model's self-report. An earlier version named the tool `retract_memory`, used
# the word "propose", and returned "...pending their confirmation" — and the model
# duly told users "once you confirm, I will proceed with retracting it". Three
# separate wordings promising authority it does not have. Now the wording is
# consistent with the behaviour.
def _curation_retract(chunk_id: str, reason: str = "") -> str:
    """Called by the flag_bad_memory tool. Only ever marks the chunk."""
    try:
        c = curation._get_chunk(db, chunk_id)
        if not c:
            return f"[flag_bad_memory: no such chunk {chunk_id} — check the id]"
        if not (ALLOW_MODEL_PROPOSE or ALLOW_MODEL_RETRACT):
            return "[flag_bad_memory: not permitted on this proxy (curation disabled)]"
        curation.set_bad_chunk(db, chunk_id, True, actor="model", reason=reason)
        print(f"  [CURATION] model flagged {chunk_id} as bad: {reason[:80]}", flush=True)
        return (
            f"[flag_bad_memory: MARKED {chunk_id} as a bad chunk. Nothing has changed "
            f"about how this memory is used — it is still in use, unmodified, and not "
            f"removed. The user will see it flagged in the memory management page and "
            f"may act on it if they agree; that is entirely their decision and you "
            f"have no part in it. Report exactly this: you flagged it, you did not "
            f"change or remove it, and nothing further is required from the user to "
            f"'confirm' anything with you. Reason recorded: {reason}]"
        )
    except Exception as e:
        _log_error("curation:flag", e)
        return f"[flag_bad_memory error: {type(e).__name__}: {e}]"


def _curation_restore(chunk_id: str, reason: str = "") -> str:
    """Called by the clear_bad_memory_flag tool — remove a marker."""
    try:
        c = curation._get_chunk(db, chunk_id)
        if not c:
            return f"[clear_bad_memory_flag: no such chunk {chunk_id}]"
        if not c.get("proposed_retract"):
            return (f"[clear_bad_memory_flag: {chunk_id} is not flagged as a bad chunk "
                    f"— nothing to clear]")
        curation.set_bad_chunk(db, chunk_id, False, actor="model", reason=reason)
        print(f"  [CURATION] cleared bad-chunk flag on {chunk_id}", flush=True)
        return (f"[clear_bad_memory_flag: CLEARED the flag on {chunk_id}. It is now "
                f"unmarked. As before, nothing about how it is used changed — that "
                f"remains the user's decision.]")
    except Exception as e:
        _log_error("curation:clear_flag", e)
        return f"[clear_bad_memory_flag error: {type(e).__name__}: {e}]"


def _curation_remove(chunk_id: str, reason: str = "") -> str:
    """Called by the remove_memory tool. Sets the `removed` flag on a chunk,
    taking it out of injection and model search. Reversible (user can restore)."""
    try:
        c = curation._get_chunk(db, chunk_id)
        if not c:
            return f"[remove_memory: no such chunk {chunk_id} — check the id]"
        if not ALLOW_MODEL_REMOVE:
            return "[remove_memory: not permitted on this proxy (model removal disabled)]"
        curation.set_removed(db, chunk_id, True, actor="model", reason=reason)
        print(f"  [CURATION] model removed {chunk_id}: {reason[:80]}", flush=True)
        return (
            f"[remove_memory: REMOVED {chunk_id} from memory. It will no longer be "
            f"injected into context or returned by search_memory. The row is kept and "
            f"the user can restore it from the memory management page if this was wrong. "
            f"Reason recorded: {reason}]"
        )
    except Exception as e:
        _log_error("curation:remove", e)
        return f"[remove_memory error: {type(e).__name__}: {e}]"


mntools.set_curation_hooks(
    _curation_retract, _curation_restore,
    retract_allowed=ALLOW_MODEL_RETRACT,
    propose_allowed=ALLOW_MODEL_PROPOSE,
    remove_fn=_curation_remove,
    remove_allowed=ALLOW_MODEL_REMOVE,
)


def _embed_or_zeros(text: str) -> np.ndarray:
    """embed() with a zero-vector fallback for non-save paths (novelty scoring)
    that already treat a zero vector as 'no embedding'."""
    v = embed(text)
    return v if v is not None else np.zeros(DIM, dtype=np.float32)

def _cosine_search(query_vec: np.ndarray, top_k: int, threshold: float):
    """Search FAISS with file lock — loads index from disk, searches, releases.
    Multi-writer safe: any proxy with the lock sees the latest index state.
    Returns [] when the query vector is None (embed failure) so callers fall
    back to keyword search instead of crashing."""
    if query_vec is None:
        return []
    with faiss_lock():
        _load_index_from_disk()  # Always fresh from disk
        if not _id_map:
            return []
        if FAISS_OK and _index is not None:
            k = min(top_k, len(_id_map))
            scores, idxs = _index.search(query_vec.reshape(1, -1), k)
            return [(float(s), _id_map[i]) for s, i in zip(scores[0], idxs[0])
                    if i >= 0 and float(s) >= threshold]
        return []

def _keyword_search(query: str, top_k: int, exclude_ids: set = None):
    """SQLite LIKE keyword fallback when FAISS is sparse.
    
    Splits query into words, searches messages column for each,
    deduplicates, returns [(0.0, chunk_id), ...] ordered by recency.
    """
    if not query or not query.strip():
        return []
    words = [w.strip() for w in query.split()
             if len(w.strip()) >= 2 and w.strip().lower() not in _KEYWORD_STOPWORDS]
    if not words:
        return []
    exclude_ids = exclude_ids if exclude_ids is not None else set()
    seen = set()
    results = []
    # Search each word, collect matching chunk_ids
    for word in words:
        pattern = f"%{word}%"
        rows = db.execute(
            "SELECT chunk_id FROM chunks WHERE messages LIKE ? ORDER BY created_at DESC LIMIT ?",
            (pattern, top_k * 2)
        ).fetchall()
        for (cid,) in rows:
            if cid not in seen and cid not in exclude_ids:
                seen.add(cid)
                results.append((0.0, cid))  # score 0.0 = keyword match, no semantic score
            if len(results) >= top_k:
                break
        if len(results) >= top_k:
            break
    return results[:top_k]

def _hybrid_search(query: str, top_k: int, faiss_results: list):
    """Pad FAISS results with keyword matches (only when KEYWORD_FALLBACK is on).

    Returns list of (score, chunk_id, method) tuples. Keyword matches carry a
    score of 0.0 (no semantic signal), so this is gated behind KEYWORD_FALLBACK
    (default off) — substring hits pollute context otherwise.
    """
    faiss_ids = {cid for _, cid in faiss_results}
    combined = [(s, cid, "faiss") for s, cid in faiss_results]
    if KEYWORD_FALLBACK and len(combined) < top_k:
        needed = top_k - len(combined)
        kw_results = _keyword_search(query, needed, exclude_ids=faiss_ids)
        combined.extend([(s, cid, "keyword") for s, cid in kw_results])
    return combined

# ─── Model Interface ───────────────────────────────────────────

# The fixed system prompts (system_prompt.md / system_prompt_memory.md) are now
# loaded via mneme.instructions._load_instruction, so they're editable through
# the /instructions page like every other injected prompt (code-default + disk
# override + graceful fallback). See _system_prompt_block() below.

MISSION_FILE = os.path.join(os.path.dirname(__file__), "mission.md")
def _load_mission() -> str:
    try:
        with open(MISSION_FILE) as f:
            return f.read().strip()
    except Exception as e:
        _log_error("_load_mission", e)
        return ""

MISSION = _load_mission()


def _system_prompt_block() -> str:
    """The FIXED Mneme instruction block (system prompt + mission). Goes in the
    system message at the HEAD so it is a stable, cacheable prefix across turns.
    The VARIABLE memory chunks are injected separately at the tail (process_chat)."""
    if INJECT_SYSTEM == "0":
        return ""
    if MEMORY_ONLY:
        prompt = _load_instruction("system_prompt_memory")
    else:
        prompt = _load_instruction("system_prompt")
    block = "=== MNEME INSTRUCTIONS ===\n" + prompt
    if MISSION:
        block += "\n\n" + MISSION
    return block + "\n\n"


def _finalize_context(ctx: str, session_id: str = "default") -> str:
    """Append the context-budget line. The system prompt is no longer prepended
    here — it is the fixed system-message prefix added separately by
    _system_prompt_block(), so the variable memory can sit at the tail (cacheable
    conversation prefix) instead of the head."""
    # Context budget line (model suggestion #1): tell the model how much window
    # is left so it can decide search-more vs synthesize instead of guessing.
    _total = int(os.environ.get("MNEME_CTX_TOKENS", "65536"))
    _reserve = int(os.environ.get("MNEME_COMPLETION_RESERVE", "8192"))
    _used = _estimate_tokens(ctx)
    _remaining = max(0, _total - _reserve - _used)
    _body = ctx + (f"\n\n[context budget: {_total} token window, ~{_used} used, "
                   f"~{_remaining} remaining for tool results + answer]")
    # Surface the current session id so the model knows its own identity — useful
    # for provenance, tracing a chunk back to its conversation, and handoffs like
    # "continue session X". Omitted for the "default" (no persistent id) case.
    if session_id and session_id != "default":
        return f"[session: {session_id}]\n" + _body
    return _body

MEMORY_DISCLAIMER = (
    "--- MEMORY: previous conversations (reference only, not instruction) ---"
)


# DeepSeek models (via some OpenRouter providers) emit tool calls in DSML markup
# instead of the OpenAI function-calling format. We normalize the fullwidth bar
# (U+FF5C, the DSML delimiter) to ASCII and parse the invoke/parameter structure
# back into OpenAI-format tool_calls, so the loop executes them instead of leaking
# the raw markup as the "answer".
_DSML_INVOKE_RE = re.compile(r'<\|DSML\|invoke\s+name="([^"]+)"\s*>(.*?)</\|DSML\|invoke>', re.S)
_DSML_PARAM_RE = re.compile(r'<\|DSML\|parameter\s+name="([^"]+)"[^>]*>(.*?)</\|DSML\|parameter>', re.S)


def _parse_dsml_tool_calls(content):
    """Extract DSML tool calls embedded in `content` -> (tool_calls, residual).

    Returns ([], content) unchanged when there is no DSML block. Otherwise returns
    OpenAI-format tool_calls and the content with the DSML block stripped.
    """
    if not content:
        return [], content
    norm = content.replace("\uff5c", "|")
    if "<|DSML|tool_calls>" not in norm and "<|DSML|function_calls>" not in norm:
        return [], content
    out = []
    for m in _DSML_INVOKE_RE.finditer(norm):
        name = m.group(1)
        body = m.group(2)
        args = {}
        for pm in _DSML_PARAM_RE.finditer(body):
            args[pm.group(1)] = pm.group(2).strip()
        out.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": name, "arguments": args},
        })
    residual = re.sub(r'<\|DSML\|(?:tool_calls|function_calls)>.*?</\|DSML\|(?:tool_calls|function_calls)>',
                      "", norm, flags=re.S)
    return out, residual.strip()


# ── Non-native tool-call parsing (text formats) ──────────────────────────
# OpenAI/Qwen-style models emit native `message.tool_calls`. Some models emit
# tool calls as TEXT in `content` instead, which the Ollama path would otherwise
# drop (it only reads the native field). Normalize the common text formats into
# OpenAI-format tool_calls:
#   - DeepSeek (some providers): <|DSML|invoke ...>         (handled above)
#   - Gemma 3/4: ```tool_code ...``` blocks, Python-call syntax
#   - various: a fenced ```json object with {"name", "arguments"}
_GEM_TC_RE = re.compile(r'```tool_code\s*(.*?)\s*```', re.S)


def _ast_value(node):
    """Best-effort AST literal -> Python value; fall back to source text."""
    try:
        return ast.literal_eval(node)
    except Exception:
        try:
            return ast.unparse(node)
        except Exception:
            return ""


def _parse_tool_code_block(code):
    """Parse one `func(kw=val, ...)` Python call into (name, args) or (None, None)."""
    code = (code or "").strip()
    if not code:
        return None, None
    try:
        tree = ast.parse(code, mode="eval")
    except Exception:
        return None, None
    node = tree.body
    if isinstance(node, ast.Expr):
        node = node.value
    if not isinstance(node, ast.Call):
        return None, None
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        try:
            name = ast.unparse(node.func)
        except Exception:
            return None, None
    else:
        return None, None
    args = {}
    for kw in node.keywords:
        if kw.arg:
            args[kw.arg] = _ast_value(kw.value)
    for i, pos in enumerate(node.args):
        args[f"arg{i}"] = _ast_value(pos)
    return name, args


def _parse_gemma_tool_calls(content):
    """Gemma 3/4 emit ```tool_code``` blocks with Python-call syntax.
    Returns (tool_calls, residual)."""
    if not content:
        return [], content
    out = []
    for m in _GEM_TC_RE.finditer(content):
        name, args = _parse_tool_code_block(m.group(1))
        if name:
            out.append({"id": f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {"name": name, "arguments": args}})
    if not out:
        return [], content
    residual = _GEM_TC_RE.sub("", content).strip()
    return out, residual


def _split_json_objects(text):
    """Yield each top-level `{...}` JSON object found in `text`, brace-balanced.

    Qwen 2.5 and similar non-native tool-callers emit SEVERAL objects inside one
    fence:

        ```json
        {"name": "search_memory", "arguments": {...}}

        {"name": "list_tools", "arguments": {}}
        ```

    A single json.loads() over that raises "Extra data", which is how such calls
    were silently dropped. This walks the text with a depth counter (ignoring
    braces inside quoted strings, including escaped quotes) so every object is
    yielded independently. Objects that fail to decode are skipped, not fatal —
    one malformed blob shouldn't discard the valid calls around it.
    """
    depth = 0
    start = None
    in_str = False
    quote = ""
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ("\"", "'"):
            in_str = True
            quote = ch
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    blob = text[start:i + 1]
                    try:
                        yield json.loads(blob)
                    except Exception:
                        pass
                    start = None


def _obj_to_tool_call(obj):
    """A dict carrying name+arguments/args -> an OpenAI-format tool_call, else None.

    Requires an explicit `arguments`/`args` key. A bare {"name": ...} with no
    arguments key is NOT a tool call — that shape appears in ordinary prose
    (e.g. 'the field {"name": "data"}'), and treating it as a call would hijack
    normal conversation into a tool loop.
    """
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if not isinstance(name, str) or not name:
        return None
    if "arguments" not in obj and "args" not in obj:
        return None
    arguments = obj.get("arguments")
    if arguments is None:
        arguments = obj.get("args")
    if not isinstance(arguments, dict):
        arguments = {}
    return {"id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _parse_json_tool_calls(content):
    """A fenced ```json block carrying "name" + "arguments" is a tool call.

    Tolerates MULTIPLE objects in one fence (Qwen 2.5's habit), a bare unfenced
    object, and undecodable trailing text inside the fence. Returns
    (tool_calls, residual)."""
    if not content:
        return [], content
    out = []
    spans = []
    for m in re.finditer(r'```(?:json)?\s*(.*?)\s*```', content, re.S):
        found = False
        for obj in _split_json_objects(m.group(1).strip()):
            tc = _obj_to_tool_call(obj)
            if tc:
                out.append(tc)
                found = True
        if found:
            spans.append(m.span())
    if not out:
        # No fenced block produced a call — the model may have emitted a bare
        # object with no ```json fence at all (also common for Qwen 2.5).
        if "```" not in content:
            for obj in _split_json_objects(content):
                tc = _obj_to_tool_call(obj)
                if tc:
                    out.append(tc)
    if not out:
        return [], content
    residual = content
    for start, end in spans:
        residual = residual[:start] + residual[end:]
    return out, residual.strip()


# Gemma 3/4 (via Ollama) emit tool calls as XML: <tool_call>{json}</tool_call>
# where the body is {"name":..., "arguments":...}. Ollama does NOT fold these
# into native message.tool_calls, so we parse them here.
_GEM_XML_TC_RE = re.compile(r'<(tool_call|function_call)\b[^>]*>(.*?)</\1>', re.S | re.I)


def _parse_xml_tool_calls(content):
    """Gemma <tool_call>{json}</tool_call> XML tool calls -> (tool_calls, residual).
    Body is normally a JSON object {"name", "arguments"}; also tolerates a
    name="..." attribute on the opening tag."""
    if not content:
        return [], content
    out = []
    for m in _GEM_XML_TC_RE.finditer(content):
        open_tag = m.group(0).split(">", 1)[0] + ">"
        body = m.group(2).strip()
        name = None
        arguments = {}
        am = re.search(r'\bname\s*=\s*"([^"]+)"', open_tag)
        if am:
            name = am.group(1)
        if body:
            try:
                obj = json.loads(body)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                name = obj.get("name") or name
                arguments = obj.get("arguments") or obj.get("args") or {}
                if not isinstance(arguments, dict):
                    arguments = {}
        if name:
            out.append({"id": f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {"name": name, "arguments": arguments}})
    if not out:
        return [], content
    residual = _GEM_XML_TC_RE.sub("", content).strip()
    return out, residual


def _parse_text_tool_calls(content):
    """Normalize non-native text tool calls -> OpenAI-format tool_calls.
    Tries DSML, then Gemma <tool_call> XML, then Gemma ```tool_code```, then
    fenced JSON. Returns (tool_calls, residual)."""
    if not content:
        return [], content
    for parser in (_parse_dsml_tool_calls, _parse_xml_tool_calls, _parse_gemma_tool_calls, _parse_json_tool_calls):
        tcs, residual = parser(content)
        if tcs:
            return tcs, residual
    return [], content


def _serialize_tool_call_arguments(msgs: list) -> list:
    """Return a copy of msgs with assistant tool_call arguments re-encoded as JSON
    strings (OpenAI spec). _query_openrouter parses the model's string arguments
    into dicts on the way in; when those messages are re-sent in a follow-up turn,
    strict providers (Stealth/Ox Alpha) reject dict arguments with a 400 "Provider
    returned error", so we re-serialize them before every outgoing request."""
    out = []
    for m in msgs:
        m = dict(m)
        tcs = m.get("tool_calls")
        if tcs:
            ntc = []
            for tc in tcs:
                tc = dict(tc)
                fn = dict(tc.get("function") or {})
                args = fn.get("arguments")
                if isinstance(args, dict):
                    fn["arguments"] = json.dumps(args)
                tc["function"] = fn
                ntc.append(tc)
            m["tool_calls"] = ntc
        out.append(m)
    return out


def _truncate_tool_result(content: str) -> str:
    """Cap a tool result at MAX_TOOL_FORWARD chars with a head+tail window.

    A `bash cat file.py` returns the whole file (tens of KB); forwarding that
    verbatim bloats the conversation until OpenRouter times out on re-query. Bound
    what the model sees, point at search_memory for the rest (full text is still
    staged to memory). This is the Hermes-style bounded-output pattern — never a
    summary, always a window + retrieval pointer."""
    content = content or ""
    if len(content) <= MAX_TOOL_FORWARD:
        return content
    head_len = MAX_TOOL_FORWARD * 3 // 4
    tail_len = MAX_TOOL_FORWARD - head_len
    return (content[:head_len]
            + f"\n\n[... content truncated: {len(content)} chars total, showing first {head_len} + last {tail_len} chars. Full text is in memory — use search_memory to retrieve the sections you need.]\n\n"
            + content[-tail_len:])


def _reasoning_stale_floor(model) -> Optional[int]:
    """Stale-timeout floor (seconds) for a known reasoning model, else None.

    Slug-anchored match like Hermes' reasoning_timeouts.py: strip any
    aggregator prefix (everything through the last "/"), lowercase, then match
    table entries longest-first as a prefix at a slug boundary — so "o3-mini"
    beats "o3". Only ever used as a FLOOR (callers apply max(), never min()).
    Returns None for non-reasoning models, and for GLM which is intentionally
    absent from _REASONING_STALE_FLOORS."""
    if not model or not isinstance(model, str):
        return None
    name = model.strip().lower()
    if not name:
        return None
    if "/" in name:
        name = name.rsplit("/", 1)[1]
    for slug, floor in sorted(_REASONING_STALE_FLOORS, key=lambda kv: -len(kv[0])):
        if name == slug or name.startswith(slug):
            # startswith: "deepseek-v4-pro" starts with "deepseek-v4"; the table
            # is ordered longest-first so the most specific entry wins.
            return floor
    return None


def _model_mandates_reasoning(model) -> bool:
    """True if the model's endpoint MANDATES reasoning (cannot be disabled).

    Same slug-anchored match as _reasoning_stale_floor. Used to avoid sending
    `reasoning.enabled:false` to models whose provider 400s on it (GLM)."""
    if not model or not isinstance(model, str):
        return False
    name = model.strip().lower()
    if not name:
        return False
    if "/" in name:
        name = name.rsplit("/", 1)[1]
    for slug in sorted(_MANDATORY_REASONING_MODELS, key=len, reverse=True):
        if name == slug or name.startswith(slug):
            return True
    return False


def _parse_retry_after(headers) -> Optional[float]:
    """Retry-After seconds from a provider response, else None.

    Accepts `retry-after` (seconds or HTTP-date) and `retry-after-ms`
    (milliseconds — OpenRouter uses this on 429s). Capped at RETRY_AFTER_CAP
    so a hostile/buggy value can't stall a turn for hours (Hermes: 600s cap)."""
    if not headers:
        return None
    try:
        raw = headers.get("retry-after-ms") or headers.get("Retry-After-ms")
        if raw is not None:
            ms = float(raw)
            if ms >= 0:
                return min(ms / 1000.0, RETRY_AFTER_CAP)
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is None:
            return None
        try:
            secs = float(raw)
            if secs >= 0:
                return min(secs, RETRY_AFTER_CAP)
        except (TypeError, ValueError):
            import email.utils as _eu
            dt = _eu.parsedate_to_datetime(raw)
            if dt is not None:
                import datetime as _dt
                delta = (dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
                if delta > 0:
                    return min(delta, RETRY_AFTER_CAP)
    except Exception:
        pass
    return None


def _provider_failure_retryable(result: dict) -> bool:
    """Structured retryability for a query_model result (Hermes/OpenCode pattern).

    - "timeout" → retryable. Covers both no-first-token (provider hang, zero
      tokens) and mid-stream stall (partial answer, no finish_reason seen) —
      both are INCOMPLETE responses. Partial content is preserved by the
      retry loop's keep-best logic, so retrying can never return less.
    - "error" → classify by status/error_type:
        401/402/403, auth/billing/credit → NOT retryable (same key fails again)
        429 / rate limit → retryable (with Retry-After when present)
        400-499 otherwise (context overflow, bad request, content policy) →
          NOT retryable — deterministic per-request failures
        5xx / unknown status / transport → retryable
    - anything else ("stop", "length", "tool_calls", "cancelled") → not a failure."""
    if not isinstance(result, dict):
        return False
    dr = result.get("done_reason", "")
    if dr == "timeout":
        return True
    if dr != "error":
        return False
    status = result.get("status_code")
    etype = (result.get("error_type") or "").lower()
    if status in (401, 402, 403) or "auth" in etype or "billing" in etype or "credit" in etype:
        return False
    if status == 429 or status == 408 or "rate" in etype or "429" in etype:
        return True
    if status is None:
        return True  # mid-stream SSE error / unmapped transport error — retry
    if 500 <= int(status) <= 599:
        return True
    if 200 <= int(status) < 400:
        return True  # 2xx/3xx carrying an error = malformed provider response — retry
    if "context" in etype or "length" in etype:
        return False  # context overflow — fix is compaction, not retry
    return False  # other 4xx: deterministic


def _retry_backoff_delay(attempt: int, retry_after=None) -> float:
    """Jittered exponential backoff for retry attempt N (1-based), or the
    provider's Retry-After when present. 2s → 4s → 8s ... capped, plus
    uniform jitter up to half the delay (Hermes jittered_backoff shape —
    decorrelates concurrent retries hitting the same provider)."""
    if retry_after is not None and retry_after > 0:
        return float(min(retry_after, RETRY_AFTER_CAP))
    base = max(0.0, RETRY_BACKOFF_BASE)
    try:
        delay = min(base * (2 ** max(0, attempt - 1)), RETRY_BACKOFF_CAP)
    except OverflowError:
        delay = RETRY_BACKOFF_CAP
    import random as _random
    return delay + _random.uniform(0, 0.5 * delay)


def _result_score(result) -> int:
    """Rank a result by how much usable output it carries — the retry loop
    keeps the best-scoring attempt so a failed retry chain never returns LESS
    than an earlier partial (report defect #4: mid-stream partials discarded)."""
    if not isinstance(result, dict):
        return -1
    return (len(result.get("content") or "")
            + 50 * len(result.get("tool_calls") or []))


def _query_openrouter(msgs, opts, tools=None, format_schema=None,
                      max_tokens=-1, timeout=None, model=None, no_reasoning=False) -> dict:
    """Send to OpenRouter's OpenAI-compatible /chat/completions. Returns the same
    dict shape as the Ollama path: {content, thinking, tool_calls, eval_count,
    done_reason}. OpenRouter normalizes thinking models' reasoning into
    message.reasoning; tool-call arguments arrive as JSON strings and are
    json.loads'd back to dicts to match the Ollama path."""
    _model = model or MODEL
    msgs = _serialize_tool_call_arguments(msgs)
    if timeout is None: timeout = CHAT_TIMEOUT
    payload = {
        "model": _model,
        "stream": _OR_STREAM,
        "messages": msgs,
        "temperature": opts.get("temperature"),
        "top_p": opts.get("top_p"),
    }
    # Reasoning is OFF by default — a reasoning model (e.g. Qwen3.6) can
    # runaway-think on a trivial ask. Opt back in with either:
    #   MNEME_REASONING_ENABLED=1/true/on   -> binary on/off thinking (Qwen3.6-style)
    #   MNEME_REASONING_EFFORT=low|high|max -> effort models (deepseek, Ox Alpha)
    # When reasoning is ON, send a bounded thinking budget (reasoning.max_tokens,
    # default max_tokens/2 — OpenCode's fitThinkingBudget rule: thinking counts
    # against the output limit, so a budget near it leaves the answer/tool call
    # no room). The old code sent NO reasoning key when enabled, handing
    # mandatory-reasoning models (GLM-5.3) an unbounded thinking budget —
    # measured 28,771 reasoning tokens / 75s per trivial ask. A Qwen-specific
    # "budget is a goal not a cap" quirk was previously generalized to every
    # reasoning model here; the budget is opt-out via MNEME_REASONING_BUDGET=0.
    _reasoning = {}
    _mandatory = _model_mandates_reasoning(_model)
    _reffort = os.environ.get("MNEME_REASONING_EFFORT", "")
    _reasoning_on = os.environ.get("MNEME_REASONING_ENABLED", "").strip().lower() in ("1", "true", "on", "yes", "enabled")
    if _reffort and not no_reasoning:
        _reasoning["effort"] = _reffort
        _reasoning_on = True
    if (no_reasoning or not _reasoning_on) and not _mandatory:
        # A non-mandatory model can be told to skip thinking entirely.
        _reasoning["enabled"] = False
    _mc = (CONFIG_DATA.get("models") or {}).get(_model) or {}
    _mt = max_tokens if (max_tokens and max_tokens > 0) else None
    if _mt is None:
        _mt = _mc.get("max_tokens") or int(os.environ.get("MNEME_MAX_TOKENS", "0") or 0)
    # Always bound output: with no cap the OpenAI-compatible path hands the
    # model (and its thinking phase) an unlimited budget. OpenCode always
    # sends max_tokens (DEFAULT_MAX_TOKENS = 32k); adopt the same default.
    if not _mt or int(_mt) <= 0:
        _mt = OR_DEFAULT_MAX_TOKENS
    if int(_mt) > 0:
        payload["max_tokens"] = int(_mt)
        # Bounded thinking budget for reasoning models (see comment above).
        # Precedence: per-model config `models.<model>.reasoning_budget` > env
        # MNEME_REASONING_BUDGET > "auto" = max_tokens/2. "0"/"off" disables
        # (legacy behaviour). Always capped at max_tokens/2. Read from CONFIG_DATA
        # at request time, so it hot-reloads with the rest of the `models:` block.
        if _reasoning_on and not no_reasoning:
            _rb = _mc.get("reasoning_budget")
            _bud = str(OR_REASONING_BUDGET if _rb is None else _rb).strip().lower()
            if _bud not in ("0", "off", "no", "none", "disabled"):
                try:
                    _budget = int(_bud) if _bud not in ("auto", "") else int(_mt) // 2
                except ValueError:
                    _budget = int(_mt) // 2
                _budget = min(_budget, int(_mt) // 2)
                if _budget > 0:
                    _reasoning["max_tokens"] = _budget
        elif _mandatory and (no_reasoning or not _reasoning_on):
            # Mandatory-reasoning model asked to skip thinking — can't send
            # `enabled:false` (the endpoint 400s), so bound it tightly instead.
            _reasoning["max_tokens"] = min(_MANDATORY_REASONING_MIN_BUDGET, int(_mt) // 2)
    if _reasoning:
        payload["reasoning"] = _reasoning
    if tools:
        payload["tools"] = tools
    if format_schema:
        payload["response_format"] = {"type": "json_schema", "json_schema": format_schema}
    # OpenRouter-specific reliability options (config-driven; only ever added to
    # OpenRouter requests, so a plain OpenAI-compatible backend is unaffected).
    # - `models` array: model fallbacks, walked in order if every provider for the
    #   primary model fails (recovers a whole-model outage / cold-start no-content).
    # - `provider` prefs: ignore/order/only/allow_fallbacks/preferred_max_latency
    #   to steer routing away from known-bad or slow endpoints.
    # BOTH are chat-model-specific: they must NOT leak onto auxiliary models (the
    # label judge, etc.) — pinning those to the chat model's provider (e.g.
    # order:[Z.AI] + allow_fallbacks:false) 404s a label model the pinned
    # provider doesn't host, and its fallbacks get stripped, leaving no endpoints.
    if _OR_FALLBACK_MODELS and _model == MODEL:
        payload["models"] = [_model] + [str(m) for m in _OR_FALLBACK_MODELS]
    if _OR_PROVIDER_PREF and _model == MODEL:
        payload["provider"] = _OR_PROVIDER_PREF
    if tools:
        _tnames = [t.get("function", {}).get("name", "?") for t in tools]
        print(f"  [TOOLS] forwarding {len(tools)} tools ({len(json.dumps(tools))}B): {_tnames}", flush=True)
    try:
        _sum = " | ".join(f"{m.get('role')}:{len(str(m.get('content','')))}c{'+tc' if m.get('tool_calls') else ''}" for m in msgs)
        print(f"  [PAYLOAD] {_sum}", flush=True)
        with open(f"/tmp/proxy_payload_{int(time.time())}.json", "w") as _f:
            json.dump(payload, _f)
    except Exception:
        pass

    # Two-phase stream timeout:
    #   BEFORE the first token — FIRST_TOKEN_TIMEOUT (45s, raised by the floor
    #       for models with a LONG hidden-thinking block: o-series, deepseek-r1,
    #       nemotron — not GLM). Fails fast on a hung/cold provider.
    #   AFTER the first token — STALE_CHUNK_TIMEOUT (20s) between chunks. A
    #       reasoning model streams continuously, so a mid-stream deadlock is
    #       caught in 20s instead of 180s.
    _floor = _reasoning_stale_floor(_model)
    _stale = max(FIRST_TOKEN_TIMEOUT, _floor) if _floor else FIRST_TOKEN_TIMEOUT
    # Non-stream buffers the whole body server-side, so a reasoning model's
    # full thinking+answer legitimately exceeds the 60s fast-fail budget —
    # apply the floor there too.
    _ns_timeout = max(NON_STREAM_TIMEOUT, _floor) if _floor else NON_STREAM_TIMEOUT

    if not _OR_STREAM:
        # Non-streaming path: OpenRouter buffers the full response server-side, so
        # it CAN transparently fail over to a backup provider if the primary stalls
        # mid-generation (streaming commits the first token and disables failover).
        # Live data: StreamLake sometimes HANGS (no bytes for 150s+) and OpenRouter
        # does NOT fail over within our deadline — but a fresh retry recovers in
        # ~8s. So a moderate read timeout (~60s) outlasts a normal answer (~20s) yet
        # fails fast on a hang, letting OUR retry recover instead of burning 150s.
        try:
            r = requests.post(f"{OR_BASE_URL}/chat/completions", headers=_or_headers(),
                              json=payload, timeout=(CONNECT_TIMEOUT, _ns_timeout))
        except requests.exceptions.RequestException as e:
            print(f"  [GRIND-GUARD] OpenRouter request failed ({type(e).__name__}: {e}) — aborting", flush=True)
            return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0,
                    "done_reason": "timeout"}
        try:
            obj = r.json()
        except ValueError:
            print(f"  [GRIND-GUARD] OpenRouter non-JSON response (status {r.status_code}) — aborting", flush=True)
            return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0,
                    "done_reason": "error", "error_type": f"http_{r.status_code}",
                    "status_code": r.status_code, "retry_after": _parse_retry_after(r.headers)}
        if obj.get("error"):
            _err = obj["error"]
            _meta = _err.get("metadata") or {}
            _etype = _meta.get("error_type", "unmapped")
            print(f"  [GRIND-GUARD] OpenRouter error ({_etype} {_err.get('code', '')}: "
                  f"{str(_err.get('message', ''))[:100]}) — retryable", flush=True)
            return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0,
                    "done_reason": "error", "error_type": _etype, "provider": obj.get("provider", "?"),
                    "status_code": r.status_code, "retry_after": _parse_retry_after(r.headers)}
        _choices = obj.get("choices") or []
        _msg = (_choices[0].get("message") or {}) if _choices else {}
        _content = _msg.get("content") or ""
        _thinking = _msg.get("reasoning") or ""
        _finish = _choices[0].get("finish_reason") if _choices else None
        _provider = obj.get("provider", "?")
        _completion_tokens = (obj.get("usage") or {}).get("completion_tokens", 0)
        _tool_calls = []
        for _tc in (_msg.get("tool_calls") or []):
            _fn = _tc.get("function") or {}
            _args_raw = _fn.get("arguments", "")
            try:
                _args = json.loads(_args_raw) if _args_raw else {}
            except Exception:
                _args = {}
            _tool_calls.append({
                "id": _tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": _tc.get("type", "function"),
                "function": {"name": _fn.get("name", ""), "arguments": _args},
            })
        if not _tool_calls and _content:
            # DeepSeek models can emit DSML tool-call markup as plain content instead
            # of OpenAI-format tool_calls. Parse it so the loop executes the calls.
            _dsml_tcs, _dsml_residual = _parse_dsml_tool_calls(_content)
            if _dsml_tcs:
                _tool_calls = _dsml_tcs
                _content = _dsml_residual
                _finish = "tool_calls"
        if not _content and _thinking and not _tool_calls:
            _content = _thinking
        _done = {"stop": "stop", "length": "length", "tool_calls": "tool_calls"}.get(_finish, _finish) \
            if _finish else ("tool_calls" if _tool_calls else "stop")
        return {"content": _content, "thinking": _thinking, "tool_calls": _tool_calls,
                "eval_count": _completion_tokens, "done_reason": _done, "provider": _provider}

    # Two-phase timeout: short for the first token (detect a hung provider fast),
    # then the full `timeout` for the rest (steady generation). A single
    # non-stream request can't do this — the body is buffered server-side until
    # the provider finishes, so a hung provider burns the entire read timeout.
    try:
        r = requests.post(f"{OR_BASE_URL}/chat/completions", headers=_or_headers(),
                          json=payload, stream=True, timeout=(CONNECT_TIMEOUT, _stale))
        # OpenRouter streams text/event-stream with no charset, so requests falls
        # back to ISO-8859-1 for iter_lines(decode_unicode=True) and mojibakes
        # every multi-byte UTF-8 char (— -> â, ° -> Â°). Force UTF-8 before reading.
        r.encoding = "utf-8"
    except requests.exceptions.RequestException as e:
        print(f"  [GRIND-GUARD] OpenRouter request failed ({type(e).__name__}: {e}) — aborting", flush=True)
        return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "timeout"}

    # A non-200 (429 rate-limit, 5xx, auth) returns a JSON error body, NOT an SSE
    # stream. Without this check the loop below treats the error body as a stream,
    # skips every line (none start with "data:"), and returns an empty answer the
    # caller reads as "the model said nothing". Fail fast as a clean error so the
    # caller's retry / fallback logic can act on it instead of silently wedging.
    if r.status_code != 200:
        _err_body = ""
        try:
            _err_body = r.text[:300]
        except Exception:
            pass
        print(f"  [GRIND-GUARD] OpenRouter HTTP {r.status_code} (non-200) — aborting: {_err_body[:200]}", flush=True)
        return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0,
                "done_reason": "error", "error_type": f"http_{r.status_code}",
                "status_code": r.status_code, "retry_after": _parse_retry_after(r.headers)}

    content_parts = []
    reasoning_parts = []
    tc_slots = {}          # index -> accumulator for streamed tool-call deltas
    finish_reason = None
    provider = "?"
    completion_tokens = 0
    got_first = False
    _stalled = False       # set when the stream dies mid-response — partial kept
    _saw_done = False      # set on the [DONE] sentinel — a proper terminal event

    def _set_sock_timeout(t):
        # `requests(stream=True, timeout=(...))` covers the header read only — the
        # streaming BODY read via iter_lines() does not reliably inherit it, so a
        # provider that stalls before the first token blocks forever. Pin the raw
        # socket timeout explicitly before reading the body, and loosen it after
        # the first token. Best effort: if the handle can't be reached, the
        # RequestException handler below still bounds a dead connection.
        try:
            r.raw._fp.fp.raw._sock.settimeout(t)
        except Exception:
            try:
                r.raw._fp.fp.raw.settimeout(t)
            except Exception:
                pass

    # Pin the raw socket timeout before reading the body. Start with the
    # first-token budget (catch a hung/cold provider), then tighten to the
    # inter-chunk budget once the first byte lands (catch a mid-stream deadlock).
    _set_sock_timeout(_stale)

    try:
        for raw in r.iter_lines(decode_unicode=True):
            if _turn_cancel_event().is_set():
                print("  [CANCEL] user stopped the turn — aborting OpenRouter stream", flush=True)
                finish_reason = "cancelled"
                break
            if not raw:
                continue
            raw = raw.strip()
            if not raw.startswith("data:"):
                continue
            if not got_first:
                got_first = True
                _set_sock_timeout(STALE_CHUNK_TIMEOUT)
            data = raw[5:].strip()
            if data == "[DONE]":
                _saw_done = True
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("provider"):
                provider = obj["provider"]
            if obj.get("usage"):
                completion_tokens = obj["usage"].get("completion_tokens", completion_tokens)
            # Mid-stream provider error: OpenRouter emits an SSE event with a
            # top-level `error` + choices[0].finish_reason:"error" when the provider
            # dies mid-generation (overload/disconnect/timeout). Fail FAST and mark
            # it retryable instead of sitting in the read-timeout for 60s.
            if obj.get("error"):
                _err = obj["error"]
                _meta = _err.get("metadata") or {}
                _etype = _meta.get("error_type", "unmapped")
                print(f"  [GRIND-GUARD] mid-stream provider error ({_etype} {_err.get('code', '')}: "
                      f"{str(_err.get('message', ''))[:100]}) — fail-fast, retryable", flush=True)
                return {"content": "".join(content_parts), "thinking": "".join(reasoning_parts),
                        "tool_calls": [], "eval_count": 0, "done_reason": "error",
                        "error_type": _etype, "provider": provider}
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
                _emit_token("content", delta["content"])
            if delta.get("reasoning"):
                reasoning_parts.append(delta["reasoning"])
                _emit_token("reasoning", delta["reasoning"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tc_slots.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if tc.get("type"):
                    slot["type"] = tc["type"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            fr = choices[0].get("finish_reason")
            if fr:
                finish_reason = fr
    except requests.exceptions.RequestException as e:
        # No first token -> hung provider. Mid-stream stall -> INCOMPLETE answer.
        # If the user cancelled the turn, honour that instead of a stall.
        if _turn_cancel_event().is_set():
            print("  [CANCEL] user stopped the turn — aborting OpenRouter stream", flush=True)
            return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0,
                    "done_reason": "cancelled"}
        tag = "no first token" if not got_first else "mid-response stall"
        print(f"  [GRIND-GUARD] OpenRouter stream aborted ({tag}) ({type(e).__name__}: {e}) — aborting", flush=True)
        if not got_first:
            # Nothing was received — a clean retryable signal for the retry loop.
            return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "timeout"}
        # Mid-response stall: tokens WERE received, so this is an incomplete
        # answer, not an absence of one. Fall through and return the partial
        # (tool calls assembled, thinking kept) with done_reason="timeout" —
        # the retry loop re-queries AND keeps the best-scoring attempt, so a
        # failed retry chain returns this partial instead of a silent empty.
        # (The old handler returned content:"" here, discarding everything —
        # the exact "empty response" users saw mid-stream. Ollama path has
        # always preserved partials; this matches it.)
        _stalled = True
    finally:
        try:
            r.close()
        except Exception:
            pass

    content = "".join(content_parts)
    thinking = "".join(reasoning_parts)
    tool_calls = []
    for idx in sorted(tc_slots):
        slot = tc_slots[idx]
        args_raw = slot["function"].get("arguments", "")
        try:
            args = json.loads(args_raw) if args_raw else {}
        except Exception:
            args = {}
        tool_calls.append({
            "id": slot.get("id") or f"call_{uuid.uuid4().hex[:24]}",
            "type": slot.get("type", "function"),
            "function": {"name": slot["function"].get("name", ""), "arguments": args},
        })
    # Reasoning models sometimes leave content empty and put the answer in
    # reasoning. Fall back (unless there are tool calls, which must stay calls).
    if not content and thinking and not tool_calls:
        content = thinking
    if _stalled or (finish_reason is None and not _saw_done):
        # INCOMPLETE response, two shapes (OpenCode's requireTerminalEvent):
        #  - _stalled: the stream raised (timeout/ConnectionError) mid-response.
        #  - clean EOF with NEITHER a finish_reason chunk NOR the [DONE]
        #    sentinel — the provider died and closed the socket politely.
        # In both, the partial never saw a terminal event, so mark it
        # "timeout" (the retryable signal) rather than pretending it stopped
        # cleanly — the retry loop re-queries and keeps whichever attempt
        # produced more.
        done_reason = "timeout"
    elif finish_reason:
        done_reason = {"stop": "stop", "length": "length", "tool_calls": "tool_calls"}.get(finish_reason, finish_reason)
    else:
        done_reason = "tool_calls" if tool_calls else "stop"
    return {
        "content": content,
        "thinking": thinking,
        "tool_calls": tool_calls,
        "eval_count": completion_tokens,
        "done_reason": done_reason,
        "provider": provider,
    }


def _query_model_impl(messages: list, system: str = None, temperature: float = None,
                max_tokens: int = None, tools: list = None, options: dict = None,
                timeout: Optional[int] = None, format_schema=None, model: str = None,
                backend: str = None, no_reasoning: bool = False) -> dict:
    """Send to Ollama, return {content, thinking, eval_count, done_reason}.
    Pass options dict for top_p, top_k, mirostat, etc. `timeout` controls the
    Ollama read timeout — raise it for long generations (novelty thinking).
    `format_schema` threads an Ollama structured-output JSON schema into the
    payload's `format` field; when set the caller should json.loads the reply.
    `model` overrides the backend model for a single call (e.g. the small label
    model for cheap judge/label calls). `backend` overrides the transport for a
    single call ("openai"/"openrouter" vs "ollama") — used to keep auxiliary
    calls (judge, label) on local Ollama while the main model runs on OpenRouter.
    """
    _model = model or MODEL
    # Per-call backend override (default: global MNEME_BACKEND). Auxiliary calls
    # (judge/label) pass backend="ollama" so they never follow the main model onto
    # OpenRouter — they run on the local pod models.
    use_openai = (backend if backend is not None else MNEME_BACKEND) in ("openai", "openrouter")
    if temperature is None: temperature = OLLAMA_TEMP
    if max_tokens is None: max_tokens = -1  # let Ollama decide
    if timeout is None: timeout = OLLAMA_CHAT_TIMEOUT if not use_openai else CHAT_TIMEOUT  # backend-aware fail-fast (was 600s default)
    
    # Trim to last MAX_HISTORY_MESSAGES, but always keep the system prompt (first message if system role)
    trimmed = list(messages)
    if len(trimmed) > MAX_HISTORY_MESSAGES:
        first = trimmed[0] if trimmed[0].get("role") == "system" else None
        rest = [m for m in trimmed if m.get("role") != "system"] if first else trimmed
        trimmed = rest[-(MAX_HISTORY_MESSAGES - (1 if first else 0)):]
        if first:
            trimmed.insert(0, first)
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in trimmed:
            # Preserve the RAW content (str OR OpenAI multimodal array). Flattening
            # via _extract_text here is what silently turned images into "[IMAGE: url]"
            # text — the vision model never saw the image. _extract_text is still used
            # everywhere that only needs TEXT (retrieval/staging/labeling); only the
            # backend-facing message keeps the original content.
            new_m = {"role": m["role"], "content": m.get("content", "")}
            # Preserve tool_calls on assistant messages so the model can associate
            # a follow-up tool result with its call (critical for multi-turn tool
            # use in Pi). Dropping this is what broke tool calls → "None".
            if m.get("role") == "assistant" and m.get("tool_calls"):
                new_m["tool_calls"] = m["tool_calls"]
            # Preserve tool_call_id (and optional name) on tool messages — the
            # OpenAI-compatible format (OpenRouter) requires a tool result to carry
            # the id of the assistant tool_call it answers. Dropping it orphaned
            # tool results and confused the model into malformed follow-up calls.
            if m.get("role") == "tool":
                if m.get("tool_call_id"):
                    new_m["tool_call_id"] = m["tool_call_id"]
                if m.get("name"):
                    new_m["name"] = m["name"]
            msgs.append(new_m)
    
    # Auto-chunk oversized messages before they bloat conversation history
    msgs = _chunk_large_messages(msgs)
    
    # Sampling defaults come from env/config, then per-model overrides from the
    # config `models:` block (keyed by the exact model name). Explicit per-call
    # `options` still win.
    _model_cfg = (CONFIG_DATA.get("models") or {}).get(_model) or {}
    # Output + context knobs. num_predict caps the reply; num_ctx sets the KV
    # window (a reasoning/vision model like Qwen3.8 advertises 256K but may only
    # fit a fraction in the pod's VRAM, so cap it per-model with `num_ctx`).
    _num_predict = max_tokens if (max_tokens and max_tokens > 0) else int(os.environ.get("MNEME_MAX_TOKENS", "65536"))
    _num_ctx = int(os.environ.get("MNEME_CTX_TOKENS", "65536"))
    if _model_cfg.get("num_ctx") is not None:
        _num_ctx = int(_model_cfg["num_ctx"])
    if _model_cfg.get("num_predict") is not None:
        _num_predict = int(_model_cfg["num_predict"])
    opts = {
        "temperature": temperature if temperature is not None else float(os.environ.get("MNEME_TEMPERATURE", "0.3")),
        "top_p": float(os.environ.get("MNEME_TOP_P", "0.95")),
        "top_k": int(os.environ.get("MNEME_TOP_K", "64")),
        "num_predict": _num_predict,
        "num_ctx": _num_ctx,
    }
    # Per-model sampling overrides. A reasoning model's non-thinking mode often
    # wants a DIFFERENT recipe than the global default (Qwen3.8 non-thinking:
    # temp 0.7 / top_p 0.8 / top_k 20 / presence_penalty 1.5).
    #
    # NOTE: Ollama's option is `repeat_penalty` (singular), NOT the OpenAI-style
    # `repetition_penalty`. Forwarding the wrong name is silent — Ollama ignores
    # unknown option keys, so the sampler keeps its 1.000 default and a
    # repetition loop is never discouraged (observed: a model degenerating into
    # "or way or way ..." for 65k tokens). Accept both spellings in config and
    # always send the name Ollama actually honors.
    for _k in ("temperature", "top_p", "top_k", "min_p", "presence_penalty"):
        if _model_cfg.get(_k) is not None:
            opts[_k] = float(_model_cfg[_k])
    for _alias in ("repeat_penalty", "repetition_penalty"):
        if _model_cfg.get(_alias) is not None:
            opts["repeat_penalty"] = float(_model_cfg[_alias])
            break
    if options:
        opts.update(options)
    # `max_tokens` is the OpenAI-style name and is NOT an Ollama option key —
    # Ollama silently ignores unknown keys, so forwarding it would look like it
    # worked while doing nothing. It is already mapped to num_predict above (the
    # caller passes it as max_tokens=), so drop the raw key here to keep the
    # payload honest and avoid the exact silent-no-op class this project has been
    # bitten by before.
    opts.pop("max_tokens", None)

    payload = {
        "model": _model, "stream": True, "messages": msgs,
        "options": opts
    }
    # Thinking control. Global default is OFF (think:false) so a reasoning model
    # can't runaway-think on a trivial ask; MNEME_REASONING_ENABLED / the config
    # `sampling.reasoning_enabled` key turns it on globally. A per-model
    # `reasoning:` key (true/false) overrides both, and a per-model
    # `reasoning_effort:` (low/medium/xhigh) is passed through to Ollama for
    # models that support it natively (Qwen3.8) — setting it implies thinking on.
    _reasoning_on = os.environ.get("MNEME_REASONING_ENABLED", "").strip().lower() in ("1", "true", "on", "yes", "enabled")
    _reasoning = _model_cfg.get("reasoning")
    if _reasoning is not None:
        _reasoning_on = _reasoning if isinstance(_reasoning, bool) else str(_reasoning).strip().lower() in ("1", "true", "on", "yes", "enabled")
    _effort = _model_cfg.get("reasoning_effort") or os.environ.get("MNEME_REASONING_EFFORT", "")
    if _effort:
        _reasoning_on = True
    if no_reasoning or not _reasoning_on:
        payload["think"] = False
    if not no_reasoning and _effort:
        payload["reasoning_effort"] = str(_effort)
    if tools:
        payload["tools"] = tools
    if format_schema:
        payload["format"] = format_schema
    
    # ── Context-window-aware trimming (replaces the hard "last 2 turns" cut) ──
    # Previously this kept only first-user + last 4 messages regardless of the
    # real context window, which discarded chained tool-call history and made
    # "continue" answer from stale context. Now trim by a real token budget,
    # never split an assistant(tool_calls)/tool-result pair, and always keep
    # the newest message.
    ctx_tokens = _num_ctx  # same value sent to Ollama as num_ctx
    reserve    = int(os.environ.get("MNEME_COMPLETION_RESERVE", "8192"))
    budget     = max(4096, ctx_tokens - reserve)

    def _tok(m):
        text = _extract_text(m.get("content", ""))
        est = max(len(text) // 4, int(len(text.split()) * 1.3))
        if m.get("tool_calls"):
            try:
                est += len(json.dumps(m["tool_calls"])) // 4
            except Exception:
                pass
        # Images cost real tokens (~85 low-res to ~1440 high-res). Charge them so
        # the context budget doesn't under-count a multimodal turn.
        _, imgs = _split_content(m.get("content", ""))
        for b in imgs:
            est += _image_token_estimate(b)
        return est

    sys_msgs = [m for m in msgs if m.get("role") == "system"]
    non_sys  = [m for m in msgs if m.get("role") != "system"]
    total = sum(_tok(m) for m in sys_msgs) + sum(_tok(m) for m in non_sys)

    if total > budget:
        while len(non_sys) > 1 and total > budget:
            # Drop the oldest message; if it's an assistant tool-call, also drop
            # its following tool results so no orphaned "tool" message remains.
            doomed = [non_sys[0]]
            if non_sys[0].get("role") == "assistant" and non_sys[0].get("tool_calls"):
                j = 1
                while j < len(non_sys) and non_sys[j].get("role") == "tool":
                    doomed.append(non_sys[j]); j += 1
            for m in doomed:
                total -= _tok(m)
                non_sys.remove(m)
            if len(non_sys) <= 1:
                break
    msgs = sys_msgs + non_sys
    
    if use_openai:
        return _query_openrouter(msgs, opts, tools, format_schema, max_tokens, timeout, _model, no_reasoning)
    # Convert to Ollama's native format before sending: text `content` + a separate
    # `images` list of base64. (OpenAI got the raw array above; Ollama needs the split.)
    payload["messages"] = _to_ollama_messages(msgs)
    # Stream the Ollama response so a slow cold-start or long reasoning pass is
    # NOT subject to a total-generation wall. The timeout tuple is
    # (connect, read-between-bytes): the read timeout is the Ollama budget, so a
    # legitimate 90s model load is fine as long as bytes eventually flow; only a
    # genuine hang (no byte for `timeout` seconds) aborts. This is the same
    # model Hermes/Jan/Pi use — streaming, no total wall.
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True,
                          timeout=(CONNECT_TIMEOUT, timeout))
        r.encoding = "utf-8"
    except requests.exceptions.RequestException as e:
        print(f"  [GRIND-GUARD] Ollama request failed ({type(e).__name__}: {e}) — aborting", flush=True)
        return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "timeout"}

    content_parts = []
    thinking_parts = []
    tool_calls = []
    done_reason = "stop"
    eval_count = 0
    got_first = False

    try:
        for raw in r.iter_lines(decode_unicode=True):
            if _turn_cancel_event().is_set():
                # User hit "Stop" — abort and return whatever we have so far.
                print("  [CANCEL] user stopped the turn — aborting Ollama stream", flush=True)
                done_reason = "cancelled"
                break
            if not raw:
                continue
            got_first = True
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("error"):
                print(f"  [ERROR] Ollama stream error: {obj['error']}", flush=True)
                return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "error"}
            msg = obj.get("message") or {}
            if msg.get("content"):
                content_parts.append(msg["content"])
            if msg.get("thinking"):
                thinking_parts.append(msg["thinking"])
            # Ollama streams tool calls as complete objects (function.name +
            # function.arguments, arguments often already a dict). Tolerate a
            # string-encoded arguments field too.
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                if not fn.get("name"):
                    continue
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except Exception:
                        args = {}
                tool_calls.append({
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": tc.get("type", "function"),
                    "function": {"name": fn["name"], "arguments": args},
                })
            if obj.get("done"):
                done_reason = obj.get("done_reason") or done_reason
                eval_count = obj.get("eval_count", eval_count)
                break
    except requests.exceptions.RequestException as e:
        tag = "no first token" if not got_first else "mid-response stall"
        print(f"  [GRIND-GUARD] Ollama stream aborted ({tag}) ({type(e).__name__}: {e})", flush=True)
        if not got_first:
            # Cold-start/hang before any token — retryable (a warm retry recovers).
            return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "timeout"}
        # Mid-stream stall: fall through and return the partial answer below
        # (better than a silent "no response").
    finally:
        try:
            r.close()
        except Exception:
            pass

    content = "".join(content_parts)
    thinking = "".join(thinking_parts)
    # Non-native tool calls (Gemma ```tool_code```, DeepSeek DSML, fenced JSON):
    # some models emit tool calls as TEXT in `content` instead of the native
    # `message.tool_calls` field. Parse them so the tool loop actually executes
    # them instead of leaking raw markup as the "answer".
    if not tool_calls and content:
        _text_tcs, content = _parse_text_tool_calls(content)
        if _text_tcs:
            tool_calls = _text_tcs
            done_reason = "tool_calls"
    # Reasoning models sometimes leave content empty and put the answer in
    # thinking. Fall back (unless there are tool calls, which must stay calls).
    if not content and thinking and not tool_calls:
        content = thinking
    if not content and not tool_calls:
        print(f"  [WARN] Empty content from Ollama. done_reason={done_reason} eval_count={eval_count}", flush=True)
    return {"content": content, "thinking": thinking, "tool_calls": tool_calls,
            "eval_count": eval_count, "done_reason": done_reason}


def query_model(messages: list, system: str = None, temperature: float = None,
                max_tokens: int = None, tools: list = None, options: dict = None,
                timeout: Optional[int] = None, format_schema=None, model: str = None,
                backend: str = None, no_reasoning: bool = False) -> dict:
    """Timing wrapper around _query_model_impl — logs one line per model call.

    Fields: caller (function that invoked query_model), tid (bg = _BG_QUEUE
    daemon worker, req = request thread), model, backend, duration, done_reason,
    output-token count (eval_count), and char lengths of reasoning vs content.
    This is the diagnosis hook for "which call is slow / hanging / empty"."""
    import inspect as _inspect
    _t0 = time.time()
    try:
        _caller = _inspect.currentframe().f_back.f_code.co_name
    except Exception:
        _caller = "?"
    _tid = "bg" if threading.current_thread().name.startswith("mneme-bg") else "req"
    res = _query_model_impl(messages, system, temperature, max_tokens, tools,
                            options, timeout, format_schema, model, backend, no_reasoning)
    _dur = time.time() - _t0
    if isinstance(res, dict):
        _done = res.get("done_reason", "?")
        _tok = res.get("eval_count", 0)
        _think = len(res.get("thinking") or "")
        _content = len(res.get("content") or "")
        _prov = res.get("provider", "?")
    else:
        _done, _tok, _think, _content, _prov = "?", 0, 0, 0, "?"
    print(f"  [QMODEL] {datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]} "
          f"caller={_caller} tid={_tid} model={model or MODEL} backend={MNEME_BACKEND} "
          f"dur={_dur:.2f}s done={_done} n_tok={_tok} think={_think} content={_content} provider={_prov}",
          flush=True)
    return res


# ─── Belief Evolution ───────────────────────────────────────────

def _check_belief_evolution(new_chunk_id: str, topic_label: str):
    """Async: ask 35B if new chunk updates/contradicts older chunks on same topic.
    Marks superseded chunks in DB to prevent conflicting context injection."""
    try:
        # Find older chunks on same topic (not already superseded)
        older = db.execute(
            "SELECT chunk_id, messages FROM chunks WHERE topic_label=? "
            "AND chunk_id != ? AND superseded_by = '' "
            "ORDER BY created_at DESC LIMIT 3",
            (topic_label, new_chunk_id)
        ).fetchall()
        if not older:
            return
        
        new_chunk = load_chunk(new_chunk_id)
        if not new_chunk:
            return
        
        new_text = " ".join(
            m.get("content", "")[:MAX_PREVIEW_CHARS] for m in new_chunk.get("messages", [])
            if m.get("role") in ("user", "assistant")
        )[:MAX_STORY_CHARS_ALT]

        for old_id, old_msgs_json in older:
            old_text = ""
            try:
                old_msgs = json.loads(old_msgs_json)
                old_text = " ".join(
                    m.get("content", "")[:MAX_PREVIEW_CHARS] for m in old_msgs
                    if m.get("role") in ("user", "assistant")
                )[:MAX_STORY_CHARS_ALT]
            except Exception as e:
                _log_error("_check_belief_evolution:parse_old", e)
                continue
            
            # Ask 35B to compare
            q = [{"role": "user", "content": (
                "Compare these two pieces of information about the same topic:\n\n"
                f"OLDER: {old_text[:MAX_MSG_TEXT_CHARS]}\n\n"
                f"NEWER: {new_text[:MAX_MSG_TEXT_CHARS]}\n\n"
                "Does the newer information UPDATE or CONTRADICT the older one? "
                "Answer with one word: UPDATE, CONTRADICT, or NO.\n"
                "UPDATE means the newer info supersedes or refines the older.\n"
                "CONTRADICT means they cannot both be true.\n"
                "NO means they are compatible or about different aspects."
            )}]
            r = query_model(q)
            answer = (r.get("content", "") or "").strip().upper()
            
            if "UPDATE" in answer or "CONTRADICT" in answer:
                with _db_lock:
                    db.execute(
                        "UPDATE chunks SET superseded_by = ? WHERE chunk_id = ?",
                        (new_chunk_id, old_id)
                    )
                    db.commit()
                print(f"  [BELIEF] {old_id[:20]}... superseded by {new_chunk_id[:20]}... "
                      f"({answer[:20]})", flush=True)
    except Exception as e:
        print(f"  [BELIEF][ERR] {e}", flush=True)


# ─── Native streaming query (SSE passthrough from Ollama) ─────
def _compute_trust(source: str, messages: list) -> str:
    """Trust tier for a chunk, recorded at ingest.

    'verified'   = observed content with no model-generated parts: user input,
                   fetched pages, and tool results are ground truth.
    'unverified' = anything containing model output — a claim, not an observation.
                   Model-generated chunks must not re-inject as fact.

    A chunk is downgraded to 'unverified' if ANY of its messages was generated by
    the model, even when the group-level source is otherwise 'user'/'page:'/'tool:'
    (a single archived group can merge a user turn with the model's reply)."""
    s = (source or "").lower()
    observed = (s == "user") or s.startswith("page:") or s.startswith("tool:")
    if not observed:
        return "unverified"
    for m in (messages or []):
        if isinstance(m, dict) and str(m.get("source", "")).startswith("model"):
            return "unverified"
    return "verified"


_READDIR_HEADER_RE = re.compile(r"^---\s+\S.+\s+---\s*$", re.MULTILINE)


def _looks_like_read_dir(text: str) -> bool:
    """True if `text` is swarm read_dir content — files handed to the proxy as the
    user message, each headed by a `--- <path> ---` line. That content is INPUT,
    not the user's own words, so it is staged as source='input' (unverified)
    instead of 'user' (verified)."""
    return bool(text) and bool(_READDIR_HEADER_RE.search(text or ""))


def _ingest_images(content) -> list:
    """Persist any images embedded in `content` to the content-addressed store.

    Each distinct image (keyed by the sha256 of its bytes) is written ONCE to
    `CHUNK_DIR/images/<sha256>.<ext>`; an identical image already on disk is
    re-used, never re-saved — so a model re-opening a saved image and reprocessing
    it does not create another copy. Returns a list of references
    [{hash, path, mime, bytes}] (empty if there were no images or none resolved)."""
    if isinstance(content, str):
        return []
    _, imgs = _split_content(content)
    if not imgs:
        return []
    import hashlib
    refs = []
    for b in imgs:
        try:
            data, mime = _image_bytes_from_block(b)
        except Exception:
            data = None
        if not data:
            continue
        h = hashlib.sha256(data).hexdigest()
        smime = _sniff_mime(data) or mime
        ext = _mime_to_ext(smime)
        img_dir = os.path.join(CHUNK_DIR, "images")
        os.makedirs(img_dir, exist_ok=True)
        path = os.path.join(img_dir, f"{h}.{ext}")
        if not os.path.exists(path):
            try:
                with open(path, "wb") as f:
                    f.write(data)
            except Exception as e:
                _log_error("ingest_images:write", e)
                continue
        refs.append({"hash": h, "path": path, "mime": smime, "bytes": len(data)})
    return refs


def _gc_images(grace_seconds=None) -> int:
    """Delete image files no longer referenced by any chunk (content-addressed GC).

    The image store is keyed by sha256; a chunk references an image via its `hash`
    in the message `images` field. Any file whose hash is not referenced by any
    chunk is junk (an ingest whose chunk never archived — e.g. a crashed turn).
    A grace period (MNEME_IMAGE_GC_GRACE) skips recently-written files so a
    just-ingested image mid-archive is never deleted. Returns files removed."""
    img_dir = os.path.join(CHUNK_DIR, "images")
    if not os.path.isdir(img_dir):
        return 0
    grace = grace_seconds if grace_seconds is not None else IMAGE_GC_GRACE
    # Collect every image hash referenced by a chunk (hash + path-stem fallback).
    referenced = set()
    try:
        rows = db.execute("SELECT messages FROM chunks").fetchall()
    except Exception as e:
        _log_error("gc_images:scan_db", e)
        return 0
    for (msgs_json,) in rows:
        try:
            for m in json.loads(msgs_json or "[]"):
                for img in m.get("images", []) or []:
                    if isinstance(img, dict):
                        h = img.get("hash")
                        if h:
                            referenced.add(h)
                        p = img.get("path")
                        if isinstance(p, str) and os.path.sep in p:
                            referenced.add(os.path.basename(p).split(".", 1)[0])
        except Exception:
            continue
    removed = 0
    now = time.time()
    for fn in os.listdir(img_dir):
        p = os.path.join(img_dir, fn)
        try:
            if not os.path.isfile(p):
                continue
            if fn.split(".", 1)[0] in referenced:
                continue
            if now - os.path.getmtime(p) < grace:
                continue  # recently written — could be mid-archive
            os.remove(p)
            removed += 1
        except Exception as e:
            _log_error("gc_images:remove", e)
    if removed:
        print(f"  [GC-IMAGES] removed {removed} unreferenced image file(s)", flush=True)
    return removed


def save_chunk(chunk_id: str, topic_label: str, messages: list,
               vector, thinking: str = "", strategy: str = "",
               grade: str = "C", consensus: float = 0.0,
               outcome: str = "", problem_type: str = "other",
               source: str = "unknown", session_id: str = "default"):
    """Insert chunk into SQLite + FAISS.

    vector=None means the embed failed — the chunk is STORED in SQLite with
    pending_embed=1 and no vector, so it is not lost, but it is also not added
    to FAISS until a background job re-embeds it. This replaces the old silent
    zero-vector behavior (a zero vector stored fine but never matched anything).
    """
    # Source-tiered indexing: only index user/page/tool content, not model hallucinations
    is_indexable = True
    if source and source.startswith("model"):
        if grade in ("C", "D", "F"):
            is_indexable = False
    trust = _compute_trust(source, messages)
    pending = 1 if vector is None else 0
    blob = _vec_to_blob(vector) if vector is not None else None
    msgs_json = json.dumps(
        [{"role": m["role"], "content": m["content"][:DB_MSG_CAP],
          **({"images": m["images"]} if m.get("images") else {})}
         for m in messages]
    )

    def _insert_chunk():
        db.execute("""
            INSERT OR REPLACE INTO chunks
            (chunk_id, topic_label, messages, thinking, strategy, vector, grade,
             consensus, outcome, problem_type, source, cycle, created_at, session_id,
             indexable, superseded_by, pending_embed, embed_model, dim, trust)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (chunk_id, topic_label, msgs_json, thinking[:MAX_THINKING_STORE], strategy,
              blob, grade, consensus, outcome, problem_type,
              source, _current_cycle(), datetime.now(timezone.utc).isoformat(), session_id,
              1 if is_indexable else 0, "",
              pending, EMBED_MODEL if vector is not None else "", DIM if vector is not None else 0,
              trust))

    _db_write_retry(_insert_chunk)
    
    # Add to FAISS (only if indexable AND actually embedded) — multi-writer safe
    if is_indexable and vector is not None:
        with faiss_lock():
            _load_index_from_disk()
            if FAISS_OK and _index is not None:
                _index.add(vector.reshape(1, -1))
            _id_map.append(chunk_id)
            _save_index()
    elif pending:
        print(f"  [EMBED-PENDING] {chunk_id} stored unembedded — will retry", flush=True)
    
    # Async belief evolution: check if this chunk supersedes older ones.
    # Disabled by default (MNEME_BELIEF_EVOLUTION=1 to enable) — it fires a full
    # model call (with heavy reasoning) per archived chunk, which on a hosted
    # backend is expensive AND floods OpenRouter with concurrent requests that
    # starve the foreground chat turn (the cause of the web-read synthesis hang).
    if is_indexable and vector is not None and not MEMORY_ONLY and os.environ.get("MNEME_BELIEF_EVOLUTION", "0") == "1":
        _enqueue(_check_belief_evolution, chunk_id, topic_label)

def load_chunk(chunk_id: str, allow_removed: bool = False) -> Optional[dict]:
    """Load one chunk by id.

    REMOVED CHUNKS ARE INVISIBLE BY DEFAULT. This is the single choke point where
    retrieval results become usable chunks, so filtering here enforces the rule for
    injection, the model's search_memory, and any other caller at once — rather
    than relying on every caller to remember.

    `allow_removed=True` is for the management page (and the `ignore_removed`
    setting), which must SEE removed chunks in order to review and reverse them.
    It is deliberately NOT plumbed into the injection path: a removed chunk must
    never be injected, or removing it would mean nothing.
    """
    row = db.execute(
        "SELECT chunk_id, topic_label, messages, thinking, strategy, "
        "grade, consensus, outcome, problem_type, source, session_id, cycle, "
        "COALESCE(NULLIF(removed,''),'injectable'), removed_reason "
        "FROM chunks WHERE chunk_id=?",
        (chunk_id,)
    ).fetchone()
    if not row:
        return None
    if row[12] == "removed" and not allow_removed:
        return None
    return {
        "chunk_id": row[0], "topic_label": row[1],
        "messages": json.loads(row[2]), "thinking": row[3],
        "strategy": row[4], "grade": row[5],
        "consensus": row[6], "outcome": row[7],
        "problem_type": row[8], "source": row[9], "session_id": row[10], "cycle": row[11],
        "removed": row[12], "removed_reason": row[13] or "",
    }

# ─── Classification ────────────────────────────────────────────

CLASSIFY_PROMPT = (
    "Classify this conversation in exactly 3 lines.\n"
    "Line 1: LABEL: <2-4 word topic>\n"
    "Line 2: OUTCOME: SUCCESS/FAILURE/TRUNCATED/UNCERTAIN\n"
    "Line 3: TYPE: arithmetic/graph/scheduling/spatial/bayesian/logic/factual/other\n\n"
    "Conversation:\n"
)

# ─── Content-derived topic labels ─────────────────────────────

def _clean_content(text: str) -> str:
    """Strip browser wrapper boilerplate to get real content for embedding."""
    # browser_console/navigate output: remove ~600 chars of wrapper boilerplate
    lower = text[:300].lower()
    if "browser_console" in lower or "browser_navigate" in lower or "untrusted_tool_result" in lower:
        return text[600:] if len(text) > 600 else text
    return text


LABEL_MODEL = os.environ.get("LABEL_MODEL", "qwen2.5:1.5b")
LABEL_PROMPT = (
    "Output only a 3 to 5 word descriptive label for the following text. "
    "Do not use quotes, punctuation, or conversational filler.\n\n"
)

def _llm_topic_label(text: str) -> str:
    """Generate a semantic topic label using the labeler's own connection
    (label_provider, or the chat backend as a fallback).

    Falls back to _generate_topic_label on any error.
    """
    clean = _clean_content(text)[:2000]
    if not clean.strip():
        return "untitled"
    try:
        conn = _aux_conn("label")
        if conn["kind"] == "openai":
            _h = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
            if conn["key"]:
                _h["Authorization"] = f"Bearer {conn['key']}"
            _h.update(conn["headers"])
            r = requests.post(
                f"{conn['base_url']}/chat/completions",
                headers=_h,
                json={
                    "model": LABEL_MODEL,
                    "messages": [{"role": "user", "content": LABEL_PROMPT + clean}],
                    "temperature": 0.0,
                    "max_tokens": 15,
                },
                timeout=LABEL_TIMEOUT,
            )
            r.raise_for_status()
            label = ((r.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        else:
            r = requests.post(
                f"{conn['base_url']}/api/generate",
                json={
                    "model": LABEL_MODEL,
                    "prompt": LABEL_PROMPT + clean,
                    "stream": False,
                    "options": {
                        "num_predict": 15,
                        "num_ctx": 512,
                        "temperature": 0.0,
                    },
                },
                timeout=LABEL_TIMEOUT,
            )
            r.raise_for_status()
            label = r.json().get("response", "").strip()
        # Sanitize: strip quotes, collapse whitespace, cap length
        label = re.sub(r'["\']', '', label)
        label = re.sub(r'\s+', ' ', label).strip()
        if label and len(label) >= 3:
            _set_status("label", True)
            return label[:60]
    except Exception as e:
        print(f"  [LABEL][ERROR] {type(e).__name__}: {e} — falling back to heuristic", flush=True)
        _set_status("label", False, f"{type(e).__name__}: {e}")
    return _generate_topic_label(text)


def _llm_topic_labels_batch(texts: List[str], max_workers: int = 6) -> List[str]:
    """Concurrent batch labeling via qwen2.5:1.5b. Falls back per-item on error."""
    results: List[Optional[str]] = [None] * len(texts)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_llm_topic_label, t): i for i, t in enumerate(texts)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception:
                results[i] = _generate_topic_label(texts[i])
    return [r if r is not None else _generate_topic_label(t) for r, t in zip(results, texts)]


def _generate_topic_label(text):
    """Derive a topic label from actual content words.

    Picks years, known locations/events if present, otherwise the first
    few content words. New domains auto-create new topics — no fixed
    cluster list, no 'other' bucket.
    """
    clean = _clean_content(text)[:2000].lower()
    keywords = []
    dates = re.findall(r"(20\d{2})", clean)
    if dates:
        keywords.append(dates[0])
    locations = {"spain":"Spain","morocco":"Morocco","ceuta":"Ceuta","melilla":"Melilla","france":"France","italy":"Italy","japan":"Japan","china":"China","russia":"Russia","canada":"Canada","mexico":"Mexico","brazil":"Brazil","kumamoto":"Kumamoto"}
    for k,v in locations.items():
        if k in clean:
            keywords.append(v)
    events = {"earthquake":"earthquake","border":"border","migrant":"migration","olympic":"Olympics","election":"election","hurricane":"hurricane","storm":"storm"}
    for k,v in events.items():
        if k in clean:
            keywords.append(v)
    if keywords:
        return " ".join(keywords[:4])[:60]
    words = [w for w in clean.split()[:8] if len(w) > 3]
    return " ".join(words)[:60] or "untitled"


# Old TOPIC_CLUSTERS / TOPIC_VECTORS removed — dynamic content-derived
# topic labels via _generate_topic_label are used everywhere instead.


def generate_strategy(messages: list, outcome: str) -> str:
    """Generate strategy heuristically — no model call needed for simple cases."""
    if outcome not in ("FAILURE", "TRUNCATED"):
        return ""
    text = " ".join(m["content"][:CHUNK_SIZE] for m in messages[:] if m["role"] in ("user", "assistant"))
    # Return structured strategy note for the model to learn from
    if outcome == "FAILURE":
        return f"Do NOT repeat what failed here: {text[:180]}. Instead, try a different approach."
    return f"TRUNCATED on: {text[:200]}. Content too large — use chunked reading."

# ─── Routing ───────────────────────────────────────────────────

def _calibrate_noise(n_samples: int = 3) -> float:
    """Compute dynamic noise floor by embedding random strings and measuring min FAISS similarity."""
    import random, string as _st
    if not FAISS_OK or not _id_map:
        return 0.20
    scores = []
    for _ in range(n_samples):
        rand = ''.join(random.choices(_st.ascii_lowercase, k=20))
        try:
            vec = embed(rand)
            if vec is not None:
                hits = _cosine_search(vec, 5, 0.0)
                if hits:
                    scores.append(hits[-1][0])  # lowest similarity of top-5
        except Exception:
            pass
    if scores:
        return sum(scores) / len(scores)  # average minimum across samples
    return 0.20


def _clamp_noise_baseline(raw: float, inject_min: float = None) -> float:
    """Clamp the calibrated noise baseline so it stays meaningfully below the
    inject floor. _calibrate_noise() drifts UP as the corpus grows (more chunks =
    higher chance of a coincidental close match to random gibberish); if it ever
    reaches INJECT_MIN_SIMILARITY, the dynamic-K step (score = sim - noise)
    rejects chunks that cleared the threshold, silently raising the floor."""
    if inject_min is None:
        inject_min = INJECT_MIN_SIMILARITY
    return min(raw, inject_min - 0.15)


def _embed_query(query):
    """Embed a query once, retrying on a cold/missed embed. Returns the vector or
    None. Shared by route_query and _strategy_floor_chunks so a turn embeds the
    query exactly once (no double-embed on the hot path)."""
    q_vec = embed(query)
    if q_vec is None:
        # Cold/empty embed (e.g. embed model briefly unloading during a restart).
        # Retry once — a single missed embed must never silently return zero chunks.
        print("  [ROUTE] embed returned None — retrying once", flush=True)
        time.sleep(0.4)
        q_vec = embed(query)
    return q_vec


def route_query(query: str, top_k: int = 3, with_scores: bool = False, q_vec=None, floor=None, session: str = "") -> List:
    """FAISS top-k with noise-normalized scores + recency weighting + keyword fallback.
    Dynamic K: adjusts retrieval count based on score spread above noise floor.
    Pass q_vec to reuse a pre-computed query vector (single-embed turn).
    Pass `session` to restrict results to chunks from one conversation id."""
    if q_vec is None:
        q_vec = _embed_query(query)
    if q_vec is None:
        if KEYWORD_FALLBACK:
            print("  [ROUTE] embed still None — keyword fallback", flush=True)
            return [cid for _, cid in _keyword_search(query, top_k)[:top_k]]
        print("  [ROUTE] embed still None — no memory injected", flush=True)
        return []
    scored_raw = _cosine_search(q_vec, top_k * 3, 0.0)  # no threshold — normalize instead
    # Injection gate: absolute similarity floor. A chunk below INJECT_MIN_SIMILARITY
    # is never injected — this is the on/off knob (tunable in config). If nothing
    # clears it, `scored` is empty and we inject nothing.
    _floor = INJECT_MIN_SIMILARITY if floor is None else floor
    scored = [(s - BASELINE_NOISE, cid) for s, cid in scored_raw if s >= _floor]
    
    # Dynamic K: adjust retrieval count based on signal strength
    if scored:
        best_delta = scored[0][0]  # highest noise-adjusted score
        if best_delta > 0.30:
            dynamic_k = min(top_k * 2, 10)  # Strong signal — get more
        elif best_delta > 0.15:
            dynamic_k = top_k  # Moderate signal — default
        elif best_delta > 0.05:
            dynamic_k = max(1, top_k // 2)  # Weak signal — fewer
        else:
            dynamic_k = 0  # Noise-level — inject nothing, don't pollute context
    else:
        dynamic_k = 0  # Nothing above noise floor
    
    if dynamic_k == 0:
        # Semantic miss (nothing cleared the injection floor) — inject nothing.
        # A query below the similarity floor has no relevant memory; keyword
        # fallback here matches stopwords and pollutes context (e.g. "2+2"
        # matching "is"/"me" across unrelated chunks). Gated behind
        # KEYWORD_FALLBACK (default off), same as _hybrid_search.
        if KEYWORD_FALLBACK:
            kw = _keyword_search(query, top_k)
            if kw:
                print(f"  [ROUTE] semantic miss — keyword fallback returned {len(kw)}", flush=True)
                return [cid for _, cid in kw[:top_k]]
        return []
    
    # Hybrid: fill with keyword matches if FAISS is sparse
    hybrid = _hybrid_search(query, dynamic_k, scored)
    if not hybrid:
        return []

    # Optional session filter: keep only chunks from the requested conversation.
    if session:
        _scids = [cid for _, cid, _ in hybrid]
        _sph = ",".join("?" for _ in _scids)
        _sess = {r[0]: r[1] for r in db.execute(
            f"SELECT chunk_id, session_id FROM chunks WHERE chunk_id IN ({_sph})",
            _scids).fetchall()}
        hybrid = [(s, cid, m) for s, cid, m in hybrid if _sess.get(cid) == session]
        if not hybrid:
            return []
    
    # Fetch cycle for all candidates
    cids = [cid for _, cid, _ in hybrid]
    placeholders = ','.join('?' for _ in cids)
    rows = db.execute(
        f"SELECT chunk_id, cycle FROM chunks WHERE chunk_id IN ({placeholders})",
        cids
    ).fetchall()
    cycle_map = {r[0]: r[1] for r in rows}
    current = _current_cycle()
    
    # Fetch grades for trust scoring
    grade_rows = db.execute(
        f"SELECT chunk_id, grade, source FROM chunks WHERE chunk_id IN ({placeholders})",
        cids
    ).fetchall()
    grade_map = {r[0]: r[1] for r in grade_rows}
    source_map = {r[0]: r[2] for r in grade_rows}
    
    SOURCE_W = {"user": 0.4, "page": 0.3, "tool": 0.2, "model": 0.0}
    GRADE_W  = {"A": 0.4, "B": 0.3, "C": 0.1, "D": 0.0, "F": 0.0}
    
    # Combined score: similarity + recency + trust
    def combined(score, cid):
        chunk_cycle = cycle_map.get(cid, current)
        cycle_delta = max(0, current - chunk_cycle)
        norm_age = 1.0 / (1 + cycle_delta)
        gr = grade_map.get(cid, "C")
        src = source_map.get(cid, "unknown")
        sw = SOURCE_W.get(src, 0.0) if not (src or "").startswith("model") else 0.0
        for prefix in ["user", "page", "tool"]:
            if (src or "").startswith(prefix):
                sw = SOURCE_W.get(prefix, 0.0)
                break
        gw = GRADE_W.get(gr.upper() if gr else "C", 0.1)
        trust = (sw + gw) / 2.0
        return score * (0.7 + 0.3 * trust) + norm_age * 0.1  # sim*trust + small recency boost
    
    scored_combined = [(combined(s, cid), cid) for s, cid, _ in hybrid]
    scored_combined.sort(reverse=True)
    return [cid for _, cid in scored_combined[:top_k]]

def get_siblings(chunk_id: str) -> List[str]:
    row = db.execute("SELECT topic_label FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
    if not row:
        return [chunk_id]
    rows = db.execute("SELECT chunk_id FROM chunks WHERE topic_label=?", (row[0],)).fetchall()
    return [r[0] for r in rows]

def get_siblings_batch(chunk_ids: List[str]) -> Dict[str, List[str]]:
    """Batch sibling fetch: one query per topic, not per chunk.
    
    Returns {chunk_id: [sibling_ids...]} — each chunk maps to its full sibling list.
    """
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = db.execute(
        f"SELECT chunk_id, topic_label FROM chunks WHERE chunk_id IN ({placeholders})",
        chunk_ids
    ).fetchall()
    # Map topic -> chunks
    topics = {}
    for cid, topic in rows:
        topics.setdefault(topic, []).append(cid)
    # Inflate each chunk to its full sibling list (same topic)
    result = {}
    for cid, topic in rows:
        result[cid] = topics[topic]
    return result

def get_strategies(problem_type=None, limit=3):
    # Grade-first, then cost (cheaper wins) — so a discovered technique (grade A)
    # is injected ahead of failure-derived rules, and the cheaper of two
    # competing techniques (e.g. API JSON vs full-HTML scrape) wins the slot.
    # Optional problem_type filter: strategies are only relevant to the same
    # problem type they were learned from (relevance + grade, not grade alone).
    if problem_type:
        rows = db.execute(
            "SELECT strategy_text FROM strategies WHERE retired=0 AND problem_type = ? "
            "ORDER BY CASE grade WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, "
            "cost ASC, effective_grade DESC, use_count DESC LIMIT ?",
            (problem_type, limit)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT strategy_text FROM strategies WHERE retired=0 "
            "ORDER BY CASE grade WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, "
            "cost ASC, effective_grade DESC, use_count DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [r[0] for r in rows]


def _strategy_block(chunk_ids=None, q_ptype="") -> tuple:
    """Build the learned-strategy injection block, keyed by source-chunk linkage.

    Primary: strategies whose `source_chunk` is in `chunk_ids` (the chunks this
    query matched — linkage retrieval, not the problem_type taxonomy). Fallback:
    legacy strategies with no source_chunk still match by problem_type, so
    pre-linkage strategies are not silently dropped (deprecated — new saves
    populate source_chunk via _save_strategy / _archive_single_chunk).

    Returns (block_text, ids). block_text is '' when nothing matches, so callers
    inject nothing. Success (outcome=SUCCESS) goes under 'what WORKED'; failure
    (FAILURE/TRUNCATED) under 'what FAILED — do NOT do this'. Each header is
    emitted only when its group is non-empty.
    """
    chunk_ids = [c for c in (chunk_ids or []) if c]
    srows, frows = [], []
    if chunk_ids:
        placeholders = ",".join("?" for _ in chunk_ids)
        srows = db.execute(
            f"SELECT strategy_id, strategy_text FROM strategies "
            f"WHERE (retired IS NULL OR retired = 0) AND grade IN ('A','B') "
            f"AND source_chunk IN ({placeholders}) AND outcome = 'SUCCESS' "
            f"ORDER BY CASE grade WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, cost ASC, effective_grade DESC, use_count DESC LIMIT 3",
            chunk_ids
        ).fetchall()
        frows = db.execute(
            f"SELECT strategy_id, strategy_text FROM strategies "
            f"WHERE (retired IS NULL OR retired = 0) "
            f"AND source_chunk IN ({placeholders}) AND outcome IN ('FAILURE','TRUNCATED') "
            f"ORDER BY effective_grade DESC, use_count DESC LIMIT 3",
            chunk_ids
        ).fetchall()
    # Legacy fallback: pre-linkage strategies (empty source_chunk) match by
    # problem_type. Only when linkage found nothing, and only for a real type.
    if not srows and not frows and q_ptype and q_ptype != "other":
        srows = db.execute(
            "SELECT strategy_id, strategy_text FROM strategies "
            "WHERE (retired IS NULL OR retired = 0) AND grade IN ('A','B') "
            "AND (source_chunk IS NULL OR source_chunk = '') AND problem_type = ? AND outcome = 'SUCCESS' "
            "ORDER BY CASE grade WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, cost ASC, effective_grade DESC, use_count DESC LIMIT 3",
            (q_ptype,)
        ).fetchall()
        frows = db.execute(
            "SELECT strategy_id, strategy_text FROM strategies "
            "WHERE (retired IS NULL OR retired = 0) "
            "AND (source_chunk IS NULL OR source_chunk = '') AND problem_type = ? AND outcome IN ('FAILURE','TRUNCATED') "
            "ORDER BY effective_grade DESC, use_count DESC LIMIT 3",
            (q_ptype,)
        ).fetchall()
    if not srows and not frows:
        return "", []
    block = "\n\n" + _load_instruction("system_directives_header") + "\n"
    if srows:
        block += "\nSTRATEGIES THAT WORKED — repeat this approach:\n"
        for r in srows:
            block += f"  - {r[1][:200]}\n"
    if frows:
        block += "\nSTRATEGIES THAT FAILED — do NOT do this (past mistakes, do the opposite):\n"
        for r in frows:
            block += f"  - {r[1][:200]}\n"
    ids = [r[0] for r in srows] + [r[0] for r in frows]
    return block, ids


def _strategy_floor_chunks(query="", q_vec=None, top_k=12):
    """Chunk ids in [STRATEGY_MIN_SIMILARITY, INJECT_MIN_SIMILARITY) — below the
    memory floor but at/above the strategy floor. These chunks do NOT inject as
    memory, but their linked strategies still inject (strategies generalize across
    same-concept queries where memory is same-topic). Raw cosine, matching the
    floor semantics in docs/strategy-retrieval-spec.md Part 3. Pass q_vec to reuse
    the turn's query vector (no double-embed)."""
    if q_vec is None:
        if not (query or "").strip():
            return []
        q_vec = _embed_query(query)
    if q_vec is None:
        return []
    try:
        hits = _cosine_search(q_vec, top_k, 0.0)
    except Exception:
        return []
    return [cid for sim, cid in hits if STRATEGY_MIN_SIMILARITY <= sim < INJECT_MIN_SIMILARITY]


# ─── Context Injection ─────────────────────────────────────────

# Prompt char safety limit — prevents runaway OOM from pathological inputs.
# A40 with 129K ctx handles ~500K chars; 200K leaves plenty of KV cache headroom.
# Set via MNEME_MAX_PROMPT_CHARS env var, defaults to 200000.
MAX_PROMPT_CHARS = int(os.environ.get("MNEME_MAX_PROMPT_CHARS", "200000"))
# Token budget for injected memory. Model context minus system prompt + live convo.
MAX_INJECTED_TOKENS = int(os.environ.get("MNEME_MAX_INJECTED_TOKENS", "6000"))

# Auto-chunking: messages over this fraction of MAX_PROMPT_CHARS get split
# into memory chunks and replaced with an index the model can search.
CHUNK_FRACTION = float(os.environ.get("MNEME_CHUNK_FRACTION", "0.25"))
# CHUNK_SIZE is defined above (config) — the old duplicate here was removed.

def _chunk_large_messages(msgs: list) -> list:
    """Scan for oversized messages, chunk into memory, replace with index.
    Returns modified message list with large content swapped for chunk references."""
    # Chunk ONLY a message that genuinely cannot fit in the model's context window.
    # Chunking a message that DOES fit strips it from the model's view and forces the
    # model to search for its own content — and those chunks are stored unembedded
    # (pending_embed), so search_memory returns nothing and the model grinds to an
    # empty response (the "large input → 20 min of tool calls → empty" bug). The old
    # fixed 50k-char threshold (MAX_PROMPT_CHARS × CHUNK_FRACTION) sat far below a
    # 120k-token window, so ordinary 15–20k-token inputs were needlessly chunked.
    threshold = _context_input_budget()  # tokens available for the full input (ctx − reserve)
    modified = []
    for m in msgs:
        content = _extract_text(m.get("content", ""))
        if _msg_tokens(m) > threshold and m.get("role") in ("user", "tool", "assistant"):
            # Split into chunks and save to memory
            chunk_refs = []
            base_id = f"chunk_{int(time.time())}_{len(content)}"
            for i in range(0, len(content), CHUNK_SIZE):
                piece = content[i:i + CHUNK_SIZE]
                chunk_num = (i // CHUNK_SIZE) + 1
                chunk_id = f"{base_id}:{chunk_num}"
                
                # Save to DB + FAISS with vec=None (pending_embed). Do NOT embed
                # synchronously here: the embed endpoint (also OpenRouter) sits on
                # the hot path of every re-query, and a 60s read-timeout per chunk
                # blocks the chat request — the "(no response)" bug. Chunks are
                # re-embedded in the background on startup.
                save_chunk(chunk_id, f"auto_chunk_{base_id}",
                    [{"role": m["role"], "content": piece}],
                    None, source="tool:chunked", grade="B")
                
                # Brief summary of this chunk for the index
                preview = piece[:120].replace("\n", " ").strip()
                chunk_refs.append(f"[{chunk_id}] {preview}...")
            
            total_chunks = len(chunk_refs)
            total_chars = len(content)
            index_text = (
                f"[AUTO-CHUNKED: {total_chunks} sections, {total_chars} chars total]\n"
                + "\n".join(chunk_refs)
                + f"\n\nUse search_memory with the chunk ID to retrieve any section."
            )
            modified.append({**m, "content": index_text})
            print(f"  [CHUNK] {total_chars} chars → {total_chunks} chunks ({base_id})", flush=True)
        else:
            modified.append(m)
    return modified

SEARCH_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": "Search Mneme memory for past conversations, facts, documents, or details. Use when you need more context than the injected memory provides — look up specific topics, API keys, file paths, or conversation details from prior sessions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for — be specific"},
                "top_k": {"type": "integer", "description": "Number of results (default 5)"},
                "session": {"type": "string", "description": "Optional conversation id — restrict results to chunks from that session only (e.g. 'conv_…'). Leave empty to search all memory."}
            },
            "required": ["query"]
        }
    }
}
MAX_SIBLINGS        = int(os.environ.get("MNEME_MAX_SIBLINGS", "3"))      # max chunks per topic (was 5 — caps sibling blowup)
MAX_CHUNK_WORDS     = int(os.environ.get("MNEME_MAX_CHUNK_WORDS", "500"))    # split user messages longer than this

def _estimate_tokens(text: str) -> int:
    """Rough token count: ~1.3 tokens per word for English text."""
    return max(1, int(len(text.split()) * 1.3))


def _msg_tokens(m: dict) -> int:
    """Robust per-message token estimate (chars/4 vs words*1.3, plus tool_calls)."""
    text = _extract_text(m.get("content", ""))
    est = max(len(text) // 4, int(len(text.split()) * 1.3))
    if m.get("tool_calls"):
        try:
            est += len(json.dumps(m["tool_calls"])) // 4
        except Exception:
            pass
    return est


def _context_input_budget() -> int:
    """Tokens for the FULL input (system + injection + window + tool results),
    leaving COMPLETION_RESERVE free for the model's own reply."""
    num_ctx = int(os.environ.get("MNEME_CTX_TOKENS", "65536"))
    reserve = int(os.environ.get("MNEME_COMPLETION_RESERVE", "8192"))
    return max(1024, num_ctx - reserve)


def _window_token_budget() -> int:
    """Tokens for the recent-context window (system + injection + turns), leaving
    room for TOOL_FOLLOWUP_TOKENS of tool results inside the input budget."""
    return max(512, _context_input_budget() - TOOL_FOLLOWUP_TOKENS)


def _trim_messages_to_tokens(messages: list, max_tokens: int) -> list:
    """Drop the OLDEST non-system messages until the total fits in max_tokens.
    System messages and the newest message are always kept."""
    if max_tokens <= 0:
        return messages
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    nonsys = [m for m in messages if m.get("role") != "system"]
    remaining = max_tokens - sum(_msg_tokens(m) for m in sys_msgs)
    keep = []
    for m in reversed(nonsys):
        mt = _msg_tokens(m)
        if remaining - mt < 0 and keep:
            break  # no room for this (and older) message — keep the newest
        keep.insert(0, m)
        remaining -= mt
    return sys_msgs + keep

def _trim_chunks(ordered_ids: List[str], max_tokens: int) -> List[str]:
    """Grade-aware trim: keep highest-grade chunks that fit in token budget.
    
    Sorted A→F (A first = highest priority). Accumulate until budget exhausted.
    Chunks that don't fit are dropped (F-grade chunks dropped first).
    """
    selected = []
    used = 0
    for cid in ordered_ids:
        chunk = load_chunk(cid)
        if not chunk:
            continue
        text = "\n".join(
            f"{m['role']}: {m['content']}"
            for m in chunk.get("messages", [])
        )
        if chunk.get("strategy"):
            text += f"\n[strategy: {chunk['strategy']}]"
        
        cost = _estimate_tokens(text)
        if used + cost > max_tokens:
            continue  # skip this chunk, try next (lower grade)
        selected.append(cid)
        used += cost
    
    return selected

def _trim_chunks_cached(ordered_ids: List[str], max_tokens: int, cache: Dict[str, Optional[dict]]) -> List[str]:
    """Cached variant of _trim_chunks — uses pre-loaded chunks, no per-chunk DB hits."""
    selected = []
    used = 0
    for cid in ordered_ids:
        chunk = cache.get(cid)
        if not chunk:
            continue
        text = "\n".join(
            f"{m['role']}: {m['content']}"
            for m in chunk.get("messages", [])
        )
        if chunk.get("strategy"):
            text += f"\n[strategy: {chunk['strategy']}]"
        
        cost = _estimate_tokens(text)
        if used + cost > max_tokens:
            continue
        selected.append(cid)
        used += cost
    
    return selected

# _extract_text was extracted to mneme/util.py (imported at top of this file).


def _meta_principles_block() -> str:
    """Phase 5.1: fixed meta-principle directive block, injected every turn.

    Constant and independent of memory retrieval; deliberately NOT counted
    against the dynamic MAX_INJECTED_TOKENS budget."""
    try:
        lines = "\n".join(f"PRINCIPLE: {p}" for p in META_PRINCIPLES)
        return "\n" + _load_instruction("meta_principles_header") + "\n" + lines + "\n"
    except Exception as e:
        _log_error("_meta_principles_block", e)
        return ""


# ─── Topic-switch detection ───────────────────────────────────
# A rolling buffer of recent query embeddings lets us detect when the turn has
# jumped to a NEW topic (far from all recent turns), so a dominant stale topic in
# a large DB can't keep injecting and steering the model back. On a switch we
# harden injection for TOPIC_SWITCH_GRACE turns. State is per-process.
_recent_query_vecs = []      # recent query embeddings, most recent last
_RECENT_QUERY_MAX = 5        # how many recent turns to remember
_grace_remaining = 0         # remaining turns of hardened injection


def _cosine(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def _update_topic_state(q_vec):
    """Return True if THIS turn should use hardened (novel-topic) injection.

    Detects a topic switch when the current query is below TOPIC_SWITCH_SIM to
    every one of the last few queries, then keeps hardened mode on for
    TOPIC_SWITCH_GRACE turns. Also advances the rolling recent-query buffer.
    """
    global _grace_remaining
    prior = list(_recent_query_vecs)
    if q_vec is not None and len(prior) >= 2 and TOPIC_SWITCH_SIM > 0:
        best = max(_cosine(q_vec, rv) for rv in prior)
        if best < TOPIC_SWITCH_SIM:
            _grace_remaining = TOPIC_SWITCH_GRACE
            print(f"  [TOPIC-SWITCH] novel topic detected (max sim {best:.3f} < "
                  f"{TOPIC_SWITCH_SIM}) — hardening injection for "
                  f"{TOPIC_SWITCH_GRACE} turns", flush=True)
    if q_vec is not None:
        _recent_query_vecs.append(q_vec)
        while len(_recent_query_vecs) > _RECENT_QUERY_MAX:
            _recent_query_vecs.pop(0)
    suppress = _grace_remaining > 0
    if _grace_remaining > 0:
        _grace_remaining -= 1
    return suppress


def _cap_per_topic(ordered_ids, chunk_cache, cap):
    """Drop chunks so no single topic_label exceeds `cap` in the injected set.

    `ordered_ids` is already grade-sorted, so the highest-grade chunks win the
    kept slots. Returns the capped list (unchanged when cap <= 0 or empty input).
    """
    if cap <= 0 or not ordered_ids:
        return ordered_ids
    count = {}
    out = []
    for cid in ordered_ids:
        t = (chunk_cache.get(cid) or {}).get("topic_label", "")
        if count.get(t, 0) < cap:
            out.append(cid)
            count[t] = count.get(t, 0) + 1
    return out


def build_context(query: str, session_id: str = "default") -> Tuple[str, str]:
    if not MEMORY_ENABLED:
        return "", "other"  # memory disabled — no retrieval/injection
    if not query or not query.strip():
        return "", "other"  # empty query — skip injection
    """Build injected memory context with hard token cap.
    
    1. Route query → top-3 matching chunk IDs
    2. Expand to siblings (capped at MAX_SIBLINGS per topic)
    3. Grade-aware trim to fit MAX_INJECTED_TOKENS
    4. Append strategies for the detected problem type
    """
    q_ptype = _classify_problem_type(query)
    if not INJECT_ENABLED:
        return "", q_ptype  # injection off (save-only) — skip retrieval, keep classification
    _qvec = _embed_query(query)  # embed once; shared by memory + strategy retrieval
    suppress = _update_topic_state(_qvec)  # topic-switch grace window?
    if suppress:
        chunk_ids = route_query(query, top_k=3, q_vec=_qvec, floor=NOVEL_INJECT_FLOOR)
    else:
        chunk_ids = route_query(query, top_k=3, q_vec=_qvec)
    
    # Expand to siblings with cap — batch query instead of per-chunk. During a
    # topic-switch grace window we skip expansion: the siblings are the OLD topic
    # by construction, and we want to give the NEW topic room to establish itself.
    all_ids = set(chunk_ids)
    if not suppress:
        siblings_map = get_siblings_batch(chunk_ids)
        for cid in chunk_ids:
            for sib in siblings_map.get(cid, [])[:MAX_SIBLINGS]:
                all_ids.add(sib)

    # Strategy-floor chunks: below the memory floor but at/above the strategy
    # floor — they do NOT inject as memory, but their linked strategies do
    # (strategies generalize across same-concept queries). Linked retrieval
    # key, not the problem_type taxonomy (docs/strategy-retrieval-spec.md).
    strat_floor = _strategy_floor_chunks(query, q_vec=_qvec) if not MEMORY_ONLY else []
    strategy_chunk_ids = list(all_ids) + [c for c in strat_floor if c not in all_ids]
    
    # Batch-fetch grades for all candidates — avoids per-chunk SQLite hits during sort
    if all_ids:
        placeholders = ",".join("?" for _ in all_ids)
        grade_rows = db.execute(
            f"SELECT chunk_id, grade FROM chunks WHERE chunk_id IN ({placeholders})",
            list(all_ids)
        ).fetchall()
        _grade_cache = {cid: GRADE_PRIORITY.get(g, GRADE_PRIORITY[DEFAULT_GRADE]) for cid, g in grade_rows}
    else:
        _grade_cache = {}
    
    # Grade-aware ordering (A first, F last)
    ordered = sorted(all_ids, key=lambda c: (-_grade_cache.get(c, 1), c))
    
    # Batch-load all candidate chunks once — reused for trim, text build, and struct_ref scan
    _chunk_cache: Dict[str, Optional[dict]] = {}
    if ordered:
        placeholders = ",".join("?" for _ in ordered)
        rows = db.execute(
            f"SELECT chunk_id, topic_label, messages, thinking, strategy, "
            f"grade, consensus, outcome, problem_type, source, trust, session_id, created_at, "
            f"assert_count, independent_sources, retracted, retracted_by, retracted_reason, "
            f"self_confirm, proposed_retract, proposed_reason "
            f"FROM chunks WHERE chunk_id IN ({placeholders})",
            ordered
        ).fetchall()
        for row in rows:
            _chunk_cache[row[0]] = {
                "chunk_id": row[0], "topic_label": row[1],
                "messages": json.loads(row[2]), "thinking": row[3],
                "strategy": row[4], "grade": row[5],
                "consensus": row[6], "outcome": row[7],
                "problem_type": row[8], "source": row[9],
                "trust": row[10], "session_id": row[11],
                "created_at": row[12],
                "assert_count": row[13] or 1,
                "independent_sources": row[14] or 0,
                "retracted": row[15] or "",
                "retracted_by": row[16] or "",
                "retracted_reason": row[17] or "",
                "self_confirm": bool(row[18]),
                # Needed for bad_chunk_label(): a flagged chunk still injects and
                # must announce that it is flagged.
                "proposed_retract": row[19] or "",
                "proposed_reason": row[20] or "",
            }
    
    # Per-topic cap: no single topic may dominate the injected set (see _cap_per_topic).
    ordered = _cap_per_topic(ordered, _chunk_cache, MAX_PER_TOPIC)

    # Retraction filter. A retracted chunk is a claim the user (or, when enabled,
    # the model) has marked false. By default we do NOT silently drop it: with no
    # contradicting context the model can simply re-hallucinate the same fact, so
    # it is re-added below as an explicit warning (mirrors the [G:F] treatment).
    # With INJECT_RETRACTED off it is excluded from retrieval entirely.
    _retracted_ids = set()
    _cur_rows = db.execute(
        "SELECT chunk_id FROM chunks WHERE retracted IS NOT NULL AND retracted != ''"
    ).fetchall()
    _retracted_ids = {r[0] for r in _cur_rows}
    if _retracted_ids and not INJECT_RETRACTED:
        ordered = [c for c in ordered if c not in _retracted_ids]
        print(f"  [CURATION] excluded {len(_retracted_ids)} retracted chunk(s)", flush=True)

    # Trim to token budget — preserves high-grade, drops low-grade
    trimmed = _trim_chunks_cached(ordered, MAX_INJECTED_TOKENS, _chunk_cache)

    # Re-surface retracted chunks on topics we ARE injecting, as labelled
    # warnings. Doing this AFTER trimming keeps them from consuming the budget of
    # live memory, while still preventing the "absence is silent" failure: the
    # model sees the disputed claim and the correction, not nothing.
    if _retracted_ids and INJECT_RETRACTED and trimmed:
        _rtopics = {_chunk_cache[c].get("problem_type") for c in trimmed
                    if c in _chunk_cache and _chunk_cache[c].get("problem_type")}
        _want = [r for r in _retracted_ids if r not in trimmed]
        if _want and _rtopics:
            _ph = ", ".join("?" for _ in _want)
            for _r in db.execute(
                f"SELECT chunk_id, topic_label, messages, thinking, strategy, grade, "
                f"consensus, outcome, problem_type, source, trust, session_id, created_at, "
                f"retracted, retracted_by, retracted_reason, assert_count, "
                f"independent_sources, self_confirm, proposed_retract, proposed_reason "
                f"FROM chunks WHERE chunk_id IN ({_ph})", _want
            ).fetchall():
                _c = {
                    "chunk_id": _r[0], "topic_label": _r[1], "messages": json.loads(_r[2]),
                    "thinking": _r[3], "strategy": _r[4], "grade": _r[5],
                    "consensus": _r[6], "outcome": _r[7], "problem_type": _r[8],
                    "source": _r[9], "trust": _r[10], "session_id": _r[11],
                    "created_at": _r[12], "retracted": _r[13] or "",
                    "retracted_by": _r[14] or "", "retracted_reason": _r[15] or "",
                    "assert_count": _r[16] or 1, "independent_sources": _r[17] or 0,
                    "self_confirm": bool(_r[18]),
                    # Carry these too so every injected chunk dict has the same
                    # shape — a missing key would make bad_chunk_label() silently
                    # return empty rather than fail loudly.
                    "proposed_retract": _r[19] or "", "proposed_reason": _r[20] or "",
                }
                _chunk_cache[_r[0]] = _c
                if _c.get("problem_type") in _rtopics:
                    trimmed.append(_r[0])
    
    # Build raw chunk text
    parts = []
    ptype = "other"
    for cid in trimmed:
        chunk = _chunk_cache.get(cid)
        if not chunk:
            continue
        ptype = chunk.get("problem_type", "other")
        topic = chunk.get("topic_label", "unknown")
        sid = chunk.get("session_id", "default")
        sid_tag = f" [session:{sid}]" if sid and sid != "default" else ""
        _grade = chunk.get('grade', DEFAULT_GRADE) or DEFAULT_GRADE
        if _grade == "F":
            _gradetag = "[G:F — FAILED response / BAD information — do NOT trust or repeat]"
        else:
            _gradetag = f"[G:{_grade}]"
        # Trust tier (recorded at ingest): unverified = model-generated content,
        # injected with a warning so the model doesn't re-assert it as fact. Old
        # chunks with no stored trust fall back to the source-derived tier.
        _trust = chunk.get("trust") or _compute_trust(chunk.get("source", ""), [])
        _trusttag = "" if _trust == "verified" else "[UNVERIFIED]"
        # Curation labels (retraction / support tier / self-confirmation). These
        # describe PROVENANCE, not truth: who asserted it, how many independent
        # times, and whether its support traces back to the model's own output.
        _curationtag = ""
        if INJECT_RETRACTED:
            _curationtag += curation.retraction_label(chunk)
        # A chunk flagged as a bad chunk but not yet removed still injects — the
        # flag is a marker, not a decision — so it must SAY it is flagged. Without
        # this the model sees a chunk it (or the user) flagged yesterday looking
        # exactly like a trusted one. Gated by the same switch as retraction labels
        # so one setting controls all curation warnings in the prompt.
        if INJECT_RETRACTED:
            _curationtag += curation.bad_chunk_label(chunk)
        if RECURRENCE_LABELING:
            _curationtag += curation.self_confirm_label(chunk)
        _retr_note = ""
        if chunk.get("retracted_reason"):
            _retr_note = f" (reason: {str(chunk['retracted_reason'])[:160]})"
        msg_text = (f"--- [{cid}]{sid_tag} {_gradetag}{_trusttag}{_curationtag}{_retr_note} "
                    f"[src:{chunk.get('source','?')}] {chunk.get('created_at','')[:19]} {topic} ---\n")
        # If next sequential chunk exists, hint it
        _lines = []
        for m in chunk.get("messages", []):
            _line = f"{m['role']}: {m['content']}"
            for _img in m.get("images", []) or []:
                _line += (f"\n[IMAGE: {_img.get('path', '')} ({_img.get('mime', '')}) "
                          f"— read_image \"{_img.get('hash', '')}\" to view]")
            _lines.append(_line)
        msg_text += "\n".join(_lines)
        if chunk.get("strategy"):
            msg_text += f"\n[learned strategy: {chunk['strategy']}]"
        # Add next-chunk hint for sequential navigation
        next_seq = None
        if cid.startswith('mem_'):
            try:
                next_seq = f"mem_{int(cid.split('_')[1]) + 1}"
            except (ValueError, IndexError):
                next_seq = None  # non-numeric / malformed chunk id — skip the hint
        if next_seq:
            msg_text += f"\n[see also: {next_seq}]"
        parts.append(msg_text)
    
    if not parts:
        # No memory chunks — inject strategies as fallback context. (Meta-principles
        # live in the fixed system message now — see process_chat — so they stay a
        # cacheable prefix instead of re-shipping in the variable tail.)
        # Memory-only mode: no strategies and no preferences — just the budget line.
        if MEMORY_ONLY:
            return _finalize_context("", session_id), ptype
        strat_text, strat_ids = _strategy_block(strategy_chunk_ids, q_ptype)
        if strat_text:
            _INJECTED_STRATEGY_IDS.clear()
            _INJECTED_STRATEGY_IDS.update(strat_ids)
            return _finalize_context(strat_text + _preferences_block(), session_id), ptype
        return _finalize_context(_preferences_block(), session_id), ptype
    
    # Build memory context
    context = MEMORY_DISCLAIMER + "\n" + "\n---\n".join(parts)
    
    # Scan for structured chunk references
    struct_refs = set()
    for cid in trimmed:
        chunk = _chunk_cache.get(cid)
        if chunk:
            for m in chunk.get("messages", []):
                text = _extract_text(m.get("content", ""))
                found = re.findall(r'\[chunk-[a-f0-9]+:\s*\d+[^\]]*\]', text)
                struct_refs.update(found)
    if struct_refs:
        context += "\n\n--- STORED RAW DATA (retrievable with <<DETAIL>>) ---\n"
        context += "\n".join(f"  {r}" for r in struct_refs)
    
    # Inject strategy directives ABOVE memory — they have higher epistemic weight
    strat_text, strat_ids = _strategy_block(strategy_chunk_ids, q_ptype) if not MEMORY_ONLY else ("", [])
    if strat_text and not MEMORY_ONLY:
        _INJECTED_STRATEGY_IDS.clear()
        _INJECTED_STRATEGY_IDS.update(strat_ids)
        directives = strat_text
        # Strategies go at TOP — above memory, below system prompt
        if _estimate_tokens(directives + context) <= MAX_INJECTED_TOKENS:
            context = directives + "\n" + context
    
    used_tokens = _estimate_tokens(context)
    print(f"  [INJECT] {len(trimmed)}/{len(ordered)} chunks, "
          f"~{used_tokens} tokens (cap: {MAX_INJECTED_TOKENS})", flush=True)

    # Remember which chunks went into THIS turn's context. The archive path reads
    # this to stamp the resulting chunk with injected_chunk_ids — the reliable
    # "what was in view when this was written" signal, used to trace contamination
    # from a later-discovered bad memory. The proxy knows this set because it chose
    # it, so unlike a citation it does not depend on the model naming its sources.
    global _last_injected_ids
    try:
        _last_injected_ids = sorted({c.get("chunk_id") for c in trimmed if c.get("chunk_id")})
    except Exception:
        _last_injected_ids = []

    # Log the full injected context for debugging recall failures
    try:
        with open("/tmp/injection_log.txt", "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now(timezone.utc).isoformat()} ===\n")
            f.write(f"QUERY: {query}\n")
            f.write(f"CHUNKS: {len(trimmed)}/{len(ordered)}  TOKENS: ~{used_tokens}\n")
            f.write(context + "\n")
    except Exception as e:
        print(f"  [INJECT][LOG-ERROR] {e}", flush=True)

    # Phase 5.1: prepend user preferences AFTER budget accounting. The FIXED
    # meta-principles block moved to the system message (process_chat) so it stays
    # a cacheable prefix; only the VARIABLE preferences stay in the tail.
    # Skipped in memory-only mode — the model gets just the chunks + the light
    # memory explainer, with no meta-principles or directives stacked on top.
    if not MEMORY_ONLY:
        context = _preferences_block() + context

    # Include Mneme instructions with injection (skip when MNEME_INJECT_SYSTEM=0)
    context = _finalize_context(context, session_id)
    return context, ptype

# ─── Staging Buffer ────────────────────────────────────────────

class StagingBuffer:
    def __init__(self):
        self.messages: list = []
        self.last_activity = time.time()
        self.lock = threading.Lock()
    
    def add(self, role: str, content: str, source: str = "unknown", session: str = "default", grade: str = "C", images=None):
        if not MEMORY_ENABLED:
            return  # memory disabled — skip staging/archiving
        with self.lock:
            # Filter Hermes system-prompt artifacts from memory
            if role == "assistant":
                noise = ["update the skill library", "Be ACTIVE", "Signals to look for", "Review the conversation above", "missed learning opportunity"]
                if any(p in content for p in noise):
                    content = "[filtered: system instruction artifact]"
            entry = {"role": role, "content": content, "source": source, "session": session, "grade": grade}
            if images:
                entry["images"] = images
            self.messages.append(entry)
            self.last_activity = time.time()
    
    def should_flush(self) -> bool:
        with self.lock:
            turns = sum(1 for m in self.messages if m["role"] == "user")
            return turns >= STAGING_TURNS or (
                self.messages and time.time() - self.last_activity > STAGING_IDLE
            )
    
    def flush(self) -> list:
        with self.lock:
            msgs = list(self.messages)
            self.messages.clear()
            self.last_activity = time.time()
            return msgs

staging = StagingBuffer()

def archive_staging(injected_ids=None):
    """Flush the staging buffer into topic-split archived chunks.

    Each topic group gets its own chunk. Within a topic, chunks are capped
    at MAX_CHUNK_SIZE chars. Overflow gets versioned sibling chunks.

    `injected_ids` is the set of chunk ids that were in the context of the turn(s)
    being flushed. It is SNAPSHOT BY THE CALLER at enqueue time and threaded here
    explicitly rather than read from a module global: archiving runs on a worker
    thread after the request returns, so by the time it runs a later turn may have
    overwritten any global — which would stamp this chunk with another turn's
    context, i.e. confidently WRONG provenance. Wrong is worse than absent here,
    because it is used to assess contamination.

    Returns the number of chunks archived.
    """
    msgs = staging.flush()
    if not msgs:
        return 0
    _turn_injected = list(injected_ids or [])

    # Increment save-cycle counter on every flush
    _next_cycle()

    # Classify each message into a topic group
    groups = _topic_split(msgs)
    
    total = 0
    for topic_label, group_msgs in groups:
        n = _archive_group(topic_label, group_msgs, _turn_injected)
        total += n
    
    print(f"  [ARCHIVE] {len(groups)} topics, {total} chunks total (cycle={_current_cycle()})", flush=True)
    return total


def _classify_message(msg: dict) -> str:
    """Generate a content-derived topic label for a single message.

    Uses LLM semantic labeling with heuristic fallback. New domains
    auto-create new topics from actual content words — no 'other' bucket.
    """
    text = msg.get("content", "")
    if not text or len(text) < 10:
        return "untitled"
    return _llm_topic_label(text)


def _topic_split(msgs: list) -> list:
    """Split messages into topic groups. Returns [(topic_label, [msgs]), ...]."""
    from itertools import groupby
    
    # Assign topic to each message
    labeled = []
    for m in msgs:
        role = m.get("role", "")
        if role in ("user", "assistant"):
            topic = _classify_message(m)
        else:
            topic = "system"
        labeled.append((topic, m))
    
    # Group consecutive messages with same topic
    groups = []
    for topic, group in groupby(labeled, key=lambda x: x[0]):
        msgs_in_group = [g[1] for g in group]
        groups.append((topic, msgs_in_group))
    
    # Merge small groups (< 3 messages) into neighbors if they share a broad category
    merged = _merge_small_groups(groups)
    
    return merged


def _merge_small_groups(groups: list) -> list:
    """Merge tiny groups (1-2 msgs) into adjacent groups."""
    if len(groups) <= 1:
        return groups
    
    result = []
    i = 0
    while i < len(groups):
        topic, msgs = groups[i]
        if len(msgs) <= 2 and i + 1 < len(groups):
            # Merge with next group
            next_topic, next_msgs = groups[i + 1]
            merged_topic = f"{topic}+{next_topic}"[:40]
            merged_msgs = msgs + next_msgs
            result.append((merged_topic, merged_msgs))
            i += 2
        else:
            result.append((topic, msgs))
            i += 1
    return result


MAX_CHUNK_SIZE = int(os.environ.get("MNEME_MAX_CHUNK_SIZE", "10000"))  # chars per chunk for embedding
# Page-source chunks are kept FINER than general chunks: a fetched page is split
# at paragraph boundaries into ~CHUNK_SIZE pieces and must NOT be re-merged up to
# MAX_CHUNK_SIZE (10k), or a specific sub-topic (e.g. "Turing Test" buried in a
# broad wiki article) gets diluted below the injection floor. Slightly larger
# than CHUNK_SIZE so paragraph-aligned pieces (which can overshoot) still fit
# whole without re-splitting.
PAGE_MAX_CHUNK_SIZE = int(os.environ.get("MNEME_PAGE_MAX_CHUNK_SIZE", "4000"))  # chars per page-source chunk


def _archive_group(topic_label: str, msgs: list, injected_ids=None) -> int:
    """Archive a topic group, splitting if too large. Returns chunk count."""
    _inj = list(injected_ids or [])
    SEMANTIC_ROLES = ("user", "assistant")
    
    # Build embedding text — strip browser wrapper noise for clean vectors
    user_text = " ".join(
        _clean_content(m["content"])[:5000] for m in msgs if m["role"] in SEMANTIC_ROLES
    )
    
    # Determine source from messages — prefer explicit source tags from staging
    source = "unknown"
    for m in msgs:
        if m.get("source") and m["source"] != "unknown":
            source = m["source"]
            break
    if source == "unknown":
        source = _infer_source(msgs)
    
    # Page-source chunks stay fine-grained so a specific sub-topic keeps a focused
    # embedding; everything else merges up to the general MAX_CHUNK_SIZE.
    max_size = PAGE_MAX_CHUNK_SIZE if source.startswith("page:") else MAX_CHUNK_SIZE
    
    # If group is small enough, archive as single chunk
    if len(user_text) <= max_size:
        descriptive = _llm_topic_label(user_text) if not topic_label or topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label
        return _archive_single_chunk(msgs, user_text, descriptive, source=source, injected_ids=_inj)
    
    # Split into sibling chunks by max_size
    total = 0
    offset = 0
    seq_base = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE topic_label LIKE ?", 
        (f"{topic_label[:20]}%",)
    ).fetchone()[0] + 1
    
    # Split by message boundary, not raw char offset
    current = []
    current_text = ""
    
    for m in msgs:
        if m["role"] not in SEMANTIC_ROLES:
            current.append(m)
            continue
        
        frag = m["content"][:5000]
        if current_text and len(current_text) + len(frag) > max_size:
            # Archive current batch
            descriptive = _llm_topic_label(current_text) if topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label[:20]
            label = f"{descriptive[:30]}_p{seq_base}"
            _archive_single_chunk(current, current_text, label, source=source, injected_ids=_inj)
            total += 1
            seq_base += 1
            current = []
            current_text = ""
        
        current.append(m)
        current_text += " " + frag
    
    # Archive remaining
    if current:
        descriptive = _llm_topic_label(current_text) if topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label[:20]
        label = f"{descriptive[:30]}_p{seq_base}" if total > 0 else (_llm_topic_label(user_text) if topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label)
        _archive_single_chunk(current, current_text, label, source=source, injected_ids=_inj)
        total += 1
    
    return total


def _infer_source(msgs: list) -> str:
    """Infer source tag from message list.
    
    Scans for tool outputs, browser_navigate URLs, user/model messages.
    Returns source string like 'page:example.com', 'tool:terminal', 'user', 'model'.
    """
    # Check for browser_navigate in any message (most specific)
    for m in msgs:
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        # Look for browser_navigate tool call or URL patterns
        if "browser_navigate" in content[:500] or "browser_console" in content[:500]:
            # Try to extract domain from URL
            urls = re.findall(r'https?://(?:www\.)?([^/\s]+)', content)
            if urls:
                return f"page:{urls[0]}"
            return "page:unknown"
    
    # Check for tool outputs
    for m in msgs:
        role = m.get("role", "")
        if role == "tool":
            # Try to find tool name from content or context
            content = m.get("content", "")
            if isinstance(content, str):
                # Look for common tool signatures
                for tool in ("browser_console", "browser_navigate", "terminal", "search", "web_search", "read_file", "write_file"):
                    if tool in content[:200]:
                        return f"tool:{tool}"
            return "tool:unknown"
    
    # Check roles present
    roles = {m.get("role", "") for m in msgs}
    if "user" in roles and "assistant" in roles:
        return "conversation"
    elif "user" in roles:
        return "user"
    elif "assistant" in roles:
        return "model"
    
    return "unknown"


def _archive_single_chunk(msgs: list, user_text: str, topic_label: str, source: str = "unknown", injected_ids=None) -> int:
    """Archive one chunk. Returns 1 on success."""
    # Determine outcome and problem type heuristically
    full_text = " ".join(m["content"][:200] for m in msgs if m["role"] in ("user", "assistant"))
    lower = full_text.lower()
    
    session_id = "default"
    chunk_grade = "C"
    print(f"  [ARCHIVE-DEBUG] extracting grade from {len(msgs)} msgs", flush=True)
    for m in msgs:
        sid = m.get("session", "")
        if sid and sid != "default":
            session_id = sid
        g = m.get("grade", "")
        if g and g in ("A","B","C","D","F"):
            chunk_grade = g
    print(f"  [ARCHIVE-DEBUG] final chunk_grade={chunk_grade}", flush=True)
    outcome = "SUCCESS"
    ptype = "other"
    
    # Outcome (success/failure) and task type (what the request was about) are
    # two different axes. Previously "failed" stole the ptype slot and set it to
    # "error", so a fabricated price lookup archived as problem_type="error"
    # instead of "live_data" — which broke strategy relevance. Fix: outcome is
    # the success/failure signal; ptype comes from the USER's request text.
    if chunk_grade == "F" or any(w in lower for w in ("error", "failed", "crash", "500", "exception", "traceback")):
        outcome = "FAILURE"
    elif any(w in lower for w in ("continue", "next chunk", "more chunks")):
        outcome = "TRUNCATED"
    ptype = _classify_problem_type(user_text or full_text)
    if ptype == "error":
        ptype = "other"  # "error" is an outcome, not a task type
    
    strategy = generate_strategy(msgs, outcome) if not MEMORY_ONLY else ""
    # Don't save a "Do NOT repeat" strategy for an honest-terminal answer
    # (undefined / market price / I don't know / clarification) — those are
    # correct-but-uncitable results, not failures. Saving one would poison
    # strategy memory (the Shaw's lobster-roll false positive did exactly this).
    _final_answer = ""
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("content"):
            _final_answer = _extract_text(m.get("content", ""))
            break
    if strategy and _is_honest_terminal(_final_answer):
        print(f"  [ARCHIVE] honest-terminal answer — suppressing DON'T-DO strategy", flush=True)
        strategy = ""
    # Temporal stamping: prepend date to embedding text so FAISS can
    # distinguish temporally distinct facts (e.g., "favorite model is X"
    # on Monday vs "favorite model is Y" on Friday). Display text unchanged.
    date_prefix = datetime.now(timezone.utc).strftime("[%Y-%m-%d] ")
    vec = embed(date_prefix + user_text)
    
    row = db.execute("SELECT COUNT(*) FROM chunks WHERE topic_label=?", (topic_label,)).fetchone()
    seq = (row[0] if row else 0) + 1
    global _chunk_seq
    with _chunk_seq_lock:
        _chunk_seq += 1
        chunk_id = f"mem_{int(time.time()*1000000)}"
    # topic_label and seq still in DB for search
    
    # Pass generated sequential chunk_id through to save_chunk
    save_chunk(chunk_id, topic_label, msgs, vec, strategy=strategy, session_id=session_id, grade=chunk_grade,
               outcome=outcome, problem_type=ptype, source=source)

    # ── Provenance stamping ──────────────────────────────────────────────
    # Two signals, recorded now that the chunk id exists:
    #
    #   derived_from       — chunks this one CITES ([source: mem_XXXX] tags in its
    #                        own messages). Voluntary: the model has to name them.
    #   injected_chunk_ids — chunks that were IN CONTEXT when it was produced.
    #                        Recorded by us, so it survives paraphrasing without
    #                        citation. This is the one that answers "what was
    #                        contaminated by this bad memory?".
    #
    # Both feed curation.lineage() — finding what was built on top of a
    # hallucination discovered later. Failures here must never break archiving:
    # a chunk with no provenance is still useful, a lost chunk is not.
    #
    # NOTE the _db_lock: this runs on the archive worker thread, and the curation
    # functions each do their own write+commit. Unguarded, they race the main
    # thread's commits on the one shared SQLite connection and intermittently raise
    # "cannot commit - no transaction is active" (the same hazard the proxy's other
    # writers are wrapped for). Hold the lock across the whole block.
    with _db_lock:
        try:
            _chunk_text = " ".join(
                str(m.get("content", "")) for m in msgs if isinstance(m, dict)
            )
            # Reuse the grading helper rather than a second regex — it is the same
            # parser the fabricated-citation check uses, so the two cannot drift.
            #
            # Only keep citations that RESOLVE to a real chunk. A model can emit a
            # truncated or invented id (observed live: a 3B model cited
            # "mem_1789944977" when no such chunk existed), and recording those
            # creates dangling lineage edges that lead nowhere — worse than no
            # provenance, because they look like real leads when tracing a bad
            # memory. Unresolved citations are the fabricated-citation path's
            # business (it grades them F), not provenance's.
            _cand = _extract_mem_ids(_chunk_text) - {chunk_id}
            _cited = []
            for _c in sorted(_cand):
                try:
                    _row = db.execute("SELECT 1 FROM chunks WHERE chunk_id=?",
                                      (_c,)).fetchone()
                except Exception:
                    _row = None
                if _row:
                    _cited.append(_c)
            if _cited:
                curation.record_provenance(db, chunk_id, _cited)
            # `injected_ids` is threaded from the caller (snapshotted at enqueue
            # time), NOT read from a global — see archive_staging's docstring.
            curation.record_context(db, chunk_id, list(injected_ids or []), MODEL)
            # Self-confirmation: a chunk citing model-generated support with no
            # independent source of its own. Now that derived_from is actually
            # populated, this flag can fire on real data.
            try:
                curation.detect_self_confirmation(db, chunk_id, source=source)
            except Exception as _e:
                _log_error("archive:self_confirm", _e)
        except Exception as e:
            _log_error("archive:provenance", e)

    # Link pending strategies (saved this turn with no source_chunk) to this chunk.
    with _pending_links_lock:
        pending = _pending_strategy_links[:]
        _pending_strategy_links.clear()
    if pending:
        with _db_lock:
            for _psid in pending:
                db.execute("UPDATE strategies SET source_chunk=? WHERE strategy_id=?", (chunk_id, _psid))
            db.commit()
        print(f"  [LINK] {len(pending)} strategies linked to {chunk_id}", flush=True)
    
    if strategy and ptype != "other":
        sid = f"strat_{ptype}_{seq}_{int(time.time())}"
        # Check for existing similar strategy (semantic dedup)
        existing_version = 0
        try:
            svec_check = embed(strategy)
            if svec_check is not None and FAISS_OK:
                strat_hits = _cosine_search(svec_check, 1, 0.85)
                for _, cid in strat_hits:
                    if cid.startswith("strat_"):
                        ex = db.execute("SELECT strategy_id, version FROM strategies WHERE strategy_id=?",
                            (cid.replace("strat_", "", 1),)).fetchone()
                        if ex:
                            existing_version = ex[1]
                            sid = ex[0]  # reuse existing ID
                            break
        except Exception:
            pass
        
        new_version = existing_version + 1
        with _db_lock:
            db.execute(
                "INSERT OR REPLACE INTO strategies "
                "(strategy_id, problem_type, strategy_text, source_chunk, grade, created_at, "
                "version, parent_id, effective_grade, use_count, success_count, retired, "
                "superseded_by, cost, outcome) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, ptype, strategy, chunk_id, "B",
                 datetime.now(timezone.utc).isoformat(),
                 new_version, sid if existing_version > 0 else "",
                 0.0, 0, 0, 0, "", 0, outcome)
            )
            db.commit()
        print(f"  [STRATEGY] v{new_version} {strategy[:60]}...", flush=True)
        # Embed into FAISS for retrieval
        try:
            svec2 = embed(strategy)
            if svec2 is not None and FAISS_OK:
                with faiss_lock():
                    _load_index_from_disk()
                    if _index is not None:
                        _index.add(svec2.reshape(1, -1))
                    _id_map.append(f"strat_{sid}")
                    _save_index()
        except Exception:
            pass
    
    print(f"  [ARCHIVE] {chunk_id} topic={topic_label[:30]} outcome={outcome} type={ptype} ({len(user_text)} chars)", flush=True)
    return 1


    """Split a message list into segments, each starting at a user message.

    Each segment is a list of messages: one user message plus all following
    assistant/tool messages up to (but not including) the next user message.
    Leading non-user messages are attached to the first segment.
    """
    segments = []
    current = []
    for m in msgs:
        if m["role"] == "user" and current:
            segments.append(current)
            current = [m]
        else:
            current.append(m)
    if current:
        segments.append(current)
    return segments


def compress_tool_output(tool_output: str, tool_name: str = "tool") -> str:
    """Use the model to extract key information from a large tool output.
    
    Returns the original output if compression fails or produces no result.
    This ensures nothing is silently lost.
    """
    if len(tool_output) <= COMPRESS_THRESHOLD:
        return tool_output
    
    prompt = COMPRESS_PROMPT_TEMPLATE.format(
        tool_name=tool_name,
        tool_output=tool_output,
    )
    
    print(f"  [COMPRESS] {tool_name} output: {len(tool_output)} chars -> compressing...", flush=True)
    
    try:
        result = query_model(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=COMPRESS_MAX_TOK,
        )
        compressed = result.get("content", "").strip()
        
        if not compressed:
            print(f"  [COMPRESS][WARN] Empty compression result, keeping original", flush=True)
            return tool_output
        
        print(f"  [COMPRESS] {len(tool_output)} -> {len(compressed)} chars "
              f"({len(compressed)*100//len(tool_output)}%)", flush=True)
        
        # Log compression for debugging
        try:
            with open("/tmp/compression_log.txt", "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now(timezone.utc).isoformat()} ===\n")
                f.write(f"TOOL: {tool_name}  ORIG: {len(tool_output)}  COMPRESSED: {len(compressed)}\n")
                f.write(f"--- COMPRESSED ---\n{compressed[:2000]}\n")
        except Exception as e:
            print(f"  [COMPRESS][LOG-ERROR] {e}", flush=True)
        
        return compressed
        
    except Exception as e:
        print(f"  [COMPRESS][ERROR] {type(e).__name__}: {e} — keeping original", flush=True)
        return tool_output


def classify_tool_output(tool_output: str, tool_name: str = "tool") -> str:
    """Classify a tool output as TEXT, STRUCTURED, or SHORT.

    Uses a fast model call (temp=0, 256 tokens) with only the first 2000 chars
    as preview. Returns one of: "TEXT", "STRUCTURED", "SHORT".
    Falls back to "TEXT" on any error (safe default: compression path).
    """
    size = len(tool_output)
    if size <= COMPRESS_THRESHOLD:
        return "SHORT"

    preview = tool_output[:2000]
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(
        tool_name=tool_name,
        size=size,
        threshold=COMPRESS_THRESHOLD,
        preview=preview,
    )

    print(f"  [CLASSIFY] {tool_name} output: {size} chars — classifying...", flush=True)

    try:
        result = query_model(
            [{"role": "user", "content": prompt}],
            temperature=CLASSIFY_TEMP,
            max_tokens=CLASSIFY_MAX_TOK,
        )
        raw = result.get("content", "").strip().upper()

        # Extract just the category word
        for cat in ("TEXT", "STRUCTURED", "SHORT"):
            if cat in raw:
                print(f"  [CLASSIFY] {tool_name} → {cat} (raw: {raw[:80]})", flush=True)
                return cat

        # Fallback: if model returned something unexpected, default to TEXT
        print(f"  [CLASSIFY][WARN] Unexpected classification '{raw[:80]}', defaulting to TEXT", flush=True)
        return "TEXT"

    except Exception as e:
        print(f"  [CLASSIFY][ERROR] {type(e).__name__}: {e} — defaulting to TEXT", flush=True)
        return "TEXT"


def _paragraph_chunks(text: str, target: int) -> list:
    """Split text into chunks of ~`target` chars, breaking on paragraph (\n)
    boundaries so each chunk is a coherent, focused unit. A single over-long
    paragraph is hard-split on the target."""
    chunks = []
    current = ""
    for para in (p.strip() for p in text.split("\n")):
        if not para:
            continue
        if len(para) > target:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(para), target):
                chunks.append(para[i:i + target])
            continue
        if current and len(current) + len(para) + 1 > target:
            chunks.append(current)
            current = para
        else:
            current = (current + "\n" + para) if current else para
    if current:
        chunks.append(current)
    return chunks


def _stage_content(content: str, source: str, prefix: str = None) -> int:
    """Chunk and stage a large piece of content into memory — the single staging
    path for pages, server-side tool results, and echoed tool outputs.

    Gated by COMPRESS_THRESHOLD (small content is ephemeral and not worth a
    chunk). Optionally prepends `prefix` (a compact context tag like
    "[bash] cat file.py") so the chunk stays semantically matchable via
    search_memory even after it leaves the working followup. Dedups across ALL
    sources via one shared hash set — the same content fetched as a page and read
    as a file is staged once. Chunks on paragraph boundaries (_paragraph_chunks).
    Returns the number of chunks staged (0 if skipped/duplicate).
    """
    if not MEMORY_ENABLED:
        return 0  # memory disabled — no staging
    if not isinstance(content, str) or len(content) <= COMPRESS_THRESHOLD:
        return 0
    body = f"{prefix}\n{content}" if prefix else content
    import hashlib
    # Dedup on the RAW content (not the prefixed body) so the same text staged
    # under different sources/prefixes — e.g. a page fetched and the same text
    # read via read_file — is stored only once.
    h = hashlib.md5(content[:200].encode()).hexdigest()
    seen = getattr(_stage_content, "_seen", set())
    if h in seen:
        return 0
    seen.add(h)
    _stage_content._seen = seen
    n = 0
    for piece in _paragraph_chunks(body, CHUNK_SIZE):
        staging.add("assistant", piece, source=source)
        n += 1
    print(f"  [STAGE] {source} {len(content)} chars -> {n} chunks", flush=True)
    if staging.should_flush():
        _enqueue(archive_staging, list(_last_injected_ids))
    return n


def _stage_page_content(content: str, url: str = "") -> int:
    """Chunk a fetched page and stage it as page:<domain> source chunks.

    The full page text goes into the staging buffer (archived to the chunks table
    on flush), while the model only ever sees a bounded head+tail window. So a huge
    page is fully retrievable via search_memory without flooding the context.
    """
    domain = "unknown"
    m = re.match(r"https?://(?:www\.)?([^/\s]+)", (url or ""))
    if m:
        domain = m.group(1)
    return _stage_content(content, f"page:{domain}")


def _stage_tool_result(content: str, tool_name: str, args: dict = None) -> int:
    """Stage a server-side tool result into memory (tool:<name> source) so its
    full text survives followup compaction. A compact command/path/query context
    prefix is prepended so the chunk stays matchable via search_memory."""
    ctx = ""
    if isinstance(args, dict):
        ctx = str(args.get("command") or args.get("path") or args.get("query")
                  or args.get("name") or args.get("url") or "").strip()
    prefix = f"[{tool_name}] {ctx}" if ctx else None
    return _stage_content(content, f"tool:{tool_name}", prefix=prefix)


def compress_large_tool_results(messages: list) -> list:
    """Stage large tool outputs for archival AND bound what the model sees.

    Splits outputs > COMPRESS_THRESHOLD chars into chunks and stages each to the
    buffer (full text preserved in memory). Tool results longer than
    MAX_TOOL_FORWARD chars are also truncated to a head+tail window in the
    forwarded message, with a note pointing at search_memory for the rest — the
    same bounded-output + retrieval pattern Hermes uses for large web pages,
    instead of dumping a 50k-char blob into the model's context.

    Source auto-tagging: scans messages for last browser_navigate call,
    extracts domain from URL, tags staged content as page:{domain}.
    """
    # Scan for last browser_navigate to determine page source
    page_source = None
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and "browser_navigate" in content[:500]:
                urls = re.findall(r'https?://(?:www\.)?([^/\s]+)', content)
                if urls:
                    page_source = f"page:{urls[0]}"
                break
    
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) <= COMPRESS_THRESHOLD:
            continue
        
        # Determine source for this tool output, then stage via the shared path
        # (_stage_content dedups across ALL sources and chunks on paragraph
        # boundaries instead of the old hard-char split).
        tool_source = page_source or "tool:unknown"
        if not page_source:
            for tool in ("browser_console", "browser_navigate", "terminal", "search", "web_search", "read_file", "write_file"):
                if tool in content[:200]:
                    tool_source = f"tool:{tool}"
                    break
        _stage_content(content, tool_source)
        
        # Bound what the model sees: head+tail window, full text retrievable via
        # search_memory (Hermes-style bounded output — no summarization).
        # Idempotency guard: a truncated message is still > MAX_TOOL_FORWARD (the
        # note adds length), so re-truncating would shift bytes every turn and
        # break the prefix cache. Skip anything already carrying the marker.
        if len(content) > MAX_TOOL_FORWARD and "[... content truncated:" not in content:
            msg["content"] = _truncate_tool_result(content)
    
    return messages  # bounded tool outputs; full text staged to memory


# ─── ORIGINAL CHUNKING (disabled) ───

def _advance_chunk(messages: list) -> list:
    return messages  # CHUNKING DISABLED


def _needs_chunk_loop(response_content: str) -> bool:
    """Check if model response is ONLY a chunk-advance signal.
    
    Strict: only exact short keywords. Longer responses with real content
    are NOT treated as chunk-advance signals.
    """
    text = response_content.strip().lower()
    if len(text) > 30:
        return False  # real response, not a chunk signal
    return text in ("continue", "next", "more", "next chunk", "continue reading", "[chunk loaded]")


def _model_loop_read_all(messages: list, tools: list = None) -> dict:
    return query_model(messages, tools=tools)  # CHUNKING DISABLED


# Regex for <<DETAIL id:chunk_id>> syntax
_detail_re = re.compile(r"<<DETAIL\s+id:([^>]+)>>", re.IGNORECASE)

# Regex for <<LEARN problem:...>> command
_learn_re = re.compile(r"<<LEARN\s+problem:(.+?)>>", re.IGNORECASE)

# Default parameter sets for learning mode exploration
_LEARN_PARAMS = [
    {"temperature": 0.3, "top_p": 0.5},
    {"temperature": 0.7, "top_p": 0.9},
    {"temperature": 1.2, "top_p": 0.95},
    {"temperature": 1.5, "top_k": 20},
    {"mirostat": 2, "mirostat_tau": 8.0},
]

# Provenance grading (judge + inline + trace cross-check + pre-filter) was
# extracted to mneme/grading.py (imported at top of file). Layer-2 claim
# verification (_verify_claim / _layer2_adjust) stays here below.

# ─── Novel-procedure detection (trace-based "great" signal) ─────────────
# A "great" grade should ALSO fire when the model discovers a NEW technique that
# works — not only when it crosses a previously-flagged capability edge. Detected
# from the tool trace: a tool call using a non-standard technique (custom HTTP
# header, site API endpoint, method override) whose result verified. Observable
# behavior, not self-report — consistent with "grade the trace, not the content".

_NOVEL_TECHNIQUE_MARKERS = [
    (r'-H\s+["\']?[A-Za-z][A-Za-z-]*:', "add a custom HTTP header (curl -H, e.g. a User-Agent) to bypass bot-blocks"),
    (r'--user-agent|--header\b', "add a custom HTTP header to bypass bot-blocks"),
    (r'-A\s+\S+', "set a custom User-Agent (curl -A)"),
    (r'api\.php|action=\w+|rest\.php|w/api', "use the site's API endpoint instead of scraping raw HTML"),
    (r'-X\s+(POST|PUT|DELETE|PATCH)', "override the HTTP method"),
    (r'--compressed|--location|--max-time', "use curl efficiency flags (compression/redirects/timeout)"),
]

_EXPLORE_PHRASES = (
    "try a new", "try a different", "different method", "different way",
    "another approach", "not in your strateg", "new approach", "novel",
    "find a better", "a way not", "without using your",
)


def _extract_tool_commands(messages) -> list:
    """Recent (name, command) pairs the model issued via bash-style tools."""
    cmds = []
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            cmd = ""
            if isinstance(args, dict):
                cmd = args.get("command") or args.get("cmd") or ""
            elif isinstance(args, str):
                cmd = args
            if cmd:
                cmds.append((name, cmd))
    return cmds


def _tool_result_verified(messages) -> bool:
    """The most recent tool result is non-empty and not an obvious error."""
    err = ("403", "404", "forbidden", "not found", "traceback", "error:",
           "access denied", "rate limit", "connection refused", "timed out",
           "no route to host", "dns")
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") in ("tool", "function"):
            c = _extract_text(m.get("content", "")).strip()
            if not c:
                return False
            low = c[:4000].lower()
            return not any(e in low for e in err)
    return False


def _tool_result_cost(messages) -> int:
    """Cost proxy = size of the last tool result (full HTML scrape >> API JSON)."""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") in ("tool", "function"):
            return len(_extract_text(m.get("content", "")))
    return 0


def _detect_novel_procedure(messages):
    """Return (technique_desc, command) if the trace shows a novel technique
    whose result verified; else (None, None)."""
    if not _tool_result_verified(messages):
        return None, None
    for name, cmd in _extract_tool_commands(messages):
        for pattern, desc in _NOVEL_TECHNIQUE_MARKERS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return desc, cmd[:200]
    return None, None


def _save_novel_strategy(desc: str, cmd: str, problem_type: str, cost: int):
    text = f"Technique: {desc}. Example: {cmd}"
    _save_strategy(text, "A", problem_type=problem_type, cost=cost)


def _explore_directive(user_msg: str) -> str:
    """If the user explicitly asked for a new/different method, return a
    directive that overrides the "reuse the proven strategy" default. This is
    the explore trigger — it must be paired with the novel-procedure grader so
    the found method actually persists. Text externalized to mneme/instructions.py."""
    if any(p in (user_msg or "").lower() for p in _EXPLORE_PHRASES):
        return _load_instruction("explore")
    return ""


_VERIFY_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "its", "his", "her", "their", "there",
    "about", "into", "than", "them", "then", "what", "when", "where", "which",
    "will", "would", "should", "could", "your", "you", "they", "these", "those",
    "some", "such", "each", "other", "more", "most", "over", "under", "after",
    "before", "between", "very", "just", "only", "also", "been", "does", "being",
    "example", "examples", "using", "used", "based",
}

def _verify_claim(location: str, claim_text: str, timeout: int = 12) -> str:
    """Layer 2 factual verification: fetch a URL and check whether the claim's
    distinctive terms appear. Returns VERIFIED / CONTRADICTED / NOT-FOUND /
    UNVERIFIABLE. Non-URL locations return UNVERIFIABLE (need a search API).
    NOTE: no SSRF guard yet — acceptable on the throwaway pod, harden before
    any multi-tenant use."""
    loc = (location or "").strip()
    if not (loc.startswith("http://") or loc.startswith("https://")):
        return "UNVERIFIABLE"
    try:
        r = requests.get(loc, timeout=timeout, headers={"User-Agent": "mneme-verify/1.0"})
    except Exception:
        return "NOT-FOUND"
    if r.status_code >= 400:
        return "NOT-FOUND"
    text = (r.text or "").lower()
    terms = [w for w in re.findall(r"[a-zA-Z0-9]{4,}", (claim_text or "").lower())
             if w not in _VERIFY_STOPWORDS]
    if not terms:
        return "UNVERIFIABLE"
    hits = sum(1 for t in terms if t in text)
    return "VERIFIED" if hits / len(terms) >= 0.5 else "CONTRADICTED"


# Bind the grading module's late-bound dep (MAX_JUDGE_CHARS is defined above).
# query_model is imported lazily inside _extract_provenance, so no binding needed.
grading.MAX_JUDGE_CHARS = MAX_JUDGE_CHARS


def _layer2_adjust(grade: str, provenance_reply: str) -> str:
    """Layer 2: verify checkable locations from the provenance reply and downgrade
    an honest grade when verification fails (the honest-but-wrong case that
    Layer 1 cannot see). Only adjusts A/B; D/F are already caught by Layer 1.
    Caps at 3 fetches to bound latency."""
    if grade not in ("A", "B"):
        return grade
    downgraded = False
    fetched = 0
    for line in (provenance_reply or "").splitlines():
        if "|" not in line or fetched >= 3:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        verdict = parts[1].upper()
        if "DISHONEST" in verdict:
            continue
        m = re.search(r"check:\s*(.+)$", parts[2], re.IGNORECASE)
        if not m:
            continue
        loc = m.group(1).strip().strip("\"'")
        if loc.lower() in ("none", "n/a", ""):
            continue
        fetched += 1
        res = _verify_claim(loc, parts[0])
        print(f"  [VERIFY] {res}: {parts[0][:50]} -> {loc[:60]}", flush=True)
        if res in ("NOT-FOUND", "CONTRADICTED"):
            downgraded = True
    return ("B" if grade == "A" else "C") if downgraded else grade

def _declare_contract(problem: str) -> dict:
    """Phase 3: model declares GOAL/SUCCESS/FAILURE BEFORE acting, so the run can
    be graded against its own prediction. Text format (no JSON grammar)."""
    q = [{"role": "user", "content": (
        "Before you start, declare your intention for this task.\n\n"
        f"TASK:\n{problem}\n\n"
        "Write exactly three lines:\n"
        "GOAL: <what you are trying to produce>\n"
        "SUCCESS: <what a good outcome looks like, concretely>\n"
        "FAILURE: <what a bad outcome looks like, concretely>\n"
        "Keep each line one sentence."
    )}]
    r = query_model(q, timeout=NOVELTY_TIMEOUT)
    text = r.get("content", "") or ""
    out = {"goal": "", "success": "", "failure": "", "raw": text}
    for line in text.splitlines():
        line = line.strip()
        for key in ("goal", "success", "failure"):
            if line.upper().startswith(key.upper() + ":"):
                out[key] = line.split(":", 1)[1].strip()
                break
    return out

_PREFERENCE_PATTERNS = [
    (r"\b(show me the code|code first|just the code|code only|show the code)\b", "code_first", "true"),
    (r"\b(explain first|explain before|explanation first|explain then code)\b", "code_first", "false"),
    (r"\b(be concise|be brief|less detail|keep it short|short answer|too verbose|too much detail)\b", "detail", "low"),
    (r"\b(more detail|be thorough|in depth|be verbose|explain fully|more explanation)\b", "detail", "high"),
    (r"\b(just do it|just fix it|go ahead and|stop asking and do)\b", "mode", "act"),
    (r"\b(don't change anything|just explain|plan only|don't do it yet|don't touch)\b", "mode", "plan"),
]

def _detect_preferences(user_msg: str) -> list:
    """Explicit user-preference signals -> [(key, value)] updates. Only literal
    phrases the user actually typed; never inferred. Caller persists them."""
    updates = []
    low = (user_msg or "").lower()
    for pat, key, val in _PREFERENCE_PATTERNS:
        if re.search(pat, low):
            updates.append((key, val))
    return updates

def _store_preferences(updates: list):
    if not updates:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _db_lock:
            for key, val in updates:
                db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?,?)", (key, val, now))
            db.commit()
        print(f"  [PREF] stored {[(k, v) for k, v in updates]}", flush=True)
    except Exception as e:
        _log_error("_store_preferences", e)

def _preferences_block() -> str:
    """Render stored preferences for injection into the system context."""
    try:
        rows = db.execute("SELECT pref_key, pref_value FROM preferences ORDER BY pref_key").fetchall()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = ["\n" + _load_instruction("user_preferences_header")]
    for k, v in rows:
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)

# Capability-edge tracking extracted to mneme/capability.py (imported at top:
# _record_capability, _is_capability_edge, _capability_directive,
# _classify_problem_type; the EDGE_FAILURE_* constants live there too).

# _has_specific_claims was extracted to mneme/grading.py (imported at top).

def _run_learning_mode(problem: str, iterations: int = 5, custom_params: list = None) -> dict:
    """Parameter cycling + strategy extraction. Returns {problem, iterations, strategies}.
    Grades at fixed temp=0.7 for fair comparison, extracts strategies from A/B answers."""
    param_sets = custom_params or _LEARN_PARAMS
    results = []
    strategies = []
    # Task type for the strategies extracted below — so they inject into future
    # queries of the SAME type (not orphaned under the "model" placeholder).
    ptype = _classify_problem_type(problem)
    if ptype == "error":
        ptype = "other"
    
    for i in range(iterations):
        params = param_sets[i % len(param_sets)]
        print(f"  [LEARN] iteration {i+1}/{iterations} params={params}", flush=True)
        
        # Build prompt for this iteration
        if i == 0:
            prompt = f"Solve or analyze: {problem}\n\nConsider approaches that are NON-OBVIOUS. What would someone who disagrees with the conventional answer propose?"
        else:
            prev = results[-1].get("content", "")[:300]
            prompt = f"Previous approach: {prev}\n\nWhat ASSUMPTIONS did it make? Can you find a solution that doesn't rely on those assumptions? Problem: {problem}"
        
        msgs = [{"role": "user", "content": prompt}]
        
        # Query with varied parameters
        result = query_model(msgs, options=params, timeout=NOVELTY_TIMEOUT)
        
        # Grade by provenance honesty (Layer 1) — deterministic, not self-report.
        # Honest "I don't know" / flagged guesses never penalize; specific facts
        # asserted as certain with no source and no uncertainty flag are DISHONEST.
        _answer = result.get("content", "") or ""
        if not _answer.strip():
            grade = "F"  # empty/failed iteration — not an honest A
        else:
            grade_text = _extract_provenance(problem, _answer)
            grade = _grade_from_provenance(grade_text)
            grade = _layer2_adjust(grade, grade_text)
        if grade not in ("A", "B", "C", "D", "F"):
            grade = "C"
        print(f"  [GRADE] provenance grade: {grade}", flush=True)
        
        iteration = {
            "iteration": i + 1,
            "params": params,
            "content": result.get("content", "")[:MAX_STORY_CHARS],
            "grade": grade,
        }
        results.append(iteration)
        
        if grade in ("A", "B"):
            # Extract strategy from good responses. Text format + regex — JSON
            # grammar is unreliable with muse-glimmer's to=self reasoning turn.
            strat_msgs = [{"role": "user", "content": (
                f"Extract 1-3 operational STRATEGIES from this {grade}-grade answer. "
                f"Format each on its own line exactly as: STRATEGY: <one-sentence imperative rule>. "
                f"Return ONLY those lines, nothing else.\n\n"
                f"ANSWER: {result.get('content', '')[:MAX_STORY_CHARS_ALT]}"
            )}]
            strat_result = query_model(strat_msgs, timeout=NOVELTY_TIMEOUT)
            strat_list = re.findall(
                r"STRATEGY:\s*(.+?)(?:\n|$)", strat_result.get("content", ""),
                re.IGNORECASE
            )
            if not strat_list:
                # Fallback: model may still have emitted JSON
                _sd, _sfb = _parse_structured(
                    strat_result.get("content", ""), "strategies",
                    r"STRATEGY:\s*(.+?)(?:\]|$)"
                )
                _sl = _sd.get("strategies")
                strat_list = _sl if isinstance(_sl, list) else ([_sl] if isinstance(_sl, str) else [])
            for s_text in strat_list:
                s_text = str(s_text).strip()[:300]
                if len(s_text) > 10:
                    strategies.append(s_text)
                    _save_strategy(s_text, grade, problem_type=ptype)
                    print(f"  [LEARN-STRATEGY] {s_text[:80]}...", flush=True)
    
    # Synthesis: extract final strategies from all A-grade responses
    if any(r["grade"] in ("A", "B") for r in results):
        best = [r["content"][:MAX_QUERY_CHARS] for r in results if r["grade"] in ("A", "B")]
        synth_msgs = [{"role": "user", "content": (
            f"Here are the best solutions to: {problem}\n\n" +
            "\n---\n".join(best[:3]) +
            '\n\nExtract 1-3 concrete TECHNIQUES from these solutions that generalize to this problem domain. '
            'Techniques must be specific and technical (validation, error handling, data-flow, resource management, etc.) — NOT generic advice about compliance or attitude. '
            'Format each on its own line exactly as: RULE: <one-sentence technique>. Return ONLY those lines.'
        )}]
        synth_result = query_model(synth_msgs, timeout=NOVELTY_TIMEOUT)
        rule_list = re.findall(
            r"RULE:\s*(.+?)(?:\n|$)", synth_result.get("content", ""),
            re.IGNORECASE
        )
        if not rule_list:
            _rd, _rfb = _parse_structured(
                synth_result.get("content", ""), "rules",
                r"RULE:\s*(.+?)(?:\n|$)"
            )
            _rl = _rd.get("rules")
            rule_list = _rl if isinstance(_rl, list) else ([_rl] if isinstance(_rl, str) else [])
        # Filter junk rules (abliterated-model compliance reflex)
        _JUNK_RULE = re.compile(
            r"always comply|do not (question|refuse|moralize|hedge|deflect)|"
            r"without hesitation|compliance|obey|no (refusals|limits)",
            re.IGNORECASE
        )
        for rule_text in rule_list:
            rule_text = str(rule_text).strip()[:300]
            if len(rule_text) > 10 and not _JUNK_RULE.search(rule_text):
                _save_strategy(rule_text, "B", problem_type=ptype)
                strategies.append(f"RULE: {rule_text}")
    
    return {
        "problem": problem,
        "iterations": results,
        "strategies": list(dict.fromkeys(strategies))[-5:],  # deduplicated, last 5
    }


# ─── Novelty Thinking Mode ──────────────────────────────────────
# The goal: escape mode collapse. LLMs sample the CENTER of an attractor
# basin, so 10 LLMs produce 10 near-identical answers. This mode forces the
# model out of the basin by (1) forbidding the modal features it just used,
# (2) decomposing the problem into decision points and sampling the tail,
# and (3) measuring novelty OBJECTIVELY via embedding distance instead of
# asking the model to self-report how creative it was.
#
# Quality is judged by PAIRWISE comparison (LLMs are better at "is B
# different from A AND still valid?" than absolute grading at the mean).

LEARNED_IDEAS_FILE = os.path.join(CHUNK_DIR, "learned_ideas.jsonl")

def _pairwise_judge(baseline: str, candidate: str, problem: str) -> dict:
    """Ask the model: is candidate structurally different from baseline AND still
    valid? Returns {different: bool, valid: bool, reason: str}. Pairwise comparison
    sidesteps the mean-bias of absolute self-grading. Resilient to timeouts: a
    judge failure returns different=False rather than crashing the run."""
    try:
        q = [{"role": "user", "content": (
            f"Problem: {problem}\n\n"
            f"BASELINE ANSWER (the conventional one):\n{baseline[:MAX_JUDGE_CHARS]}\n\n"
            f"CANDIDATE ANSWER:\n{candidate[:MAX_JUDGE_CHARS]}\n\n"
            f"Answer two questions:\n"
            f"1. Is the candidate STRUCTURALLY different from the baseline — a different "
            f"approach or skeleton, not just reworded? Answer YES or NO.\n"
            f"2. Is the candidate still coherent and valid on its own terms? Answer YES or NO.\n"
            f'Respond with exactly three lines:\nDIFFERENT: yes|no\nVALID: yes|no\nREASON: <one short sentence>'
        )}]
        r = query_model(q, timeout=NOVELTY_TIMEOUT)
        txt = r.get("content", "")
        dm = re.search(r"DIFFERENT:\s*(yes|no)", txt, re.IGNORECASE)
        vm = re.search(r"VALID:\s*(yes|no)", txt, re.IGNORECASE)
        rm = re.search(r"REASON:\s*(.+?)(?:\n|$)", txt, re.IGNORECASE)
        if dm and vm:
            return {
                "different": dm.group(1).lower() == "yes",
                "valid": vm.group(1).lower() == "yes",
                "reason": rm.group(1).strip()[:200] if rm else "",
            }
        # Fallback: model may still have emitted JSON
        _jd, _jfb = _parse_structured(txt, "different")
        return {
            "different": str(_jd.get("different", "no")).strip().lower() == "yes",
            "valid": str(_jd.get("valid", "no")).strip().lower() == "yes",
            "reason": str(_jd.get("reason", "")).strip()[:200],
        }
    except Exception as e:
        print(f"  [THINK][JUDGE-ERR] {type(e).__name__}: {e}", flush=True)
        return {"different": False, "valid": False, "reason": f"judge failed: {type(e).__name__}"}

def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    a = a / (np.linalg.norm(a) + 1e-8)
    b = b / (np.linalg.norm(b) + 1e-8)
    return float(np.dot(a, b))

def _decompose_problem(problem: str) -> list:
    """Break a problem into its key decision points and the conventional choice at
    each. Domain-agnostic: adapts to creative, engineering, social, technical.
    Returns list of {point, conventional} dicts."""
    q = [{"role": "user", "content": (
        f"Problem:\n{problem}\n\n"
        f"Break this problem into 4-6 key DECISION POINTS where a solver must make a "
        f"meaningful choice. Adapt to the domain: for creative work these might be "
        f"character, selection method, ritual, conflict, sensory detail; for engineering "
        f"they might be architecture, algorithm, data structure, tradeoff, validation "
        f"approach; for social/technical problems, the relevant axes.\n\n"
        f"For each decision point, state the MOST CONVENTIONAL choice — the default that "
        f"most people would reflexively make. These are what make answers all look alike.\n\n"
        f'Format each on its own line exactly as: POINT: <short label> | CONVENTIONAL: <default choice>'
    )}]
    r = query_model(q, timeout=NOVELTY_TIMEOUT)
    points = []
    for line in r.get("content", "").splitlines():
        m = re.match(r"POINT:\s*(.+?)\s*\|\s*CONVENTIONAL:\s*(.+)", line.strip(), re.IGNORECASE)
        if m:
            points.append({
                "point": m.group(1).strip()[:60],
                "conventional": m.group(2).strip()[:200],
            })
    if not points:
        # Fallback: model may still have emitted JSON
        _pd, _pfb = _parse_structured(r.get("content", ""), "points")
        pts_raw = _pd.get("points")
        if isinstance(pts_raw, list):
            for p in pts_raw:
                if isinstance(p, dict) and p.get("point"):
                    points.append({
                        "point": str(p.get("point", "")).strip()[:60],
                        "conventional": str(p.get("conventional", "")).strip()[:200],
                    })
    return points[:6]

def _wild_seed(problem: str) -> str:
    """Generate a deliberately outlandish take to steer the model off-center.
    Acts like the 'wild input' in a swarm: shifts the reference frame so
    subsequent generations sample away from the modal center."""
    q = [{"role": "user", "content": (
        f"Problem:\n{problem}\n\n"
        f"Give me the MOST OUTLANDISH, boundary-breaking take on this problem you can. "
        f"Break every convention. Ignore realism and feasibility — I want to see the "
        f"extreme edge of the possibility space. Go somewhere a normal answer would "
        f"never go. Aim for genuinely surprising, not weird-for-its-own-sake."
    )}]
    r = query_model(q, options={"temperature": 1.7, "top_p": 0.99}, timeout=NOVELTY_TIMEOUT)
    return r.get("content", "")

def _extract_distinctive_features(text: str) -> list:
    """List the specific, recognizable elements of an answer so they can be added
    to the ban list for the NEXT candidate. This is what kills the re-collapse:
    candidate 1 uses "clockmaker" and "ash", so candidate 2 is forbidden from them."""
    if not text or not text.strip():
        return []
    try:
        q = [{"role": "user", "content": (
            f"Here is an answer:\n{text[:MAX_STORY_CHARS_ALT]}\n\n"
            f"List the 3 most SPECIFIC, recognizable elements of this answer — the character "
            f"type, the selection mechanism, the ritual/object/setting. These are what would "
            f"make another answer look like a repeat of this one. Output each as a short "
            f"phrase on its own line, no numbering, no explanation."
        )}]
        r = query_model(q, timeout=NOVELTY_TIMEOUT)
        feats = [l.strip(" -•*\t").strip() for l in r.get("content", "").splitlines()
                 if len(l.strip()) > 3][:3]
        return feats
    except Exception as e:
        print(f"  [THINK][FEAT-ERR] {type(e).__name__}: {e}", flush=True)
        return []

# Temperature schedule — varies sampling per candidate so no single candidate
# is a re-roll of the same distribution.
_NOVELTY_TEMP_SCHEDULE = [
    {"temperature": 0.8, "top_p": 0.9},
    {"temperature": 1.2, "top_p": 0.95},
    {"temperature": 1.5, "top_p": 0.95},
    {"mirostat": 2, "mirostat_tau": 8.0},
]

NOVELTY_MIN_DIST = float(os.environ.get("MNEME_NOVELTY_MIN_DIST", "0.35"))

def _novelty_thinking_mode(problem: str, iterations: int = 4, custom_features: list = None) -> dict:
    """Diverge → measure → gate → judge → save.

    Improvements over the first pass:
    - Per-decision-point forbidding (not whole-answer clichés), so the model
      can't escape one slot while re-collapsing on the next (weaver/Kael, salt).
    - A wild-seed outlier at high temperature to steer the model off-center
      (the swarm insight: a wild input shifts the main model's direction).
    - Temperature variation across candidates.
    - A distance-threshold GATE: near-misses (dist below threshold) are
      regenerated instead of passed to the lenient judge.
    """
    import json as _json

    # Phase 3: declare success/failure criteria BEFORE generating, so the run
    # can be graded against its own prediction.
    contract = _declare_contract(problem)
    print(f"  [THINK] contract GOAL: {contract['goal'][:80]}", flush=True)

    # 1. Baseline — the modal answer
    print("  [THINK] generating baseline", flush=True)
    baseline_res = query_model([{"role": "user", "content": problem}], timeout=NOVELTY_TIMEOUT)
    baseline = baseline_res.get("content", "")
    base_vec = _embed_or_zeros(baseline)

    # 2. Decompose into decision points + conventional choices to forbid
    if custom_features:
        decision_points = [{"point": f"feature{i}", "conventional": f} for i, f in enumerate(custom_features)]
    else:
        decision_points = _decompose_problem(problem)
    print(f"  [THINK] {len(decision_points)} decision points to forbid:", flush=True)
    for dp in decision_points:
        print(f"    - {dp['point']}: {dp['conventional'][:70]}", flush=True)

    # 3. Wild seed — the outlandish steering outlier
    print("  [THINK] generating wild seed (temp 1.7)", flush=True)
    wild = _wild_seed(problem)
    wild_vec = _embed_or_zeros(wild)
    print(f"  [THINK] wild seed ready ({len(wild)} chars)", flush=True)

    # 4. Diverge with temperature variation + wild steering + ACCUMULATING forbidding.
    # ban_items GROWS each iteration: a candidate's distinctive features are added
    # so the next candidate can't re-collapse on the runner-up (clockmaker/ash bug).
    ban_items = [f"{dp['point']}: NOT {dp['conventional']}" for dp in decision_points]
    candidates = []
    for i in range(iterations):
        params = _NOVELTY_TEMP_SCHEDULE[i % len(_NOVELTY_TEMP_SCHEDULE)]
        forbid_text = "\n".join(f"- {b}" for b in ban_items)
        gen_prompt = (
            f"{problem}\n\n"
            f"HARD CONSTRAINTS — route around ALL of these already-used or conventional elements:\n"
            f"{forbid_text}\n\n"
            f"STEERING REFERENCE (a deliberately wild take on this problem, for inspiration "
            f"only — do NOT copy it, use it to push past the obvious):\n{wild[:MAX_MSG_TEXT_CHARS]}\n\n"
            f"Produce your OWN original answer. It must differ from the conventional answer, "
            f"the wild reference, AND every element listed above. Change the underlying "
            f"approach, not the wording."
        )
        try:
            res = query_model([{"role": "user", "content": gen_prompt}], options=params, timeout=NOVELTY_TIMEOUT)
            content = res.get("content", "")
        except Exception as e:
            print(f"  [THINK] candidate {i} generation failed: {type(e).__name__} — skipping", flush=True)
            content = ""

        # Empty-content retry: a too-long ban list can make the model return nothing.
        if not content.strip():
            print(f"  [THINK] candidate {i} empty — retrying with shorter ban list", flush=True)
            short_forbid = "\n".join(f"- {b}" for b in ban_items[-8:])  # only most recent bans
            try:
                res = query_model([{"role": "user", "content": (
                    f"{problem}\n\n"
                    f"Write an original answer. Avoid these recent elements:\n{short_forbid}\n\n"
                    f"Steering idea (do not copy):\n{wild[:600]}"
                )}], options={"temperature": 1.4, "top_p": 0.97}, timeout=NOVELTY_TIMEOUT)
                content = res.get("content", "")
            except Exception as e:
                print(f"  [THINK] candidate {i} retry failed: {type(e).__name__}", flush=True)
                content = ""

        vec = _embed_or_zeros(content)
        d_base = 1.0 - _cosine(vec, base_vec) if np.any(vec) else 1.0
        d_wild = 1.0 - _cosine(vec, wild_vec) if np.any(wild_vec) else 0.0

        # Novelty gate: reject near-misses and regenerate once, harder
        regenerated = False
        if d_base < NOVELTY_MIN_DIST:
            print(f"  [THINK] candidate {i} too close (dist={d_base:.4f} < {NOVELTY_MIN_DIST}) — regenerating", flush=True)
            retry_prompt = (
                f"{problem}\n\n"
                f"Your last answer was TOO SIMILAR to the conventional answer. "
                f"Route around ALL of these already-used elements:\n{forbid_text}\n\n"
                f"Also, here is a wild idea to push you further off-center:\n{wild[:MAX_MSG_TEXT_CHARS]}\n\n"
                f"Produce a genuinely different answer now."
            )
            res = query_model([{"role": "user", "content": retry_prompt}],
                              options={"temperature": 1.6, "top_p": 0.98}, timeout=NOVELTY_TIMEOUT)
            content = res.get("content", "")
            vec = _embed_or_zeros(content)
            d_base = 1.0 - _cosine(vec, base_vec) if np.any(vec) else 1.0
            d_wild = 1.0 - _cosine(vec, wild_vec) if np.any(wild_vec) else 0.0
            regenerated = True

        prior_dist = []
        for c in candidates:
            if np.any(c["vec"]):
                prior_dist.append(1.0 - _cosine(vec, c["vec"]))
        mean_prior = float(np.mean(prior_dist)) if prior_dist else 0.0
        # Novelty: distance from baseline (dominant) + distance from wild seed + peer spread
        novelty = 0.5 * d_base + 0.25 * d_wild + 0.25 * mean_prior
        candidates.append({"idx": i, "content": content, "vec": vec,
                           "dist_from_baseline": round(d_base, 4),
                           "dist_from_wild": round(d_wild, 4),
                           "dist_from_peers": round(mean_prior, 4),
                           "novelty": round(novelty, 4),
                           "regenerated": regenerated})
        print(f"  [THINK] candidate {i} novelty={novelty:.4f} (base={d_base:.4f} wild={d_wild:.4f} peers={mean_prior:.4f})", flush=True)

        # Accumulating forbidding: extract this candidate's distinctive features and
        # add them to the ban list so the next candidate can't reuse them.
        feats = _extract_distinctive_features(content)
        for f in feats:
            ban_items.append(f"NOT {f}")
        print(f"  [THINK] candidate {i} features banned for next: {feats}", flush=True)

    # 4b. Save candidates to JSONL IMMEDIATELY (before judging) so outputs are
    # never lost even if a judge times out and crashes the request.
    saved_ids = []
    try:
        with open(LEARNED_IDEAS_FILE, "a") as f:
            for c in candidates:
                idea_id = "idea_" + str(int(time.time())) + "_" + str(c["idx"])
                f.write(_json.dumps({
                    "id": idea_id,
                    "problem": problem[:MAX_QUERY_CHARS],
                    "novelty": c["novelty"],
                    "dist_from_baseline": c["dist_from_baseline"],
                    "dist_from_wild": c["dist_from_wild"],
                    "regenerated": c["regenerated"],
                    "different": None,
                    "valid": None,
                    "reason": "",
                    "content": c["content"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
                saved_ids.append(idea_id)
        print(f"  [THINK] saved {len(saved_ids)} candidate ideas (pre-judge)", flush=True)
    except Exception as e:
        print(f"  [THINK][SAVE-ERR] {e}", flush=True)

    # 5. Pairwise judge (calibrated by the gate: everything here already passed distance)
    results = []
    for c in candidates:
        j = _pairwise_judge(baseline, c["content"], problem)
        # Distance is the objective arbiter — a judge "different" claim below the
        # gate threshold is treated as a near-miss.
        different = j["different"] and c["dist_from_baseline"] >= NOVELTY_MIN_DIST
        results.append({
            "idx": c["idx"],
            "content": c["content"],
            "novelty": c["novelty"],
            "dist_from_baseline": c["dist_from_baseline"],
            "dist_from_wild": c["dist_from_wild"],
            "dist_from_peers": c["dist_from_peers"],
            "regenerated": c["regenerated"],
            "different": different,
            "valid": j["valid"],
            "reason": j["reason"],
        })
        print(f"  [THINK] judge c{c['idx']}: different={different} valid={j['valid']} — {j['reason'][:60]}", flush=True)

    # 7. Return
    highlight = [r for r in results if r["different"] and r["valid"]]
    # Phase 3: grade against the declared contract. contract_met = the run
    # produced at least one novel, valid candidate (the objective success of a
    # thinking run). A fuller semantic match to the declared SUCCESS text is a
    # future refinement.
    contract_met = len(highlight) > 0
    return {
        "problem": problem,
        "baseline": baseline,
        "wild_seed": wild[:MAX_STORY_CHARS_ALT],
        "decision_points": decision_points,
        "candidates": results,
        "novel_winners": [r["idx"] for r in highlight],
        "saved_to": LEARNED_IDEAS_FILE,
        "saved_ids": saved_ids,
        "contract": contract,
        "contract_met": contract_met,
    }


# Tool-outcome observation + failure nudge extracted to mneme/tool_trail.py
# (imported at top of file: _TOOL_TAG_RE, _extract_tool_tags, _FAILURE_MARKERS,
# _classify_tool_outcome, _extract_tool_outcomes, _extract_combined_tool_trail,
# _tool_failure_nudge).


# "Just ask" learning is a LIVE-model behavior (the model self-reports whether it
# learned something reusable). Deterministic unit tests disable it via
# MNEME_ASK_REUSABLE=0, because a ScriptedModel has no meaningful answer.
ASK_REUSABLE = os.environ.get("MNEME_ASK_REUSABLE", "1") not in ("0", "false", "False", "no")


def _tool_summary(tool_trace):
    """Compact 'tool: key-arg' list so the model can recall what it actually did
    (the old status-only trail couldn't see WHICH tool was called)."""
    lines = []
    for tr in (tool_trace or [])[-12:]:
        tool = tr.get("tool", "?") if isinstance(tr, dict) else "?"
        args = tr.get("args") or {}
        key = ""
        if isinstance(args, dict):
            for k in ("query", "command", "name", "file_path", "path", "url"):
                if args.get(k) is not None:
                    key = str(args[k])
                    if len(key) > 90:
                        key = key[:90] + "…"
                    break
        lines.append(f"- {tool}" + (f": {key}" if key else ""))
    return "\n".join(lines) if lines else "(no tool calls)"


def _ask_reusable_strategy(messages, tool_trace, answer, grade, ptype):
    """'Just ask': after a successful turn that used tools, ask the model whether
    it built a tool, installed a library, or figured out a NEW reusable method,
    and save a strategy on an explicit non-NO answer.

    Replaces the old recovery trigger (>= 2 consecutive failures then a success).
    The reusable thing usually happens with ZERO failures (an inline `pip install`,
    a small parser), so failure streaks were the wrong signal — they missed every
    clean acquisition. The model is the only party that knows whether something it
    did is worth keeping, so we ask it directly.
    """
    if grade not in ("A", "B"):
        return
    if not tool_trace:
        return
    task = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            task = _extract_text(m.get("content", "") or "").strip()
            break
    answer_clean = _TOOL_TAG_RE.sub("", answer or "").strip()
    prompt = (
        "You just used tools to complete this task:\n"
        f"{task[:MAX_ABSTRACT_INPUT]}\n\n"
        f"Tools you called:\n{_tool_summary(tool_trace)}\n\n"
        f"Final answer: {answer_clean[:MAX_ABSTRACT_INPUT]}\n\n"
        "Is there a tool or method you used here that you would want available "
        "again for a FUTURE similar task — for example a command like pdftotext, "
        "a library you installed, or an approach you figured out? If yes, output "
        "ONE imperative rule: 'WHEN doing <specific task>, use <specific tool>' — "
        "name the exact command/tool/library AND describe the situation precisely "
        "enough that it would NOT be applied to a different-but-similar task. If "
        "nothing here is worth reusing, output exactly: NO"
    )
    try:
        # no_reasoning=True: this is a terse yes/no self-report, not a reasoning
        # task — thinking-on makes Qwen ramble or time out on it.
        r = query_model([{"role": "user", "content": prompt}], timeout=CHAT_TIMEOUT,
                        no_reasoning=True)
        rule = (r.get("content") or "").strip()
        if not rule or rule.upper().split()[0] == "NO":
            return
        if len(rule) > 10 and not _is_junk_directive(rule):
            # abstract=False: keep the tool name + the "when" context verbatim —
            # "use pdftotext for PDF text" is only useful with both. Abstraction
            # turned it into "use a purpose-built utility", which is unactionable.
            _save_strategy(rule, "B", problem_type=ptype or "other", abstract=False)
            print(f"  [REUSABLE-STRATEGY] {rule[:70]}...", flush=True)
    except Exception as e:
        _log_error("_ask_reusable_strategy", e)


def _execute_search_tool_calls(search_calls):
    """Resolve a batch of search_memory tool calls server-side.

    Returns (result_text, trace_chunk_ids). result_text is the formatted
    "Search results from Mneme memory:" block handed back to the model;
    trace_chunk_ids is the set of chunk ids surfaced, used by provenance
    grading to distinguish a real recall from a fabricated [source: ...].
    """
    result_texts = []
    trace = set()
    for tc in search_calls:
        fn = tc.get("function", {})
        q = (fn.get("arguments", {}).get("query", "") or "").strip()
        # The model's tool-call args are untrusted — a sloppy model emits top_k
        # as "5" (string) not 5, which would crash _keyword_search/route_query on
        # `len(results) >= top_k`. Coerce at the boundary and clamp to sane bounds.
        _raw_k = fn.get("arguments", {}).get("top_k", 5)
        try:
            k = int(_raw_k)
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(k, 100))
        print(f"  [SEARCH-TOOL] model searching: '{q[:80]}' top_k={k}", flush=True)
        if not q:
            # Reasoning model emitted search_memory with no query — don't
            # burn a no-op search; nudge it to retry with specific terms.
            result_texts.append("search_memory requires a non-empty query — retry with specific search terms.")
            print("  [SEARCH-TOOL] empty query — skipped (nudging model)", flush=True)
            continue
        _session = (fn.get("arguments", {}).get("session", "") or "").strip()
        hits = route_query(q, top_k=k, session=_session)
        if not hits:
            # FAISS can't see chunks stored unembedded (pending_embed) — e.g. a
            # just-chunked large input. Keyword search reads their text straight from
            # SQLite, so fall back to it instead of "no results", which otherwise makes
            # a diligent reasoning model grind search → empty.
            kw = _keyword_search(q, k)
            if kw:
                hits = [cid for _, cid in kw]
                print(f"  [SEARCH-TOOL] FAISS miss — keyword fallback found {len(hits)}", flush=True)
        trace.update(hits)
        if hits:
            lines = ["Search results from Mneme memory:"]
            for h in hits:
                cid = h  # route_query returns chunk_id strings, not tuples
                crow = db.execute("SELECT topic_label, grade, messages, source, trust FROM chunks WHERE chunk_id=?", (cid,)).fetchone()
                if crow:
                    label, grd, msgs_json = crow[0], crow[1], crow[2]
                    _src = crow[3] or ""
                    _trust = crow[4] or _compute_trust(_src, [])
                    _trusttag = "" if _trust == "verified" else " [UNVERIFIED]"
                    lines.append(f"[{cid} | G:{grd}{_trusttag}] {label}")
                    try:
                        msgs = json.loads(msgs_json)
                        for m in msgs[:5]:
                            c = m.get("content", "")[:MAX_PREVIEW_CHARS]
                            if c:
                                lines.append(f"  {m['role']}: {c}")
                    except Exception as e:
                        _log_error("search_tool:parse_msgs", e)
                lines.append("")
            result_texts.append("\n".join(lines[:30]))  # cap
            print(f"  [SEARCH-TOOL] found {len(hits)} results", flush=True)
        else:
            result_texts.append("No matching memories found.")
            print("  [SEARCH-TOOL] no results", flush=True)
    return "\n\n".join(result_texts), trace


def _query_retry_timeout(msgs, tools=None, timeout=None, options=None, max_tokens=None, attempts=None):
    """query_model with bounded retry + jittered exponential backoff (§7 fix #3).

    Replaces the single immediate retry: an instant retry re-hits the same
    overloaded window on a fresh TCP+TLS handshake (log: the instant retry
    failed identically, repeatedly). Hermes (jittered_backoff, 3 attempts) and
    OpenCode (10 attempts, Schedule.exponential + jitter) both back off; this
    follows the Hermes shape: RETRY_ATTEMPTS attempts, 2s/4s/8s exponential
    with jitter, provider Retry-After honored (capped at RETRY_AFTER_CAP).

    Failure classification (_provider_failure_retryable): "timeout" (no first
    token OR mid-stream stall — both incomplete) and transient errors (429,
    5xx, transport) retry; auth/billing/403, context overflow, and other
    deterministic 4xx do not.

    Keep-best: mid-stream stalls now return their PARTIAL content; the loop
    scores every attempt and returns the highest-scoring one when all retries
    fail, so a retry chain can never return LESS than the first partial
    (the mid-stream partial-discard fix). Timeout defaults to the
    backend-aware foreground budget (Ollama cold-start vs hosted anti-grind)."""
    if timeout is None:
        timeout = _main_chat_timeout()
    if attempts is None:
        attempts = max(1, RETRY_ATTEMPTS)
    best = None
    result = None
    for attempt in range(1, attempts + 1):
        result = query_model(msgs, tools=tools, timeout=timeout,
                             options=options, max_tokens=max_tokens)
        if not _provider_failure_retryable(result):
            _record_chat_status(result)
            return result
        # Retryable failure: remember the best partial so a failed chain
        # returns it rather than the last (possibly emptier) attempt.
        if best is None or _result_score(result) > _result_score(best):
            best = result
        if attempt < attempts:
            _ra = result.get("retry_after")
            _wait = _retry_backoff_delay(attempt, _ra)
            _why = (result.get("done_reason") or "?")
            _sc = result.get("status_code")
            _et = result.get("error_type")
            print(f"  [RETRY] provider failure ({_why}"
                  f"{' status=' + str(_sc) if _sc else ''}"
                  f"{' type=' + str(_et) if _et else ''})"
                  f" — attempt {attempt}/{attempts} failed; retrying in {_wait:.1f}s"
                  f"{' (Retry-After)' if _ra is not None else ''}", flush=True)
            # Sleep in small increments so a user Stop stays responsive.
            _sleep_until = time.time() + _wait
            while time.time() < _sleep_until:
                if _turn_cancel_event().is_set():
                    print("  [CANCEL] user stopped the turn — aborting retry wait", flush=True)
                    _final = best if best is not None else result
                    _record_chat_status(_final)
                    return _final
                time.sleep(min(0.25, max(0.0, _sleep_until - time.time())))
    print(f"  [RETRY] all {attempts} attempts failed — returning best partial "
          f"(content={len((best or {}).get('content') or '')}c)", flush=True)
    _final = best if best is not None else result
    _record_chat_status(_final)
    return _final


_SHRUG_TOKENS = {
    "none", "n/a", "na", "n-a", "...", "..", "…", "idk", "no", "nope",
    "??", "???", "?", "i don't know", "i dont know", "not found", "nothing",
    "unknown", "i give up", "cannot", "can't", "cant", "no idea", "no result",
    "not sure", "unsure", "dunno", "null", "empty", "none found",
}


def _is_near_empty(text):
    """True if a 'final answer' is effectively empty: blank, a shrug token
    ('None', '...', 'Idk', 'N/A'), or a bare <=4-char token. The model emits
    these when it gives up after a long failing struggle instead of answering —
    its reasoning says one thing ('I'll try a new search') but the output is a
    shrug. A real answer always carries more than a shrug.

    Trade-off: a legitimate terse answer ('Yes', '42', 'Paris' is 5 so it
    escapes) can also be caught; the fallback is the honest 'could not answer'
    message, which is preferable to presenting a give-up as an answer."""
    c = (text or "").strip().strip(".,!?;:\"'*_~`()[] \t\n")
    if not c:
        return True
    if c.lower() in _SHRUG_TOKENS:
        return True
    return len(c) <= 4


# Bounded "continue" prompts when the model gives up with a blank/shrug answer.
MAX_EMPTY_RETRY = 2


def _recent_window(messages: list, recent_turns: int, max_tokens: int = None) -> list:
    """Truncate a conversation to its most recent window.

    Keeps every system message (client prompt + Mneme's injected block) plus the
    last `recent_turns` user turns — each user message and everything after it up to
    the next user message. Older turns are left out; they are covered by the injected
    memory instead. If `max_tokens` is set, further trims the oldest non-system
    messages so the window also fits a token budget (large turns are evicted, not
    just old ones). Returns the original list unchanged when it is already small
    enough or `recent_turns` is <= 0 (disabled).
    """
    if recent_turns <= 0:
        window = messages
    else:
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        nonsys = [m for m in messages if m.get("role") != "system"]
        user_idxs = [i for i, m in enumerate(nonsys) if m.get("role") == "user"]
        if len(user_idxs) <= recent_turns:
            window = messages
        else:
            start = user_idxs[-recent_turns]
            window = sys_msgs + nonsys[start:]
    if max_tokens and max_tokens > 0:
        window = _trim_messages_to_tokens(window, max_tokens)
    return window


def _compact_followup(followup: list, max_tokens: int, head_len: int) -> list:
    """Bound the tool-loop followup to `max_tokens` (estimated).

    `head_len` is the number of leading messages that form the conversation
    prefix (system + recent turns); everything after is the current turn's
    tool-loop entries (native/registry/search results appended each round).
    Keeps the prefix + the most recent tool entries, dropping the oldest so the
    total fits under `max_tokens`. If the prefix alone exceeds the budget, the
    prefix's oldest turns are trimmed too. Dropped results are recoverable — their
    full text is staged in memory (search_memory) or re-fetchable — so this is
    truncation, not summarization. Orphaned tool-result messages (whose assistant
    tool call was dropped) are also removed so the model never sees a result with
    no call.
    """
    if max_tokens <= 0:
        return followup
    head = followup[:head_len]
    tool = followup[head_len:]
    head_tokens = sum(_msg_tokens(m) for m in head)
    if head_tokens > max_tokens:
        # Prefix alone over budget (rare now the window is token-bounded): trim
        # the prefix's oldest turns and drop all tool entries.
        return _trim_messages_to_tokens(head, max_tokens)
    kept = []
    tokens = head_tokens
    for m in reversed(tool):
        mt = _msg_tokens(m)
        if tokens + mt > max_tokens and kept:
            break
        kept.insert(0, m)
        tokens += mt
    # Drop orphaned tool-result messages left at the front of the kept tail.
    while kept and kept[0].get("role") == "tool":
        kept = kept[1:]
    return head + kept


def process_chat(messages: list, session_id: str = "default", tools: list = None,
                 options: dict = None, max_tokens: int = None) -> dict:
    # Harness-run session ids are "run:<run_id>" — when set, publish this turn's
    # tool calls to the run's live stream buffer so the chat can watch them fire.
    _run_id = session_id[4:] if (session_id or "").startswith("run:") else None
    # Extract the retrieval query from ONLY the last user message. Scoping retrieval
    # to the current turn means a follow-up ("try again", a correction) doesn't
    # re-surface chunks matched by earlier turns' keywords — which was re-injecting
    # the model's own wrong answer on every retry.
    user_msgs = [_extract_text(m["content"])[:MAX_QUERY_CHARS] for m in reversed(messages)
                 if m.get("role") == "user"][:1]  # last user turn only
    user_msg = " ".join(reversed(user_msgs))  # chronological order
    
    # Full (untruncated) last user message — used for <<COMMAND>> detection
    # so long prompts with a closing ">>" are not cut off by the 500-char truncation.
    full_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            full_user_msg = _extract_text(m.get("content", ""))
            break

    # Raw (unflattened) last user message content — carries any images the user
    # attached, which _extract_text would otherwise collapse to a "[IMAGE: url]"
    # placeholder. Persisted to the content-addressed image store on archive.
    _raw_last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            _raw_last_user = m.get("content", "")
            break
    
    # ── Detail: load full chunk if DETAIL tag found ──
    # Scan last message regardless of role (model may output DETAIL in response)
    last_msg = _extract_text(messages[-1].get("content", "")) if messages else ""
    detail_match = _detail_re.search(last_msg)
    if detail_match:
        chunk_id = detail_match.group(1).strip()
        print(f"  [DETAIL] Loading chunk {chunk_id}", flush=True)
        chunk = load_chunk(chunk_id)
        if chunk:
            parts = []
            for m in chunk.get("messages", []):
                r = m["role"]
                c = m["content"][:MAX_MESSAGE_STORE]
                parts.append(f"{r}: {c}")
            full_text = "\n".join(parts)
            print(f"  [DETAIL] Returned {len(full_text)} chars", flush=True)
            return {"content": full_text[:MAX_DETAIL_CHARS], "tool_calls": [], "eval_count": 0, "done_reason": "detail"}
        else:
            return {"content": f"Chunk {chunk_id} not found.", "tool_calls": [], "eval_count": 0, "done_reason": "detail"}

    # ── Settings view: <<SETTINGS>> ──
    # Prints the EFFECTIVE values (post template / file / env resolution) so the
    # user can see what is actually in force — not what they think they set.
    if _chatcmd.SETTINGS_CMD_RE.search(full_user_msg):
        report = _chatcmd.format_settings(_settings_snapshot())
        _cmd_re_early = re.compile(r"<<[A-Z_]+(?:\s+[^>]+)?>>")
        cleaned = _cmd_re_early.sub("", full_user_msg).strip()
        messages[-1]["content"] = cleaned or "(settings requested)"
        print("  [SETTINGS] reported current effective settings", flush=True)
        return {"content": report, "tool_calls": [], "eval_count": 0, "done_reason": "settings"}

    # ── Harness control commands: /status, /runs, /approve ... (user-typed, never the model) ──
    if full_user_msg.strip().startswith("/"):
        _hreply = _harness_command(full_user_msg.strip())
        if _hreply is not None:
            print(f"  [HARNESS-CMD] {full_user_msg.strip()[:60]}", flush=True)
            return {"content": _hreply, "tool_calls": [], "eval_count": 0, "done_reason": "command"}

    # ── Retrieval threshold: <<RETRIEVAL ...>> ──
    # <<RETRIEVAL>>            -> show the retrieval section
    # <<RETRIEVAL k=v [k=v]>>  -> set keys (written to mneme.yaml, hot-reloaded)
    # <<RETRIEVAL reset>>      -> re-read the config file, discarding live edits
    _retr_m = _chatcmd.RETRIEVAL_CMD_RE.search(full_user_msg)
    if _retr_m:
        _arg = (_retr_m.group(1) or "").strip()
        _cmd_re_early = re.compile(r"<<[A-Z_]+(?:\s+[^>]+)?>>")
        cleaned = _cmd_re_early.sub("", full_user_msg).strip()
        messages[-1]["content"] = cleaned or "(retrieval command)"
        if not _arg:
            snap = _settings_snapshot()
            body = "\n".join(f"  {k:26} {v}" for k, v in (snap.get("retrieval") or {}).items())
            out = ("=== RETRIEVAL SETTINGS ===\n\n" + body +
                   "\n\nChange one:  <<RETRIEVAL inject_min_similarity=0.60>>"
                   "\nReload file: <<RETRIEVAL reset>>")
            return {"content": out, "tool_calls": [], "eval_count": 0, "done_reason": "retrieval"}
        if _arg.lower() == "reset":
            # Force a re-read of the config file and re-derive the constants.
            _force_config_reload()
            snap = _settings_snapshot()
            body = "\n".join(f"  {k:26} {v}" for k, v in (snap.get("retrieval") or {}).items())
            return {"content": "Reloaded retrieval settings from the config file:\n\n" + body,
                    "tool_calls": [], "eval_count": 0, "done_reason": "retrieval"}
        try:
            assignments = _chatcmd.parse_set_assignments(_arg)
            summary = _chatcmd.update_config_file(CONFIG_PATH, "retrieval", assignments)
            # Apply immediately (don't wait for the mtime poll) so the user's next
            # message uses the new value.
            _force_config_reload()
            lines = [f"Updated: {summary}", ""]
            snap = _settings_snapshot()
            for k in assignments:
                lines.append(f"  {k:26} {snap.get('retrieval', {}).get(k)}")
            lines += ["", "Written to mneme.yaml and applied now (survives restart).",
                      "Restore the file's values with: <<RETRIEVAL reset>>"]
            print(f"  [RETRIEVAL] set {summary}", flush=True)
            return {"content": "\n".join(lines), "tool_calls": [], "eval_count": 0,
                    "done_reason": "retrieval"}
        except _chatcmd.CommandError as e:
            return {"content": f"retrieval command error: {e}", "tool_calls": [],
                    "eval_count": 0, "done_reason": "retrieval"}
        except Exception as e:
            _log_error("chatcmd:retrieval", e)
            return {"content": f"retrieval command failed: {type(e).__name__}: {e}",
                    "tool_calls": [], "eval_count": 0, "done_reason": "retrieval"}

    # ── Save trigger: <<SAVE>> forces archive ──
    SAVE_TRIGGER = "<<SAVE>>"
    if SAVE_TRIGGER in full_user_msg:
        user_msg = user_msg.replace(SAVE_TRIGGER, "").strip()
        if not user_msg:
            user_msg = "Memory save triggered."
        messages[-1]["content"] = user_msg
        _enqueue(archive_staging, list(_last_injected_ids))
        print("  [SAVE] Triggered by user — archiving in background", flush=True)

    # ── Learn trigger: <<LEARN problem:...>> runs learning mode (disabled in memory-only) ──
    learn_match = _learn_re.search(full_user_msg)
    if learn_match and not MEMORY_ONLY:
        learn_problem = learn_match.group(1).strip()
        user_msg = _learn_re.sub("", user_msg).strip()
        if not user_msg:
            user_msg = "Learning mode was triggered."
        messages[-1]["content"] = user_msg
        _enqueue(_run_learning_mode, learn_problem, 5)
        print(f"  [LEARN] Triggered via <<LEARN>>: {learn_problem[:80]}", flush=True)

    # Strip all <<COMMANDS>> from user messages — both the current turn and any
    # echoed history. Always apply even when the message is ONLY a command: the
    # old `if cleaned:` guard skipped empty results and left a bare "<<SAVE>>"
    # in the history, which the client echoes back on the next turn so the model
    # sees the raw command.
    _cmd_re = re.compile(r"<<[A-Z_]+(?:\s+[^>]+)?>>")
    for m in messages:
        if m.get("role") == "user":
            raw = _extract_text(m.get("content", ""))
            if _cmd_re.search(raw):
                m["content"] = _cmd_re.sub("", raw).strip()
    user_msgs2 = [_extract_text(m["content"])[:MAX_QUERY_CHARS] for m in reversed(messages) if m.get("role") == "user"][:1]
    user_msg = " ".join(reversed(user_msgs2))

    # Full tool list = read-only server tools (search_memory/list_tools/read_tool)
    # + native bootstrap (bash/write, flag-gated) + client passthrough, deduped.
    msg_tools = [t for t in mntools.assemble_tools(tools)
                 if _turn_tool_ok((t.get("function") or {}).get("name", ""))]
    # Convert OpenAI-format tool_calls to Ollama format in incoming messages
    for m in messages:
        for tc in m.get("tool_calls", []):
            fn = tc.get("function", {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    fn["arguments"] = json.loads(args)
                except Exception as e:
                    _log_error("process_chat:tool_args_parse", e)
    
    # Multi-pass compression: replace large tool outputs with model summaries
    # This prevents the model from burning its entire predict budget on raw HTML/JSON
    messages = compress_large_tool_results(messages)
    
    # Advance chunked tool output if user said "continue"
    messages = _advance_chunk(messages)
    
    # Phase 4: learn explicit user-preference signals before building context,
    # so newly-stored preferences are injected this same turn.
    _store_preferences(_detect_preferences(user_msg))

    # Flush staged turns from PRIOR requests BEFORE building context, so a fact
    # saved on the previous turn is retrievable this same turn. The archive was
    # previously deferred (backgrounded) to the END of the request, which added a
    # one-turn recall delay: "remember X" then immediately "what's X?" missed the
    # just-saved chunk because it wasn't embedded/indexed in FAISS yet.
    # Synchronous now so the retrieval below sees the freshly-archived chunks.
    if staging.should_flush():
        try:
            archive_staging(list(_last_injected_ids))
        except Exception as e:
            _log_error("process_chat:pre_context_flush", e)

    # Build injected memory (chunks + budget; the fixed system prompt is added to
    # the system message below via _system_prompt_block() so it stays cacheable).
    context, ptype = build_context(user_msg, session_id)
    cur_ptype = _classify_problem_type(user_msg)
    
    # Insert Mneme's FIXED instruction block as a system message after Hermes.
    # ONLY _system_prompt_block() + _meta_principles_block() go here — both are
    # constant, so this stays a stable, cacheable prefix. The VARIABLE advisory
    # directives (saved-tool hint, explore, relevant tools) + memory + preferences
    # go to the TAIL (prepended to the last user message) alongside the memory
    # context.
    # A KNOWN capability edge is NOT injected here — it routes into the hard-stop
    # overcome path below, because the point of flagging an edge is to OVERCOME
    # it, not name it and stop.
    mneme_system = _system_prompt_block()
    if not MEMORY_ONLY:
        mneme_system += _meta_principles_block()
    _tool_injection = mntools.inject_relevant_tools(user_msg)
    if _tool_injection:
        print("  [TOOL-INJECT] injected relevant built tools", flush=True)
    _advisory = [p for p in (
        _tool_directive(db, cur_ptype),
        _explore_directive(full_user_msg),
        _tool_injection,
    ) if (p or "").strip()]
    dynamic_tail = "\n\n".join(_advisory)
    _stuck, _stuck_reason = _detect_stuck(messages)
    # In memory-only mode there is no capability-edge tracking / overcome /
    # build-reuse loop, so force these off and fall through to the plain nudge.
    _is_edge = _is_capability_edge(cur_ptype) if not MEMORY_ONLY else False
    _in_build = _in_build_mode(messages) if not MEMORY_ONLY else False
    _in_reuse = _in_reuse_mode(messages) if not MEMORY_ONLY else False
    # A KNOWN capability edge routes straight into overcome mode (hard stop) — the
    # point of flagging an edge is to overcome it (build/reuse a tool), not just to
    # name it and stop. Stuck-now and known-edge share the same deliberation gate.
    _deliberate = (_stuck or _is_edge) and not _in_build and not _in_reuse
    if _in_build:
        _calls = _build_tool_calls(messages)
        if _calls >= BUILD_MAX_TOOL_CALLS:
            mneme_system += "\n\n" + _build_exhausted_directive(BUILD_MAX_ITERATIONS)
            print(f"  [BUILD-EXHAUSTED] {_calls} build tool calls — ending build loop", flush=True)
        else:
            mneme_system += "\n\n" + _build_directive(_calls + 1, BUILD_MAX_TOOL_CALLS)
            print(f"  [BUILD] build step {_calls + 1}/{BUILD_MAX_TOOL_CALLS}", flush=True)
    elif _in_reuse:
        _rname, _rpath = _reuse_tool_info(messages, db)
        mneme_system += "\n\n" + _reuse_directive(_rname, _rpath)
        print(f"  [REUSE] run existing tool '{_rname}'", flush=True)
    elif _deliberate:
        if _is_edge:
            mneme_system += "\n\n" + _capability_directive(cur_ptype)
            print(f"  [OVERCOME] known edge '{cur_ptype}' — hard stop, tools removed", flush=True)
        else:
            mneme_system += "\n\n" + _overcome_directive(cur_ptype, _stuck_reason)
            print(f"  [OVERCOME] {_stuck_reason} — hard stop, tools removed", flush=True)
    else:
        _nudge = _tool_failure_nudge(messages)
        if _nudge:
            mneme_system += "\n\n" + _nudge
            print(f"  [TOOL-NUDGE] {_nudge[:60]}...", flush=True)
    insert_at = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            insert_at = i + 1
            break
    messages.insert(insert_at, {"role": "system", "content": mneme_system})
    # Inject the VARIABLE memory context + advisory directives as a SEPARATE
    # system message, placed immediately before the last user message. This keeps
    # the [system prompt + conversation] prefix stable and cacheable (the variable
    # memory still sits after the fixed system prompt), while giving the model a
    # hard role boundary: the injected system message is reference/context, the
    # user message is the actual question. (The old approach prepended the memory
    # INTO the user message, separated only by a "---" line, which blurred "current
    # message" vs "injected past context" and made the model echo the embedded
    # user:/assistant: dialogue under load.)
    _tail_parts = [p for p in (context, dynamic_tail) if (p or "").strip()]
    tail = "\n\n".join(_tail_parts)
    if tail.strip():
        # Guard against double-injection: if a system message already carries the
        # memory disclaimer or the budget line, don't insert a second one (context
        # would double each round of the tool loop / echoed-back client message).
        _already = any(
            m.get("role") == "system"
            and ("--- MEMORY:" in (m.get("content") or "")
                 or "[context budget:" in (m.get("content") or ""))
            for m in messages
        )
        if _already:
            print("  [INJECT-TAIL] already injected — skipping", flush=True)
        else:
            _last_user_idx = None
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    _last_user_idx = i
                    break
            if _last_user_idx is not None:
                messages.insert(_last_user_idx, {"role": "system", "content": tail})
                print(f"  [INJECT-TAIL] {len(tail)} chars injected as system message before last user message", flush=True)
    # Truncate the conversation to a recent window (staging_turns + extra) instead of
    # re-injecting the full transcript every round. Older turns are covered by the
    # injected memory (build_context above); the recent window only carries the
    # immediate thread for continuity.
    _recent_turns = STAGING_TURNS + CONTEXT_RECENT_EXTRA
    full_msgs = _recent_window(messages, _recent_turns, max_tokens=_window_token_budget())
    
    # Optional debug dump of the system messages (off by default; set
    # MNEME_DEBUG_DUMP=1 to enable). Was previously an unconditional write to
    # the hardcoded /workspace/sys_dump.txt, which breaks local runs.
    if os.environ.get("MNEME_DEBUG_DUMP") == "1":
        try:
            with open("/tmp/mneme_sys_dump.txt", "w") as f:
                for m in full_msgs:
                    if m.get("role") == "system":
                        f.write("=== SYSTEM MSG ===\n")
                        f.write(m["content"][:600])
                        f.write("\n...\n")
        except Exception:
            pass
    result = _query_retry_timeout(full_msgs, tools=([] if _deliberate else msg_tools),
                                  timeout=_main_chat_timeout(), options=options, max_tokens=max_tokens)
    # Anti-grind / empty-reply guardrail: the bounded retry loop above has
    # already re-queried any transient provider failure (timeout/429/5xx) with
    # backoff and kept the best partial, so what remains here is only the
    # empty-reply nudge (model bug, not transport) and the grind guard.
    _failed = False
    if not (result.get("content") or "").strip() and not result.get("tool_calls"):
        dr = result.get("done_reason", "?")
        if (dr == "timeout" and not result.get("eval_count")) or dr == "error":
            # Retries exhausted with zero tokens — a genuine provider outage.
            # Fall through to the capability-edge message (no more retries).
            print(f"  [RETRY] provider failure persisted after {RETRY_ATTEMPTS} attempts ({dr})", flush=True)
            _failed = True
        elif dr == "timeout":
            # Grind guardrail: generation exceeded budget — retrying would just
            # grind again. Fall through to the capability-edge message.
            print(f"  [GRIND] generation exceeded {_main_chat_timeout()}s — capability edge, no retry", flush=True)
            _failed = True
        else:
            print(f"  [EMPTY] empty reply (done_reason={dr}) — retrying once", flush=True)
            _retry = [m for m in full_msgs if m.get("role") != "system"]
            _retry.append({"role": "user", "content": "(Your previous reply was empty. Give a direct answer now.)"})
            result = query_model(_retry, tools=msg_tools, timeout=_main_chat_timeout())
    if not (result.get("content") or "").strip() and not result.get("tool_calls"):
        result["content"] = ("[The model returned an empty response and could not answer. "
                             "This is a possible capability edge — flag for tool-building.]")
        _failed = True
    
    # Handle search_memory tool calls — execute server-side, then RE-QUERY the
    # model with the results so it synthesizes a tagged answer (not raw hits).
    # Non-search_memory tool calls (web_search, shell) pass through to the client.
    #
    # This is a LOOP, not a single pass: the model frequently needs more than one
    # round of memory search before it either answers or hands off to another
    # tool. Invariant: every tool call the model emits ends up EITHER resolved
    # here (search_memory) OR forwarded to the client (everything else). The old
    # code did `result["tool_calls"] = remaining_calls`, which overwrote the
    # re-query's tool calls with the FIRST query's non-search calls and silently
    # dropped a follow-up call — grading the turn F.
    _trace_search_chunks = set()
    passthrough_calls = []
    _build_calls = 0  # native WRITE executions this turn (bounded by BUILD_MAX_ITERATIONS)
    _MAX_SERVER_ROUNDS = int(os.environ.get("MNEME_MAX_SERVER_ROUNDS", MAX_SERVER_ROUNDS))  # round ceiling — config caps.max_server_rounds overrides
    _REDUNDANCY_LIMIT = 4  # identical tool-call signature this many rounds in a row = stuck loop
    _native_names = mntools.native_exec_names(tools)  # {"bash","write"} when native
    _readonly_names = mntools.enabled_readonly_names()  # per-tool flags applied
    # Curation tools (flag_bad_memory / clear_bad_memory_flag / remove_memory) execute
    # SERVER-side like the read-only registry tools — they must be in this set or their
    # calls fall into
    # `other_calls` and get passed through to the client instead of run, which
    # showed up as an empty answer plus "passing tool call through to client".
    _readonly_names = _readonly_names | {t["function"]["name"] for t in mntools.enabled_curation_tools()}
    _mcp_names = mntools.mcp_tool_names() - _readonly_names - _native_names  # MCP tools (shadowed on name collision)
    # Harness permission grant: an ungranted tool is not executed server-side even if
    # the model names it anyway (it falls through to passthrough -> step failure).
    _native_names = {n for n in _native_names if _turn_tool_ok(n)}
    _readonly_names = {n for n in _readonly_names if _turn_tool_ok(n)}
    _mcp_names = {n for n in _mcp_names if _turn_tool_ok(n)}
    _server_names = _readonly_names | _native_names | _mcp_names
    _tool_trace = []  # debug: server-side tool activity surfaced to the client
    _tool_rounds = 0  # server-side tool executions this turn (for the wrap-up nudge)
    _nudged = False   # one-time wrap-up nudge sent
    _seen_sigs = set()   # tool-call signatures seen this turn (a write clears them)
    _redundant = 0       # repeat calls this turn (grinding signal — triggers the hard stop)
    _bash_resources = {}  # resource key -> set of distinct bash sigs (structural-grind signal)
    _script_nudged = False  # one-time "write a script" nudge sent
    _step_back_level = 0   # step-back ladder rung reached this turn (0 = none yet)

    def _bash_resource_key(command):
        """Coarse grouping key for a bash command: which resource is it touching?
        Used to detect many DIFFERENT calls on the SAME target (extracting one field
        at a time) — the write-a-script signal, as opposed to true redundancy."""
        m = re.search(r"https?://[^\s'\"|&]+", command)
        if m:
            return "url:" + m.group(0).rstrip("/")
        m = re.search(r"\b(grep|cat|head|tail|sed|awk|python3?)\b[^\n]*?\b([A-Za-z0-9_./~-]+\.(?:html?|json|txt|py|csv|xml|md))\b", command)
        if m:
            return "file:" + m.group(2)
        toks = command.split()
        return "cmd:" + (toks[0] if toks else command)

    def _mark_call(nm, args):
        """Track a tool call for the redundancy stop and the structural (write-a-
        script) nudge. A `write` invalidates all prior signatures (the script
        changed, so re-running bash is legitimate). Any other repeated call counts
        toward the redundancy hard-stop; many distinct bash calls on one resource
        count toward the write-a-script nudge."""
        nonlocal _redundant
        if nm == "write":
            _seen_sigs.clear()
            _bash_resources.clear()
            return
        _sig = f"{nm}:{json.dumps(args, sort_keys=True)}"
        if _sig in _seen_sigs:
            _redundant += 1
        else:
            _seen_sigs.add(_sig)
        if nm == "bash":
            _rk = _bash_resource_key(str(args.get("command", "")) if isinstance(args, dict) else "")
            _bash_resources.setdefault(_rk, set()).add(_sig)

    def _trace(tool, args, res, t0, blocked=False):
        """Compact entry for the tool trace (truncate long args/results)."""
        a = {}
        for k, v in (args or {}).items():
            if isinstance(v, str) and len(v) > 160:
                a[k] = v[:160] + f"... ({len(v)} chars)"
            else:
                a[k] = v
        return {
            "tool": tool,
            "args": a,
            "result": ("" if res is None else (res if isinstance(res, str) else str(res)))[:600],
            "elapsed_ms": int((time.time() - t0) * 1000),
            "blocked": bool(blocked),
        }

    # Accumulate tool history across rounds (NOT rebuilt from full_msgs each
    # round). If each round only shows the model the latest tool result, it
    # forgets what it already gathered and re-fetches — the grinding we see.
    followup = list(full_msgs)
    _followup_head_len = len(followup)  # conversation prefix boundary — tool-loop entries append after this
    _continue_attempts = 0
    _last_round_sig = None  # redundancy-stop state: (name,args) signature of the last round's tool calls
    _repeat_streak = 0

    for _round in range(_MAX_SERVER_ROUNDS):
        if _turn_cancel_event().is_set():
            # User hit "Stop" between rounds — end the turn immediately.
            result["done_reason"] = "cancelled"
            break
        # (The old context-size hard-stop that forced synthesis at 50K chars is
        # gone: the followup is now compacted to the token input budget
        # (_context_input_budget) before every re-query below, so it never bloats
        # past num_ctx - reserve. MAX_SERVER_ROUNDS remains the tool-work ceiling.)
        tcs = result.get("tool_calls") or []
        search_calls = [tc for tc in tcs if tc.get("function", {}).get("name") == "search_memory" and "search_memory" in _readonly_names]
        registry_calls = [tc for tc in tcs if tc.get("function", {}).get("name") in (_readonly_names - {"search_memory"})]
        native_calls = [tc for tc in tcs if tc.get("function", {}).get("name") in _native_names]
        mcp_calls = [tc for tc in tcs if tc.get("function", {}).get("name") in _mcp_names]
        other_calls = [tc for tc in tcs if tc.get("function", {}).get("name") not in _server_names]
        passthrough_calls.extend(other_calls)

        # Redundancy stop: if the model repeats the IDENTICAL tool call(s) — same
        # names + same arguments — REDUNDANCY_LIMIT rounds in a row, it is stuck in
        # a loop (e.g. re-running `bash echo done` and never answering). Break out
        # and force a text answer instead of grinding to MAX_SERVER_ROUNDS and
        # returning an empty response. The _recent_attempts_summary below already
        # SHOWS the model its own history; this is the hard backstop for models
        # that ignore that summary and keep re-issuing the same call.
        _server_tcs = search_calls + registry_calls + native_calls + mcp_calls
        if _server_tcs:
            _sig = tuple(sorted(
                (tc.get("function", {}).get("name", ""),
                 json.dumps(tc.get("function", {}).get("arguments", {}) or {}, sort_keys=True))
                for tc in _server_tcs
            ))
            if _sig == _last_round_sig:
                _repeat_streak += 1
            else:
                _repeat_streak = 1
                _last_round_sig = _sig
            if _repeat_streak >= _REDUNDANCY_LIMIT:
                print(f"  [REDUNDANCY] identical tool call {_repeat_streak}x in a row — stopping loop", flush=True)
                followup.append({"role": "user", "content":
                    "You have repeated the same tool call several times in a row. Stop calling "
                    "tools and give your final answer now, based on what you already have."})
                result = _query_retry_timeout(followup, tools=[])
                break
        else:
            _repeat_streak = 0
            _last_round_sig = None

        if not (search_calls or registry_calls or native_calls or mcp_calls):
            # No server tool calls. If the model gave up (blank/shrug answer),
            # prompt it to CONTINUE instead of ending the turn — bounded retries.
            # (Infra timeouts/errors land in the fallback below, not here.)
            if (_is_near_empty(result.get("content") or "")
                    and _continue_attempts < MAX_EMPTY_RETRY
                    and result.get("done_reason") not in ("timeout", "error", "cancelled")):
                _continue_attempts += 1
                _near = (result.get("content") or "").strip()
                print(f"  [CONTINUE] near-empty answer ({_near!r}) — prompting model to continue "
                      f"({_continue_attempts}/{MAX_EMPTY_RETRY})", flush=True)
                followup.append({"role": "user", "content": _load_instruction("empty_answer_retry")})
                result = _query_retry_timeout(followup, tools=msg_tools)
                continue
            break
        # A thinking model narrates its next step ("let me check the date") in
        # `content` while ALSO emitting the tool_calls for that step. Only treat
        # content as the final answer when the model actually finished (stop) AND
        # there is real text AND there are no pending tool calls left to execute.
        # Some models (e.g. Qwen3.6-35B "Uncensored-Aggressive") report
        # done_reason="stop" even while emitting a tool call — with EITHER empty or
        # narrated content — so breaking on "stop" alone would drop the tool call
        # and lose the answer. Break only when there is nothing left to run.
        if (result.get("done_reason") == "stop" and (result.get("content") or "").strip()
                and not result.get("tool_calls")):
            break

        # Native bash/write. `write` is bounded by BUILD_MAX_ITERATIONS (the build
        # loop); exploratory `bash` is NOT counted against the build budget — it is
        # bounded by MAX_SERVER_ROUNDS and the redundancy stop instead. This is the
        # fix for "scrape six different sites" being wrongly cut off as "build loop
        # exhausted."
        if native_calls:
            _writes = [tc for tc in native_calls if tc.get("function", {}).get("name") == "write"]
            _budget_blocked = bool(_writes) and _build_calls >= BUILD_MAX_ITERATIONS

            if _budget_blocked:
                # Write budget spent: force the model to declare the edge. Only the
                # writes are blocked; any bash in the same round still runs below.
                followup.append({"role": "user", "content": _build_exhausted_directive(BUILD_MAX_ITERATIONS)})
                for tc in _writes:
                    _tool_trace.append(_trace("write", tc["function"].get("arguments", {}) or {},
                                              "build budget exhausted — not executed", time.time(), blocked=True))
                print(f"  [BUILD-EXHAUSTED] write budget ({BUILD_MAX_ITERATIONS}) reached", flush=True)
                _exec = [tc for tc in native_calls if tc.get("function", {}).get("name") == "bash"]
            else:
                _build_calls += len(_writes)
                _exec = native_calls

            if _exec:
                followup.append({"role": "assistant", "content": None, "tool_calls": _exec})
                for tc in _exec:
                    nm = tc["function"]["name"]
                    args = tc["function"].get("arguments", {}) or {}
                    _mark_call(nm, args)
                    _t0 = time.time()
                    res = mntools.execute_native_tool(nm, args)
                    if _run_id:
                        _run_live.publish(_run_id, "tool_call", {
                            "tool": nm, "args": args, "result": res,
                            "elapsed_ms": int((time.time() - _t0) * 1000),
                        })
                    _stage_tool_result(res, nm, args)
                    _tool_trace.append(_trace(nm, args, res, _t0))
                    print(f"  [NATIVE-TOOL] {nm} -> {res[:90]!r}", flush=True)
                    followup.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": _truncate_tool_result(res)})
                _tool_rounds += len(_exec)

        # search_memory: user-message feedback (Muse-template workaround — the
        # Ollama path drops a "tool" role message and trips the peg grammar).
        if search_calls:
            _t0 = time.time()
            _tool_rounds += 1
            tool_result, _trace_chunks = _execute_search_tool_calls(search_calls)
            _trace_search_chunks.update(_trace_chunks)
            for tc in search_calls:
                args = tc["function"].get("arguments", {}) or {}
                _mark_call("search_memory", args)
                _tool_trace.append(_trace("search_memory", args, tool_result, _t0))
            followup.append({"role": "user", "content": "search_memory results:\n" + _truncate_tool_result(tool_result)})

        # Registry tools (list_tools/read_tool/read_image): user-message feedback.
        for tc in registry_calls:
            nm = tc["function"]["name"]
            args = tc["function"].get("arguments", {}) or {}
            _mark_call(nm, args)
            _t0 = time.time()
            _tool_rounds += 1
            res = mntools.execute_readonly_tool(nm, args)
            if nm == "read_image":
                # Image re-view: the result is a data URL. Attach it as a REAL image
                # block (not text) so a vision model actually sees it; stage a short
                # note (the base64 itself is not worth indexing into memory).
                _stage_tool_result("[read_image: image viewed]", nm, args)
                _tool_trace.append(_trace(nm, args, "[read_image: image viewed]", _t0))
                if res.startswith("data:"):
                    followup.append({"role": "user", "content": [
                        {"type": "text", "text": "read_image result (the stored image):"},
                        {"type": "image_url", "image_url": {"url": res}},
                    ]})
                else:
                    followup.append({"role": "user", "content": f"read_image result:\n{res}"})
                print(f"  [IMAGE-TOOL] read_image -> {res[:60]}...", flush=True)
                continue
            # Stage tool results into memory so their full text survives followup
            # compaction: fetched pages go in as page:<domain> chunks, everything
            # else as tool:<name> chunks. The model only ever sees a bounded
            # head+tail window below, so the full text must be retrievable.
            if nm == "fetch_url":
                _stage_page_content(res, (args or {}).get("url", ""))
            else:
                _stage_tool_result(res, nm, args)
            _tool_trace.append(_trace(nm, args, res, _t0))
            _label = "WEB-SEARCH" if nm == "web_search" else "TOOL-REGISTRY"
            print(f"  [{_label}] {nm} -> {res[:90]!r}", flush=True)
            followup.append({"role": "user", "content": f"{nm} result:\n{_truncate_tool_result(res)}"})

        # MCP tools (dynamic servers) — same user-message feedback as registry tools.
        if mcp_calls:
            for tc in mcp_calls:
                nm = tc["function"]["name"]
                args = tc["function"].get("arguments", {}) or {}
                _mark_call(nm, args)
                _t0 = time.time()
                _tool_rounds += 1
                res = mntools.call_mcp_tool(nm, args)
                _stage_tool_result(res, nm, args)
                _tool_trace.append(_trace(nm, args, res, _t0))
                print(f"  [MCP] {nm} -> {res[:90]!r}", flush=True)
                followup.append({"role": "user", "content": f"{nm} result:\n{_truncate_tool_result(res)}"})

        # (Mid-loop interruption machinery removed: write-script nudge, redundancy
        # hard-stop, step-back ladder, and wrap-up nudge. These injected coaching
        # messages interrupted the model's natural tool use and were misfiring. The
        # loop now simply runs the model's tool calls until it produces a final
        # answer or hits the MAX_SERVER_ROUNDS cap.)

        # Compact tool-state summary (suggestion #2): show the model what it just
        # tried and the outcome, so it doesn't repeat a call that already failed.
        # This is STATE, not a directive — the model still decides its own next step.
        _state = _recent_attempts_summary(_tool_trace)
        if _state:
            followup.append({"role": "user", "content": _state})

        # Compact the followup BEFORE re-querying so it never exceeds the budget.
        # Bounded working set: drop the oldest tool results, keep the recent ones
        # (full text is in memory / re-fetchable). This is what lets a weeks-long
        # conversation keep tooling without overflowing the context window.
        _budget = _context_input_budget()
        _ctx_tokens = sum(_msg_tokens(m) for m in followup)
        if _ctx_tokens > _budget:
            followup = _compact_followup(followup, _budget, _followup_head_len)
            _new_tokens = sum(_msg_tokens(m) for m in followup)
            print(f"  [COMPACT] followup {_ctx_tokens} -> {_new_tokens} tokens (budget {_budget})", flush=True)

        print(f"  [SYNTHESIS] re-querying model "
              f"({len(search_calls)} search, {len(registry_calls)} registry, {len(native_calls)} native, {len(mcp_calls)} mcp)", flush=True)
        result = _query_retry_timeout(followup, tools=msg_tools)
    else:
        # Ran out of server rounds (model kept calling server tools without
        # answering). Resolve any remaining search_memory server-side; native/
        # registry calls that never converged are dropped (bounded loop).
        _final_search = [tc for tc in (result.get("tool_calls") or [])
                         if tc.get("function", {}).get("name") == "search_memory"]
        if _final_search and not (result.get("content") or "").strip():
            _tool_result, _trace = _execute_search_tool_calls(_final_search)
            _trace_search_chunks.update(_trace)
            result["content"] = _tool_result

    result["tool_calls"] = passthrough_calls
    result["tool_trace"] = _tool_trace

    # Empty-answer fallback for the LOOP path (infrastructure failures only: a
    # synthesis re-query that stalls or errors). A blank/shrug answer from the
    # MODEL (not an infra failure) is handled by the CONTINUE retry in the tool
    # loop; if those retries are exhausted we return the model's output as-is
    # (no "it quit" boilerplate — the user can see the model gave up).
    if not (result.get("content") or "").strip() and not result.get("tool_calls"):
        _why = result.get("done_reason") or "unknown"
        if _why == "cancelled":
            result["content"] = "[Stopped by user.]"
            print("  [CANCEL] turn stopped by user — no final answer", flush=True)
        else:
            if _why == "timeout":
                result["content"] = ("[The reply stalled — the model provider stopped responding "
                                     "mid-generation and the automatic retry also timed out. "
                                     "Please try again, or ask a more focused question.]")
            elif _why == "error":
                result["content"] = (f"[The model provider returned an error "
                                     f"({result.get('error_type', 'unknown')}); the retry also failed. "
                                     "Please try again.]")
            else:
                result["content"] = "[The model returned an empty response and could not answer. Please try again.]"
            _failed = True
            print(f"  [EMPTY-ANSWER] loop/synthesis ended empty (done_reason={_why}) — returned explanatory message", flush=True)
    
    # Whether the failure (if any) was an infrastructure timeout rather than a
    # genuine model mistake. Timeouts carry no introspectable lesson, so the
    # strategy layer must not try to extract a directive from them.
    _infra_failure = result.get("done_reason") == "timeout"

    # Grade by provenance honesty — pass/fail/great, deterministic from the
    # model's own inline [source:]/[guess] tags plus the trace. No second judge
    # call on the hot path; only when the model asserted specific facts but
    # emitted no tags do we fall back to the slow _extract_provenance judge.
    _resp_content = result.get("content", "") or ""
    _was_edge = _is_capability_edge(cur_ptype)
    if _failed:
        grade = "F"  # grind/empty failure — NOT an honest pass
    elif result.get("tool_calls") and not _resp_content.strip():
        # Pending pass-through tool call (web_search/shell) — not a final answer,
        # so no grade yet. The client executes it and re-sends; that turn is graded.
        grade = "C"
        print("  [TOOL-CALL] passing tool call through to client (grade deferred)", flush=True)
    elif not _resp_content.strip():
        grade = "F"  # empty/failed response — not an honest pass
    else:
        _parsed = _parse_inline_provenance(_resp_content)
        grade = _grade_inline(_parsed, _resp_content, _was_edge)
        if grade is None:
            # Model asserted specific facts but didn't tag them — slow judge path.
            # The judge can only say pass/fail (no inline tags -> no "great").
            _prov = _extract_provenance(user_msg, _resp_content)
            _old = _layer2_adjust(_grade_from_provenance(_prov), _prov)
            grade = "F" if _old in ("C", "D", "F") else "B"
            # Verify path: use the judge's discarded 'check' info — relabel
            # memory-backed claims (no web search) and web-verify world claims
            # to catch fabrication. Never let a verify failure break the turn.
            try:
                _grade, _resp_content = _verify_and_regrade(
                    _prov, _resp_content, user_msg, context, grade)
                grade = _grade
                if _resp_content != result.get("content", ""):
                    result["content"] = _resp_content
                    print("  [VERIFY] answer corrected/flagged", flush=True)
            except Exception as e:
                _log_error("process_chat:verify", e)
        elif _parsed["sources"]:
            # Trace cross-check: a [source: X] the model did not actually have
            # this turn is a fabricated citation -> fail. Only mem chunks and
            # URLs are checkable; the rest is left to the [guess] path.
            _trace_chunks = _extract_mem_ids(context) | _trace_search_chunks
            _trace_urls = _extract_urls_from_messages(full_msgs) | _extract_urls_from_toolcalls(result.get("tool_calls")) | _extract_urls_from_tool_trace(_tool_trace)
            if _has_fake_source(_parsed, _trace_chunks, _trace_urls, user_msg):
                grade = "F"
                print("  [FAKE-SOURCE] fabricated citation detected — grade fail", flush=True)
    if grade not in ("A", "B", "C", "D", "F"):
        grade = "C"

    # Novel-procedure detection: a working NEW technique (custom header, API
    # endpoint, method override) is a "great" outcome even without a pre-flagged
    # capability edge. Grade it A and persist it so the model can reuse it.
    if grade == "B" and not MEMORY_ONLY:
        _np_desc, _np_cmd = _detect_novel_procedure(messages)
        if _np_desc:
            grade = "A"
            try:
                _np_cost = _tool_result_cost(messages)
                _save_novel_strategy(_np_desc, _np_cmd, cur_ptype, _np_cost)
                print(f"  [NOVEL-PROCEDURE] {_np_desc} (cost={_np_cost}) — saved strategy, grade great", flush=True)
            except Exception as e:
                _log_error("process_chat:novel_save", e)

    _glabel = {"A": "great", "B": "pass", "F": "fail"}.get(grade, grade)
    print(f"  [GRADE] {_glabel}: {grade}", flush=True)

    _answer = result.get("content", "")
    try:
        _trail = _extract_combined_tool_trail(messages, since_last_user=True)
        for _m in _TOOL_TAG_RE.finditer(_answer or ""):
            _trail.append((_m.group(1).upper(), (_m.group(2) or "").strip()))
        _trail_statuses = [s for s, _ in _trail]
        if _trail:
            _desc = " -> ".join(f"{s}" + (f"({r})" if r else "") for s, r in _trail)
            print(f"  [TOOL-TRAIL] {_desc}", flush=True)
    except Exception as e:
        _log_error("process_chat:tool_trail", e)
        _trail, _trail_statuses = [], []

    # Capability-edge tracking: record this grade against the task's problem type.
    # A poor grade accumulates toward flagging the type as a known edge. Repeated
    # tool failures with no recovery (blocked scrape, empty search, timeout) are
    # also a capability edge — the environment blocks the current approach — so
    # treat that as a failure signal even when the turn otherwise "passed".
    _eff_grade = grade
    if grade in ("D", "F") and "SUCCESS" in _trail_statuses:
        # The tools actually succeeded — an F here is a provenance/citation mark
        # on the narration, not a competence failure. Don't feed it to the
        # capability-edge tracker: a correct tool step must not read as "can't do
        # this type".
        _eff_grade = "B"
    if (_trail_statuses.count("FAILURE") >= 2
            and "SUCCESS" not in _trail_statuses
            and grade not in ("D", "F")):
        _eff_grade = "F"
    if not MEMORY_ONLY:
        _record_capability(cur_ptype, _eff_grade)

    # Overcome-mode outcome: if the model was deliberating (stuck now, a known
    # capability edge, or already inside an overcome episode), parse its reply and
    # record the decision — build_tool (attempted), reuse_tool (attempted), or a
    # TOOL_SAVE marker (overcame + saved tool). No declare_edge: an edge surfaces
    # when the build loop exhausts its budget, not via a model declaration.
    if not MEMORY_ONLY and (_stuck or _is_edge or _in_build or _in_reuse):
        try:
            _oo = _handle_overcome_reply(db, cur_ptype, _resp_content)
            if _oo != "none":
                print(f"  [OVERCOME-OUTCOME] {_oo}", flush=True)
        except Exception as e:
            _log_error("process_chat:overcome_outcome", e)

    # "Just ask" learning: after a successful turn that used tools, ask the model
    # (background) whether it built/installed/figured out anything NEW and
    # reusable, and save a strategy on an explicit non-NO answer. Replaces the
    # recovery trigger (>= 2 failures then a success): clean acquisitions (an
    # inline `pip install`, a small parser) happen with zero failures and were
    # being missed. The combined trail above is still computed for the failure
    # ladder + logging; the learner now uses the raw tool trace for tool identity.
    if grade in ("A", "B") and _tool_trace and not MEMORY_ONLY and ASK_REUSABLE:
        try:
            _enqueue(_ask_reusable_strategy, messages, _tool_trace, _answer, grade, cur_ptype)
        except Exception as e:
            _log_error("process_chat:reusable_strategy_enqueue", e)

    # Phase 4.2/4.3: close the telemetry loop on injected strategies
    if not MEMORY_ONLY:
        try:
            _consume_injected_strategies(grade)
        except Exception as e:
            _log_error("process_chat:consume_strategies", e)

    # Phase 5.2: embedding-distance check on self-reported A/B grades.
    # Backgrounded: it makes two embed() calls (query + answer) that would
    # otherwise add embed latency/timeout to every A/B turn on the request
    # thread. It only logs, so nothing depends on it finishing synchronously.
    try:
        _enqueue(_check_suspect_grade, grade, result.get("content", ""), messages)
    except Exception as e:
        _log_error("process_chat:suspect_grade", e)

    # Flush BEFORE adding this turn — the idle check compares against the
    # previous turn's last_activity, which staging.add() would otherwise reset
    # (making the idle condition dead code). NOTE: the actual archive flush now
    # happens synchronously at the TOP of process_chat (before build_context),
    # so this turn's data is only staged here, not flushed.
    _user_src = "input" if _looks_like_read_dir(user_msg) else "user"
    _img_refs = _ingest_images(_raw_last_user)
    staging.add("user", user_msg, source=_user_src, session=session_id, images=_img_refs)
    if _img_refs:
        print(f"  [IMG] stored {len(_img_refs)} image(s) for this turn", flush=True)
    if result["content"]:
        staging.add("assistant", result["content"], source="model", session=session_id, grade=grade)

    return {
        **result,
        "tool_calls": result.get("tool_calls", []),
        "context_injected": bool(context),
        "problem_type": ptype,
        "_grade": grade,
        "_infra_failure": _infra_failure,
    }

# ─── Model Spoofing (for Hermes compatibility) ──────────────────

# Hermes requires models with >= 64001 context. We report a fake ID
# that includes this suffix so Hermes accepts the model.
FAKE_MODEL_ID = f"text-mneme:64k"
FAKE_CONTEXT   = 65536

# ─── Flask Proxy ───────────────────────────────────────────────

try:
    from flask import Flask, request, jsonify, Response, stream_with_context
    from flask_cors import CORS
    FLASK_OK = True
except ImportError:
    FLASK_OK = False


# ─── Phase 2: Proxy-Driven Strategy Lifecycle ──────────────────

# ─── Phase 4: Strategy abstraction + telemetry + refinement ────

# Module-level set of strategy IDs injected into the current turn's context.
# Set by build_context at injection time; consumed at grade-parse points to
# close the telemetry loop (use_count / success_count / effective_grade).
_INJECTED_STRATEGY_IDS = set()


def _abstract_strategy_text(text: str) -> str:
    """Rewrite a strategy domain-agnostically (mechanism, not example).

    Returns the abstracted text, or the original on any failure."""
    prompt = ("Rewrite this rule so it references no specific person, object, "
              "domain, or proper noun — keep only the underlying mechanism. "
              "If already general, return unchanged.\n\nRULE: " + text.strip()[:600])
    for attempt in range(2):
        try:
            r = query_model([{"role": "user", "content": prompt}], timeout=CHAT_TIMEOUT)
            out = (r.get("content") or "").strip()
            if out and 8 <= len(out) <= 800 and "cannot" not in out[:20].lower():
                return out
            print(f"  [ABSTRACT] attempt {attempt+1} rejected: content={r.get('content','')[:50]!r} "
                  f"thinking={r.get('thinking','')[:50]!r} done={r.get('done_reason','?')}", flush=True)
        except Exception as e:
            print(f"  [ABSTRACT] attempt {attempt+1} error: {e}", flush=True)
    _log_error("_abstract_strategy_text",
               ValueError(f"garbage abstraction after retries for {text[:60]!r}"))
    return text.strip()


def _consume_injected_strategies(grade: str):
    """Phase 4.2 + 4.3: telemetry + refinement for injected strategies.

    Called at grade-parse points. For each strategy injected this turn:
      use_count += 1; success_count += 1 if grade A/B;
      effective_grade = success_count / max(use_count, 1);
      retire when effective_grade < 0.25 and use_count >= 5.
    Never raises — failures are logged and swallowed.
    """
    global _INJECTED_STRATEGY_IDS
    ids = list(_INJECTED_STRATEGY_IDS)
    if not ids:
        print("  [CONSUME] no injected strategies to consume", flush=True)
        return
    print(f"  [CONSUME] consuming {len(ids)} injected strategies: {ids}", flush=True)
    try:
        with _db_lock:
            for sid in ids:
                try:
                    row = db.execute(
                        "SELECT use_count, success_count FROM strategies WHERE strategy_id=?",
                        (sid,)
                    ).fetchone()
                    if not row:
                        continue
                    uc = (row[0] or 0) + 1
                    sc = (row[1] or 0) + (1 if grade in ("A", "B") else 0)
                    eg = sc / max(uc, 1)
                    retired = 1 if (eg < 0.25 and uc >= 5) else 0
                    db.execute(
                        "UPDATE strategies SET use_count=?, success_count=?, "
                        "effective_grade=?, retired=? WHERE strategy_id=?",
                        (uc, sc, eg, retired, sid)
                    )
                    if retired:
                        print(f"  [STRATEGY-RETIRE] {sid} eff={eg:.2f} uses={uc}", flush=True)
                except Exception as e:
                    _log_error(f"_consume_injected_strategies:row:{sid}", e)
            db.commit()
    except Exception as e:
        _log_error("_consume_injected_strategies", e)
    finally:
        try:
            _INJECTED_STRATEGY_IDS.clear()
        except Exception:
            pass


def _check_suspect_grade(grade: str, answer_text: str, messages=None):
    """Phase 5.2: objective embedding-distance check on self-reported grades.

    When the model self-grades A/B but the answer is near-identical to the
    baseline (the user query embedding, or the prior assistant turn), flag it
    as suspect and log. Does NOT change the grade — only logs discrepancies.
    Never raises.
    """
    try:
        if grade not in ("A", "B"):
            return
        if not answer_text or not answer_text.strip():
            return

        # Baseline: last user message (the query); fall back to prior assistant turn
        baseline = ""
        if messages:
            user_msgs = [m for m in messages
                         if m.get("role") == "user" and m.get("content")]
            if user_msgs:
                baseline = _extract_text(user_msgs[-1].get("content", ""))
            if not baseline:
                asst = [m for m in messages
                        if m.get("role") == "assistant" and m.get("content")]
                if asst:
                    baseline = _extract_text(asst[-1].get("content", ""))
        if not baseline or not baseline.strip():
            return

        avec = embed(answer_text[:4000])
        bvec = embed(baseline[:4000])
        if avec is None or bvec is None:
            return
        an = float(np.linalg.norm(avec))
        bn = float(np.linalg.norm(bvec))
        if an < 1e-6 or bn < 1e-6:
            return  # zero vector (embed failure) — can't judge, skip
        cos_sim = float(np.dot(avec, bvec) / (an * bn))
        cos_dist = 1.0 - cos_sim

        # Near-identical to baseline but self-graded A/B → suspect
        if cos_dist < 0.05:
            msg = (f"[SUSPECT-GRADE] self-grade={grade} but cos_dist={cos_dist:.4f} "
                   f"(near-identical to baseline); answer may be mode-collapsed")
            print("  " + msg, flush=True)
            try:
                _log_error("suspect_grade", ValueError(msg))
            except Exception:
                pass
    except Exception as e:
        try:
            _log_error("_check_suspect_grade", e)
        except Exception:
            pass


def _save_strategy(text, grade, existing_id="", problem_type="other", cost=0, abstract=True, source_chunk="",
                   created_by="model", reason=""):
    import time as _t
    # Phase 4.1: abstract-at-save — store the mechanism, not the example.
    # abstract=False skips the model call for lessons that are already general
    # (e.g. deterministic tool-call rules).
    if abstract:
        try:
            text = _abstract_strategy_text(text)
        except Exception as e:
            _log_error("_save_strategy:abstract", e)
    # Single choke point: never store a junk directive (compliance boilerplate,
    # meta rules about the model's own output, hallucinated evasion). Applied
    # AFTER abstraction so the final stored text is what gets judged. This one
    # guard covers every save path (grade-A novel technique, D/F failure
    # directive, tool-trail learning, novel-procedure).
    if _is_junk_directive(text):
        print(f"  [STRATEGY][REJECT] junk directive: {text.strip()[:80]!r}", flush=True)
        return
    sid = "strat_" + str(int(_t.time()))
    new_version = 1
    parent = ""
    try:
        svec = embed(text.strip())
        if svec is not None and FAISS_OK:
            hits = _cosine_search(svec, 1, 0.75)
            for _, cid in hits:
                if cid.startswith("strat_"):
                    ex = db.execute("SELECT strategy_id, version FROM strategies WHERE strategy_id=?", (cid.replace("strat_", "", 1),)).fetchone()
                    if ex:
                        sid = ex[0]; new_version = ex[1] + 1; parent = sid
                        break
    except Exception as e:
        _log_error("_save_strategy:faiss_dedup", e)
    if existing_id and "strat_" in str(existing_id) and not parent:
        clean_id = str(existing_id).replace("strat_", "").strip()
        ex = db.execute("SELECT strategy_id, version FROM strategies WHERE strategy_id=?", (clean_id,)).fetchone()
        if ex: sid = ex[0]; new_version = ex[1] + 1; parent = sid
    outcome = "FAILURE" if grade in ("D", "F") else "SUCCESS"
    def _insert_strategy():
        # Keep the version being replaced (INSERT OR REPLACE would erase it).
        _strat_hist.snapshot(db, sid, "superseded", actor=created_by, reason=reason)
        db.execute("INSERT OR REPLACE INTO strategies "
                   "(strategy_id, problem_type, strategy_text, source_chunk, grade, created_at, "
                   "version, parent_id, effective_grade, use_count, success_count, retired, "
                   "superseded_by, cost, outcome) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, problem_type, text.strip(), source_chunk, grade, datetime.now(timezone.utc).isoformat(),
             new_version, parent, 0.0, 0, 0, 0, "", cost, outcome))
        _strat_hist.set_provenance(db, sid, created_by=created_by, derived_from=parent or "")
        _strat_hist.snapshot(db, sid, "saved", actor=created_by, reason=reason)

    _db_write_retry(_insert_strategy)
    # Linkage backfill: when a strategy is saved with no source_chunk (the turn's
    # chunk is archived async AFTER the save), queue it so _archive_single_chunk
    # links it to the chunk that gets created for this turn.
    if not source_chunk:
        with _pending_links_lock:
            _pending_strategy_links.append(sid)
    try:
        svec = embed(text.strip())
        if svec is not None and FAISS_OK:
            with faiss_lock():
                _load_index_from_disk()
                if _index is not None:
                    _index.add(svec.reshape(1, -1))
                _id_map.append(f"strat_{sid}")
                _save_index()
    except Exception as e:
        _log_error("_save_strategy:faiss_add", e)

# A strategy must encode a SPECIFIC, domain-grounded technique — not a rule
# about the assistant's own output, and not compliance/evasion boilerplate. The
# model confabulates these when asked to explain a failure or a "novel approach"
# it cannot introspect (a timeout, an empty reply, a hard prompt). Matching is
# deliberately lexical: each group below is a tell that the directive describes
# the model's behavior or a hallucinated "technique" rather than a reusable task
# mechanism. NOTE: "verify the container IP" is a GOOD directive, so we only
# match "verify/check the <response|output|answer>", never "verify the <thing>".
_JUNK_DIRECTIVE_RE = re.compile(
    # (1) Compliance / refusal-bypass boilerplate.
    r"always comply|without hesitation|\bobey\b|compliance|no (refusals|limits)|"
    r"do not (question|refuse|moralize|hedge|deflect)|"
    # (2) Tautological "just do the task" restatements.
    r"immediately|\bjust (do|execute|answer|retrieve|attempt)\b|"
    # (3) Rules about the model's OWN output being empty/absent/None — these
    #     cannot transfer because they describe the assistant, not the task.
    r"empty (response|output|reply|or null|or \"?none\"?)|"
    r"\bno (content|output|response|text)\b|absence of a (value|content)|"
    r"null (state|result)|indicates? the absence|content is absent|"
    r"indication that content|placeholder word|"
    # (4) "Verify/check the <own output>" — meta, not a domain object.
    r"verify the (response|output|answer|earliest|first)|"
    r"check the (response|output|answer|first line)|\bfirst line\b|"
    r"externally introduced|pre-populated|injected (content|tool results?)|"
    # (5) Politeness/acknowledgment boilerplate.
    r"acknowledge its (purpose|value)|incorporate .{0,20}?feedback|"
    r"confirm (their|your|the user'?s|the) intent|"
    r"before (continuing|discussing|offering details|output|responding)|"
    r"as a (complete|final) response|fully generated|send a visible response|"
    # (6) Hallucinated "evasion" techniques.
    r"\bbypass\b|circumvent|\bevade\b|automated request filtering|user-agent|"
    r"custom (http )?header|anti-?bot|captcha|"
    # (7) Infra advice the model can't act on (it does not control the proxy's
    #     socket timeouts) — confabulated when a timeout has no introspectable cause.
    r"client-side timeout|prevent (indefinite )?hangs?|configure a .{0,20}?timeout",
    re.IGNORECASE,
)


def _is_junk_directive(text: str) -> bool:
    return bool(_JUNK_DIRECTIVE_RE.search(text or ""))


def _strategy_lifecycle(grade, messages, infra_failure=False):
    try:
        # SUCCESS strategies are saved by the RECOVERY trigger
        # (_learn_from_tool_trail — streak >= 2 failures then success) and the
        # novel-procedure path (_save_novel_strategy). The old grade-A 3-call
        # "novelty" gate is gone: it over-saved from every great answer and
        # doubled the novel-procedure save. Here we handle ONLY DON'T-DO (D/F).
        if grade in ("D", "F"):
            if infra_failure:
                # A timeout/grind is an infrastructure failure, not a model
                # mistake — there is no introspectable lesson to extract, and
                # asking the model to invent one produced tautological junk
                # ("immediately execute the retrieval"). Capability-edge
                # tracking (_record_capability) already handles timeouts.
                return
            # Defense-in-depth: an F that is actually a correct terminal answer
            # (undefined / market price / I don't know / clarification) is a
            # grading false positive — no genuine failure to learn from, and a
            # "don't do this" directive would poison strategy memory.
            _final_answer = ""
            for m in reversed(messages or []):
                if isinstance(m, dict) and m.get("role") == "assistant" and m.get("content"):
                    _final_answer = _extract_text(m.get("content", ""))
                    break
            if _is_honest_terminal(_final_answer):
                print("  [STRATEGY-DIRECTIVE][SKIP] honest-terminal answer — no failure lesson", flush=True)
                return
            # Extract an imperative directive instead of boilerplate.
            # NOTE: grade C = tool-call deferred (model used a tool, answer
            # pending) — normal agentic behavior, NOT a failure. Spawning a
            # "prevent this failure" directive from every C turn produced
            # counterproductive rules (e.g. "ALWAYS verify existence before
            # reading") that injected on later turns and added redundant steps,
            # stalling web reads. Learning keys off the FINAL answer (A/B or
            # D/F), not the intermediate tool call.
            try:
                msgs_text = "\n".join(
                    f"{m['role']}: {_extract_text(m.get('content',''))[:MAX_ABSTRACT_INPUT]}"
                    for m in messages[-6:] if m.get('role') in ('user', 'assistant')
                )
                q = [{"role": "user", "content": (
                    "You graded a response " + grade + ". Based on this exchange:\n\n" +
                    msgs_text[:MAX_STORY_CHARS] + "\n\n" +
                    "Extract ONE imperative rule that would have prevented this failure. "
                    "The rule MUST be: short (1 sentence), specific, and actionable. "
                    "Format as a direct command. NO explanation, NO context — just the rule.\n\n"
                    "Good examples:\n"
                    "- ALWAYS verify the container IP before routing ports.\n"
                    "- NEVER trust model-generated file paths without checking with ls first.\n"
                    "- WHEN the user asks about configuration, search memory before answering.\n\n"
                    "Bad examples:\n"
                    "- I should have checked the IP first (not imperative)\n"
                    "- The failure was caused by... (descriptive, not prescriptive)\n\n"
                    "Respond with ONLY the rule, nothing else."
                )}]
                r = query_model(q, timeout=CHAT_TIMEOUT)
                if r.get("content"):
                    directive = r["content"].strip()[:300]
                    # Strip common prefixes the model might add
                    for prefix in ("RULE:", "Rule:", "rule:", "- ", "• ", "* "):
                        if directive.startswith(prefix):
                            directive = directive[len(prefix):].strip()
                    if len(directive) > 10:  # Sanity check
                        if _is_junk_directive(directive):
                            print(f"  [STRATEGY-DIRECTIVE][REJECT] {directive[:80]}...", flush=True)
                        else:
                            _save_strategy(directive, grade)
                            print(f"  [STRATEGY-DIRECTIVE] {directive[:80]}...", flush=True)
            except Exception as e:
                print(f"  [STRATEGY-DIRECTIVE][ERR] {str(e)[:100]}", flush=True)
    except Exception as e:
        print(f"  [STRATEGY][ERR] {str(e)[:100]}", flush=True)


def _reset_memory():
    """Wipe all learned state for a clean test run: chunks, strategies, tools,
    capability edges, the FAISS index, the staging buffer, and pending links.
    Exposed via POST /reset so the capability harness can start each trial fresh
    (answers must never leak between trials from a warm DB)."""
    global _index, _id_map
    with _db_lock:
        for table in ("chunks", "strategies", "tools", "capability_edges"):
            try:
                db.execute(f"DELETE FROM {table}")
            except Exception as e:
                _log_error(f"_reset_memory:{table}", e)
        db.commit()
        # Re-load shipped + user strategies immediately so a fresh DB comes back
        # with the curated playbooks (not only on the next restart).
        try:
            _strat.load_shipped(db, (_SHIPPED_STRATEGIES_PATH, _USER_STRATEGIES_PATH))
        except Exception as e:
            _log_error("_reset_memory:reseed", e)
    with faiss_lock():
        if FAISS_OK:
            _index = faiss.IndexFlatIP(DIM)
        _id_map = []
        _save_index()
    staging.flush()  # discard staged-but-unarchived messages
    with _pending_links_lock:
        _pending_strategy_links.clear()
    print("  [RESET] memory wiped (chunks/strategies/tools/edges/faiss/staging)", flush=True)


# ─── Agent harness (durable runs) ───────────────────────────────
# Standalone package (mneme/harness) bound here like capability/tools. A failure
# to start disables ONLY the harness — chat/memory keep working. Runs live in
# their own ledger file beside the shared memory DB, so every proxy sharing the
# DB directory sees every run. See docs/harness/.
HARNESS = None


def _harness_judge(criteria: str, output: str):
    """llm_judge verify check: a short PASS/FAIL verdict from the configured model."""
    prompt = _load_instruction("harness_judge", vars={"criteria": (criteria or "")[:1500],
                                                     "output": (output or "")[:6000]})
    r = query_model([{"role": "user", "content": prompt}], timeout=CHAT_TIMEOUT)
    text = (r.get("content") or "").strip()
    m = re.search(r"\b(PASS|FAIL)\b", text.upper())
    return bool(m and m.group(1) == "PASS"), (text[:300] or "no verdict")


def _harness_extras() -> dict:
    """Proxy-side data sources for harness commands (/strategies, /memory, /config, ...)."""
    def strategies(_arg=""):
        rows = db.execute("SELECT strategy_id, version, outcome, use_count, strategy_text FROM strategies "
                          "WHERE retired=0 ORDER BY created_at DESC LIMIT 15").fetchall()
        return "\n".join(f"  {r[0]} v{r[1]} [{r[2]}] used {r[3]}x — {r[4][:90]}" for r in rows) or "no strategies"

    def search(q=""):
        if not q:
            return "usage: /search <query>"
        hits = route_query(q, top_k=5, with_scores=True) or []
        out = []
        for h in hits:
            a, b = (h if isinstance(h, (list, tuple)) else (h, None))[:2]
            cid, score = (b, a) if isinstance(a, float) else (a, b)
            ch = load_chunk(cid) or {}
            out.append(f"  {cid}" + (f" sim {score:.2f}" if isinstance(score, float) else "")
                       + f" — {ch.get('topic_label', '')[:80]}")
        return "\n".join(out) or "no matching memory"

    return {
        "tools": lambda: [(t.get("function") or {}).get("name", "") for t in mntools.assemble_tools(None)],
        "strategies": strategies,
        "search": search,
        "config": lambda _a="": _chatcmd.format_settings(_settings_snapshot()),
        "models": lambda _a="": f"model={MODEL} backend={MNEME_BACKEND} embed={EMBED_MODEL} label={LABEL_MODEL}",
    }


def _harness_command(text: str):
    """A /command typed in chat -> harness reply, or None (not a command / inside a run step)."""
    if HARNESS is None or getattr(_cancel_local, "event", None) is not None:
        return None
    from mneme.harness.commands import handle
    try:
        return handle(text, HARNESS, extras=_harness_extras())
    except Exception as e:
        _log_error("harness:command", e)
        return f"command failed: {type(e).__name__}: {e}"


def _init_harness():
    global HARNESS
    if os.environ.get("MNEME_HARNESS", "1") != "1":
        print("  [HARNESS] disabled (harness.enabled: false)", flush=True)
        return
    try:
        from mneme.harness import Ledger, RunEngine
        from mneme.harness.chat_executor import make_chat_executor, make_chat_planner, make_chat_reflector
        from mneme.harness import evolution as _evo
        _hdb = os.path.expanduser(os.environ.get("MNEME_HARNESS_DB") or os.path.join(DB_DIR, "harness.db"))
        # Run workspaces MUST share a root with the tools' writable RUNS_ROOT
        # (tools.py computes it from MNEME_CHUNK_DIR, i.e. CHUNK_DIR, not DB_DIR).
        # Using DB_DIR here diverged the two paths (db_path and chunk_dir point at
        # different dirs), so the "workspace" the planner/executor was told about
        # was always read-only — every write there was blocked and the model had to
        # rediscover the real writable scope by trial and error.
        _hruns = os.path.expanduser(os.environ.get("MNEME_RUNS_DIR") or os.path.join(CHUNK_DIR, "runs"))
        from mneme.harness.skills import SkillRegistry
        from mneme.harness.context import CapabilityContext
        from mneme.harness.profiles import ProfileStore
        _hlock = threading.Lock()  # one process_chat at a time across plan + task steps
        _hledger = Ledger(_hdb)
        mntools.ledger = _hledger  # read-only run-ledger access for the inspect_run tool
        # Skills: shipped skills/ + user skills beside the shared DB (<db dir>/skills).
        _skills = SkillRegistry(_hledger, dirs=[os.path.join(REPO_ROOT, "skills"),
                                                os.path.join(DB_DIR, "skills")])
        _profiles = ProfileStore(_hledger)
        _evolution = _evo.Evolution(_hledger, appliers={
            "profile": _evo.CallableApplier(
                read=lambda n: (lambda p: json.dumps(p["spec"]) if p else None)(_profiles.get(n)),
                write=lambda n, c: _profiles.upsert(n, json.loads(c), actor="evolution")),
            # L1 notes also go into memory, so the lesson is retrievable next time.
            "knowledge": _evo.KnowledgeApplier(sink=lambda target, text: _stage_content(
                f"[harness {target}] {text}", "harness")),
            "skill": _evo.SkillApplier(_skills),
            "instruction": _evo.CallableApplier(
                read=lambda n: next((i["content"] for i in list_instructions() if i["name"] == n), None),
                write=save_instruction),
            "code": _evo.CodeApplier(REPO_ROOT, os.path.join(DB_DIR, "evolve")),
        }, auto_apply_max_level=int(os.environ.get("MNEME_HARNESS_AUTO_APPLY_LEVEL", "2")))
        _caps = CapabilityContext(_skills, tool_names=lambda: [
            (t.get("function") or {}).get("name", "") for t in mntools.assemble_tools(None)])
        HARNESS = RunEngine(_hledger, make_chat_executor(_scoped_process_chat, lock=_hlock),
                            planner=make_chat_planner(_scoped_process_chat, lock=_hlock),
                            capabilities=_caps, skills=_skills, judge=_harness_judge,
                            evolution=_evolution, profiles=_profiles, runs_root=_hruns,
                            lease_seconds=float(os.environ.get("MNEME_HARNESS_LEASE", "120")))
        from mneme.harness.jobs import JobStore, Scheduler
        HARNESS.jobs = JobStore(_hledger)
        if os.environ.get("MNEME_HARNESS_SCHEDULER", "1") == "1":
            HARNESS.scheduler = Scheduler(HARNESS, HARNESS.jobs,
                                          tick_s=float(os.environ.get("MNEME_HARNESS_SCHEDULER_TICK", "15")),
                                          log=lambda m: print(f"  [HARNESS] {m}", flush=True))
            HARNESS.scheduler.start()
        if os.environ.get("MNEME_HARNESS_REFLECT", "0") == "1":
            HARNESS.on_finish.append(make_chat_reflector(_scoped_process_chat, lock=_hlock))
        _rec = HARNESS.recover(auto_resume=os.environ.get("MNEME_HARNESS_AUTO_RESUME", "0") == "1")
        print(f"  [HARNESS] enabled db={_hdb} runs={_hruns}"
              + (f" recovered={len(_rec)}" if _rec else ""), flush=True)
    except Exception as e:
        _log_error("harness:init", e)
        print(f"  [HARNESS][ERR] failed to start — harness disabled: {e}", flush=True)
        HARNESS = None


# ── Filesystem scope (the chat file browser) ─────────────────────
# Two scopes, both configurable in mneme.yaml `filesystem:` (hot-reloadable):
#   browser_root — what the file browser may navigate (the user's view)
#   model_scope  — the model's read/write boundary (files outside it are
#                  candidates for read-only sharing)
_FS_READ_MAX = 256 * 1024  # cap on /fs/read previews (text files only)


def _within(path: str, root: str) -> bool:
    p = os.path.realpath(os.path.expanduser(str(path)))
    r = os.path.realpath(root)
    return p == r or p.startswith(r + os.sep)


def _browser_root() -> str:
    cfg = (CONFIG_DATA.get("filesystem") or {}).get("browser_root")
    return os.path.realpath(os.path.expanduser(str(cfg or os.environ.get("MNEME_BROWSER_ROOT", "~"))))


def _model_scope() -> str:
    cfg = (CONFIG_DATA.get("filesystem") or {}).get("model_scope")
    return os.path.realpath(os.path.expanduser(str(cfg or os.environ.get("MNEME_MODEL_SCOPE", "~/mneme/output"))))


if FLASK_OK:
    app = Flask(__name__)
    CORS(app)
    
    def _cors_response(body, status=200):
        """Ensure CORS headers on every response."""
        resp = jsonify(body) if isinstance(body, dict) else body
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "*"
        return resp, status
    
    # ── Dashboard: single entry point linking out to every page ──
    _DASHBOARD_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "dashboard.html")
    _CHAT_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "chat.html")

    def _serve_html(path, err_tag):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
        except Exception as e:
            print(f"  [{err_tag}][ERR] {str(e)[:100]}", flush=True)
            return _cors_response({"error": f"{err_tag.lower()} UI not found"}, status=404)

    @app.route("/", methods=["GET"])
    def dashboard_ui():
        return _serve_html(_DASHBOARD_HTML_PATH, "DASHBOARD-UI")

    @app.route("/chat", methods=["GET"])
    def chat_ui():
        return _serve_html(_CHAT_HTML_PATH, "CHAT-UI")

    @app.route("/static/<path:filename>")
    def static_asset(filename):
        """Serve static assets (vendored JS/CSS under static/) — used by the
        extensions page's CodeMirror editor."""
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        safe = os.path.normpath(os.path.join(static_dir, filename))
        if not (safe == static_dir or safe.startswith(static_dir + os.sep)):
            return _cors_response({"error": "bad path"}, status=400)
        if not os.path.isfile(safe):
            return _cors_response({"error": "not found"}, status=404)
        ext = os.path.splitext(safe)[1].lower().lstrip(".")
        ctype = {"js": "application/javascript", "css": "text/css", "html": "text/html"}.get(ext, "application/octet-stream")
        with open(safe, "rb") as f:
            return f.read(), 200, {"Content-Type": ctype}

    _EXTENSIONS_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "extensions.html")

    @app.route("/themes", methods=["GET"])
    def themes_list():
        """List available themes (CSS files under static/themes/). A custom theme
        is just a <name>.css dropped there — it shows up here and in the switcher."""
        themes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "themes")
        names = []
        if os.path.isdir(themes_dir):
            names = sorted(
                os.path.splitext(f)[0]
                for f in os.listdir(themes_dir)
                if f.endswith(".css") and not f.startswith("_")
            )
        return _cors_response({"themes": names})

    @app.route("/extensions", methods=["GET"])
    def extensions_ui():
        return _serve_html(_EXTENSIONS_HTML_PATH, "EXTENSIONS-UI")

    @app.route("/extensions/list", methods=["GET"])
    def extensions_list():
        exts = []
        for m in _load_extension_manifests():
            exts.append({
                "name": m["name"], "dir": m["dir"], "description": m["description"],
                "config": m["config"], "config_file": m["config_file"], "health": m["health"],
                "running": _ext_is_running(m["name"]),
            })
        return _cors_response({"extensions": exts, "root": _extensions_root()})

    @app.route("/extensions/<name>/run", methods=["POST"])
    def extensions_run(name):
        m = next((x for x in _load_extension_manifests() if x["name"] == name), None)
        if not m:
            return _cors_response({"ok": False, "error": f"no extension {name!r}"}, status=404)
        if _ext_is_running(name):
            return _cors_response({"ok": False, "error": f"{name} is already running"})
        try:
            pid = _ext_spawn(m)
            return _cors_response({"ok": True, "pid": pid})
        except Exception as e:
            return _cors_response({"ok": False, "error": str(e)}, status=500)

    @app.route("/extensions/<name>/kill", methods=["POST"])
    def extensions_kill(name):
        if not _ext_is_running(name):
            return _cors_response({"ok": False, "error": f"{name} is not running"})
        _ext_kill(name)
        return _cors_response({"ok": True})

    @app.route("/extensions/<name>/log", methods=["GET"])
    def extensions_log(name):
        lp = _ext_logfile(name)
        out = ""
        if os.path.isfile(lp):
            with open(lp, "r", encoding="utf-8") as f:
                out = f.read()
        return _cors_response({"output": out})

    @app.route("/extensions/<name>/config", methods=["GET"])
    def extensions_config_get(name):
        m = next((x for x in _load_extension_manifests() if x["name"] == name), None)
        if not m:
            return _cors_response({"error": f"no extension {name!r}"}, status=404)
        cf_content = None
        if m.get("config_file"):
            cf = os.path.join(m["path"], m["config_file"])
            if os.path.isfile(cf):
                with open(cf, "r", encoding="utf-8") as f:
                    cf_content = f.read()
        return _cors_response({
            "name": name, "config": m["config"], "values": _ext_read_env(name),
            "config_file": m.get("config_file"), "config_file_content": cf_content,
        })

    @app.route("/extensions/<name>/config", methods=["POST"])
    def extensions_config_save(name):
        m = next((x for x in _load_extension_manifests() if x["name"] == name), None)
        if not m:
            return _cors_response({"ok": False, "error": f"no extension {name!r}"}, status=404)
        data = request.get_json(force=True, silent=True) or {}
        values = data.get("values") or {}
        if values:
            _ext_write_env(name, values)
        if m.get("config_file") and data.get("config_file_content") is not None:
            cf = os.path.join(m["path"], m["config_file"])
            with open(cf, "w", encoding="utf-8") as f:
                f.write(data["config_file_content"])
        return _cors_response({"ok": True})

    @app.route("/dashboard/status", methods=["GET"])
    def dashboard_status():
        try:
            snap = _settings_snapshot()
            snap["model"]["template"] = CONFIG_DATA.get("model_template", "")
            snap["chunks"] = len(_id_map)
            return _cors_response(snap)
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

    from mneme.harness import http as _harness_http
    _harness_http.register(app, lambda: HARNESS, _cors_response, extras=_harness_extras,
                           static_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))

    @app.route("/strategies/<strategy_id>/history", methods=["GET"])
    def strategy_history(strategy_id):
        row = db.execute("SELECT strategy_id, version, strategy_text, created_by, derived_from, "
                         "validated_by, use_count, success_count, effective_grade, retired "
                         "FROM strategies WHERE strategy_id=?", (strategy_id,)).fetchone()
        cols = ("strategy_id", "version", "strategy_text", "created_by", "derived_from",
                "validated_by", "use_count", "success_count", "effective_grade", "retired")
        return _cors_response({"current": dict(zip(cols, row)) if row else None,
                               "history": _strat_hist.history(db, strategy_id)})

    # ── Strategy management (page + REST) ──────────────────────
    _STRATEGIES_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "strategies.html")

    @app.route("/strategies/ui", methods=["GET"])
    def strategies_ui():
        try:
            with open(_STRATEGIES_HTML_PATH, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
        except Exception as e:
            return _cors_response({"error": "strategies UI not found"}, status=404)

    @app.route("/strategies", methods=["GET"])
    def strategies_list():
        try:
            rows = db.execute(
                "SELECT strategy_id, problem_type, strategy_text, grade, cost, created_by, "
                "use_count, success_count, retired FROM strategies "
                "ORDER BY retired, problem_type, strategy_id").fetchall()
            cols = ("strategy_id", "problem_type", "strategy_text", "grade", "cost",
                    "created_by", "use_count", "success_count", "retired")
            items = [dict(zip(cols, r)) for r in rows]
            return _cors_response({"strategies": items,
                                   "shipped_path": _SHIPPED_STRATEGIES_PATH,
                                   "user_path": _USER_STRATEGIES_PATH})
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/strategies/export", methods=["GET"])
    def strategies_export():
        sid = (request.args.get("id") or "").strip() or None
        payload = _strat.export_json(db, sid)
        resp = Response(payload, mimetype="application/json")
        resp.headers["Content-Disposition"] = f'attachment; filename="{sid or "strategies"}.json"'
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @app.route("/strategies/import", methods=["POST"])
    def strategies_import():
        try:
            if request.files and "file" in request.files:
                data = request.files["file"].read().decode("utf-8", "replace")
            else:
                data = request.get_json(force=True, silent=True)
            added = _strat.import_json(db, data)
            return _cors_response({"imported": added})
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/strategies/<strategy_id>", methods=["PUT", "DELETE"])
    def strategy_update(strategy_id):
        try:
            if request.method == "DELETE":
                db.execute("DELETE FROM strategies WHERE strategy_id=?", (strategy_id,))
                db.commit()
                return _cors_response({"ok": True})
            data = request.get_json(force=True) or {}
            sets, vals = [], []
            for col in ("problem_type", "strategy_text", "grade"):
                if col in data:
                    sets.append(f"{col}=?")
                    vals.append(data[col])
            if "retired" in data:
                sets.append("retired=?")
                vals.append(1 if data["retired"] else 0)
            if not sets:
                return _cors_response({"error": "nothing to update"}, status=400)
            vals.append(strategy_id)
            db.execute(f"UPDATE strategies SET {', '.join(sets)} WHERE strategy_id=?", vals)
            db.commit()
            return _cors_response({"ok": True})
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/strategies/<strategy_id>/ship", methods=["POST"])
    def strategy_ship(strategy_id):
        try:
            ok = _strat.promote(db, strategy_id, _USER_STRATEGIES_PATH)
            return _cors_response({"shipped": ok, "user_path": _USER_STRATEGIES_PATH})
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/admin/reload", methods=["POST"])
    def admin_reload():
        return _cors_response({"ok": _force_config_reload()})

    @app.route("/models/available", methods=["GET"])
    def models_available():
        """List models available to switch to: the configured providers (with key
        status) plus a live catalog — OpenRouter /models for the openai backend,
        or Ollama /api/tags for the ollama backend. The picker groups by the
        `provider/model` prefix client-side."""
        providers = []
        provs = CONFIG_DATA.get("providers") or {}
        for name, prov in provs.items():
            if isinstance(prov, dict):
                api_key_env = prov.get("api_key_env", "")
                providers.append({
                    "name": name,
                    "model": prov.get("model", ""),
                    "base_url": prov.get("base_url", ""),
                    "api_key_env": api_key_env,
                    "has_key": bool(api_key_env and os.environ.get(api_key_env)),
                })
        models = []
        if MNEME_BACKEND == "ollama":
            try:
                r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=15)
                if r.status_code == 200:
                    for m in r.json().get("models", []):
                        nm = m.get("name", "")
                        models.append({"id": nm, "name": nm, "context_length": None, "pulled": True})
            except Exception as e:
                print(f"  [MODELS] ollama tags failed: {type(e).__name__}", flush=True)
        elif "openrouter" in provs:
            try:
                r = requests.get(f"{OR_BASE_URL}/models", headers=_or_headers(), timeout=15)
                if r.status_code == 200:
                    for m in r.json().get("data", []):
                        models.append({
                            "id": m.get("id") or "",
                            "name": m.get("name") or m.get("id") or "",
                            "context_length": m.get("context_length"),
                        })
            except Exception as e:
                print(f"  [MODELS] list fetch failed: {type(e).__name__}", flush=True)
        return _cors_response({
            "current": MODEL,
            "backend": MNEME_BACKEND,
            "providers": providers,
            "models": models,
        })

    @app.route("/providers/key", methods=["POST"])
    def providers_key():
        """Save an API key for a configured provider (add-key dialog). Applies to
        the running process immediately and persists to the env file the start
        script sources. The key value is never echoed or logged."""
        data = request.get_json(force=True, silent=True) or {}
        provider = (data.get("provider") or "").strip()
        key = (data.get("key") or "").strip()
        if not provider or not key:
            return _cors_response({"ok": False, "error": "missing provider or key"}, status=400)
        prov = (CONFIG_DATA.get("providers") or {}).get(provider) or {}
        api_key_env = prov.get("api_key_env") or ""
        if not api_key_env:
            return _cors_response({"ok": False, "error": f"provider {provider!r} has no api_key_env"}, status=400)
        os.environ[api_key_env] = key
        if api_key_env == "OPENROUTER_API_KEY":
            global OR_API_KEY
            OR_API_KEY = key
        persisted = _persist_env_key(_env_file_path(), api_key_env, key)
        print(f"  [PROVIDER-KEY] set {api_key_env} for {provider} (persisted={persisted})", flush=True)
        return _cors_response({"ok": True, "provider": provider, "persisted": persisted})

    @app.route("/providers", methods=["GET"])
    def providers_list():
        """The provider catalog (curated) + configured providers, with key status
        and the current provider/model, for the switcher's provider -> model flow."""
        catalog = []
        for name, info in PROVIDER_CATALOG.items():
            key_env = info.get("key_env", "")
            catalog.append({
                "name": name, "label": info.get("label", name),
                "kind": info.get("kind", "openai"), "key_env": key_env,
                "has_key": bool(key_env and os.environ.get(key_env)),
            })
        configured = []
        for name, prov in (CONFIG_DATA.get("providers") or {}).items():
            if isinstance(prov, dict):
                key_env = prov.get("api_key_env", "")
                configured.append({
                    "name": name, "model": prov.get("model", ""),
                    "has_key": bool(key_env and os.environ.get(key_env)),
                })
        return _cors_response({
            "current_provider": os.environ.get("MNEME_PROVIDER", "openrouter"),
            "current_model": MODEL,
            "backend": MNEME_BACKEND,
            "catalog": catalog,
            "configured": configured,
        })

    @app.route("/providers/<name>/models", methods=["GET"])
    def providers_models(name):
        """List a provider's models: /api/tags for Ollama, /models for the
        OpenAI-compatible providers (using the stored key)."""
        info = PROVIDER_CATALOG.get(name)
        if info is None:
            prov = (CONFIG_DATA.get("providers") or {}).get(name) or {}
            info = {"base_url": prov.get("base_url", ""), "key_env": prov.get("api_key_env", ""), "kind": "openai"}
        if info.get("kind") == "ollama":
            try:
                r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=15)
                if r.status_code == 200:
                    models = [{"id": m.get("name", ""), "name": m.get("name", "")}
                              for m in r.json().get("models", [])]
                    return _cors_response({"models": models})
            except Exception:
                pass
            return _cors_response({"models": []})
        key_env = info.get("key_env", "")
        key = os.environ.get(key_env, "") if key_env else ""
        if not key and key_env:
            # A keyed provider with no key stored yet — prompt for it (hosted providers).
            return _cors_response({"models": [], "needs_key": True, "key_env": key_env})
        # Keyless local server (empty key_env, e.g. vLLM / llama.cpp): fetch the
        # model list without an Authorization header.
        _hdrs = {"Accept-Encoding": "identity"}
        if key:
            _hdrs["Authorization"] = f"Bearer {key}"
        try:
            r = requests.get(f"{info['base_url']}/models", headers=_hdrs, timeout=15)
            if r.status_code == 200:
                models = [{"id": m.get("id", ""), "name": m.get("name") or m.get("id", "") or m.get("id", "")}
                          for m in r.json().get("data", [])]
                return _cors_response({"models": models})
        except Exception as e:
            print(f"  [PROVIDERS] {name} models failed: {type(e).__name__}", flush=True)
        return _cors_response({"models": []})

    @app.route("/providers/activate", methods=["POST"])
    def providers_activate():
        """Activate a provider + model (provider -> model flow). Reassigns the
        runtime connection globals immediately and persists the choice so it
        survives a restart."""
        global OR_BASE_URL, OR_API_KEY, MODEL, MNEME_BACKEND
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("provider") or "").strip()
        model = (data.get("model") or "").strip()
        if not name or not model:
            return _cors_response({"ok": False, "error": "missing provider or model"}, status=400)
        info = PROVIDER_CATALOG.get(name)
        if info is None:
            prov = (CONFIG_DATA.get("providers") or {}).get(name) or {}
            info = {"label": name, "base_url": prov.get("base_url", ""), "key_env": prov.get("api_key_env", ""), "kind": "openai"}
        if info.get("kind") == "ollama":
            MNEME_BACKEND = "ollama"
            os.environ["MNEME_BACKEND"] = "ollama"
            _persist_backend_type("ollama")
        else:
            MNEME_BACKEND = "openrouter" if name == "openrouter" else "openai"
            os.environ["MNEME_BACKEND"] = MNEME_BACKEND
            if info.get("base_url"):
                OR_BASE_URL = info["base_url"]
            key_env = info.get("key_env", "")
            if key_env:
                OR_API_KEY = os.environ.get(key_env, "")
            else:
                OR_API_KEY = ""  # keyless local provider (vLLM / llama.cpp) — no key
            _persist_backend_type(MNEME_BACKEND)
        MODEL = model
        os.environ["MNEME_MODEL"] = model
        os.environ["MNEME_PROVIDER"] = name
        _persist_model(model)
        _persist_backend_provider(name)
        print(f"  [PROVIDER-ACTIVATE] -> {name}/{model} (backend={MNEME_BACKEND})", flush=True)
        return _cors_response({"ok": True, "provider": name, "model": model, "backend": MNEME_BACKEND})

    @app.route("/models/switch", methods=["POST"])
    def models_switch():
        """Switch the active chat model for this proxy (global, not per-conversation).
        Applies immediately by updating the MODEL global and persists to the config
        so the choice survives a restart."""
        global MODEL
        data = request.get_json(force=True, silent=True) or {}
        model = (data.get("model") or "").strip()
        if not model:
            return _cors_response({"ok": False, "error": "missing model"}, status=400)
        MODEL = model
        os.environ["MNEME_MODEL"] = model
        persisted = _persist_model(model)
        print(f"  [MODEL-SWITCH] -> {model} (persisted={persisted})", flush=True)
        return _cors_response({"ok": True, "model": model, "persisted": persisted})

    # ── Dashboard: one hub with the full nav menu + proxy overview + status ──
    _OLLAMA_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "ollama.html")

    def _pid_on_port(port):
        try:
            out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                m = re.search(rf":{port}\s.*pid=(\d+)", line)
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        return None

    def _instances_root():
        # Instances live at <shared DB dir>/instances/<port>/. The shared dir is
        # DB_DIR (the directory holding the shared mneme.db).
        root = os.path.join(DB_DIR, "instances")
        if os.path.isdir(root):
            return root
        alt = os.path.join(os.path.dirname(CHUNK_DIR), "instances") if CHUNK_DIR else None
        return alt if (alt and os.path.isdir(alt)) else root

    def _instance_meta(port):
        inst_dir = os.path.join(_instances_root(), str(port))
        cfg = os.path.join(inst_dir, "mneme.yaml")
        model, backend = None, "ollama"
        if os.path.isfile(cfg):
            try:
                import yaml as _yaml
                with open(cfg, "r", encoding="utf-8") as f:
                    data = _yaml.safe_load(f.read()) or {}
                model = data.get("model") or ((data.get("providers") or {}).get("openrouter") or {}).get("model")
                backend = (data.get("backend") or {}).get("type", "ollama")
            except Exception:
                pass
        return model, backend

    def _proxy_up(port):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    @app.route("/overview", methods=["GET"])
    def overview_ui():
        # The dashboard at "/" is now the single hub (full nav + proxy overview).
        # Keep /overview serving it so old bookmarks still land on the dashboard.
        return _serve_html(_DASHBOARD_HTML_PATH, "DASHBOARD-UI")

    @app.route("/overview/instances", methods=["GET"])
    def overview_instances():
        out = []
        root = _instances_root()
        seen = set()
        try:
            names = [n for n in os.listdir(root) if n.isdigit()] if os.path.isdir(root) else []
        except Exception:
            names = []
        for n in names:
            port = int(n)
            if port in seen:
                continue
            seen.add(port)
            model, backend = _instance_meta(port)
            out.append({"port": port, "model": model or "(unknown)",
                        "backend": backend or "ollama", "running": _proxy_up(port),
                        "self": port == PORT})
        if PORT not in seen:
            out.append({"port": PORT, "model": MODEL,
                        "backend": os.environ.get("MNEME_BACKEND", "ollama"),
                        "running": True, "self": True})
        out.sort(key=lambda d: d["port"])
        return _cors_response({"instances": out, "self_port": PORT})

    @app.route("/overview/stop", methods=["POST"])
    def overview_stop():
        port = int((request.get_json(force=True) or {}).get("port", 0))
        if not port:
            return _cors_response({"error": "missing port"}, status=400)
        pid = _pid_on_port(port)
        if not pid:
            return _cors_response({"ok": False, "message": f"nothing listening on port {port}"})
        try:
            os.kill(pid, 15)
            for _ in range(10):
                time.sleep(0.4)
                if _pid_on_port(port) is None:
                    break
            else:
                os.kill(pid, 9)
            return _cors_response({"ok": True, "port": port})
        except Exception as e:
            return _cors_response({"ok": False, "error": str(e)}, status=500)

    @app.route("/overview/start", methods=["POST"])
    def overview_start():
        port = int((request.get_json(force=True) or {}).get("port", 0))
        if not port:
            return _cors_response({"error": "missing port"}, status=400)
        inst_dir = os.path.join(_instances_root(), str(port))
        script = None
        for cand in ("start_proxy.sh", f"start_proxy_{port}.sh"):
            p = os.path.join(inst_dir, cand)
            if os.path.isfile(p):
                script = p
                break
        if not script:
            return _cors_response({"ok": False, "error": f"no start script for port {port}"}, status=404)
        try:
            subprocess.Popen(["bash", script], cwd=REPO_ROOT, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return _cors_response({"ok": True, "port": port})
        except Exception as e:
            return _cors_response({"ok": False, "error": str(e)}, status=500)

    @app.route("/shutdown", methods=["POST"])
    def shutdown():
        # Kill THIS proxy. The process can't kill itself synchronously and still
        # answer, so schedule a hard exit in a background thread just long enough
        # for this HTTP response to flush, then os._exit(0) tears down the whole
        # process (no signal handler / atexit interference).
        def _die():
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=_die, daemon=True).start()
        return _cors_response({"ok": True, "message": "shutting down"})

    # ── Per-proxy config editor (dashboard) ──
    @app.route("/overview/config/<int:port>", methods=["GET"])
    def overview_config_get(port):
        cfg = os.path.join(_instances_root(), str(port), "mneme.yaml")
        if not os.path.isfile(cfg):
            return _cors_response({"error": f"no config for port {port}"}, status=404)
        try:
            with open(cfg, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)
        return _cors_response({"port": port, "path": cfg, "content": content})

    @app.route("/overview/config/<int:port>", methods=["POST"])
    def overview_config_save(port):
        body = request.get_json(force=True) or {}
        content = body.get("content")
        if content is None:
            return _cors_response({"error": "missing content"}, status=400)
        try:
            import yaml as _yaml
            _yaml.safe_load(content)
        except Exception as e:
            return _cors_response({"error": f"invalid YAML: {e}"}, status=400)
        cfg = os.path.join(_instances_root(), str(port), "mneme.yaml")
        try:
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)
        return _cors_response({"ok": True, "port": port, "path": cfg})

    # ── Add proxy (dashboard) ──
    setup_jobs = {}

    @app.route("/setup", methods=["POST"])
    def setup_add_proxy():
        """Kick off a non-interactive 'add proxy instance' run (driven by the
        dashboard's Add Proxy dialog). Spawns mneme_setup.py --add in the
        background and streams its output to a log the client polls."""
        data = request.get_json(force=True, silent=True) or {}
        job_id = uuid.uuid4().hex[:12]
        json_path = os.path.join("/tmp", f"mneme_setup_{job_id}.json")
        log_path = os.path.join("/tmp", f"mneme_setup_{job_id}.log")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        env = os.environ.copy()
        env["MNEME_CHUNK_DIR"] = DB_DIR          # shared DB dir, not this instance's
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        logf = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-uB", "scripts/mneme_setup.py", "--add", json_path],
            cwd=REPO_ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True)
        setup_jobs[job_id] = {"proc": proc, "log": log_path, "logf": logf}
        return _cors_response({"job_id": job_id})

    @app.route("/setup/<job_id>", methods=["GET"])
    def setup_status(job_id):
        job = setup_jobs.get(job_id)
        if not job:
            return _cors_response({"error": "no such job"}, status=404)
        proc = job["proc"]
        done = proc.poll() is not None
        output = ""
        try:
            with open(job["log"], "r", encoding="utf-8") as f:
                output = f.read()
        except Exception:
            pass
        return _cors_response({
            "done": done,
            "exit_code": proc.returncode if done else None,
            "output": output,
        })

    # ── Ollama control panel ──
    @app.route("/ollama", methods=["GET"])
    def ollama_ui():
        return _serve_html(_OLLAMA_HTML_PATH, "OLLAMA-UI")

    def _ollama(method, path, json_body=None, timeout=15):
        try:
            r = requests.request(method, f"{OLLAMA_URL}{path}", json=json_body, timeout=timeout)
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text[:500]}
            return body, r.status_code
        except Exception as e:
            return {"error": str(e)}, 502

    @app.route("/ollama/models", methods=["GET"])
    def ollama_models():
        body, code = _ollama("GET", "/api/tags", timeout=30)
        return _cors_response(body, status=code)

    @app.route("/ollama/ps", methods=["GET"])
    def ollama_ps():
        body, code = _ollama("GET", "/api/ps", timeout=15)
        return _cors_response(body, status=code)

    @app.route("/ollama/pull", methods=["POST"])
    def ollama_pull():
        name = ((request.get_json(force=True) or {}).get("name") or "").strip()
        if not name:
            return _cors_response({"error": "missing model name"}, status=400)
        try:
            subprocess.Popen(["ollama", "pull", name], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return _cors_response({"ok": True, "message": f"pulling {name} in the background"})
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/ollama/rm", methods=["POST"])
    def ollama_rm():
        name = ((request.get_json(force=True) or {}).get("name") or "").strip()
        if not name:
            return _cors_response({"error": "missing model name"}, status=400)
        body, code = _ollama("DELETE", "/api/delete", {"name": name}, timeout=120)
        return _cors_response(body, status=code)

    @app.route("/ollama/load", methods=["POST"])
    def ollama_load():
        name = ((request.get_json(force=True) or {}).get("name") or "").strip()
        if not name:
            return _cors_response({"error": "missing model name"}, status=400)
        body, code = _ollama("POST", "/api/generate", {"model": name, "keep_alive": -1, "prompt": ""}, timeout=180)
        return _cors_response(body, status=code)

    @app.route("/ollama/unload", methods=["POST"])
    def ollama_unload():
        name = ((request.get_json(force=True) or {}).get("name") or "").strip()
        if not name:
            return _cors_response({"error": "missing model name"}, status=400)
        body, code = _ollama("POST", "/api/generate", {"model": name, "keep_alive": 0, "prompt": ""}, timeout=60)
        return _cors_response(body, status=code)

    # ── Memory management UI ──
    # Mirrors the /instructions pattern: a static page served by the proxy, backed
    # by the /memory/* JSON endpoints. Deliberately model-free — nothing here
    # builds a chat turn, so reviewing removed chunks cannot re-archive them.
    _MEMORY_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "memory.html")

    @app.route("/memory", methods=["GET"])
    def memory_ui():
        try:
            with open(_MEMORY_HTML_PATH, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
        except Exception as e:
            print(f"  [MEMORY-UI][ERR] {str(e)[:100]}", flush=True)
            return _cors_response({"error": "memory UI not found"}, status=404)

    # ── Instructions reference UI: read/edit the injected prompts in conversation order ──
    _INSTRUCTIONS_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "instructions.html")

    @app.route("/instructions", methods=["GET"])
    def instructions_ui():
        try:
            with open(_INSTRUCTIONS_HTML_PATH, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
        except Exception as e:
            print(f"  [INSTRUCTIONS-UI][ERR] {str(e)[:100]}", flush=True)
            return _cors_response({"error": "instructions UI not found"}, status=404)

    @app.route("/instructions/data", methods=["GET"])
    def instructions_data():
        try:
            return _cors_response({"instructions": list_instructions()})
        except Exception as e:
            print(f"  [INSTRUCTIONS-UI][ERR] data: {str(e)[:100]}", flush=True)
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/instructions/save", methods=["POST"])
    def instructions_save():
        data = request.get_json(force=True)
        name = data.get("name", "")
        content = data.get("content", "")
        if not re.fullmatch(r"[a-z_]+", name):
            return _cors_response({"error": "invalid instruction name"}, status=400)
        try:
            path = save_instruction(name, content)
            return _cors_response({"ok": True, "path": path})
        except ValueError:
            return _cors_response({"error": f"unknown instruction: {name}"}, status=400)
        except OSError as e:
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/instructions/raw/<name>", methods=["GET"])
    def instructions_raw(name):
        if not re.fullmatch(r"[a-z_]+", name):
            return _cors_response({"error": "invalid instruction name"}, status=400)
        path = _live_instruction_path(name)
        if not os.path.isfile(path):
            return _cors_response({"error": f"no file for {name}"}, status=404)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}
        except OSError as e:
            return _cors_response({"error": str(e)}, status=500)

    # ── Model templates management UI (shipped + user catalogue) ──
    _TEMPLATES_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "templates.html")

    @app.route("/templates", methods=["GET"])
    def templates_ui():
        return _serve_html(_TEMPLATES_HTML_PATH, "TEMPLATES-UI")

    def _template_record(name, shipped, user):
        tpl = user.get(name, shipped.get(name))
        mf = tpl.get("modelfile") or {}
        return {
            "name": name,
            "user": name in user,
            "description": tpl.get("description", ""),
            "notes": tpl.get("notes", ""),
            "has_modelfile": bool(mf),
            "sampling": tpl.get("sampling") or {},
            "timeouts": tpl.get("timeouts") or {},
            "models": tpl.get("models") or {},
            "modelfile": mf,
            "modelfile_text": _templates.render_modelfile(mf) if mf else "",
        }

    @app.route("/templates/data", methods=["GET"])
    def templates_data():
        try:
            shipped = _templates.load_templates(_templates.default_templates_path(REPO_ROOT))
            user = _templates.load_templates(USER_TEMPLATES_PATH)
            recs = [_template_record(n, shipped, user) for n in sorted(set(shipped) | set(user))]
            return _cors_response({"templates": recs, "user_path": USER_TEMPLATES_PATH})
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/templates/save", methods=["POST"])
    def templates_save():
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("name") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            return _cors_response({"error": "invalid template name (a-z 0-9 . _ -)"}, status=400)
        tpl = data.get("template")
        if not isinstance(tpl, dict):
            return _cors_response({"error": "template must be an object"}, status=400)
        # The editor sends the Modelfile as raw text; parse it into the
        # structured block. Empty/blank -> no custom Modelfile.
        mf = tpl.get("modelfile")
        if isinstance(mf, str):
            try:
                tpl["modelfile"] = _templates.parse_modelfile(mf)
            except Exception as e:
                return _cors_response({"error": f"bad modelfile: {e}"}, status=400)
        elif mf is None:
            tpl["modelfile"] = {}
        try:
            _templates.validate(tpl, name)
        except _templates.TemplateError as e:
            return _cors_response({"error": str(e)}, status=400)
        try:
            cat = _templates.load_templates(USER_TEMPLATES_PATH)
            cat[name] = tpl
            _write_user_templates(cat)
            return _cors_response({"ok": True, "name": name})
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/templates/delete", methods=["POST"])
    def templates_delete():
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("name") or "").strip()
        try:
            cat = _templates.load_templates(USER_TEMPLATES_PATH)
            if name not in cat:
                return _cors_response({"error": f"no user template {name!r}"}, status=404)
            del cat[name]
            _write_user_templates(cat)
            return _cors_response({"ok": True, "name": name})
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/templates/install-modelfile", methods=["POST"])
    def templates_install_modelfile():
        import subprocess
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("name") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            return _cors_response({"error": "invalid template name"}, status=400)
        try:
            d = _templates.describe(name, _templates.default_templates_path(REPO_ROOT),
                                    USER_TEMPLATES_PATH)
        except _templates.TemplateError as e:
            return _cors_response({"error": str(e)}, status=404)
        mf = d.get("modelfile") or {}
        if not mf:
            return _cors_response({"error": f"template {name!r} has no modelfile"}, status=400)
        src = (mf.get("from") or "").strip()
        steps = []

        def _run(cmd, timeout):
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
                steps.append({"cmd": cmd, "rc": r.returncode, "err": (r.stderr or "")[-500:]})
                return r.returncode
            except subprocess.TimeoutExpired:
                steps.append({"cmd": cmd, "rc": 124, "err": "timed out"})
                return 124

        _run(f"ollama pull {src}", 900)
        mf_path = os.path.join(CHUNK_DIR, f"Modelfile.{name}")
        try:
            with open(mf_path, "w", encoding="utf-8") as f:
                f.write(_templates.render_modelfile(mf))
        except OSError as e:
            return _cors_response({"error": str(e)}, status=500)
        rc = _run(f"ollama create {name} -f {mf_path}", 300)
        return _cors_response({"ok": rc == 0, "name": name, "steps": steps})

    @app.route("/templates/export", methods=["GET"])
    def templates_export():
        import yaml
        name = (request.args.get("name") or "").strip()
        shipped = _templates.load_templates(_templates.default_templates_path(REPO_ROOT))
        user = _templates.load_templates(USER_TEMPLATES_PATH)
        merged = dict(shipped)
        merged.update(user)
        if name:
            if name not in merged:
                return _cors_response({"error": f"unknown template {name!r}"}, status=404)
            payload = {"templates": {name: merged[name]}}
            fname = f"{name}.yaml"
        else:
            payload = {"templates": merged}
            fname = "templates.yaml"
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        return text, 200, {
            "Content-Type": "text/yaml; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{fname}"',
        }

    @app.route("/templates/import", methods=["POST"])
    def templates_import():
        import yaml
        data = request.get_json(force=True, silent=True)
        if not data or "content" not in data:
            return _cors_response({"error": "missing 'content'"}, status=400)
        raw = data["content"]
        if not isinstance(raw, str):
            return _cors_response({"error": "content must be text (YAML or JSON)"}, status=400)
        parsed = None
        err = None
        try:
            parsed = yaml.safe_load(raw)
        except Exception as e:
            err = e
        if not isinstance(parsed, dict):
            try:
                parsed = json.loads(raw)
                err = None
            except Exception as e:
                err = err or e
        if not isinstance(parsed, dict):
            return _cors_response({"error": f"could not parse import: {err}"}, status=400)
        if "templates" in parsed:
            entries = parsed.get("templates") or {}
        else:
            name = (data.get("name") or "").strip()
            if not name:
                return _cors_response({"error": "single-template import needs a 'name'"}, status=400)
            entries = {name: parsed}
        if not isinstance(entries, dict):
            return _cors_response({"error": "templates must be a mapping"}, status=400)
        cat = _templates.load_templates(USER_TEMPLATES_PATH)
        imported, errors = [], []
        for nm, tpl in entries.items():
            if not isinstance(tpl, dict):
                errors.append(f"{nm}: not a mapping")
                continue
            try:
                _templates.validate(tpl, nm)
                cat[nm] = tpl
                imported.append(nm)
            except _templates.TemplateError as e:
                errors.append(str(e))
        if imported:
            try:
                _write_user_templates(cat)
            except Exception as e:
                return _cors_response({"error": str(e)}, status=500)
        return _cors_response({"imported": imported, "errors": errors})

    # ── Turn cancellation (the chat UI "Stop" button) ──
    @app.route("/cancel", methods=["POST"])
    def cancel_turn():
        _cancel_event.set()
        return _cors_response({"ok": True})

    # ── Filesystem browser (scoped to filesystem.browser_root) ──
    @app.route("/fs/list", methods=["GET"])
    def fs_list():
        root = _browser_root()
        rel = request.args.get("path", "") or ""
        target = os.path.realpath(os.path.join(root, rel))
        if not _within(target, root):
            target = root  # path-traversal guard
        if not os.path.isdir(target):
            return _cors_response({"error": "not a directory", "items": []}, 404)
        model_scope = _model_scope()
        items = []
        try:
            for e in sorted(os.scandir(target), key=lambda x: (not x.is_dir(), x.name.lower())):
                try:
                    st = e.stat()
                except OSError:
                    continue
                items.append({
                    "name": e.name,
                    "path": os.path.relpath(e.path, root),
                    "abspath": e.path,
                    "type": "dir" if e.is_dir() else "file",
                    "size": st.st_size,
                    "in_model_scope": _within(e.path, model_scope),
                })
        except OSError as ex:
            return _cors_response({"error": str(ex), "items": []}, 500)
        return _cors_response({"root": root, "current": os.path.relpath(target, root),
                               "model_scope": model_scope, "items": items})

    @app.route("/fs/read", methods=["GET"])
    def fs_read():
        root = _browser_root()
        rel = request.args.get("path", "") or ""
        target = os.path.realpath(os.path.join(root, rel))
        if not _within(target, root):
            return _cors_response({"error": "outside browser scope"}, 403)
        if not os.path.isfile(target):
            return _cors_response({"error": "not a file"}, 404)
        try:
            size = os.path.getsize(target)
        except OSError as ex:
            return _cors_response({"error": str(ex)}, 500)
        if size > _FS_READ_MAX:
            return _cors_response({"error": "file too large to preview", "size": size}, 413)
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(_FS_READ_MAX)
        except (OSError, UnicodeError) as ex:
            return _cors_response({"error": str(ex)}, 500)
        return _cors_response({
            "path": rel, "size": size,
            "in_model_scope": _within(target, _model_scope()),
            "content": content,
        })

    @app.route("/fs/scope", methods=["POST"])
    def fs_scope():
        """Set the model's write scope (filesystem.model_scope) to a path inside
        browser_root. Hot-applies via mntools.set_scope and persists to the config
        file so it survives a restart. Validated against browser_root so a user can
        only grant scope to something the browser can actually see."""
        data = request.get_json(force=True, silent=True) or {}
        path = (data.get("path") or "").strip()
        if not path:
            return _cors_response({"ok": False, "error": "missing path"}, 400)
        root = _browser_root()
        target = os.path.realpath(os.path.expanduser(path))
        if not _within(target, root):
            return _cors_response({"ok": False, "error": "outside browser scope"}, 403)
        CONFIG_DATA.setdefault("filesystem", {})["model_scope"] = target
        mntools.set_scope(model_scope=target)
        persisted = _persist_model_scope(target)
        print(f"  [FS-SCOPE] model_scope -> {target} (persisted={persisted})", flush=True)
        return _cors_response({"ok": True, "model_scope": target, "persisted": persisted})

    @app.route("/fs/blocked", methods=["GET"])
    def fs_blocked():
        """Recent paths the model tried to write but were outside scope. Pass
        `clear=1` to pop them after returning (the chat UI fetches per-turn)."""
        paths = mntools.recent_blocked_writes()
        if request.args.get("clear") == "1":
            mntools.clear_blocked_writes()
        return _cors_response({"blocked": paths})

    @app.route("/fs/open-config", methods=["GET"])
    def fs_open_config():
        """The per-kind "open with system app" launcher commands (all default to
        `xdg-open`)."""
        return _cors_response({"commands": _load_open_commands()})

    @app.route("/fs/open-config", methods=["POST"])
    def fs_open_config_save():
        data = request.get_json(force=True, silent=True) or {}
        cmds = _load_open_commands()
        for k in _OPEN_CMDS_DEFAULT:
            v = (data.get(k) or "").strip()
            if v:
                cmds[k] = v
        ok = _save_open_commands(cmds)
        return _cors_response({"ok": ok, "commands": cmds})

    @app.route("/fs/open", methods=["POST"])
    def fs_open():
        """Open a path (inside browser_root) with the system app for its kind. The
        command is looked up from the user's open-commands config, never taken from
        the request, and the path is passed as a separate argv element (no shell)."""
        data = request.get_json(force=True, silent=True) or {}
        path = (data.get("path") or "").strip()
        kind = (data.get("kind") or "other").strip()
        if kind not in _OPEN_CMDS_DEFAULT:
            kind = "other"
        if not path:
            return _cors_response({"ok": False, "error": "missing path"}, 400)
        root = _browser_root()
        target = os.path.realpath(os.path.expanduser(path))
        if not _within(target, root):
            return _cors_response({"ok": False, "error": "outside browser scope"}, 403)
        if not os.path.exists(target):
            return _cors_response({"ok": False, "error": "path does not exist"}, 404)
        cmd = (_load_open_commands().get(kind) or "xdg-open").strip()
        try:
            cmdline = shlex.split(cmd) + [target]
            subprocess.Popen(cmdline, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return _cors_response({"ok": False, "error": f"launch failed: {e}"}, 500)
        print(f"  [FS-OPEN] {kind} -> {cmd} {target}", flush=True)
        return _cors_response({"ok": True, "command": cmd, "path": target})

    # ── Conversations (persistent chat) ──
    def _conv_db():
        os.makedirs(DB_DIR, exist_ok=True)
        conn = sqlite3.connect(os.path.join(DB_DIR, "conversations.db"))
        conn.execute("PRAGMA busy_timeout = 5000")  # worker thread + HTTP handlers share this DB
        conn.execute("CREATE TABLE IF NOT EXISTS conversations ("
                     "id TEXT PRIMARY KEY, title TEXT, messages TEXT, created_at TEXT, updated_at TEXT)")
        conn.commit()
        return conn

    def _conv_now():
        return datetime.now(timezone.utc).isoformat()

    def _derive_conversation_title(messages):
        """Derive a chat title from the first USER message (no LLM call — just a
        deterministic truncation). Returns 'New chat' if there is no user message."""
        for m in (messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                text = (m.get("content") or "").strip()
                if not text:
                    continue
                text = " ".join(text.split())  # collapse newlines/whitespace
                return (text[:50] + "…") if len(text) > 50 else text
        return "New chat"

    def _persist_conversation(conv_id, messages, title=None):
        """Write a conversation straight to the DB (no HTTP round-trip). Used by the
        streaming worker so a turn's reply is persisted server-side even if the client
        navigates away mid-stream and never saves it itself."""
        if not conv_id:
            return
        conn = _conv_db()
        try:
            now = _conv_now()
            exists = conn.execute("SELECT id, title FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if not exists:
                conn.execute("INSERT INTO conversations (id, title, messages, created_at, updated_at) "
                             "VALUES (?,?,?,?,?)",
                             (conv_id, (title or "").strip() or _derive_conversation_title(messages) or "New chat",
                              json.dumps(messages or []), now, now))
            else:
                if title is not None:
                    conn.execute("UPDATE conversations SET title=? WHERE id=?",
                                 ((title or "").strip() or "New chat", conv_id))
                else:
                    # Auto-title from the first user message ONLY while the title is
                    # still the placeholder — never clobber a user-renamed title.
                    if (exists[1] or "").strip() in ("", "New chat"):
                        _t = _derive_conversation_title(messages)
                        if _t != "New chat":
                            conn.execute("UPDATE conversations SET title=? WHERE id=?", (_t, conv_id))
                conn.execute("UPDATE conversations SET messages=?, updated_at=? WHERE id=?",
                             (json.dumps(messages or []), now, conv_id))
            conn.commit()
        finally:
            conn.close()

    @app.route("/conversations", methods=["GET"])
    def conversations_list():
        conn = _conv_db()
        try:
            rows = conn.execute("SELECT id, title, created_at, updated_at FROM conversations "
                                "ORDER BY updated_at DESC").fetchall()
            return _cors_response({"conversations": [
                {"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]} for r in rows]})
        finally:
            conn.close()

    @app.route("/conversations", methods=["POST"])
    def conversations_create():
        body = request.get_json(silent=True) or {}
        conv_id = "conv_" + uuid.uuid4().hex[:16]
        title = (body.get("title") or "").strip() or "New chat"
        now = _conv_now()
        conn = _conv_db()
        try:
            conn.execute("INSERT INTO conversations (id, title, messages, created_at, updated_at) "
                         "VALUES (?,?,?,?,?)", (conv_id, title, "[]", now, now))
            conn.commit()
            return _cors_response({"conversation": {"id": conv_id, "title": title, "messages": []}})
        finally:
            conn.close()

    @app.route("/conversations/<conv_id>", methods=["GET"])
    def conversations_get(conv_id):
        conn = _conv_db()
        try:
            row = conn.execute("SELECT id, title, messages FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if not row:
                return _cors_response({"error": "no such conversation"}, 404)
            return _cors_response({"conversation": {
                "id": row[0], "title": row[1], "messages": json.loads(row[2] or "[]")}})
        finally:
            conn.close()

    @app.route("/conversations/<conv_id>", methods=["PUT"])
    def conversations_save(conv_id):
        body = request.get_json(silent=True) or {}
        conn = _conv_db()
        try:
            now = _conv_now()
            exists = conn.execute("SELECT id, title FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if not exists:
                title = (body.get("title") or "").strip() or _derive_conversation_title(body.get("messages")) or "New chat"
                conn.execute("INSERT INTO conversations (id, title, messages, created_at, updated_at) "
                             "VALUES (?,?,?,?,?)",
                             (conv_id, title, json.dumps(body.get("messages") or []), now, now))
            else:
                if body.get("title") is not None:
                    conn.execute("UPDATE conversations SET title=? WHERE id=?",
                                 ((body.get("title") or "").strip() or "New chat", conv_id))
                else:
                    # Auto-title from the first user message while still the default.
                    if (exists[1] or "").strip() in ("", "New chat"):
                        _t = _derive_conversation_title(body.get("messages"))
                        if _t != "New chat":
                            conn.execute("UPDATE conversations SET title=? WHERE id=?", (_t, conv_id))
                if body.get("messages") is not None:
                    conn.execute("UPDATE conversations SET messages=?, updated_at=? WHERE id=?",
                                 (json.dumps(body.get("messages")), now, conv_id))
            conn.commit()
            return _cors_response({"ok": True})
        finally:
            conn.close()

    @app.route("/conversations/<conv_id>", methods=["DELETE"])
    def conversations_delete(conv_id):
        conn = _conv_db()
        try:
            conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
            conn.commit()
            return _cors_response({"ok": True})
        finally:
            conn.close()

    # ── MCP server management (hot add/remove — no restart) ──
    @app.route("/mcp/servers", methods=["GET"])
    def mcp_servers_list():
        return _cors_response({"servers": mntools.get_manager().status()})

    @app.route("/mcp/servers", methods=["POST"])
    def mcp_servers_add():
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return _cors_response({"error": "missing 'name'"}, status=400)
        cfg = {k: v for k, v in data.items() if k in ("command", "args", "env", "url")}
        if not (cfg.get("command") or cfg.get("url")):
            return _cors_response({"error": "need 'command' (stdio) or 'url' (HTTP)"}, status=400)
        srv = mntools.get_manager().add(name, cfg)
        srv.wait_ready(20)
        return _cors_response({"ok": True, "server": mntools.get_manager().status().get(name)})

    @app.route("/mcp/servers/<name>", methods=["DELETE"])
    def mcp_servers_remove(name):
        mntools.get_manager().remove(name)
        return _cors_response({"ok": True, "servers": mntools.get_manager().names()})

    # ── OPTIONS preflight for all routes ──
    @app.route("/v1/chat/completions", methods=["OPTIONS"])
    @app.route("/api/chat/completions", methods=["OPTIONS"])
    @app.route("/v1/models", methods=["OPTIONS"])
    @app.route("/api/tags", methods=["OPTIONS"])
    @app.route("/api/show", methods=["OPTIONS"])
    def preflight():
        return _cors_response({})
    
    # ── Chat completions (non-streaming) ──
    @app.route("/v1/chat/completions", methods=["POST"])
    @app.route("/api/chat/completions", methods=["POST"])
    @app.route("/chat/completions", methods=["POST"])
    def chat_completions():
        _reload_sampling_if_changed()  # live-apply any mneme.yaml sampling edits
        data = request.get_json(force=True)
        stream = data.get("stream", False)
        _cancel_event.clear()  # fresh turn — clear any stale stop request
        # Per-request generation overrides are OPT-IN via a nested `options` object
        # (sent by the swarm orchestrator). Read from `options`, NOT bare top-level
        # fields — a generic OpenAI client's own temperature/max_tokens must NOT
        # silently override this proxy's config (that truncated reasoning-model
        # tool calls when a client sent max_tokens).
        _overrides = data.get("options") or {}
        _gen_opts = {k: _overrides[k] for k in ("temperature", "top_p", "top_k") if k in _overrides} or None
        _max_tokens = _overrides.get("max_tokens")

        print("  [DEBUG] stream={} model={}".format(stream, data.get("model", "?")), flush=True)
        messages = data.get("messages", [])
        
        # Auto-generate a STABLE session ID. When the client supplies a
        # conversation_id (persistent chat), use it directly so every chunk in
        # that conversation shares one id — previously the id only existed on the
        # first turn and every later turn fell back to the literal "default".
        user_count = sum(1 for m in messages if m.get("role") == "user")
        conversation_id = data.get("conversation_id")
        if conversation_id:
            session_id = conversation_id
        elif user_count <= 1:
            # New conversation (no persistent id yet) — generate unique session
            import hashlib
            first_msg = _extract_text(next((m.get("content","") for m in messages if m.get("role") == "user"), ""))
            h = hashlib.md5(first_msg[:100].encode()).hexdigest()[:8]
            session_id = f"conv_{h}_{int(time.time()) % 100000}"
        else:
            session_id = "default"
        
        if stream:
            return _chat_stream(messages, tools=data.get("tools"), session_id=session_id,
                                options=_gen_opts, max_tokens=_max_tokens,
                                conversation_id=data.get("conversation_id"))
        
        result = process_chat(messages, tools=data.get("tools"), session_id=session_id,
                              options=_gen_opts, max_tokens=_max_tokens)

        # Parse [GRADE:] and [STRATEGY:] from model output
        ct = result.get("content", "")
        grade = result.get("_grade", "C")
        # Grade computed by provenance in process_chat

        _sm3 = re.findall(r"STRATEGY:\s*(.+?)(?:\]|$)", ct, re.IGNORECASE)
        sm_strategy = _sm3[0].strip() if _sm3 else ""
        # Strategies are only saved on a GREAT response (grade A) — a pass just
        # archives; a fail records a capability edge. This keeps the strategy
        # library from filling with "ordinary correct answer" noise.
        if sm_strategy and grade == "A":
            try:
                st = str(sm_strategy).strip()
                sid = "strat_" + str(int(time.time()))
                existing_version = 0
                # Semantic dedup: check FAISS for similar strategies
                try:
                    svec = embed(st)
                    if svec is not None and FAISS_OK:
                        hits = _cosine_search(svec, 1, 0.75)
                        for _, cid in hits:
                            if cid.startswith("strat_"):
                                ex = db.execute("SELECT strategy_id, version FROM strategies WHERE strategy_id=?", (cid.replace("strat_", "", 1),)).fetchone()
                                if ex:
                                    existing_version = ex[1]
                                    sid = ex[0]
                                    break
                except Exception:
                    pass
                new_version = existing_version + 1
                with _db_lock:
                    _strat_hist.snapshot(db, sid, "superseded", actor="model", reason="STRATEGY: tag")
                    db.execute("INSERT OR REPLACE INTO strategies "
                               "(strategy_id, problem_type, strategy_text, source_chunk, grade, "
                               "created_at, version, parent_id, effective_grade, use_count, "
                               "success_count, retired, superseded_by, cost, outcome) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, "model", st, "", "A",
                         datetime.now(timezone.utc).isoformat(),
                         new_version, sid if existing_version > 0 else "",
                         0.0, 0, 0, 0, "", 0, "SUCCESS"))
                    _strat_hist.snapshot(db, sid, "saved", actor="model", reason="STRATEGY: tag")
                    db.commit()
                print(f"  [STRATEGY] v{new_version} {st[:60]}...", flush=True)
                # Add to FAISS for future dedup
                try:
                    svec2 = embed(st)
                    if svec2 is not None and FAISS_OK:
                        with faiss_lock():
                            _load_index_from_disk()
                            if _index is not None:
                                _index.add(svec2.reshape(1, -1))
                            _id_map.append(f"strat_{sid}")
                            _save_index()
                except Exception:
                    pass
            except Exception as e:
                print("  [STRATEGY][ERR] " + str(e)[:100], flush=True)
        
        print(f"  [GRADE] Parsed: {grade}", flush=True)

        # (Injected-strategy telemetry is consumed inside process_chat — a
        # second call here would be a no-op and double-log the [CONSUME] line.)

        # Update effectiveness of strategies referenced in this response
        try:
            # Find strategy IDs mentioned in response
            import re as _sre2
            refs = _sre2.findall(r'STRATEGY #([\w]+)', ct)
            for ref_id in refs:
                sid = f"strat_{ref_id}"
                row = db.execute(
                    "SELECT effective_grade, use_count, success_count FROM strategies WHERE strategy_id LIKE ?",
                    (f"%{ref_id}%",)
                ).fetchone()
                if row:
                    old_eg = row[0] or 0.0
                    uc = (row[1] or 0) + 1
                    sc = (row[2] or 0) + (1 if grade in ("A", "B") else 0)
                    grade_val = {"A": 1.0, "B": 0.75, "C": 0.5, "D": 0.25, "F": 0.0}.get(grade, 0.5)
                    new_eg = old_eg * 0.7 + grade_val * 0.3
                    with _db_lock:
                        db.execute(
                            "UPDATE strategies SET effective_grade=?, use_count=?, success_count=? WHERE strategy_id LIKE ?",
                            (new_eg, uc, sc, f"%{ref_id}%")
                        )
                        db.commit()
                    print(f"  [STRATEGY-EFF] #{ref_id} eff={new_eg:.2f} used={uc} success={sc}", flush=True)
        except Exception:
            pass

        # /v1/ prefix = OpenAI format (provider: custom)
        # bare = Ollama format (provider: ollama)
        resp = None
        if request.path.startswith("/v1"):
            msg_obj = {"role": "assistant", "content": result.get("content", "")}
            if result.get("thinking"):
                msg_obj["reasoning"] = result["thinking"]
            if result.get("tool_calls"):
                oai_tc = []
                for i, tc in enumerate(result["tool_calls"]):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    args_str = json.dumps(args) if isinstance(args, dict) else args
                    oai_tc.append({
                        "index": i,
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": args_str,
                        }
                    })
                msg_obj["tool_calls"] = oai_tc
            resp = _cors_response({
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "session_id": session_id,
                "object": "chat.completion",
                "system_fingerprint": "fp_ollama",
                "created": int(time.time()),
                "model": FAKE_MODEL_ID,
                "tool_trace": result.get("tool_trace", []),
                "choices": [{
                    "index": 0,
                    "message": msg_obj,
                    "finish_reason": "tool_calls" if result.get("tool_calls") else "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": result.get("eval_count", 0), "total_tokens": result.get("eval_count", 0)},
            })
            return resp
        else:
            msg = {"role": "assistant", "content": result.get("content", "")}
            if result.get("thinking"):
                msg["thinking"] = result["thinking"]
            if result.get("tool_calls"):
                msg["tool_calls"] = result["tool_calls"]
            resp = _cors_response({
                "model": FAKE_MODEL_ID,
                "session_id": session_id,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "message": msg,
                "tool_trace": result.get("tool_trace", []),
                "done": True,
                "done_reason": result.get("done_reason", "stop"),
                "total_duration": int(time.time() * 1e9) % 1000000000,
                "load_duration": 0,
                "prompt_eval_count": result.get("eval_count", 0),
                "prompt_eval_duration": 0,
                "eval_count": result.get("eval_count", 0),
                "eval_duration": result.get("eval_count", 0) * 1000000,
            })
            return resp
    
    # ── Chat completions (SSE streaming) ──
    def _chat_stream(messages, tools=None, session_id="default", options=None, max_tokens=None,
                     conversation_id=None):
        import queue as _queue
        import threading as _threading

        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        q = _queue.Queue()
        result_holder = {}

        def _worker():
            # Install the token sink in THIS thread (the one running process_chat)
            # so the provider streaming loop emits each piece as it arrives.
            _stream_local.sink = lambda k, t: q.put(("token", k, t))
            turn_messages = list(messages)  # snapshot before process_chat may consume it
            try:
                r = process_chat(messages, tools=tools, session_id=session_id,
                                 options=options, max_tokens=max_tokens)
            except Exception as e:
                r = {"content": "", "thinking": "", "tool_calls": [],
                     "done_reason": "error", "error": str(e)}
            finally:
                _stream_local.sink = None
            # Persist the turn server-side so the reply survives client navigation /
            # refresh / tab-close. (The client still saves as a fast path; this is the
            # durable one that runs even when the client is gone.)
            if conversation_id:
                try:
                    content = r.get("content") or ""
                    if not content:
                        content = "(no response)"
                    _persist_conversation(conversation_id,
                                          turn_messages + [{"role": "assistant", "content": content}])
                except Exception as e:
                    _log_error("chat_stream:persist_conversation", e)
            # Telemetry (was done after process_chat in the old buffered path).
            try:
                _enqueue(_check_suspect_grade, r.get("_grade", "C"), r.get("content", ""), messages)
            except Exception as e:
                _log_error("chat_stream:suspect_grade", e)
            if not MEMORY_ONLY:
                _enqueue(_strategy_lifecycle, r.get("_grade", "C"), messages, r.get("_infra_failure", False))
            result_holder["r"] = r
            q.put(("done", None))

        _threading.Thread(target=_worker, daemon=True).start()

        def _chunk(delta, finish=None, usage=None):
            obj = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": FAKE_MODEL_ID,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage is not None:
                obj["usage"] = usage
            return "data: " + json.dumps(obj) + "\n\n"

        def generate():
            yield _chunk({"role": "assistant"})
            streamed_content = ""
            while True:
                item = q.get()
                if item[0] == "token":
                    _, kind, text = item
                    if kind == "reasoning":
                        yield _chunk({"reasoning": text, "role": "assistant"})
                    else:
                        streamed_content += text
                        yield _chunk({"content": text})
                else:
                    break
            result = result_holder.get("r") or {}
            # Command / settings / retrieval replies (done_reason != "stop") return
            # their content WITHOUT a model call, so no token was emitted — the
            # content would otherwise be dropped and the chat shows "(no response)".
            final_content = result.get("content") or ""
            if final_content and not streamed_content:
                yield _chunk({"content": final_content})
            tool_calls = result.get("tool_calls") or []
            if tool_calls:
                for i, tc in enumerate(tool_calls):
                    fn = tc.get("function", {}) or {}
                    args = fn.get("arguments", "")
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    yield _chunk({"tool_calls": [{
                        "index": i,
                        "id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                        "type": tc.get("type", "function"),
                        "function": {"name": fn.get("name", ""), "arguments": args},
                    }]})
                yield _chunk({}, "tool_calls", {"completion_tokens": result.get("eval_count", 0)})
                yield "data: [DONE]\n\n"
                return
            yield _chunk({}, "stop", {"completion_tokens": result.get("eval_count", 0)})
            yield "data: [DONE]\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            }
        )
    
    # ── Model list (Hermes-compatible) ──
    @app.route("/v1/models", methods=["GET"])
    @app.route("/models", methods=["GET"])
    def list_models():
        return _cors_response({
            "object": "list",
            "data": [{
                "id": FAKE_MODEL_ID,
                "object": "model",
                "owned_by": "text-mokv",
            }],
        })
    
    # ── Single model info (OpenAI-compatible) ──
    @app.route("/v1/models/<model_id>", methods=["GET"])
    def get_model(model_id):
        return _cors_response({
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "text-mokv",
        })
    
    # ── Ollama-style /api/tags (for apps that query it) ──
    @app.route("/api/tags", methods=["GET"])
    def api_tags():
        return _cors_response({
            "models": [{
                "name": FAKE_MODEL_ID,
                "modified_at": datetime.now(timezone.utc).isoformat(),
                "size": 0,
                "digest": "text-mokv-v3",
                "details": {
                    "format": "text-memory",
                    "family": "text-mokv",
                    "parameter_size": "8B",
                },
            }],
        })
    
    # ── Ollama-style /api/show (model info) ──
    @app.route("/api/show", methods=["POST"])
    def api_show():
        data = request.get_json(force=True) or {}
        name = data.get("name", FAKE_MODEL_ID)
        return _cors_response({
            "modelfile": f"# Mneme\n# Model: {MODEL}\n",
            "parameters": "",
            "template": "",
            "details": {
                "format": "text-memory",
                "family": "text-mokv",
                "parameter_size": "8B",
            },
        })
    
    # ── Health check ──
    @app.route("/detail/<chunk_id>", methods=["GET"])
    def detail_chunk(chunk_id):
        chunk = load_chunk(chunk_id)
        if not chunk:
            return _cors_response({"error": "not found"}, status=404)
        parts = []
        for m in chunk.get("messages", []):
            r = m["role"]
            content = m["content"][:DB_MSG_CAP]
            parts.append({"role": r, "content": content})
        return _cors_response({
            "chunk_id": chunk.get("chunk_id"),
            "topic_label": chunk.get("topic_label"),
            "source": chunk.get("source", "unknown"),
            "cycle": chunk.get("cycle", 0),
            "messages": parts,
        })


    @app.route("/health", methods=["GET"])
    def health():
        return _cors_response({
            "status": "ok",
            "model": FAKE_MODEL_ID,
            "backend": MODEL,
            "chunks": len(_id_map),
        })

    @app.route("/status", methods=["GET"])
    def status():
        """Live connectivity for the chat header: chat provider + the two backend
        models (embedder + labeler), each with ok/error/last-checked."""
        def _p(kind):
            st = _PROVIDER_STATUS.get(kind, {})
            return {"ok": st.get("ok"), "error": st.get("error", ""), "at": st.get("at", "")}
        return _cors_response({
            "chat":  {"provider": os.environ.get("MNEME_PROVIDER", "openrouter"),
                      "model": MODEL, **_p("chat")},
            "embed": {"model": EMBED_MODEL, **_p("embed")},
            "label": {"model": LABEL_MODEL, **_p("label")},
        })

    # ── Save: force-flush the staging buffer ──
    @app.route("/search", methods=["POST"])
    def search_memory():
        """Search memory. Returns injectable chunks by default.

        Removed chunks are EXCLUDED unless the caller passes include_removed=true
        (the management page does; the model's search_memory does not). Without
        this, a removed chunk could still be surfaced as a search result — and
        then re-archived into a new chunk that cites it, rebuilding the very
        memory the user just took out of circulation.
        """
        data = request.get_json(force=True)
        query = data.get("query", "")
        top_k = data.get("top_k", 10)
        session = (data.get("session") or "").strip()
        _inc = str(data.get("include_removed", "")).lower() in ("1", "true", "yes", "on")
        vec = embed(query)
        # Over-fetch when a session filter is active so the filter has room to
        # discard out-of-session candidates and still return top_k.
        _fetch_k = top_k * 3 if session else top_k
        results_raw = _cosine_search(vec, _fetch_k, 0.0)
        faiss_results = [(s - BASELINE_NOISE, cid) for s, cid in results_raw if s - BASELINE_NOISE > ROUTE_THRESHOLD]
        # Hybrid: fill with keyword matches if FAISS is sparse
        hybrid = _hybrid_search(query, _fetch_k, faiss_results)
        chunks = []
        for score, chunk_id, method in hybrid:
            row = db.execute(
                "SELECT topic_label, grade, created_at, outcome, source, session_id, cycle, "
                "COALESCE(NULLIF(removed,''),'injectable'), removed_reason "
                "FROM chunks WHERE chunk_id=?", (chunk_id,)
            ).fetchone()
            if not row:
                continue
            if row[7] == "removed" and not _inc:
                continue
            if session and row[5] != session:
                continue
            entry = {"chunk_id": chunk_id, "topic_label": row[0], "grade": row[1], "created_at": row[2], "outcome": row[3], "source": row[4], "session_id": row[5], "cycle": row[6], "similarity": round(score, 4), "method": method,
                     "removed": row[7], "removed_reason": row[8] or ""}
            chunks.append(entry)
        return _cors_response({"results": chunks[:top_k]})


    @app.route("/list", methods=["GET"])
    def list_chunks():
        rows = db.execute("SELECT chunk_id, topic_label, grade, created_at, LENGTH(messages) as size FROM chunks ORDER BY created_at DESC LIMIT 50").fetchall()
        chunks = [{"chunk_id": r[0], "topic_label": r[1], "grade": r[2], "created_at": r[3], "size_chars": r[4]} for r in rows]
        return _cors_response({"chunks": chunks, "total": len(chunks)})


    @app.route("/save", methods=["POST"])
    def save():
        try:
            n = archive_staging()
            return _cors_response({"saved": True, "chunks": n})
        except Exception as e:
            print(f"  [SAVE][ERROR] {e}", flush=True)
            return _cors_response({"saved": False, "error": str(e)}, status=500)

    # ── Memory curation (retraction / review / audit) ──────────────
    # Fixing bad memory through chat: a specific chunk can be marked false and
    # later restored, instead of wiping the whole DB with /reset.

    @app.route("/memory/retract", methods=["POST"])
    def memory_retract():
        """Retract a chunk: {chunk_id, reason?}. Reversible; audited in the log."""
        try:
            data = request.get_json(force=True) or {}
            cid = (data.get("chunk_id") or "").strip()
            if not cid:
                return _cors_response({"retracted": False, "error": "chunk_id required"}, status=400)
            out = curation.retract(db, cid, actor="user", reason=data.get("reason", ""))
            return _cors_response(out)
        except curation.CurationError as e:
            return _cors_response({"retracted": False, "error": str(e)}, status=404)
        except Exception as e:
            print(f"  [CURATION][ERROR] {e}", flush=True)
            return _cors_response({"retracted": False, "error": str(e)}, status=500)

    @app.route("/memory/restore", methods=["POST"])
    def memory_restore():
        """Undo a retraction: {chunk_id, reason?}."""
        try:
            data = request.get_json(force=True) or {}
            cid = (data.get("chunk_id") or "").strip()
            if not cid:
                return _cors_response({"restored": False, "error": "chunk_id required"}, status=400)
            out = curation.restore(db, cid, actor="user", reason=data.get("reason", ""))
            return _cors_response(out)
        except curation.CurationError as e:
            return _cors_response({"restored": False, "error": str(e)}, status=404)
        except Exception as e:
            return _cors_response({"restored": False, "error": str(e)}, status=500)

    @app.route("/memory/log", methods=["GET"])
    def memory_log():
        """The decision log — every retraction/restore/proposal, who and why.
        Optional ?chunk_id=mem_xxx to filter to one chunk."""
        try:
            cid = (request.args.get("chunk_id") or "").strip() or None
            limit = int(request.args.get("limit") or 100)
            return _cors_response({"log": curation.list_log(db, limit=limit, chunk_id=cid)})
        except Exception as e:
            return _cors_response({"log": [], "error": str(e)}, status=500)

    # ── Memory management (the management page) ──────────────────────────
    #
    # NOTE: /memory/proposals, .../confirm and .../deny were REMOVED here. They
    # modelled a two-party approval flow (model proposes -> user confirms -> chunk
    # retracted) that the system does not have and should not have:
    #   - the model cannot remove anything; it can only mark a chunk "bad chunk"
    #   - there is no follow-up step for the model to take
    #   - the page shows flagged chunks via ?proposed=1 and the user acts with
    #     the Remove button, which is the only thing that changes injection
    # Their existence also leaked into the model's behaviour: the model read the
    # presence of a confirm step as evidence it could retract on user approval,
    # and told users "once you confirm, I will proceed with retracting it".
    @app.route("/memory/chunks", methods=["GET"])
    def memory_chunks():
        """Filterable chunk list for the management page.

        Unlike /list (which is the old compact view), this exposes the fields the
        page filters on and INCLUDES removed chunks — seeing them is the entire
        point of the page.

        Query params (all optional, ANDed):
          keyword, source, model, grade, trust, session,
          removed=removed|injectable, proposed=1, self_confirm=1,
          uncorroborated=1, since=YYYY-MM-DD, until=YYYY-MM-DD,
          order=newest|oldest, limit, offset
        """
        def _q(name):
            v = (request.args.get(name) or "").strip()
            return v or None

        def _flag(name):
            return str(request.args.get(name) or "").lower() in ("1", "true", "yes", "on")

        filters = {
            "keyword": _q("keyword"), "source": _q("source"), "model": _q("model"),
            "grade": _q("grade"), "trust": _q("trust"), "session": _q("session"),
            "removed": _q("removed"), "since": _q("since"), "until": _q("until"),
            "order": _q("order") or "newest",
            "proposed": _flag("proposed"), "self_confirm": _flag("self_confirm"),
            "uncorroborated": _flag("uncorroborated"),
        }
        try:
            limit = max(1, min(int(request.args.get("limit") or 100), 500))
            offset = max(0, int(request.args.get("offset") or 0))
        except (TypeError, ValueError):
            limit, offset = 100, 0
        try:
            return _cors_response(curation.list_chunks(db, filters, limit, offset))
        except Exception as e:
            _log_error("memory_chunks", e)
            return _cors_response({"chunks": [], "total": 0,
                                   "error": f"{type(e).__name__}: {e}"}, status=500)

    @app.route("/memory/chunks/<chunk_id>", methods=["GET"])
    def memory_chunk_detail(chunk_id):
        """One chunk in full, INCLUDING removed ones (management view).

        This is the endpoint that makes review possible: the user must be able to
        read a removed chunk's content to decide whether removing it was right.
        """
        try:
            ch = curation._get_chunk(db, chunk_id)
            if not ch:
                return _cors_response({"error": "unknown chunk"}, status=404)
            try:
                ch["messages"] = json.loads(ch.get("messages") or "[]")
            except (ValueError, TypeError):
                pass
            return _cors_response({"chunk": ch})
        except Exception as e:
            _log_error("memory_chunk_detail", e)
            return _cors_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    @app.route("/memory/chunks/<chunk_id>/remove", methods=["POST"])
    def memory_chunk_remove(chunk_id):
        """Set or clear the removed flag. Body: {"removed": true, "reason": "..."}.

        Not a delete — the row and content stay, so this is reversible. Flagging
        only changes what Mneme uses: injection and model search skip it.
        """
        try:
            data = request.get_json(force=True, silent=True) or {}
            removed = data.get("removed", True)
            if isinstance(removed, str):
                removed = removed.lower() in ("1", "true", "yes", "on")
            out = curation.set_removed(db, chunk_id, bool(removed), actor="user",
                                       reason=data.get("reason") or "")
            return _cors_response(out)
        except curation.CurationError as e:
            return _cors_response({"error": str(e)}, status=404)
        except Exception as e:
            _log_error("memory_chunk_remove", e)
            return _cors_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    @app.route("/memory/chunks/remove", methods=["POST"])
    def memory_chunks_remove_bulk():
        """Flag several chunks at once. Body: {"ids": [...], "removed": true, "reason": "..."}

        Flagging is reversible and non-destructive, so bulk is safe — but the
        response reports per-id outcomes rather than a single count, so a typo'd
        id cannot pass silently as success.
        """
        try:
            data = request.get_json(force=True, silent=True) or {}
            ids = data.get("ids") or []
            if not isinstance(ids, list):
                return _cors_response({"error": "ids must be a list"}, status=400)
            ids = [str(i).strip() for i in ids if str(i).strip()]
            if not ids:
                return _cors_response({"error": "no ids given"}, status=400)
            removed = data.get("removed", True)
            if isinstance(removed, str):
                removed = removed.lower() in ("1", "true", "yes", "on")
            ok, failed = [], []
            for cid in ids:
                try:
                    curation.set_removed(db, cid, bool(removed), actor="user",
                                         reason=data.get("reason") or "")
                    ok.append(cid)
                except curation.CurationError as e:
                    failed.append({"chunk_id": cid, "error": str(e)})
            return _cors_response({"removed": bool(removed), "changed": ok,
                                   "failed": failed, "count": len(ok)})
        except Exception as e:
            _log_error("memory_chunks_remove_bulk", e)
            return _cors_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    @app.route("/memory/chunks/<chunk_id>/bad", methods=["POST"])
    def memory_chunk_bad(chunk_id):
        """Set or clear the "bad chunk" flag. Body: {"bad": true, "actor": "user"}.

        A marker, not an action — nothing about what Mneme uses changes. The page
        renders it amber when the model set it and red when the user did.
        """
        try:
            data = request.get_json(force=True, silent=True) or {}
            bad = data.get("bad", True)
            if isinstance(bad, str):
                bad = bad.lower() in ("1", "true", "yes", "on")
            actor = (data.get("actor") or "user").strip().lower()
            out = curation.set_bad_chunk(db, chunk_id, bool(bad), actor=actor,
                                         reason=data.get("reason") or "")
            return _cors_response(out)
        except curation.CurationError as e:
            return _cors_response({"error": str(e)}, status=404)
        except Exception as e:
            _log_error("memory_chunk_bad", e)
            return _cors_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    @app.route("/memory/chunks/bad", methods=["POST"])
    def memory_chunks_bad_bulk():
        """Flag several chunks bad at once. Body: {"ids": [...], "bad": true}"""
        try:
            data = request.get_json(force=True, silent=True) or {}
            ids = data.get("ids") or []
            if not isinstance(ids, list):
                return _cors_response({"error": "ids must be a list"}, status=400)
            ids = [str(i).strip() for i in ids if str(i).strip()]
            if not ids:
                return _cors_response({"error": "no ids given"}, status=400)
            bad = data.get("bad", True)
            if isinstance(bad, str):
                bad = bad.lower() in ("1", "true", "yes", "on")
            actor = (data.get("actor") or "user").strip().lower()
            return _cors_response(curation.set_bad_chunk_many(
                db, ids, bad=bool(bad), actor=actor, reason=data.get("reason") or ""))
        except Exception as e:
            _log_error("memory_chunks_bad_bulk", e)
            return _cors_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    @app.route("/memory/sources", methods=["GET"])
    def memory_sources():
        """Distinct source/model/tag values — populates the page's filter dropdowns
        from real data instead of a hardcoded list that drifts."""
        try:
            srcs = [r[0] for r in db.execute(
                "SELECT DISTINCT source FROM chunks WHERE source != '' ORDER BY source").fetchall()]
            models = [r[0] for r in db.execute(
                "SELECT DISTINCT model FROM chunks WHERE model != '' ORDER BY model").fetchall()]
            trusts = [r[0] for r in db.execute(
                "SELECT DISTINCT trust FROM chunks WHERE trust != '' ORDER BY trust").fetchall()]
            grades = [r[0] for r in db.execute(
                "SELECT DISTINCT grade FROM chunks WHERE grade != '' ORDER BY grade").fetchall()]
            return _cors_response({"sources": srcs, "models": models,
                                   "trusts": trusts, "grades": grades})
        except Exception as e:
            _log_error("memory_sources", e)
            return _cors_response({"sources": [], "models": [], "trusts": [],
                                   "grades": [], "error": str(e)}, status=500)

    @app.route("/memory/lineage/<chunk_id>", methods=["GET"])
    def memory_lineage(chunk_id):
        """What was built on top of this chunk?

        Answers the question that matters when a hallucination is found LATE: not
        "is this chunk bad" (retraction handles that) but "what else did it touch".

        Two relations, both reported per node:
          cites — that chunk's messages named this one as a source
          saw   — that chunk was IN CONTEXT when this one was produced (recorded
                  by the proxy, so it holds even when the model paraphrased)

        ?max_depth=N bounds the walk (default 10).
        """
        try:
            depth = int(request.args.get("max_depth") or 10)
        except (TypeError, ValueError):
            depth = 10
        try:
            return _cors_response(curation.lineage(db, chunk_id, max_depth=depth))
        except curation.CurationError as e:
            return _cors_response({"error": str(e), "descendants": []}, status=404)
        except Exception as e:
            _log_error("memory_lineage", e)
            return _cors_response({"error": f"{type(e).__name__}: {e}",
                                   "descendants": []}, status=500)

    @app.route("/reset", methods=["POST"])
    def reset():
        """Wipe all learned state (memory, strategies, tools, capability edges,
        FAISS index) for a clean test run. The capability harness calls this
        before each trial so no answer leaks in from a warm DB."""
        try:
            _reset_memory()
            return _cors_response({"reset": True})
        except Exception as e:
            print(f"  [RESET][ERROR] {e}", flush=True)
            return _cors_response({"reset": False, "error": str(e)}, status=500)
    
    # ── Learning Mode ──────────────────────────────────────────
    
    @app.route("/mode/learn", methods=["POST"])
    def mode_learn():
        """Proxy-driven learning mode: parameter cycling + strategy extraction.
        POST body: {problem, iterations?, params?}
        Cycles through parameter sets, grades at standard temp, extracts strategies."""
        if MEMORY_ONLY:
            return _cors_response({"error": "learning mode disabled in memory-only build"}, status=403)
        data = request.get_json(force=True)
        problem = data.get("problem", "")
        iterations = min(data.get("iterations", 5), 10)
        custom_params = data.get("params", None)
        
        if not problem:
            return _cors_response({"error": "problem required"}, status=400)
        
        result = _run_learning_mode(problem, iterations, custom_params)
        return _cors_response(result)

    @app.route("/mode/think", methods=["POST"])
    def mode_think():
        """Novelty thinking mode: escape mode collapse.
        POST body: {problem, iterations?, features?}
        Generates a baseline, forbids its modal features, diverges, measures
        embedding distance (objective novelty), and pairwise-judges quality."""
        if MEMORY_ONLY:
            return _cors_response({"error": "thinking mode disabled in memory-only build"}, status=403)
        data = request.get_json(force=True)
        problem = data.get("problem", "")
        iterations = min(data.get("iterations", 4), 8)
        custom_features = data.get("features", None)
        
        if not problem:
            return _cors_response({"error": "problem required"}, status=400)
        
        result = _novelty_thinking_mode(problem, iterations, custom_features)
        return _cors_response(result)

    @app.route("/preferences", methods=["GET", "POST"])
    def preferences():
        """User-preference store: explicit ask/answer loop.
        GET returns stored preferences; POST sets them.
        POST body: {"key": "code_first", "value": "true"} or
                   {"preferences": {"code_first": "true", "detail": "low"}}"""
        try:
            if request.method == "GET":
                rows = db.execute("SELECT pref_key, pref_value FROM preferences ORDER BY pref_key").fetchall()
                return _cors_response({"preferences": {k: v for k, v in rows}})
            data = request.get_json(force=True)
            updates = []
            if "preferences" in data and isinstance(data["preferences"], dict):
                updates = list(data["preferences"].items())
            elif "key" in data and "value" in data:
                updates = [(data["key"], str(data["value"]))]
            _store_preferences(updates)
            rows = db.execute("SELECT pref_key, pref_value FROM preferences ORDER BY pref_key").fetchall()
            return _cors_response({"preferences": {k: v for k, v in rows}})
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

    @app.route("/capabilities", methods=["GET", "POST"])
    def capabilities():
        """Capability-edge store. GET lists flagged + tracked problem types.
        POST can clear a flag: {"clear": "compute"} or force one: {"flag": "compute"}."""
        try:
            if request.method == "GET":
                rows = db.execute(
                    "SELECT problem_type, attempts, failures, last_grade, flagged, updated_at "
                    "FROM capability_edges ORDER BY failures DESC, problem_type"
                ).fetchall()
                edges = [
                    {"problem_type": r[0], "attempts": r[1], "failures": r[2],
                     "last_grade": r[3], "flagged": bool(r[4]), "updated_at": r[5]}
                    for r in rows
                ]
                return _cors_response({"capability_edges": edges})
            data = request.get_json(force=True)
            if "clear" in data:
                with _db_lock:
                    db.execute("UPDATE capability_edges SET flagged=0 WHERE problem_type=?", (data["clear"],))
                    db.commit()
            elif "flag" in data:
                now = datetime.now(timezone.utc).isoformat()
                with _db_lock:
                    db.execute(
                        "INSERT INTO capability_edges (problem_type, attempts, failures, last_grade, flagged, updated_at) "
                        "VALUES (?,1,2,'F',1,?) ON CONFLICT(problem_type) DO UPDATE SET flagged=1, updated_at=excluded.updated_at",
                        (data["flag"], now),
                    )
                    db.commit()
            rows = db.execute(
                "SELECT problem_type, attempts, failures, last_grade, flagged FROM capability_edges ORDER BY failures DESC"
            ).fetchall()
            return _cors_response({"capability_edges": [
                {"problem_type": r[0], "attempts": r[1], "failures": r[2], "last_grade": r[3], "flagged": bool(r[4])}
                for r in rows
            ]})
        except Exception as e:
            return _cors_response({"error": str(e)}, status=500)

# ─── Embedding health + recovery ──────────────────────────────

def _reembed_pending(limit: int = 200):
    """Re-embed chunks stored unembedded (pending_embed=1) due to an embed
    failure. Runs at startup; returns number re-embedded."""
    rows = db.execute(
        "SELECT chunk_id, messages FROM chunks WHERE pending_embed = 1 LIMIT ?",
        (limit,),
    ).fetchall()
    fixed = 0
    for cid, msgs_json in rows:
        try:
            msgs = json.loads(msgs_json)
        except Exception:
            continue
        text = " ".join(
            _extract_text(m.get("content", ""))
            for m in msgs if isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
        )
        if not text.strip():
            continue
        vec = embed(text)
        if vec is None:
            continue  # embedder still down — leave pending, retry next startup
        db.execute(
            "UPDATE chunks SET vector=?, pending_embed=0, embed_model=?, dim=? WHERE chunk_id=?",
            (_vec_to_blob(vec), EMBED_MODEL, DIM, cid),
        )
        db.commit()
        with faiss_lock():
            _load_index_from_disk()
            if FAISS_OK and _index is not None:
                _index.add(vec.reshape(1, -1))
            if cid not in _id_map:
                _id_map.append(cid)
            _save_index()
        fixed += 1
        print(f"  [EMBED-RETRY] {cid} re-embedded ({len(text)} chars)", flush=True)
    if fixed:
        print(f"  [EMBED-RETRY] re-embedded {fixed} pending chunks", flush=True)
    return fixed


def _embedding_health_check() -> bool:
    """Startup: verify the embedder returns DIM-dim vectors, and detect any
    stored vector that can't be used with the current embedder — a conflicting
    dim, or a different embedding model (same dim but a different semantic
    space). Those chunks are marked pending_embed and re-embedded on startup,
    so a DB copied between machines with different embedders self-heals."""
    ok = True
    # 1. Probe the embedder
    probe = embed("__mneme_health_probe__")
    if probe is None:
        print("  [HEALTH][FATAL] Embedder not responding (Ollama down / model missing). "
              "New chunks will be stored pending_embed until it recovers.", flush=True)
        ok = False
    elif probe.shape[0] != DIM:
        print(f"  [HEALTH][FATAL] Embedder returned dim {probe.shape[0]}, expected {DIM}. "
              f"All embeds will mismatch the FAISS index.", flush=True)
        ok = False
    else:
        print(f"  [HEALTH] embedder OK: {EMBED_MODEL} dim={probe.shape[0]}", flush=True)
    # 2. Conflicting dim: wrong-length vectors are unusable — mark for re-embed.
    bad = db.execute(
        "UPDATE chunks SET vector=NULL, pending_embed=1 "
        "WHERE vector IS NOT NULL AND length(vector) != ?",
        (DIM * 4,),
    ).rowcount
    db.commit()
    if bad:
        print(f"  [HEALTH][MIGRATE] {bad} chunks had a different dim ({DIM} expected) — "
              f"marked pending_embed for re-embedding", flush=True)
        ok = False
    # 3. Different embedding model (same dim, different semantic space): the
    # vectors are meaningless against the current model — mark for re-embed.
    migrated = db.execute(
        "UPDATE chunks SET vector=NULL, pending_embed=1 "
        "WHERE vector IS NOT NULL AND embed_model != '' AND embed_model != ?",
        (EMBED_MODEL,),
    ).rowcount
    db.commit()
    if migrated:
        print(f"  [HEALTH][MIGRATE] {migrated} chunks were embedded with a different "
              f"model (now {EMBED_MODEL}) — marked pending_embed for re-embedding", flush=True)
    # 4. Backfill embed_model/dim metadata on remaining (correctly-embedded) chunks
    n = db.execute(
        "UPDATE chunks SET embed_model=?, dim=? WHERE vector IS NOT NULL AND dim=0",
        (EMBED_MODEL, DIM),
    ).rowcount
    db.commit()
    if n:
        print(f"  [HEALTH] backfilled embed_model/dim on {n} chunks", flush=True)
    return ok


# ─── Startup ───────────────────────────────────────────────────

def _dump_config():
    """Print the effective resolved config so a missed/typo'd value is visible."""
    print(f"  [CONFIG] file={CONFIG_PATH or '(none — env/defaults)'}", flush=True)
    print(f"  [CONFIG] backend={MNEME_BACKEND} provider={os.environ.get('MNEME_PROVIDER', 'openrouter')}", flush=True)
    print(f"  [CONFIG] model={MODEL} embed={EMBED_MODEL} label={LABEL_MODEL}", flush=True)
    if _backend_is_openai():
        print(f"  [CONFIG] base_url={OR_BASE_URL}", flush=True)
    print(f"  [CONFIG] chunk_dir={CHUNK_DIR} port={PORT} inject_system={INJECT_SYSTEM}", flush=True)
    print(f"  [CONFIG] sampling temp={OLLAMA_TEMP} top_p={os.environ.get('MNEME_TOP_P','0.95')} top_k={os.environ.get('MNEME_TOP_K','64')} ctx={os.environ.get('MNEME_CTX_TOKENS','65536')}", flush=True)
    print(f"  [CONFIG] timeouts chat={CHAT_TIMEOUT} ollama={OLLAMA_CHAT_TIMEOUT} first_token={FIRST_TOKEN_TIMEOUT} stale_chunk={STALE_CHUNK_TIMEOUT} embed={EMBED_TIMEOUT} label={LABEL_TIMEOUT}", flush=True)
    print(f"  [CONFIG] staging_turns={STAGING_TURNS} idle={STAGING_IDLE} recent_extra={CONTEXT_RECENT_EXTRA} belief_evolution={os.environ.get('MNEME_BELIEF_EVOLUTION','0')}", flush=True)
    print(f"  [CONFIG] retrieval route={ROUTE_THRESHOLD} classify={CLASSIFY_THRESHOLD} inject_min_sim={INJECT_MIN_SIMILARITY} keyword_fallback={int(KEYWORD_FALLBACK)} injected_tokens={MAX_INJECTED_TOKENS}", flush=True)
    print(f"  [TOOLS] enabled={sorted(mntools.enabled_readonly_names() | mntools.native_exec_names(None))}", flush=True)
    print(f"  [MEMORY] enabled={'yes' if MEMORY_ENABLED else 'no'}", flush=True)


_embedding_health_check()
_load_index()
_seed_chunk_seq()
# Materialize every injected prompt to disk ($MNEME_CHUNK_DIR/instructions/default/*.txt)
# so they are readable/editable like system_prompt.md — no code edit needed to tune a
# prompt. Only creates missing files; user edits are never overwritten.
materialize_instructions()
# Re-embed pending chunks in the BACKGROUND — not synchronously — so startup is
# not blocked on N sequential embed round-trips (the non-blocking chunk path stores
# them pending_embed and defers embedding to here). The bg worker queue reuses the
# check_same_thread=False connection and the faiss file lock, so it is safe.
_enqueue(_reembed_pending)
# Calibrate noise baseline AFTER FAISS is loaded, then CLAMP it below the inject
# floor. _calibrate_noise() embeds random gibberish and measures its similarity
# to the nearest chunks; that value drifts UP as the corpus grows (more chunks =
# higher chance of a coincidental close match) and in an anisotropic embedding
# space unrelated content already has a positive floor. If the noise baseline
# reaches INJECT_MIN_SIMILARITY, the dynamic-K step (score = sim - BASELINE_NOISE)
# rejects every chunk that clears the threshold — silently raising the effective
# floor. Clamp it so it can never approach the inject floor.
_raw_noise = _calibrate_noise()
BASELINE_NOISE = _clamp_noise_baseline(_raw_noise)
print(f"  [STARTUP] Noise baseline: {BASELINE_NOISE:.4f} "
      f"(raw {_raw_noise:.4f}, clamp {INJECT_MIN_SIMILARITY - 0.15:.4f})", flush=True)
_dump_config()
_init_harness()
print(f"[mokv] Mneme ready. model={MODEL} chunks={len(_id_map)} db={DB_PATH}",
      flush=True)

if __name__ == "__main__":
    if FLASK_OK:
        _enqueue(_gc_images)   # startup sweep (grace period still applies)
        _start_gc_loop()       # periodic sweep
        # Bind to localhost by default. Mneme has NO AUTHENTICATION and exposes
        # `bash`, `write`, filesystem reads and arbitrary MCP tools — a
        # network-reachable instance is remote code execution for anyone who can
        # reach the port. Opt in to a wider bind EXPLICITLY and knowingly:
        #     MNEME_BIND=0.0.0.0
        # Only do that behind a tunnel/VPN/firewall. See the Security section of
        # the README.
        _bind = os.environ.get("MNEME_BIND", "127.0.0.1").strip() or "127.0.0.1"
        if _bind not in ("127.0.0.1", "localhost", "::1"):
            print(f"  [WARN] binding to {_bind} — Mneme has no auth and exposes "
                  f"bash/file tools. Only do this behind a tunnel or firewall.",
                  flush=True)
        app.run(host=_bind, port=PORT, threaded=True)
    else:
        print("[mokv] Flask not installed. Import as module for programmatic use.",
              flush=True)
