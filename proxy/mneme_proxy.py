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

import json, os, re, sqlite3, sys, threading, time, uuid, struct, queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import numpy as np
import requests

from mneme.util import _extract_text, _log_error
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
from mneme.instructions import _load_instruction, materialize_instructions, list_instructions, save_instruction, _instructions_dir
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

# ─── Config file loading ────────────────────────────────────────
# A single config file (YAML or JSON) holds every tunable. Loaded BEFORE the
# constants below so config values flow into them via env-var defaults.
# Precedence: environment variable > config file > built-in default.
# See docs/config-spec.md for the full schema.

CONFIG_PATH: Optional[str] = None
CONFIG_DATA: Dict = {}           # raw sections for runtime lookup (providers, models)
_PROVIDER_HEADERS: Dict = {}     # extra headers from the active provider block
_OR_FALLBACK_MODELS: list = []   # OpenRouter model fallbacks (the `models` array)
_OR_PROVIDER_PREF: Dict = {}     # OpenRouter provider routing prefs (ignore/order/...)
_OR_STREAM: bool = True          # OpenRouter stream toggle (non-streaming enables OR failover)

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
    "timeouts.chat_timeout": "MNEME_CHAT_TIMEOUT",
    "timeouts.ollama_chat_timeout": "MNEME_OLLAMA_CHAT_TIMEOUT",
    "timeouts.first_token_timeout": "MNEME_FIRST_TOKEN_TIMEOUT",
    "timeouts.novelty_timeout": "MNEME_NOVELTY_TIMEOUT",
    "timeouts.embed_timeout": "MNEME_EMBED_TIMEOUT",
    "timeouts.label_timeout": "MNEME_LABEL_TIMEOUT",
    "timeouts.edge_failures": "MNEME_EDGE_FAILURES",
    "timeouts.edge_ratio": "MNEME_EDGE_RATIO",
    "storage.chunk_dir": "MNEME_CHUNK_DIR",
    "storage.port": "MNEME_PORT",
    "storage.inject_system": "MNEME_INJECT_SYSTEM",
    "storage.memory_only": "MNEME_MEMORY_ONLY",
    "storage.staging_turns": "MNEME_STAGING_TURNS",
    "storage.staging_idle": "MNEME_STAGING_IDLE",
    "storage.belief_evolution": "MNEME_BELIEF_EVOLUTION",
    "retrieval.max_injected_tokens": "MNEME_MAX_INJECTED_TOKENS",
    "retrieval.route_threshold": "MNEME_ROUTE_THRESHOLD",
    "retrieval.classify_threshold": "MNEME_CLASSIFY_THRESHOLD",
    "retrieval.baseline_noise": "MNEME_BASELINE_NOISE",
    "retrieval.inject_min_similarity": "MNEME_INJECT_MIN_SIMILARITY",
    "retrieval.strategy_min_similarity": "MNEME_STRATEGY_MIN_SIMILARITY",
    "retrieval.keyword_fallback": "MNEME_KEYWORD_FALLBACK",
    "retrieval.age_decay_days": "MNEME_AGE_DECAY_DAYS",
    "retrieval.max_siblings": "MNEME_MAX_SIBLINGS",
    "retrieval.max_chunk_words": "MNEME_MAX_CHUNK_WORDS",
    "retrieval.max_chunk_size": "MNEME_MAX_CHUNK_SIZE",
    "caps.max_history_messages": "MNEME_MAX_HISTORY_MESSAGES",
    "caps.db_msg_cap": "MNEME_DB_MSG_CAP",
    "caps.compress_threshold": "MNEME_COMPRESS_THRESHOLD",
    "caps.compress_max_tok": "MNEME_COMPRESS_MAX_TOK",
    "caps.max_tool_forward": "MNEME_MAX_TOOL_FORWARD",
    "caps.chunk_size": "MNEME_CHUNK_SIZE",
    "tools.native": "MNEME_NATIVE_TOOLS",
    "tools.dir": "MNEME_TOOLS_DIR",
    "tools.bash_timeout": "MNEME_TOOLS_BASH_TIMEOUT",
    "tools.inject_min_similarity": "MNEME_TOOL_INJECT_MIN_SIMILARITY",
    "tools.inject_max": "MNEME_TOOL_INJECT_MAX",
    "tools.inject_tokens": "MNEME_TOOL_INJECT_TOKENS",
    # top-level backward-compat keys (old flat env-var names)
    "model": "MNEME_MODEL",
    "embed_model": "EMBED_MODEL",
    "label_model": "LABEL_MODEL",
    "ollama_url": "MNEME_OLLAMA_URL",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "openrouter_base_url": "OPENROUTER_BASE_URL",
}

_STRUCTURAL_SECTIONS = {"providers", "models"}


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
    cd = os.environ.get("MNEME_CHUNK_DIR")
    if cd and os.path.exists(os.path.join(cd, "mneme.yaml")):
        return os.path.join(cd, "mneme.yaml")
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
        if not isinstance(val, dict):
            raise SystemExit(f"[CONFIG] {path}: section '{section}' must be a mapping")
        for key, v in val.items():
            flat = f"{section}.{key}"
            env = _CONFIG_ENV_MAP.get(flat)
            if env is None:
                raise SystemExit(
                    f"[CONFIG] {path}: unknown key '{flat}' (typo?) — see docs/config-spec.md"
                )
            if os.environ.get(env) is None and v is not None:
                os.environ[env] = _config_scalar(v)


def _resolve_provider():
    """Resolve the active OpenAI-compatible provider's connection details into
    the flat env vars the code reads (base URL, API key, model names)."""
    global _PROVIDER_HEADERS, _OR_FALLBACK_MODELS, _OR_PROVIDER_PREF, _OR_STREAM
    backend_type = os.environ.get("MNEME_BACKEND", "ollama")
    if backend_type not in ("openai", "openrouter"):
        return
    name = os.environ.get("MNEME_PROVIDER", "openrouter")
    prov = (CONFIG_DATA.get("providers") or {}).get(name) or {}
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
        if k and os.environ.get("OPENROUTER_API_KEY") is None:
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


def load_config():
    global CONFIG_PATH
    path = _find_config_path()
    if not path:
        return
    CONFIG_PATH = path
    data = _parse_config_file(path)
    _apply_config(data, path)
    _resolve_provider()
    # Expand ~ in the chunk dir (config files are the natural place to fix the
    # pod-path default /workspace/mneme_chunks on a laptop).
    cd = os.environ.get("MNEME_CHUNK_DIR")
    if cd and cd.startswith("~"):
        os.environ["MNEME_CHUNK_DIR"] = os.path.expanduser(cd)
    print(f"  [CONFIG] loaded {path}", flush=True)


# ─── Config ────────────────────────────────────────────────────
load_config()
mntools.reload_config()  # tools.py is imported before load_config(); refresh its env-derived knobs

OLLAMA_URL  = os.environ.get("MNEME_OLLAMA_URL", "http://localhost:11434")
MODEL       = os.environ.get("MNEME_MODEL", "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest")

# Backend: "ollama" (native API) | "openai"/"openrouter" (OpenAI-compatible, hosted).
# "openrouter" is an alias for "openai" — OpenRouter is just an OpenAI-compatible
# aggregator. Provider connection details come from the config `providers:` block
# or env vars (OPENROUTER_BASE_URL / OPENROUTER_API_KEY / model names).
MNEME_BACKEND = os.environ.get("MNEME_BACKEND", "ollama")
OR_API_KEY    = os.environ.get("OPENROUTER_API_KEY", "")
OR_BASE_URL   = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


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
INJECT_SYSTEM = os.environ.get("MNEME_INJECT_SYSTEM", "1")  # "0" to skip Mneme instructions injection
MEMORY_ONLY = os.environ.get("MNEME_MEMORY_ONLY", "0") == "1"  # "1" = inject memory chunks only (light memory-explainer prompt, no tagging/meta-principles/directives)
PORT        = int(os.environ.get("MNEME_PORT", "8080"))
DB_PATH     = os.path.join(CHUNK_DIR, "mneme.db")

