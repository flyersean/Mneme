"""In-chat control commands: <<SETTINGS>> and <<RETRIEVAL ...>>.

Two things the user asked for:
  1. Print the model's CURRENT effective settings in the chat.
  2. Change the retrieval threshold from the chat, without editing files.

Why these are commands (``<<...>>``) rather than tools: they control the harness
itself, not the task. A model tool that mutates its own retrieval settings could
be manipulated by content it reads; a command is typed by the human, so authority
stays with the user. The existing <<SAVE>> / <<LEARN>> commands work this way.

Why a file write at all: the config file is the source of truth and survives
restarts. The setter edits mneme.yaml (preserving every other line) and the
hot-reload path picks the change up on the next request. A value that only lived
in memory would be a trap — it would silently revert on restart.

Command grammar:

    <<SETTINGS>>                        print current effective settings
    <<RETRIEVAL>>                       print just the retrieval section
    <<RETRIEVAL inject_min_similarity=0.60>>     set one retrieval key
    <<RETRIEVAL inject_min_similarity=0.60 max_injected_tokens=4000>>
    <<RETRIEVAL reset>>                 restore config-file values (clear overrides)

The setter is deliberately restricted to a whitelist of retrieval keys. A general
"write any config key from chat" command would be a footgun (and a prompt-injection
target); anything outside the whitelist is rejected with the list.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Optional, Tuple


class CommandError(Exception):
    """Raised for an invalid command invocation (bad key, bad value)."""


# Keys the <<RETRIEVAL>> setter may change. All are float/int scalars with a
# known safe range, all are hot-reloadable, and all live in the `retrieval:`
# section of mneme.yaml. Anything else is refused.
RETRIEVAL_KEYS = {
    "inject_min_similarity": (float, 0.0, 1.0),
    "strategy_min_similarity": (float, 0.0, 1.0),
    "novel_inject_floor": (float, 0.0, 1.0),
    "topic_switch_sim": (float, 0.0, 1.0),
    "max_injected_tokens": (int, 1, 10_000_000),
    "max_per_topic": (int, 0, 10_000),
    "max_siblings": (int, 0, 10_000),
    "topic_switch_grace": (int, 0, 1000),
    "age_decay_days": (float, 0.0, 100_000.0),
}

RETRIEVAL_CMD_RE = re.compile(r"<<RETRIEVAL(?:\s+([^>]*?))?\s*>>", re.I)
SETTINGS_CMD_RE = re.compile(r"<<(SETTINGS|RETRIEVAL_SETTINGS)>>", re.I)


def _coerce(key: str, raw: str):
    """Parse `raw` for `key`, enforcing the whitelist's type and range."""
    spec = RETRIEVAL_KEYS.get(key)
    if spec is None:
        raise CommandError(
            f"unknown retrieval key {key!r}. Settable: {', '.join(sorted(RETRIEVAL_KEYS))}"
        )
    typ, lo, hi = spec
    try:
        val = typ(raw)
    except (TypeError, ValueError):
        raise CommandError(f"{key} must be a {typ.__name__}, got {raw!r}")
    if not (lo <= val <= hi):
        raise CommandError(f"{key}={val} out of range [{lo}, {hi}]")
    return val


def parse_set_assignments(arg: str) -> Dict[str, object]:
    """Parse 'k=v k=v' into a validated dict. Raises CommandError on anything odd."""
    out: Dict[str, object] = {}
    for tok in (arg or "").split():
        if "=" not in tok:
            raise CommandError(
                f"expected key=value, got {tok!r}. "
                f"Example: <<RETRIEVAL inject_min_similarity=0.60>>"
            )
        k, _, v = tok.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not k:
            raise CommandError(f"missing key in {tok!r}")
        out[k] = _coerce(k, v)
    if not out:
        raise CommandError("no assignments given")
    return out


