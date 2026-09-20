"""Model templates — named, known-good generation settings for specific models.

Why: some models only behave with particular settings, and finding those took
measurement (sometimes days). A template packages the result so it is selected
at setup time rather than rediscovered. With no template selected, behaviour is
byte-identical to before this module existed.

Resolution order (lowest to highest priority):

    built-in code defaults  <  template  <  mneme.yaml values  <  env vars

So adopting a template never locks you in: any key it sets can still be
overridden in mneme.yaml. That ordering is enforced by merging the template in
as a DEFAULTS LAYER beneath the file, not by rewriting the file.

Why unknown keys are rejected loudly: a template that sets a knob the proxy does
not read would silently do nothing — precisely the "I changed a setting and it
had no effect" class of bug this audit was about. Failing loud on an unknown
template key turns a silent no-op into a startup error.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a hard dep elsewhere
    yaml = None

# Keys a template may set. Anything else is an error (see module docstring).
ALLOWED_SAMPLING_KEYS = {
    "temperature", "top_p", "top_k", "ctx_tokens", "completion_reserve",
    "max_tokens", "reasoning_enabled", "reasoning_effort",
}
ALLOWED_TIMEOUT_KEYS = {
    "chat_timeout", "ollama_chat_timeout", "first_token_timeout",
}
# Per-model keys the proxy honours (see the model-override loop in the payload
# builder). Kept in sync deliberately — a template writing an unknown per-model
# key would be a silent no-op.
ALLOWED_MODEL_KEYS = {
    "temperature", "top_p", "top_k", "min_p", "presence_penalty",
    "repetition_penalty", "repeat_penalty", "num_ctx", "num_predict",
    "reasoning", "reasoning_effort",
}
ALLOWED_TOP_KEYS = {"description", "notes", "sampling", "timeouts", "models"}


class TemplateError(Exception):
    """Raised for an unknown template or an invalid key inside one."""


def default_templates_path(repo_root: str) -> str:
    return os.path.join(repo_root, "model_templates.yaml")


def load_templates(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the template catalogue. Returns {} when the file is absent.

    A missing catalogue is NOT an error — it just means no templates are
    available, which is the pre-existing behaviour.
    """
    if not path or not os.path.exists(path):
        return {}
    if yaml is None:
        raise TemplateError("PyYAML is required for model templates")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return (data.get("templates") or {})


def list_template_names(path: Optional[str] = None) -> list:
    return sorted(load_templates(path).keys())


def _fail(name: str, msg: str):
    raise TemplateError(f"model template {name!r}: {msg}")


def validate(template: Dict, name: str = "?") -> None:
    """Reject unknown keys/top-levels so a typo cannot silently do nothing."""
    for k in template:
        if k not in ALLOWED_TOP_KEYS:
            _fail(name, f"unknown top-level key {k!r} (allowed: {sorted(ALLOWED_TOP_KEYS)})")
    for section, allowed in (("sampling", ALLOWED_SAMPLING_KEYS),
                             ("timeouts", ALLOWED_TIMEOUT_KEYS)):
        block = template.get(section) or {}
        if not isinstance(block, dict):
            _fail(name, f"{section!r} must be a mapping")
        for k in block:
            if k not in allowed:
                _fail(name, f"unknown {section} key {k!r} "
                            f"(the proxy would ignore it — allowed: {sorted(allowed)})")
    models = template.get("models") or {}
    if not isinstance(models, dict):
        _fail(name, "models must be a mapping")
    for mname, overrides in models.items():
        if not isinstance(overrides, dict):
            _fail(name, f"models.{mname} must be a mapping")
        for k in overrides:
            if k not in ALLOWED_MODEL_KEYS:
                _fail(name, f"unknown models key {k!r} in {mname!r} "
                            f"(allowed: {sorted(ALLOWED_MODEL_KEYS)})")


def expand(template: Dict, model: str, name: str = "?") -> Dict:
    """Return the template with {model} substituted for the real model name.

    Templates key their per-model block as "{model}" so one template works for
    any tag (e.g. a different quant of the same family) without editing.
    """
    validate(template, name)
    out = {k: v for k, v in template.items() if k not in ("description", "notes")}
    if model and isinstance(out.get("models"), dict):
        models = {}
        for mname, overrides in out["models"].items():
            key = model if mname == "{model}" else mname
            models[key] = dict(overrides)
        out["models"] = models
    return out


def apply_template(data: Dict, model: str, template_name: Optional[str],
                   path: Optional[str] = None) -> Dict:
    """Merge template defaults UNDER `data` (the parsed mneme.yaml).

    Returns a new dict; `data` is not mutated. File values always win over
    template values, so a template is a starting point, never a lock-in.
    Missing/blank template name -> `data` returned unchanged (default settings).

    Subtlety worth knowing: per-model overrides are applied LAST at payload-build
    time, so a template's `models.<name>.temperature` would silently beat a global
    `sampling.temperature` the user wrote in the file. That would violate the
    "file wins" promise in a way nobody would notice. So for any key the user set
    EXPLICITLY in the file's own `sampling:` block, the template's per-model value
    is dropped — the user's stated global intent wins over the template's default.
    """
    if not template_name:
        return data
    catalogue = load_templates(path)
    if template_name not in catalogue:
        raise TemplateError(
            f"unknown model template {template_name!r}. Available: "
            f"{sorted(catalogue.keys()) or 'none'}"
        )
    tpl = expand(catalogue[template_name], model, template_name)
    merged = dict(data)
    file_sampling = data.get("sampling") or {}
    for section in ("sampling", "timeouts"):
        block = tpl.get(section)
        if block:
            merged[section] = {**block, **file_sampling} if section == "sampling" \
                else {**block, **(data.get(section) or {})}
    # Per-model overrides: template is the base, the file's own block for the
    # same model wins key-by-key, and file blocks for OTHER models are kept.
    if tpl.get("models"):
        merged_models = {}
        for mname, overrides in tpl["models"].items():
            block = dict(overrides)
            # Drop any template per-model key the user set globally in sampling:,
            # because the per-model layer is applied after sampling at request
            # time and would otherwise silently override it.
            for k in list(block):
                if k in file_sampling and file_sampling[k] is not None:
                    block.pop(k, None)
            merged_models[mname] = block
        for mname, overrides in (data.get("models") or {}).items():
            merged_models[mname] = {**merged_models.get(mname, {}), **(overrides or {})}
        merged["models"] = merged_models
    return merged


def describe(name: str, path: Optional[str] = None) -> Dict:
    """Metadata for the setup wizard / a /templates endpoint."""
    tpl = (load_templates(path) or {}).get(name)
    if tpl is None:
        raise TemplateError(f"unknown model template {name!r}")
    return {
        "name": name,
        "description": tpl.get("description", ""),
        "notes": tpl.get("notes", ""),
        "sampling": tpl.get("sampling") or {},
        "timeouts": tpl.get("timeouts") or {},
        "models": tpl.get("models") or {},
    }