# Sampling defaults (per-model overrides live in config `models:`)
OLLAMA_TEMP    = float(os.environ.get("MNEME_TEMPERATURE", "0.3"))

# ─── Multi-pass compression config ───
MAX_HISTORY_MESSAGES = int(os.environ.get("MNEME_MAX_HISTORY_MESSAGES", "32"))  # trim conversation to keep predict budget free
CHUNK_SIZE   = int(os.environ.get("MNEME_CHUNK_SIZE", "3000"))  # chars per chunk (was defined twice: 2000 then 3000 — collapsed)
DB_MSG_CAP   = int(os.environ.get("MNEME_DB_MSG_CAP", "8000"))  # chars per message stored in SQLite (full content)
COMPRESS_THRESHOLD = int(os.environ.get("MNEME_COMPRESS_THRESHOLD", "500"))  # chars — tool results larger than this get staged
MAX_TOOL_FORWARD = int(os.environ.get("MNEME_MAX_TOOL_FORWARD", "12000"))  # chars — cap on a tool result forwarded to the model (head+tail window)
COMPRESS_MODEL     = MODEL   # use same model for compression
COMPRESS_MAX_TOK   = int(os.environ.get("MNEME_COMPRESS_MAX_TOK", "2048"))  # max tokens for compression response

# Staging: archive after N user turns or idle seconds
STAGING_TURNS  = int(os.environ.get("MNEME_STAGING_TURNS", "6"))
STAGING_IDLE   = int(os.environ.get("MNEME_STAGING_IDLE", "120"))

# Routing thresholds (same as KV version)
CLASSIFY_THRESHOLD = float(os.environ.get("MNEME_CLASSIFY_THRESHOLD", "0.78"))
ROUTE_THRESHOLD    = float(os.environ.get("MNEME_ROUTE_THRESHOLD", "0.08"))  # tunable: raise for stricter matching, lower for more recall
BASELINE_NOISE     = float(os.environ.get("MNEME_BASELINE_NOISE", "0.20"))  # fallback — overridden at startup by _calibrate_noise()
# Absolute injection floor: a chunk is injected only if its raw cosine similarity
# is >= this value; below it, nothing is injected. Tunable per setup — embedder and
# corpus similarity scales differ, so this needs hand-tuning on each deployment.
# Default 0.62 sits above the voyage-4-lite noise floor (~0.48) and below its
# relevant-signal band (~0.70-0.72).
INJECT_MIN_SIMILARITY = float(os.environ.get("MNEME_INJECT_MIN_SIMILARITY", "0.62"))
# Strategy-only floor (below the memory floor). A chunk below INJECT_MIN_SIMILARITY
# does NOT inject as memory, but if it sits at/above this floor its LINKED
# strategies still inject — strategies are meant to generalize (same-concept,
# medium similarity) where memory is same-topic (high similarity). Measured on
# voyage-4-lite: same-concept sits ~0.43-0.62 (docs/strategy-retrieval-spec.md).
STRATEGY_MIN_SIMILARITY = float(os.environ.get("MNEME_STRATEGY_MIN_SIMILARITY", "0.55"))
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
FIRST_TOKEN_TIMEOUT = int(os.environ.get("MNEME_FIRST_TOKEN_TIMEOUT", "180"))
CONNECT_TIMEOUT = 15  # TCP+TLS connect timeout for OpenAI-style calls
NOVELTY_TIMEOUT = int(os.environ.get("MNEME_NOVELTY_TIMEOUT", "600"))
EMBED_TIMEOUT = int(os.environ.get("MNEME_EMBED_TIMEOUT", "60"))
LABEL_TIMEOUT = int(os.environ.get("MNEME_LABEL_TIMEOUT", "30"))

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
_pending_links_lock = threading.Lock()
# Single sqlite connection shared by the main thread + 2 background workers
# (check_same_thread=False). Writes must be serialized: an unguarded commit()
# racing another thread's commit on the same connection raises
# "cannot commit - no transaction is active". Wrap every write+commit pair
# (save_chunk, _save_strategy, _archive_single_chunk) in this lock.
_db_lock = threading.RLock()

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

def _enqueue(fn, *args, **kwargs):
    """Submit a background job. Workers are started lazily on first enqueue."""
    _start_bg_workers()
    _BG_QUEUE.put((fn, args, kwargs))

# ─── Database ──────────────────────────────────────────────────

db = sqlite3.connect(DB_PATH, check_same_thread=False)
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
):
    try:
        db.execute(migration)
    except sqlite3.OperationalError:
        pass  # column already exists
db.commit()
# Backfill: mark failure-derived strategies as FAILURE so they inject under the
# "do NOT do this" header rather than as success examples. Only matches the old
# "FAILURE on:"/"TRUNCATED on:" text — new failures set outcome at insert time.
try:
    db.execute("UPDATE strategies SET outcome='FAILURE' WHERE strategy_text LIKE 'FAILURE on:%' OR strategy_text LIKE 'TRUNCATED on:%'")
    # Older code saved strategies with problem_type='model' placeholder, which
    # never matches a query's classified type (so they never injected). Fold them
    # back to 'other'.
    db.execute("UPDATE strategies SET problem_type='other' WHERE problem_type='model'")
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
FAISS_INDEX_FILE = os.path.join(CHUNK_DIR, "faiss.index")
FAISS_IDMAP_FILE = os.path.join(CHUNK_DIR, "faiss.idmap")
FAISS_LOCK_FILE   = os.path.join(CHUNK_DIR, "faiss.lock")

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

# arctic-embed2 token limit is 8192; ~4000 chars is safely under even with
# dense prose. Overlap keeps sentence context across chunk boundaries.
CHUNK_CHARS    = 4000
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
    """Embed one chunk. Ollama /api/embeddings by default (the embed model is
    always local snowflake-arctic-embed2, 1024-dim, so existing chunk vectors stay
    valid even when the main chat model runs on OpenRouter). Raises on failure."""
    if _aux_backend("MNEME_EMBED_BACKEND") in ("openai", "openrouter"):
        r = requests.post(
            f"{OR_BASE_URL}/embeddings",
            headers=_or_headers(),
            json={"model": EMBED_MODEL, "input": text},
            timeout=EMBED_TIMEOUT,
        )
        r.raise_for_status()
        v = np.array(r.json()["data"][0]["embedding"], dtype=np.float32)
    else:
        r = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=EMBED_TIMEOUT,
        )
        r.raise_for_status()
        v = np.array(r.json()["embedding"], dtype=np.float32)
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
            return _embed_single(chunks[0])
        vecs = [_embed_single(c) for c in chunks]
        pooled = pool_embeddings(vecs)
        print(f"  [EMBED] chunked {len(text)} chars into {len(chunks)} windows "
              f"-> pooled centroid", flush=True)
        return pooled
    except Exception as e:
        print(f"  [EMBED][ERROR] {type(e).__name__}: {e} — returning None (pending_embed)",
              flush=True)
        return None


mntools.embed = embed  # bind the tool system's embed function


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

