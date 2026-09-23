"""Model templates — named, known-good generation settings for specific models.

Why: some models only behave with particular settings, and finding those took
measurement (sometimes days). A template packages the result so it is selected
at setup time rather than rediscovered. With no template selected, behaviour is
byte-identical to before this module existed.

A template may also carry an OPTIONAL ``modelfile`` block. Some models (e.g.
Muse Glimmer's Harmony channel format) need a corrected Ollama chat template —
Ollama's auto-detected one stalls generation. The ``modelfile`` block declares
the source model + the corrected TEMPLATE/PARAMETERs so the setup wizard (or the
/templates page) can ``ollama create`` it automatically instead of asking the
user to do it by hand.

Resolution order (lowest to highest priority):

    built-in code defaults  <  template  <  mneme.yaml values  <  env vars

So adopting a template never locks you in: any key it sets can still be
overridden in mneme.yaml. That ordering is enforced by merging the template in
as a DEFAULTS LAYER beneath the file, not by rewriting the file.

Why unknown keys are rejected loudly: a template that sets a knob the proxy does
not read would silently do nothing — precisely the "I changed a setting and it
had no effect" class of bug this audit was about. Failing loud on an unknown
template key turns a silent no-op into a startup error.

User templates: a separate catalogue (``user_path``) is merged on top of the
shipped one, so a user's own templates survive ``git pull`` of the repo. A user
template with the same name as a shipped one wins.
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
# The optional modelfile block — an Ollama-side chat-template recipe. Only
# relevant to the setup wizard / templates page; it is NOT merged into config.
ALLOWED_MODEFILE_KEYS = {"from", "template", "parameters"}
# Ollama Modelfile PARAMETERs a template may pin. Keep tight: anything else is
# rejected loudly so a typo cannot silently do nothing.
ALLOWED_MODEFILE_PARAM_KEYS = {
    "num_ctx", "num_predict", "stop", "temperature", "top_p", "top_k",
    "repeat_penalty", "presence_penalty", "reasoning_effort", "min_p",
}
ALLOWED_TOP_KEYS = {"description", "notes", "sampling", "timeouts", "models",
                    "modelfile"}


class TemplateError(Exception):
    """Raised for an unknown template or an invalid key inside one."""


def default_templates_path(repo_root: str) -> str:
    return os.path.join(repo_root, "model_templates.yaml")


def _load_file(path: Optional[str]) -> Dict[str, Any]:
    """Load one template catalogue file. Returns {} when absent/empty."""
    if not path or not os.path.exists(path):
        return {}
    if yaml is None:
        raise TemplateError("PyYAML is required for model templates")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return (data.get("templates") or {})


def load_templates(path: Optional[str] = None,
                   user_path: Optional[str] = None) -> Dict[str, Any]:
    """Load the template catalogue — shipped file merged with user overrides.

    A missing catalogue is NOT an error — it just means no templates are
    available, which is the pre-existing behaviour. User templates (if any) are
    merged on top of the shipped ones; same-named user templates win.
    """
    merged = _load_file(path)
    merged.update(_load_file(user_path))
    return merged


def list_template_names(path: Optional[str] = None,
                        user_path: Optional[str] = None) -> list:
    return sorted(load_templates(path, user_path).keys())


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
    # Optional modelfile block ({} or absent = no custom Modelfile).
    mf = template.get("modelfile") or {}
    if mf:
        if not isinstance(mf, dict):
            _fail(name, "modelfile must be a mapping")
        for k in mf:
            if k not in ALLOWED_MODEFILE_KEYS:
                _fail(name, f"unknown modelfile key {k!r} "
                            f"(allowed: {sorted(ALLOWED_MODEFILE_KEYS)})")
        if not isinstance(mf.get("from"), str) or not (mf.get("from") or "").strip():
            _fail(name, "modelfile.from is required (the source model to pull)")
        tmpl = mf.get("template")
        if tmpl is not None and not isinstance(tmpl, str):
            _fail(name, "modelfile.template must be a string")
        params = mf.get("parameters") or {}
        if not isinstance(params, dict):
            _fail(name, "modelfile.parameters must be a mapping")
        for pk, pv in params.items():
            if pk not in ALLOWED_MODEFILE_PARAM_KEYS:
                _fail(name, f"unknown modelfile parameter {pk!r} "
                            f"(allowed: {sorted(ALLOWED_MODEFILE_PARAM_KEYS)})")
            if pk == "stop":
                if not isinstance(pv, (list, tuple)) or \
                        not all(isinstance(s, str) for s in pv):
                    _fail(name, "modelfile.parameters.stop must be a list of strings")
            elif not isinstance(pv, (str, int, float, bool)):
                _fail(name, f"modelfile.parameters.{pk} must be a scalar")


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
                   path: Optional[str] = None,
                   user_path: Optional[str] = None) -> Dict:
    """Merge a template into the config so the TEMPLATE'S VALUES WIN.

    Priority (highest first):

        env vars  >  template  >  explicit config-file values  >  built-in default

    Rationale: selecting a template means "use this model's known-good settings".
    For that to be true, the template must actually take effect — including for
    keys the config file also mentions. The previous rule was "file beats
    template", which combined badly with the setup wizard (it always writes
    temperature/top_p/top_k/reasoning_enabled), so picking a template silently did
    nothing for exactly the knobs it cared most about.

    Any key the template does NOT set keeps whatever the config file (or the
    built-in default) provides, so a template never has to be exhaustive.

    The ``modelfile`` block (if present) is NOT merged — it is an install
    directive for the setup wizard / templates page, not a config value.

    To override a single template value deliberately, delete that key from the
    template OR put the value in an env var (env still wins over everything).
    `<<SETTINGS>>` prints what is actually in force.
    """
    if not template_name:
        return data
    catalogue = load_templates(path, user_path)
    if template_name not in catalogue:
        raise TemplateError(
            f"unknown model template {template_name!r}. Available: "
            f"{sorted(catalogue.keys()) or 'none'}"
        )
    tpl = expand(catalogue[template_name], model, template_name)
    merged = dict(data)
    # Sections: template values override the file's; file keeps anything the
    # template does not mention.
    for section in ("sampling", "timeouts"):
        block = tpl.get(section)
        if block:
            merged[section] = {**(data.get(section) or {}), **block}
    # Per-model overrides: template wins over the file's block for the same model.
    if tpl.get("models"):
        merged_models = dict(data.get("models") or {})
        for mname, overrides in tpl["models"].items():
            merged_models[mname] = {**(merged_models.get(mname) or {}), **overrides}
        merged["models"] = merged_models
    return merged


def render_modelfile(modelfile: Dict) -> str:
    """Render an Ollama Modelfile from a template's ``modelfile`` block.

    Produces ``FROM <source>`` + the optional ``TEMPLATE`` + one ``PARAMETER``
    line per entry (a ``stop`` list emits one line per stop token). The caller
    writes this to a file and runs ``ollama create <name> -f <file>``.
    """
    def _val(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return f'"{v}"'  # strings (stop tokens, etc.) are quoted like the docs

    lines = [f"FROM {modelfile['from'].strip()}"]
    tmpl = (modelfile.get("template") or "").strip()
    if tmpl:
        lines.append(f'TEMPLATE """{tmpl}"""')
    for k, v in (modelfile.get("parameters") or {}).items():
        vals = v if isinstance(v, (list, tuple)) else [v]
        for item in vals:
            lines.append(f"PARAMETER {k} {_val(item)}")
    return "\n".join(lines) + "\n"


def _coerce_param(v: str):
    """Best-effort type coercion for a PARAMETER value read back from text."""
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def parse_modelfile(text: str) -> Dict:
    """Parse an Ollama Modelfile back into a structured ``modelfile`` block.

    Reverse of ``render_modelfile()``: ``FROM`` -> from, ``TEMPLATE`` -> template
    (triple-quote aware, so a multi-line template round-trips), ``PARAMETER``
    lines -> parameters (a repeated key becomes a list). Returns {} for blank
    input. Best-effort — malformed lines are skipped, not an error.
    """
    if not text or not text.strip():
        return {}
    out: Dict = {}
    params: Dict = {}
    tmpl_lines = []
    in_tmpl = False
    for raw in text.splitlines():
        line = raw.strip()
        if in_tmpl:
            if '"""' in line:
                head = line.split('"""', 1)[0]
                if head.strip():
                    tmpl_lines.append(head)
                in_tmpl = False
            else:
                tmpl_lines.append(line)
            continue
        if line.startswith("FROM "):
            out["from"] = line[5:].strip().strip('"')
        elif line.startswith("TEMPLATE "):
            rest = line[len("TEMPLATE "):].strip()
            if rest.startswith('"""'):
                rest = rest[3:]
                if '"""' in rest:
                    out["template"] = rest.split('"""', 1)[0]
                else:
                    tmpl_lines.append(rest)
                    in_tmpl = True
            else:
                out["template"] = rest
        elif line.startswith("PARAMETER "):
            rest = line[len("PARAMETER "):].strip()
            if not rest:
                continue
            parts = rest.split(None, 1)
            key = parts[0]
            val = _coerce_param(parts[1]) if len(parts) > 1 else ""
            if key == "stop":
                # `stop` is always a list in the schema, even for one token.
                params.setdefault("stop", []).append(val)
            elif key in params:
                params[key] = (params[key] if isinstance(params[key], list)
                               else [params[key]]) + [val]
            else:
                params[key] = val
    if tmpl_lines:
        out["template"] = "\n".join(tmpl_lines)
    if params:
        out["parameters"] = params
    return out


def describe(name: str, path: Optional[str] = None,
             user_path: Optional[str] = None) -> Dict:
    """Metadata for the setup wizard / a /templates endpoint."""
    tpl = (load_templates(path, user_path) or {}).get(name)
    if tpl is None:
        raise TemplateError(f"unknown model template {name!r}")
    return {
        "name": name,
        "description": tpl.get("description", ""),
        "notes": tpl.get("notes", ""),
        "sampling": tpl.get("sampling") or {},
        "timeouts": tpl.get("timeouts") or {},
        "models": tpl.get("models") or {},
        "modelfile": tpl.get("modelfile") or {},
    }