def _fmt_val(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def format_settings(snapshot: Dict) -> str:
    """Render the effective-settings report shown in chat.

    `snapshot` is a dict of section -> {key: value} built by the proxy (it owns
    the real values). Pure formatting, so it is trivially testable.
    """
    lines = ["=== CURRENT MNEME SETTINGS (effective values) ===", ""]
    model = snapshot.get("model") or {}
    if model:
        lines.append("MODEL / BACKEND")
        for k, v in model.items():
            lines.append(f"  {k:24} {v}")
        lines.append("")
    for section in ("sampling", "thinking", "retrieval", "timeouts", "storage", "curation"):
        block = snapshot.get(section) or {}
        if not block:
            continue
        lines.append(section.upper())
        for k, v in block.items():
            lines.append(f"  {k:24} {v}")
        lines.append("")
    pmo = snapshot.get("per_model_overrides") or {}
    if pmo:
        lines.append("PER-MODEL OVERRIDES (these beat SAMPLING above)")
        for k, v in pmo.items():
            lines.append(f"  {k:24} {v}")
        lines.append("")
    overrides = snapshot.get("overrides") or {}
    if overrides:
        lines.append("IN-CHAT OVERRIDES (<<RETRIEVAL>>, not written to config)")
        for k, v in overrides.items():
            lines.append(f"  {k:24} {v}")
        lines.append("")
    template = snapshot.get("template")
    if template:
        lines.append(f"MODEL TEMPLATE: {template}")
        lines.append("")
    lines.append("Change retrieval:  <<RETRIEVAL inject_min_similarity=0.60>>")
    lines.append("Reset to config:   <<RETRIEVAL reset>>")
    return "\n".join(lines)


def update_config_file(path: str, section: str, assignments: Dict[str, object]) -> str:
    """Set `section`.<key> = value in the YAML config at `path`, preserving the file.

    Rewrites ONLY the affected keys, leaving comments, ordering, and every other
    setting intact — a full yaml.safe_load + dump round-trip would strip the
    user's comments, which for this project (self-documenting config) would be
    destructive.

    Returns a short human-readable summary of what changed.
    """
    if not path or not os.path.exists(path):
        raise CommandError(f"config file not found: {path}")
    with open(path, encoding="utf-8") as f:
        text = f.read()

    changed = []
    for key, val in assignments.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise CommandError(f"unsafe config key {key!r}")
        rendered = _fmt_val(val)
        # Find the section block, then the key within it.
        sec_re = re.compile(rf"^{re.escape(section)}\s*:\s*$", re.M)
        m = sec_re.search(text)
        if not m:
            # Section absent — append it.
            text = text.rstrip("\n") + f"\n\n{section}:\n  {key}: {rendered}\n"
            changed.append(f"{section}.{key}={rendered} (section added)")
            continue
        start = m.end()
        # Section body = up to the next top-level (non-indented, non-comment) line.
        rest = text[start:]
        body_m = re.search(r"^(?=\S)", rest, re.M)
        end = start + (body_m.start() if body_m else len(rest))
        body = text[start:end]
        # Replace the key's line if present, else insert after the section header.
        key_re = re.compile(rf"^(\s+)({re.escape(key)}\s*:)([^\n]*)$", re.M)
        if key_re.search(body):
            def _sub(mo):
                # Keep any trailing comment on the line so the file stays documented.
                tail = mo.group(3)
                cmt = ""
                if "#" in tail:
                    cmt = "  " + tail[tail.index("#"):].strip()
                return f"{mo.group(1)}{mo.group(2)} {rendered}{cmt}"
            new_body = key_re.sub(_sub, body, count=1)
        else:
            # Insert as the first entry of the section, matching its indent.
            indent_m = re.search(r"^(\s+)\S", body, re.M)
            indent = indent_m.group(1) if indent_m else "  "
            new_body = f"\n{indent}{key}: {rendered}" + body
        text = text[:start] + new_body + text[end:]
        changed.append(f"{section}.{key}={rendered}")

    # Atomic write so a proxy hot-reload can never read a half-written file.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return ", ".join(changed)