SYSTEM_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "system_prompt.md")
def _load_system_prompt():
    try:
        with open(SYSTEM_PROMPT_FILE) as f:
            return f.read().strip()
    except Exception as e:
        _log_error("_load_system_prompt", e)
        return "You are a helpful AI assistant."

SYSTEM_PROMPT = _load_system_prompt()

SYSTEM_PROMPT_MEMORY_FILE = os.path.join(os.path.dirname(__file__), "system_prompt_memory.md")
def _load_system_prompt_memory():
    try:
        with open(SYSTEM_PROMPT_MEMORY_FILE) as f:
            return f.read().strip()
    except Exception as e:
        _log_error("_load_system_prompt_memory", e)
        return _load_system_prompt()

SYSTEM_PROMPT_MEMORY = _load_system_prompt_memory()


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
    prompt = SYSTEM_PROMPT_MEMORY if MEMORY_ONLY else SYSTEM_PROMPT
    block = "=== MNEME INSTRUCTIONS ===\n" + prompt
    if MISSION:
        block += "\n\n" + MISSION
    return block + "\n\n"


def _finalize_context(ctx: str) -> str:
    """Append the context-budget line. The system prompt is no longer prepended
    here — it is the fixed system-message prefix added separately by
    _system_prompt_block(), so the variable memory can sit at the tail (cacheable
    conversation prefix) instead of the head."""
    # Context budget line (model suggestion #1): tell the model how much window
    # is left so it can decide search-more vs synthesize instead of guessing.
    _total = int(os.environ.get("MNEME_CTX_TOKENS", "256000"))
    _reserve = int(os.environ.get("MNEME_COMPLETION_RESERVE", "8192"))
    _used = _estimate_tokens(ctx)
    _remaining = max(0, _total - _reserve - _used)
    return ctx + (f"\n\n[context budget: {_total} token window, ~{_used} used, "
                  f"~{_remaining} remaining for tool results + answer]")

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


def _query_openrouter(msgs, opts, tools=None, format_schema=None,
                      max_tokens=-1, timeout=None, model=None) -> dict:
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
    # Reasoning controls (opt-in, model-specific):
    #   MNEME_REASONING_EFFORT=low|high|max  -> reasoning.effort (models WITH effort levels,
    #       e.g. deepseek xhigh|high, Ox Alpha low|high|max).
    #   MNEME_REASONING_ENABLED=0/off        -> reasoning.enabled=false (Qwen3.6-style models:
    #       thinking is binary on/off, no effort levels. Disabling = "instruct mode", fast,
    #       zero thinking tokens. Qwen's "thinking_budget"/reasoning.max_tokens is NOT a cap —
    #       the model treats it as a goal and over-thinks MORE, so don't use it.)
    _reasoning = {}
    _reffort = os.environ.get("MNEME_REASONING_EFFORT", "")
    if _reffort:
        _reasoning["effort"] = _reffort
    if os.environ.get("MNEME_REASONING_ENABLED", "").strip().lower() in ("0", "false", "off", "no", "disabled"):
        _reasoning["enabled"] = False
    if _reasoning:
        payload["reasoning"] = _reasoning
    _mt = max_tokens if (max_tokens and max_tokens > 0) else None
    if _mt is None:
        _mc = (CONFIG_DATA.get("models") or {}).get(_model) or {}
        _mt = _mc.get("max_tokens") or int(os.environ.get("MNEME_MAX_TOKENS", "0"))
    if _mt and int(_mt) > 0:
        payload["max_tokens"] = int(_mt)
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
    if _OR_FALLBACK_MODELS:
        payload["models"] = [_model] + [str(m) for m in _OR_FALLBACK_MODELS]
    if _OR_PROVIDER_PREF:
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
                              json=payload, timeout=timeout)
        except requests.exceptions.RequestException as e:
            print(f"  [GRIND-GUARD] OpenRouter request failed ({type(e).__name__}: {e}) — aborting", flush=True)
            return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0,
                    "done_reason": "timeout"}
        try:
            obj = r.json()
        except ValueError:
            print(f"  [GRIND-GUARD] OpenRouter non-JSON response (status {r.status_code}) — aborting", flush=True)
            return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0,
                    "done_reason": "timeout"}
        if obj.get("error"):
            _err = obj["error"]
            _meta = _err.get("metadata") or {}
            _etype = _meta.get("error_type", "unmapped")
            print(f"  [GRIND-GUARD] OpenRouter error ({_etype} {_err.get('code', '')}: "
                  f"{str(_err.get('message', ''))[:100]}) — retryable", flush=True)
            return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0,
                    "done_reason": "error", "error_type": _etype, "provider": obj.get("provider", "?")}
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
                          json=payload, stream=True, timeout=(CONNECT_TIMEOUT, FIRST_TOKEN_TIMEOUT))
        # OpenRouter streams text/event-stream with no charset, so requests falls
        # back to ISO-8859-1 for iter_lines(decode_unicode=True) and mojibakes
        # every multi-byte UTF-8 char (— -> â, ° -> Â°). Force UTF-8 before reading.
        r.encoding = "utf-8"
    except requests.exceptions.RequestException as e:
        print(f"  [GRIND-GUARD] OpenRouter request failed ({type(e).__name__}: {e}) — aborting", flush=True)
        return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "timeout"}

    content_parts = []
    reasoning_parts = []
    tc_slots = {}          # index -> accumulator for streamed tool-call deltas
    finish_reason = None
    provider = "?"
    completion_tokens = 0
    got_first = False

    def _bump_socket():
        # After the first token, extend the read timeout from the short TTFT to
        # the full timeout so a steady-but-slow generation isn't cut off. Best
        # effort — if the socket handle can't be reached, keep the short timeout
        # (harmless for the fast cloud models on this path).
        try:
            r.raw._fp.fp.raw._sock.settimeout(timeout)
        except Exception:
            try:
                r.raw._fp.fp.raw.settimeout(timeout)
            except Exception:
                pass

    try:
        for raw in r.iter_lines(decode_unicode=True):
            if not got_first:
                got_first = True
                _bump_socket()
            if not raw:
                continue
            raw = raw.strip()
            if not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
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
            if delta.get("reasoning"):
                reasoning_parts.append(delta["reasoning"])
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
        # No first token -> hung provider. Mid-stream stall -> incomplete answer.
        # Both are retryable: signal "timeout" with 0 tokens so the retry logic
        # (which keys off done_reason=="timeout" and eval_count==0) fires.
        tag = "no first token" if not got_first else "mid-response stall"
        print(f"  [GRIND-GUARD] OpenRouter stream aborted ({tag}) ({type(e).__name__}: {e}) — aborting", flush=True)
        return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "timeout"}
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
    if finish_reason:
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
                backend: str = None) -> dict:
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
            mc = _extract_text(m.get("content", ""))
            new_m = {"role": m["role"], "content": mc}
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
    # config `models:` block (P8). Explicit per-call `options` still win.
    _model_cfg = (CONFIG_DATA.get("models") or {}).get(MODEL) or {}
    opts = {
        "temperature": temperature if temperature is not None else float(os.environ.get("MNEME_TEMPERATURE", "0.3")),
        "top_p": float(os.environ.get("MNEME_TOP_P", "0.95")),
        "top_k": int(os.environ.get("MNEME_TOP_K", "64")),
    }
    for _k in ("temperature", "top_p", "top_k"):
        if _model_cfg.get(_k) is not None:
            opts[_k] = float(_model_cfg[_k])
    if options:
        opts.update(options)
    
    payload = {
        "model": _model, "stream": False, "messages": msgs,
        "options": opts
    }
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
    ctx_tokens = int(os.environ.get("MNEME_CTX_TOKENS", "256000"))  # 256k default; deepseek-v4-flash has 1M ctx
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
        return _query_openrouter(msgs, opts, tools, format_schema, max_tokens, timeout, _model)
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=timeout)
    except requests.exceptions.ReadTimeout:
        print(f"  [GRIND-GUARD] Ollama generation exceeded {timeout}s — aborting (done_reason=timeout)", flush=True)
        return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "timeout"}
    d = r.json()
    if "error" in d:
        print(f"  [ERROR] Ollama returned: {d['error']}", flush=True)
        return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "error"}
    msg = d.get("message", {})
    content = msg.get("content", "")
    thinking = msg.get("thinking", "")
    tool_calls = msg.get("tool_calls", [])
    # Reasoning models (gemma4) sometimes put the answer in "thinking" and leave
    # "content" empty. Fall back to thinking so generations aren't dropped.
    # BUT NOT when the model emitted tool_calls — those must stay tool_calls
    # (filling content with thinking would mask the pending tool call).
    if not content and thinking and not tool_calls:
        content = thinking
    result = {
        "content": content,
        "thinking": thinking,
        "tool_calls": tool_calls,
        "eval_count": d.get("eval_count", 0),
        "done_reason": d.get("done_reason", "?"),
    }
    if not result["content"] and not result["tool_calls"]:
        print(f"  [WARN] Empty content from Ollama. done_reason={result['done_reason']} eval_count={result['eval_count']}", flush=True)
    return result


