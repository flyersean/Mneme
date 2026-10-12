"""models.dev registry — source of truth for provider/model metadata.

models.dev (https://models.dev, MIT) is the open registry OpenCode and the Vercel
AI SDK use. Each provider carries its base URL (`api`), key env var (`env`), and SDK
package (`npm`); each model carries its context window (`limit.context`), output cap
(`limit.output`), and reasoning support (`reasoning` + `reasoning_options`).

We load its JSON so a provider that is NOT in our curated PROVIDER_CATALOG still
resolves its base URL + key automatically — no hand-written entry required.

The reasoning WIRE FORMAT (`thinking` vs `reasoning` request param / response field)
is NOT in models.dev; it lives in the Vercel AI SDK's TypeScript adapters. We keep
that in PROVIDER_CATALOG for the providers that deviate from the OpenAI-compatible
default (deepseek/zai/...); every provider that resolves via this module falls back to
the OpenAI-compatible default (reasoning/reasoning), which is correct for the vast
majority of registry providers.

TODO(future): if the hand-rolled wire contract (PROVIDER_CATALOG) becomes a recurring
source of breakage, adopt a maintained Python provider layer. LiteLLM (MIT core,
100+ providers, normalizes reasoning per provider) is the drop-in that would replace
this module + PROVIDER_CATALOG's wire exceptions.
"""

import json
import os

_REG = None
_REG_PATH = None

# Base URLs for providers whose endpoint is NOT in models.dev's `api` field — their
# endpoint lives in the Vercel AI SDK's dedicated packages. Verified against the SDK's
# packages/*/src/*-provider.ts. Account-specific endpoints (azure, amazon-bedrock,
# google-vertex, watsonx, cloudflare-ai-gateway, gitlab, salad-cloud, vercel, qvac,
# sap-ai-core) can't be flat-mapped — they need per-account/resource config and are
# deliberately absent here.
MODELS_DEV_BASE_URL_OVERRIDES = {
    "deepinfra": "https://api.deepinfra.com/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "perplexity": "https://api.perplexity.ai",
    "aihubmix": "https://aihubmix.com/v1",
    "venice": "https://api.venice.ai/api/v1",
    "togetherai": "https://api.together.xyz/v1",
}


def _candidates():
    cands = []
    if os.environ.get("MODELS_DEV_PATH"):
        cands.append(os.environ["MODELS_DEV_PATH"])
    cd = os.environ.get("MNEME_CHUNK_DIR")
    if cd:
        cands.append(os.path.join(cd, "models-dev.api.json"))
        cands.append(os.path.join(os.path.dirname(cd), "models-dev.api.json"))
    cands.append("/home/ubuntu/mneme/chunks/models-dev.api.json")
    return cands


def _load():
    global _REG, _REG_PATH
    for c in _candidates():
        if c and os.path.isfile(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    _REG = json.load(f)
                _REG_PATH = c
                return _REG
            except Exception:
                continue
    _REG = {}
    _REG_PATH = None
    return _REG


def registry():
    return _REG if _REG is not None else _load()


def path():
    if _REG_PATH is None:
        _load()
    return _REG_PATH


def provider(pid):
    d = registry()
    return d.get(pid) if isinstance(d, dict) else None


def base_url(pid):
    if pid in MODELS_DEV_BASE_URL_OVERRIDES:
        return MODELS_DEV_BASE_URL_OVERRIDES[pid]
    p = provider(pid)
    return (p.get("api") or "") if p else ""


def key_env(pid):
    p = provider(pid)
    env = (p.get("env") or []) if p else []
    return env[0] if env else ""


def models(pid):
    p = provider(pid)
    return (p.get("models") or {}) if p else {}


def model(pid, mid):
    return models(pid).get(mid)


def context_window(pid, mid):
    m = model(pid, mid)
    if not m:
        return None
    return (m.get("limit") or {}).get("context")


def output_cap(pid, mid):
    m = model(pid, mid)
    if not m:
        return None
    return (m.get("limit") or {}).get("output")


def models_list(pid):
    """Model list for the picker: [{id, name, context, output, reasoning}]."""
    out = []
    for mid, m in models(pid).items():
        lim = m.get("limit") or {}
        out.append({
            "id": mid,
            "name": m.get("name") or mid,
            "context": lim.get("context"),
            "output": lim.get("output"),
            "reasoning": bool(m.get("reasoning")),
        })
    return out