def query_model(messages: list, system: str = None, temperature: float = None,
                max_tokens: int = None, tools: list = None, options: dict = None,
                timeout: Optional[int] = None, format_schema=None, model: str = None,
                backend: str = None) -> dict:
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
                            options, timeout, format_schema, model, backend)
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
    pending = 1 if vector is None else 0
    blob = _vec_to_blob(vector) if vector is not None else None
    msgs_json = json.dumps(
        [{"role": m["role"], "content": m["content"][:DB_MSG_CAP]} for m in messages]
    )

    with _db_lock:
        db.execute("""
            INSERT OR REPLACE INTO chunks
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (chunk_id, topic_label, msgs_json, thinking[:MAX_THINKING_STORE], strategy,
              blob, grade, consensus, outcome, problem_type,
              source, _current_cycle(), datetime.now(timezone.utc).isoformat(), session_id,
              1 if is_indexable else 0, "",
              pending, EMBED_MODEL if vector is not None else "", DIM if vector is not None else 0))
        db.commit()
    
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
    if is_indexable and vector is not None and os.environ.get("MNEME_BELIEF_EVOLUTION", "0") == "1":
        _enqueue(_check_belief_evolution, chunk_id, topic_label)

def load_chunk(chunk_id: str) -> Optional[dict]:
    row = db.execute(
        "SELECT chunk_id, topic_label, messages, thinking, strategy, "
        "grade, consensus, outcome, problem_type, source, session_id, cycle FROM chunks WHERE chunk_id=?",
        (chunk_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "chunk_id": row[0], "topic_label": row[1],
        "messages": json.loads(row[2]), "thinking": row[3],
        "strategy": row[4], "grade": row[5],
        "consensus": row[6], "outcome": row[7],
        "problem_type": row[8], "source": row[9], "session_id": row[10], "cycle": row[11],
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


LABEL_MODEL = os.environ.get("LABEL_MODEL", "qwen2.5:0.5b")
LABEL_PROMPT = (
    "Output only a 3 to 5 word descriptive label for the following text. "
    "Do not use quotes, punctuation, or conversational filler.\n\n"
)

def _llm_topic_label(text: str) -> str:
    """Call qwen2.5:0.5b via Ollama to generate a semantic topic label.
    
    Falls back to _generate_topic_label on any error.
    """
    clean = _clean_content(text)[:2000]
    if not clean.strip():
        return "untitled"
    try:
        if _aux_backend("MNEME_LABEL_BACKEND") in ("openai", "openrouter"):
            r = requests.post(
                f"{OR_BASE_URL}/chat/completions",
                headers=_or_headers(),
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
                f"{OLLAMA_URL}/api/generate",
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
            return label[:60]
    except Exception as e:
        print(f"  [LABEL][ERROR] {type(e).__name__}: {e} — falling back to heuristic", flush=True)
    return _generate_topic_label(text)


def _llm_topic_labels_batch(texts: List[str], max_workers: int = 6) -> List[str]:
    """Concurrent batch labeling via qwen2.5:0.5b. Falls back per-item on error."""
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


def route_query(query: str, top_k: int = 3, with_scores: bool = False, q_vec=None) -> List:
    """FAISS top-k with noise-normalized scores + recency weighting + keyword fallback.
    Dynamic K: adjusts retrieval count based on score spread above noise floor.
    Pass q_vec to reuse a pre-computed query vector (single-embed turn)."""
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
    scored = [(s - BASELINE_NOISE, cid) for s, cid in scored_raw if s >= INJECT_MIN_SIMILARITY]
    
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
    threshold = int(MAX_PROMPT_CHARS * CHUNK_FRACTION)
    modified = []
    for m in msgs:
        content = _extract_text(m.get("content", ""))
        if len(content) > threshold and m.get("role") in ("user", "tool", "assistant"):
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
                "top_k": {"type": "integer", "description": "Number of results (default 5)"}
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


def build_context(query: str) -> Tuple[str, str]:
    if not query or not query.strip():
        return "", "other"  # empty query — skip injection
    """Build injected memory context with hard token cap.
    
    1. Route query → top-3 matching chunk IDs
    2. Expand to siblings (capped at MAX_SIBLINGS per topic)
    3. Grade-aware trim to fit MAX_INJECTED_TOKENS
    4. Append strategies for the detected problem type
    """
    q_ptype = _classify_problem_type(query)
    _qvec = _embed_query(query)  # embed once; shared by memory + strategy retrieval
    chunk_ids = route_query(query, top_k=3, q_vec=_qvec)
    
    # Expand to siblings with cap — batch query instead of per-chunk
    all_ids = set()
    siblings_map = get_siblings_batch(chunk_ids)
    for cid in chunk_ids:
        all_ids.add(cid)
        siblings = siblings_map.get(cid, [])
        for sib in siblings[:MAX_SIBLINGS]:
            all_ids.add(sib)

    # Strategy-floor chunks: below the memory floor but at/above the strategy
    # floor — they do NOT inject as memory, but their linked strategies do
    # (strategies generalize across same-concept queries). Linked retrieval
    # key, not the problem_type taxonomy (docs/strategy-retrieval-spec.md).
    strat_floor = _strategy_floor_chunks(query, q_vec=_qvec)
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
            f"grade, consensus, outcome, problem_type FROM chunks WHERE chunk_id IN ({placeholders})",
            ordered
        ).fetchall()
        for row in rows:
            _chunk_cache[row[0]] = {
                "chunk_id": row[0], "topic_label": row[1],
                "messages": json.loads(row[2]), "thinking": row[3],
                "strategy": row[4], "grade": row[5],
                "consensus": row[6], "outcome": row[7],
                "problem_type": row[8],
            }
    
    # Trim to token budget — preserves high-grade, drops low-grade
    trimmed = _trim_chunks_cached(ordered, MAX_INJECTED_TOKENS, _chunk_cache)
    
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
        msg_text = f"--- [{cid}]{sid_tag} {_gradetag} [src:{chunk.get('source','?')}] {chunk.get('created_at','')[:19]} {topic} ---\n"
        # If next sequential chunk exists, hint it
        msg_text += "\n".join(
            f"{m['role']}: {m['content']}"
            for m in chunk.get("messages", [])
        )
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
        strat_text, strat_ids = _strategy_block(strategy_chunk_ids, q_ptype)
        if strat_text:
            _INJECTED_STRATEGY_IDS.clear()
            _INJECTED_STRATEGY_IDS.update(strat_ids)
            return _finalize_context(strat_text + _preferences_block()), ptype
        return _finalize_context(_preferences_block()), ptype
    
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
    strat_text, strat_ids = _strategy_block(strategy_chunk_ids, q_ptype)
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
    context = _finalize_context(context)
    return context, ptype

# ─── Staging Buffer ────────────────────────────────────────────

class StagingBuffer:
    def __init__(self):
        self.messages: list = []
        self.last_activity = time.time()
        self.lock = threading.Lock()
    
    def add(self, role: str, content: str, source: str = "unknown", session: str = "default", grade: str = "C"):
        with self.lock:
            # Filter Hermes system-prompt artifacts from memory
            if role == "assistant":
                noise = ["update the skill library", "Be ACTIVE", "Signals to look for", "Review the conversation above", "missed learning opportunity"]
                if any(p in content for p in noise):
                    content = "[filtered: system instruction artifact]"
            self.messages.append({"role": role, "content": content, "source": source, "session": session, "grade": grade})
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

def archive_staging():
    """Flush the staging buffer into topic-split archived chunks.

    Each topic group gets its own chunk. Within a topic, chunks are capped
    at MAX_CHUNK_SIZE chars. Overflow gets versioned sibling chunks.

    Returns the number of chunks archived.
    """
    msgs = staging.flush()
    if not msgs:
        return 0

    # Increment save-cycle counter on every flush
    _next_cycle()

    # Classify each message into a topic group
    groups = _topic_split(msgs)
    
    total = 0
    for topic_label, group_msgs in groups:
        n = _archive_group(topic_label, group_msgs)
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


def _archive_group(topic_label: str, msgs: list) -> int:
    """Archive a topic group, splitting if too large. Returns chunk count."""
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
    
    # If group is small enough, archive as single chunk
    if len(user_text) <= MAX_CHUNK_SIZE:
        descriptive = _llm_topic_label(user_text) if not topic_label or topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label
        return _archive_single_chunk(msgs, user_text, descriptive, source=source)
    
    # Split into sibling chunks by MAX_CHUNK_SIZE
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
        if current_text and len(current_text) + len(frag) > MAX_CHUNK_SIZE:
            # Archive current batch
            descriptive = _llm_topic_label(current_text) if topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label[:20]
            label = f"{descriptive[:30]}_p{seq_base}"
            _archive_single_chunk(current, current_text, label, source=source)
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
        _archive_single_chunk(current, current_text, label, source=source)
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


def _archive_single_chunk(msgs: list, user_text: str, topic_label: str, source: str = "unknown") -> int:
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
    
    strategy = generate_strategy(msgs, outcome)
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
                "INSERT OR REPLACE INTO strategies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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


def store_tool_chunk(tool_output: str, tool_name: str = "tool") -> str:
    """Store raw tool output via unified staging → archive → chunks pipeline.

    Returns a short reference string to replace the content in messages.
    """
    size = len(tool_output)

    # Stage for unified ingestion — will be archived into chunks table
    staging.add("assistant", tool_output, source=f"tool:{tool_name}")

    reference = f"[Tool output staged as tool:{tool_name}: {size:,} chars — will be archived to memory]"
    print(f"  [CHUNK] Staged {tool_name} output ({size:,} chars) for unified ingestion", flush=True)

    return reference


def get_tool_chunk(chunk_id: str) -> Optional[str]:
    """Retrieve a stored chunk by ID from the unified chunks table.
    
    Repointed from tool_output_chunks to chunks table.
    """
    chunk = load_chunk(chunk_id)
    if not chunk:
        return None
    parts = []
    for m in chunk.get("messages", []):
        parts.append(m.get("content", ""))
    return "\n".join(parts) if parts else None


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
    _staged_hashes = getattr(compress_large_tool_results, '_staged_hashes', set())
    
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
    
    import hashlib
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) <= COMPRESS_THRESHOLD:
            continue
        
        # Dedup: don't stage the same content twice in same session
        h = hashlib.md5(content[:200].encode()).hexdigest()
        if h not in _staged_hashes:
            _staged_hashes.add(h)
            
            # Determine source for this tool output
            tool_source = page_source or "tool:unknown"
            if not page_source:
                # Try to identify tool from content
                for tool in ("browser_console", "browser_navigate", "terminal", "search", "web_search", "read_file", "write_file"):
                    if tool in content[:200]:
                        tool_source = f"tool:{tool}"
                        break
            
            # Split into chunks and stage each with source metadata
            for i in range(0, len(content), CHUNK_SIZE):
                chunk = content[i:i+CHUNK_SIZE]
                staging.add("assistant", chunk, source=tool_source)
            
            print(f"  [STAGE] {len(content)} chars split into {(len(content)-1)//CHUNK_SIZE+1} chunks (source={tool_source})", flush=True)
            
            # Trigger archive if buffer has substantial content
            if staging.should_flush():
                import threading
                _enqueue(archive_staging)
                print(f"  [STAGE] Auto-flushed staging buffer", flush=True)
        
        # Bound what the model sees: head+tail window, full text retrievable via
        # search_memory (Hermes-style bounded output — no summarization).
        # Idempotency guard: a truncated message is still > MAX_TOOL_FORWARD (the
        # note adds length), so re-truncating would shift bytes every turn and
        # break the prefix cache. Skip anything already carrying the marker.
        if len(content) > MAX_TOOL_FORWARD and "[... content truncated:" not in content:
            head_len = MAX_TOOL_FORWARD * 3 // 4
            tail_len = MAX_TOOL_FORWARD - head_len
            msg["content"] = (
                content[:head_len]
                + f"\n\n[... content truncated: {len(content)} chars total, showing first {head_len} + last {tail_len}. Full text stored to memory — use search_memory to retrieve the sections you need.]\n\n"
                + content[-tail_len:]
            )
    
    compress_large_tool_results._staged_hashes = _staged_hashes
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


def _learn_from_tool_trail(trail_tags, answer, grade, ptype):
    """Turn a failure->success tool trail into a reusable strategy (background).

    Fires when the current task's trail has one or more FAILUREs followed by a
    SUCCESS and the turn was graded honestly. Asks the model to extract the
    method that worked vs. the one that didn't, tagged to ptype for reuse on
    similar tasks.
    """
    if grade not in ("A", "B"):
        return
    statuses = [s for s, _ in trail_tags]
    if "SUCCESS" not in statuses:
        return
    last_success = max(i for i, s in enumerate(statuses) if s == "SUCCESS")
    # Require a RECOVERY: >= 2 consecutive failures immediately before the
    # success (a single flaky failure is not a lesson).
    streak = 0
    for s in reversed(statuses[:last_success]):
        if s == "FAILURE":
            streak += 1
        else:
            break
    if streak < 2:
        return
    trail = "; ".join(f"{s}{':' + r if r else ''}" for s, r in trail_tags[-8:])
    answer_clean = _TOOL_TAG_RE.sub("", answer or "").strip()
    prompt = (
        "You attempted a task and had some tool failures before succeeding.\n"
        f"Tool trail: {trail}\n"
        f"Final answer: {answer_clean[:MAX_ABSTRACT_INPUT]}\n\n"
        "Extract ONE imperative rule of the form 'WHEN doing <task>, use <method "
        "that worked> (the method <that failed> failed)'. Short, specific, "
        "actionable. Respond with ONLY the rule."
    )
    try:
        r = query_model([{"role": "user", "content": prompt}], timeout=CHAT_TIMEOUT)
        rule = (r.get("content") or "").strip()
        if len(rule) > 10 and not _is_junk_directive(rule):
            _save_strategy(rule, "B", problem_type=ptype or "other")
            print(f"  [TOOL-TRAIL-LEARN] {rule[:70]}...", flush=True)
    except Exception as e:
        _log_error("_learn_from_tool_trail", e)


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
        k = fn.get("arguments", {}).get("top_k", 5)
        print(f"  [SEARCH-TOOL] model searching: '{q[:80]}' top_k={k}", flush=True)
        if not q:
            # Reasoning model emitted search_memory with no query — don't
            # burn a no-op search; nudge it to retry with specific terms.
            result_texts.append("search_memory requires a non-empty query — retry with specific search terms.")
            print("  [SEARCH-TOOL] empty query — skipped (nudging model)", flush=True)
            continue
        hits = route_query(q, top_k=k)
        trace.update(hits)
        if hits:
            lines = ["Search results from Mneme memory:"]
            for h in hits:
                cid = h  # route_query returns chunk_id strings, not tuples
                crow = db.execute("SELECT topic_label, grade, messages FROM chunks WHERE chunk_id=?", (cid,)).fetchone()
                if crow:
                    label, grd, msgs_json = crow[0], crow[1], crow[2]
                    lines.append(f"[{cid} | G:{grd}] {label}")
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


def _query_retry_timeout(msgs, tools=None, timeout=CHAT_TIMEOUT):
    """query_model with ONE retry on a transient provider failure.

    A transient OpenRouter stream stall (the GRIND-GUARD aborts with
    done_reason="timeout" and 0 tokens) or a mid-stream provider error
    (done_reason="error") should not kill the whole turn. The initial query in
    process_chat already retries this case; the tool-loop re-queries and the
    hard-stop queries were missing it, so a single stalled re-query returned
    empty ("(no response)") with no recovery.
    """
    result = query_model(msgs, tools=tools, timeout=timeout)
    dr = result.get("done_reason", "")
    empty = not (result.get("content") or "").strip() and not result.get("tool_calls")
    retryable = (dr == "timeout" and empty and not result.get("eval_count")) or dr == "error"
    if retryable:
        print(f"  [RETRY] provider failure ({dr}) — retrying once", flush=True)
        result = query_model(msgs, tools=tools, timeout=timeout)
    return result


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


def process_chat(messages: list, session_id: str = "default", tools: list = None) -> dict:
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

    # ── Save trigger: <<SAVE>> forces archive ──
    SAVE_TRIGGER = "<<SAVE>>"
    if SAVE_TRIGGER in full_user_msg:
        user_msg = user_msg.replace(SAVE_TRIGGER, "").strip()
        if not user_msg:
            user_msg = "Memory save triggered."
        messages[-1]["content"] = user_msg
        _enqueue(archive_staging)
        print("  [SAVE] Triggered by user — archiving in background", flush=True)

    # ── Learn trigger: <<LEARN problem:...>> runs learning mode ──
    learn_match = _learn_re.search(full_user_msg)
    if learn_match:
        learn_problem = learn_match.group(1).strip()
        user_msg = _learn_re.sub("", user_msg).strip()
        if not user_msg:
            user_msg = "Learning mode was triggered."
        messages[-1]["content"] = user_msg
        _enqueue(_run_learning_mode, learn_problem, 5)
        print(f"  [LEARN] Triggered via <<LEARN>>: {learn_problem[:80]}", flush=True)

    # Strip all <<COMMANDS>> from user messages
    _cmd_re = re.compile(r"<<[A-Z_]+(?:\s+[^>]+)?>>")
    for m in messages:
        if m.get("role") == "user":
            raw = _extract_text(m.get("content", ""))
            cleaned = _cmd_re.sub("", raw).strip()
            if cleaned: m["content"] = cleaned
    user_msgs2 = [_extract_text(m["content"])[:MAX_QUERY_CHARS] for m in reversed(messages) if m.get("role") == "user"][:1]
    user_msg = " ".join(reversed(user_msgs2))

    # Full tool list = read-only server tools (search_memory/list_tools/read_tool)
    # + native bootstrap (bash/write, flag-gated) + client passthrough, deduped.
    msg_tools = mntools.assemble_tools(tools)
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
    
    # Build injected memory (chunks + budget; the fixed system prompt is added to
    # the system message below via _system_prompt_block() so it stays cacheable).
    context, ptype = build_context(user_msg)
    cur_ptype = _classify_problem_type(user_msg)
    
    # Insert Mneme's FIXED instruction block as a system message after Hermes.
    # ONLY _system_prompt_block() + _meta_principles_block() go here — both are
    # constant, so this stays a stable, cacheable prefix. The VARIABLE advisory
    # directives (saved-tool hint, explore, relevant tools) + memory + preferences
    # go to the TAIL (prepended to the last user message) alongside the memory
    # context (docs/build-plan.md Phase 1).
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
    _is_edge = _is_capability_edge(cur_ptype)
    _in_build = _in_build_mode(messages)
    _in_reuse = _in_reuse_mode(messages)
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
    # Inject the VARIABLE memory context + advisory directives at the TAIL: prepend
    # to the last user message. This keeps the [system prompt + conversation]
    # prefix stable and cacheable (KV prefix cache), instead of re-processing the
    # whole conversation every turn when the memory chunks reshuffle at the head.
    _tail_parts = [p for p in (context, dynamic_tail) if (p or "").strip()]
    tail = "\n\n".join(_tail_parts)
    if tail.strip():
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                _cur = messages[i].get("content") or ""
                # Guard against double-injection: if a client echoes the mutated
                # message back on a later call, the memory disclaimer is already at
                # the head — don't prepend again (else context doubles each round).
                if context.strip() and "--- MEMORY:" in _cur:
                    print("  [INJECT-TAIL] already injected — skipping", flush=True)
                    break
                messages[i]["content"] = tail + "\n\n---\n" + _cur
                print(f"  [INJECT-TAIL] {len(tail)} chars prepended to last user message", flush=True)
                break
    full_msgs = messages
    
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
    result = query_model(full_msgs, tools=([] if _deliberate else msg_tools), timeout=CHAT_TIMEOUT)
    # Anti-grind / empty-reply guardrail: if the model returned nothing (timeout
    # or empty reasoning), retry once with a nudge, then fall back to a clear
    # message so the client never sees an empty/"None" reply.
    _failed = False
    if not (result.get("content") or "").strip() and not result.get("tool_calls"):
        dr = result.get("done_reason", "?")
        if (dr == "timeout" and not result.get("eval_count")) or dr == "error":
            # Provider hang (zero tokens received) or a mid-stream provider error
            # — NOT a grind. OpenRouter occasionally drops large synthesis requests
            # without a single byte, or dies mid-generation; retry once on a fresh
            # connection before declaring a capability edge.
            print(f"  [RETRY] provider failure ({dr}) — retrying once", flush=True)
            result = query_model(full_msgs, tools=msg_tools, timeout=CHAT_TIMEOUT)
            if not (result.get("content") or "").strip() and not result.get("tool_calls"):
                _failed = True
        elif dr == "timeout":
            # Grind guardrail: generation exceeded budget — retrying would just
            # grind again. Fall through to the capability-edge message.
            print(f"  [GRIND] generation exceeded {CHAT_TIMEOUT}s — capability edge, no retry", flush=True)
            _failed = True
        else:
            print(f"  [EMPTY] empty reply (done_reason={dr}) — retrying once", flush=True)
            _retry = [m for m in full_msgs if m.get("role") != "system"]
            _retry.append({"role": "user", "content": "(Your previous reply was empty. Give a direct answer now.)"})
            result = query_model(_retry, tools=msg_tools, timeout=CHAT_TIMEOUT)
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
    _MAX_SERVER_ROUNDS = MAX_SERVER_ROUNDS  # absolute round ceiling (high backstop)
    _native_names = mntools.native_exec_names(tools)  # {"bash","write"} when native
    _server_names = {"search_memory", "list_tools", "read_tool", "read_file", "fetch_url", "web_search"} | _native_names
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
    _continue_attempts = 0

    for _round in range(_MAX_SERVER_ROUNDS):
        # Context-size guard: if the followup has bloated (many file reads via
        # bash), force a final synthesis NOW instead of growing the payload until
        # OpenRouter times out on re-query. This is a hard backstop; legitimate
        # multi-source exploration should stay well under it.
        _ctx_chars = sum(len(str(m.get("content", ""))) for m in followup)
        if _ctx_chars > 50000:
            print(f"  [TOOL-HARD-STOP] followup {_ctx_chars} chars — forcing synthesis", flush=True)
            followup.append({"role": "user", "content":
                "You have gathered a lot of context. Synthesize your findings into a "
                "final answer now — do not run any more tools."})
            result = _query_retry_timeout(followup, tools=msg_tools)
            break
        tcs = result.get("tool_calls") or []
        search_calls = [tc for tc in tcs if tc.get("function", {}).get("name") == "search_memory"]
        registry_calls = [tc for tc in tcs if tc.get("function", {}).get("name") in ("list_tools", "read_tool", "read_file", "fetch_url", "web_search")]
        native_calls = [tc for tc in tcs if tc.get("function", {}).get("name") in _native_names]
        other_calls = [tc for tc in tcs if tc.get("function", {}).get("name") not in _server_names]
        passthrough_calls.extend(other_calls)

        if not (search_calls or registry_calls or native_calls):
            # No server tool calls. If the model gave up (blank/shrug answer),
            # prompt it to CONTINUE instead of ending the turn — bounded retries.
            # (Infra timeouts/errors land in the fallback below, not here.)
            if (_is_near_empty(result.get("content") or "")
                    and _continue_attempts < MAX_EMPTY_RETRY
                    and result.get("done_reason") not in ("timeout", "error")):
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
        # there is real text. Some models (e.g. Qwen3.6-35B "Uncensored-Aggressive")
        # report done_reason="stop" even while emitting a pending tool call with
        # EMPTY content — breaking here would drop the tool call and lose the
        # answer, so only break on "stop" when there's actual narration.
        if result.get("done_reason") == "stop" and (result.get("content") or "").strip():
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

        # Registry tools (list_tools/read_tool): user-message feedback.
        for tc in registry_calls:
            nm = tc["function"]["name"]
            args = tc["function"].get("arguments", {}) or {}
            _mark_call(nm, args)
            _t0 = time.time()
            _tool_rounds += 1
            res = mntools.execute_readonly_tool(nm, args)
            _tool_trace.append(_trace(nm, args, res, _t0))
            _label = "WEB-SEARCH" if nm == "web_search" else "TOOL-REGISTRY"
            print(f"  [{_label}] {nm} -> {res[:90]!r}", flush=True)
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

        print(f"  [SYNTHESIS] re-querying model "
              f"({len(search_calls)} search, {len(registry_calls)} registry, {len(native_calls)} native)", flush=True)
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
            if _has_fake_source(_parsed, _trace_chunks, _trace_urls):
                grade = "F"
                print("  [FAKE-SOURCE] fabricated citation detected — grade fail", flush=True)
    if grade not in ("A", "B", "C", "D", "F"):
        grade = "C"

    # Novel-procedure detection: a working NEW technique (custom header, API
    # endpoint, method override) is a "great" outcome even without a pre-flagged
    # capability edge. Grade it A and persist it so the model can reuse it.
    if grade == "B":
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
    if (_trail_statuses.count("FAILURE") >= 2
            and "SUCCESS" not in _trail_statuses
            and grade not in ("D", "F")):
        _eff_grade = "F"
    _record_capability(cur_ptype, _eff_grade)

    # Overcome-mode outcome: if the model was deliberating (stuck now, a known
    # capability edge, or already inside an overcome episode), parse its reply and
    # record the decision — build_tool (attempted), reuse_tool (attempted), or a
    # TOOL_SAVE marker (overcame + saved tool). No declare_edge: an edge surfaces
    # when the build loop exhausts its budget, not via a model declaration.
    if _stuck or _is_edge or _in_build or _in_reuse:
        try:
            _oo = _handle_overcome_reply(db, cur_ptype, _resp_content)
            if _oo != "none":
                print(f"  [OVERCOME-OUTCOME] {_oo}", flush=True)
        except Exception as e:
            _log_error("process_chat:overcome_outcome", e)

    # Tool-outcome learning: a RECOVERY — >= 2 consecutive failures immediately
    # before a success — becomes a reusable strategy. NOT "any failure + any
    # success" (a single flaky request is not a lesson). The combined trail is
    # passed cheap+sync; the model extraction runs background (build-plan Phase 2).
    _streak = 0
    if "SUCCESS" in _trail_statuses:
        _last_s = max(i for i, s in enumerate(_trail_statuses) if s == "SUCCESS")
        for _s in reversed(_trail_statuses[:_last_s]):
            if _s == "FAILURE":
                _streak += 1
            else:
                break
    if _streak >= 2:
        try:
            _enqueue(_learn_from_tool_trail, _trail, _answer, grade, cur_ptype)
        except Exception as e:
            _log_error("process_chat:tool_trail_enqueue", e)

    # Phase 4.2/4.3: close the telemetry loop on injected strategies
    try:
        _consume_injected_strategies(grade)
    except Exception as e:
        _log_error("process_chat:consume_strategies", e)

    # Phase 5.2: embedding-distance check on self-reported A/B grades
    try:
        _check_suspect_grade(grade, result.get("content", ""), messages)
    except Exception as e:
        _log_error("process_chat:suspect_grade", e)

    # Flush BEFORE adding this turn — the idle check compares against the
    # previous turn's last_activity, which staging.add() would otherwise reset
    # (making the idle condition dead code).
    if staging.should_flush():
        _enqueue(archive_staging)

    staging.add("user", user_msg, source="user", session=session_id)
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


def _save_strategy(text, grade, existing_id="", problem_type="other", cost=0, abstract=True, source_chunk=""):
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
    with _db_lock:
        db.execute("INSERT OR REPLACE INTO strategies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, problem_type, text.strip(), source_chunk, grade, datetime.now(timezone.utc).isoformat(),
             new_version, parent, 0.0, 0, 0, 0, "", cost, outcome))
        db.commit()
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
    with faiss_lock():
        if FAISS_OK:
            _index = faiss.IndexFlatIP(DIM)
        _id_map = []
        _save_index()
    staging.flush()  # discard staged-but-unarchived messages
    with _pending_links_lock:
        _pending_strategy_links.clear()
    print("  [RESET] memory wiped (chunks/strategies/tools/edges/faiss/staging)", flush=True)


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
    
    # ── Chat UI: simple light-theme HTML front end (thin client -> /v1/chat/completions) ──
    _CHAT_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "chat.html")

    @app.route("/", methods=["GET"])
    @app.route("/chat", methods=["GET"])
    def chat_ui():
        try:
            with open(_CHAT_HTML_PATH, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
        except Exception as e:
            print(f"  [CHAT-UI][ERR] {str(e)[:100]}", flush=True)
            return _cors_response({"error": "chat UI not found"}, status=404)

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
        path = os.path.join(_instructions_dir(), "default", name + ".txt")
        if not os.path.isfile(path):
            return _cors_response({"error": f"no file for {name}"}, status=404)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}
        except OSError as e:
            return _cors_response({"error": str(e)}, status=500)

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
        data = request.get_json(force=True)
        stream = data.get("stream", False)

        print("  [DEBUG] stream={} model={}".format(stream, data.get("model", "?")), flush=True)
        messages = data.get("messages", [])
        
        # Auto-generate session ID for new conversations
        user_count = sum(1 for m in messages if m.get("role") == "user")
        if user_count <= 1:
            # New conversation — generate unique session
            import hashlib
            first_msg = _extract_text(next((m.get("content","") for m in messages if m.get("role") == "user"), ""))
            h = hashlib.md5(first_msg[:100].encode()).hexdigest()[:8]
        session_id = f"conv_{h}_{int(time.time()) % 100000}" if user_count <= 1 else "default"
        
        if stream:
            return _chat_stream(messages, tools=data.get("tools"), session_id=session_id)
        
        result = process_chat(messages, tools=data.get("tools"), session_id=session_id)

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
                    db.execute("INSERT OR REPLACE INTO strategies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, "model", st, "", "A",
                         datetime.now(timezone.utc).isoformat(),
                         new_version, sid if existing_version > 0 else "",
                         0.0, 0, 0, 0, "", 0))
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
    
    # ── Chat completions (SSE streaming) ──
    def _chat_stream(messages, tools=None, session_id="default"):
        result = process_chat(messages, tools=tools, session_id=session_id)
        ct = result.get("content", "")
        grade = result.get("_grade", "C")
        # (Injected-strategy telemetry is consumed inside process_chat — a
        # second call here would be a no-op and double-log the [CONSUME] line.)
        # Phase 5.2: embedding-distance check on self-reported A/B grades
        try:
            _check_suspect_grade(grade, ct, messages)
        except Exception as e:
            _log_error("chat_stream:suspect_grade", e)
        _enqueue(_strategy_lifecycle, grade, messages, result.get("_infra_failure", False))
        content = result.get("content", "")
        tool_calls = result.get("tool_calls", [])

        # (search_memory is resolved inside process_chat — its bounded loop and
        # fallback already handle memory search server-side, so there is nothing
        # left to do here. Forwarding a search_memory to the client would hit the
        # empty shim and stall.)
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        
        def generate():
            # Send role first
            yield "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": FAKE_MODEL_ID,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }) + "\n\n"
            
            # Stream thinking as reasoning if present
            thinking = result.get("thinking", "")
            if thinking:
                thinking_chunk = 16
                for i in range(0, len(thinking), thinking_chunk):
                    piece = thinking[i:i + thinking_chunk]
                    yield "data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": FAKE_MODEL_ID,
                        "choices": [{"index": 0, "delta": {"reasoning": piece, "role": "assistant"}, "finish_reason": None}],
                    }) + "\n\n"
            
            # If model returned tool_calls, emit them as deltas (OpenAI-style)
            if tool_calls:
                for i, tc in enumerate(tool_calls):
                    yield "data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": FAKE_MODEL_ID,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": i,
                                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                                    "type": tc.get("type", "function"),
                                    "function": {
                                        "name": tc.get("function", {}).get("name", ""),
                                        "arguments": json.dumps(tc.get("function", {}).get("arguments", {})) if isinstance(tc.get("function", {}).get("arguments"), dict) else tc.get("function", {}).get("arguments", ""),
                                    },
                                }]
                            },
                            "finish_reason": None,
                        }],
                    }) + "\n\n"
                # Done with tool_calls finish reason
                yield "data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": FAKE_MODEL_ID,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {"completion_tokens": result.get("eval_count", 0)},
                }) + "\n\n"
                yield "data: [DONE]\n\n"
                return
            
            # Stream thinking as reasoning if present
            thinking = result.get("thinking", "")
            if thinking:
                thinking_chunk = 16
                for i in range(0, len(thinking), thinking_chunk):
                    piece = thinking[i:i + thinking_chunk]
                    yield "data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": FAKE_MODEL_ID,
                        "choices": [{"index": 0, "delta": {"reasoning": piece, "role": "assistant"}, "finish_reason": None}],
                    }) + "\n\n"
            
            # Stream content in chunks
            chunk_size = 16
            for i in range(0, len(content), chunk_size):
                piece = content[i:i + chunk_size]
                yield "data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": FAKE_MODEL_ID,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }) + "\n\n"
            
            # Done
            yield "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": FAKE_MODEL_ID,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": result.get("eval_count", 0)},
            }) + "\n\n"
            yield "data: [DONE]\n\n"
        
        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
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

    # ── Save: force-flush the staging buffer ──
    @app.route("/search", methods=["POST"])
    def search_memory():
        data = request.get_json(force=True)
        query = data.get("query", "")
        top_k = data.get("top_k", 10)
        vec = embed(query)
        results_raw = _cosine_search(vec, top_k, 0.0)
        faiss_results = [(s - BASELINE_NOISE, cid) for s, cid in results_raw if s - BASELINE_NOISE > ROUTE_THRESHOLD]
        # Hybrid: fill with keyword matches if FAISS is sparse
        hybrid = _hybrid_search(query, top_k, faiss_results)
        chunks = []
        for score, chunk_id, method in hybrid:
            row = db.execute("SELECT topic_label, grade, created_at, outcome, source, session_id, cycle FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
            if row:
                entry = {"chunk_id": chunk_id, "topic_label": row[0], "grade": row[1], "created_at": row[2], "outcome": row[3], "source": row[4], "cycle": row[5], "similarity": round(score, 4), "method": method}
                chunks.append(entry)
        return _cors_response({"results": chunks})


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
    print(f"  [CONFIG] sampling temp={OLLAMA_TEMP} top_p={os.environ.get('MNEME_TOP_P','0.95')} top_k={os.environ.get('MNEME_TOP_K','64')} ctx={os.environ.get('MNEME_CTX_TOKENS','256000')}", flush=True)
    print(f"  [CONFIG] timeouts chat={CHAT_TIMEOUT} ollama={OLLAMA_CHAT_TIMEOUT} first_token={FIRST_TOKEN_TIMEOUT} embed={EMBED_TIMEOUT} label={LABEL_TIMEOUT}", flush=True)
    print(f"  [CONFIG] staging_turns={STAGING_TURNS} idle={STAGING_IDLE} belief_evolution={os.environ.get('MNEME_BELIEF_EVOLUTION','0')}", flush=True)
    print(f"  [CONFIG] retrieval route={ROUTE_THRESHOLD} classify={CLASSIFY_THRESHOLD} inject_min_sim={INJECT_MIN_SIMILARITY} keyword_fallback={int(KEYWORD_FALLBACK)} injected_tokens={MAX_INJECTED_TOKENS}", flush=True)


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
# Calibrate noise baseline AFTER FAISS is loaded
BASELINE_NOISE = _calibrate_noise()
print(f"  [STARTUP] Noise baseline: {BASELINE_NOISE:.4f}", flush=True)
_dump_config()
print(f"[mokv] Mneme ready. model={MODEL} chunks={len(_id_map)} db={DB_PATH}",
      flush=True)

if __name__ == "__main__":
    if FLASK_OK:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    else:
        print("[mokv] Flask not installed. Import as module for programmatic use.",
              flush=True)
