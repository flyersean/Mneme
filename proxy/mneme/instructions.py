"""Externalized instruction loader — every injected prompt as an editable file.

code-default + disk-override + graceful fallback. This is the prompt analog of
mneme.yaml: a user can tune wording for a quirky model or an edge case without
a code edit, and a bad file degrades to the shipped default instead of breaking
an injection.

The prompts are AUTO-MATERIALIZED at startup (materialize_instructions()): every
shipped prompt is written to disk so it can be READ and EDITED like system_prompt.md
— no code edit, and no need to hand-create a file with the right format.

Directory layout (under $MNEME_CHUNK_DIR/instructions/):

    default/<name>.txt       — the prompt (auto-created on first run; edit to override)
    <model-dir>/<name>.txt   — per-model override (wins over default/)

Each file carries optional frontmatter (self-documenting), commented lines that the
loader strips before substitution:

    # when: injected when a task's problem type is a flagged capability edge
    # vars: {{problem_type}}
    # used_by: _capability_directive

    <body with {{placeholders}}>

`_load_instruction(name, default, vars)` is the single entry point. Placeholder
substitution uses `{{var}}` (reserved syntax — prompts contain literal braces,
so `{{...}}` is unambiguous). An unknown placeholder raises rather than emitting
broken text (fail loud).
"""

import os
import re


def _instructions_dir() -> str:
    # MNEME_CHUNK_DIR is normalized by the monolith at startup; default mirrors
    # its CHUNK_DIR fallback. Resolved lazily so env is set by first use.
    cd = os.environ.get("MNEME_CHUNK_DIR", "/workspace/mneme_chunks")
    return os.path.join(cd, "instructions")


_FRONTMATTER_RE = re.compile(r'^\s*#\s*(when|vars|used_by)\s*:\s*(.*)$')
_VAR_RE = re.compile(r'\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}')


# ── Shipped defaults (the code-default layer) ──────────────────────────
# Each name maps to the exact string that was previously hardcoded inline. An
# override file replaces it; a missing/malformed override falls back here.

DEFAULT_INSTRUCTIONS = {
    "explore": (
        "\n\n=== EXPLORE DIRECTIVE (user-requested) ===\n"
        "The user explicitly asked you to try a NEW method not covered by your "
        "saved strategies. Do NOT reuse a known strategy — find a different "
        "technique. A novel method that works is graded 'great' and saved for "
        "future reuse.\n"
    ),
    "capability_edge": (
        "=== CAPABILITY EDGE ===\n"
        "You have previously failed or performed poorly on tasks of type '{{problem_type}}'. "
        "Do NOT retry it from memory alone — OVERCOME the edge instead. STOP making other "
        "calls and decide exactly one:\n"
        "  - \"DECISION: build_tool\" + a \"PLAN:\" (write a script that solves it, test it with bash, then \"TOOL_SAVE:\" it)\n"
        "  - \"DECISION: reuse_tool\" + \"TOOL: <name>\" (a tool you already built — list_tools/read_tool — or one you find online via web_search)\n"
        "  - \"DECISION: declare_edge\" + a \"MISSING:\" note — only AFTER you have genuinely tried to build or reuse a tool and failed\n"
    ),
    "overcome": (
        "\n=== OVERCOME MODE ===\n"
        "You are stuck: {{reason}}. STOP — do NOT make any more tool calls, searches, or fetch "
        "attempts. Retrying will not help.\n"
        "You cannot call tools right now. Respond with plain text ONLY, choosing exactly one of:\n"
        "  - \"DECISION: reuse_tool\" plus \"TOOL: <name>\" (if a listed built tool already solves this)\n"
        "  - \"DECISION: build_tool\" plus a \"PLAN:\" (the tool/script that would solve this, and how to build and test it)\n"
        "  - \"DECISION: declare_edge\" plus a \"MISSING:\" note (the capability you lack)\n"
        "(\"build a tool\" means write a script you can run via bash — you cannot add or modify the fixed "
        "harness tools: read, bash, edit, write, search_memory, web_search, web_scrape.)"
    ),
    "overcome_reuse": (
        "\n=== REUSE MODE ===\n"
        "You chose to reuse the existing tool '{{tool}}'. Run it with bash and use its output to "
        "answer the user. The tool is at: {{path}}\n"
        "If it works, answer directly. If it fails, say so and reconsider (build a new tool or declare the edge)."
    ),
    "synthesize_nudge": (
        "\n=== WRAP UP ===\n"
        "You have made {{count}} tool calls without a final answer. You almost certainly have enough "
        "information now. Synthesize your final answer to the user — do NOT make another tool call "
        "unless something critical is genuinely missing."
    ),
    "hard_wrapup": (
        "\n=== STOP AND ANSWER ===\n"
        "You have made {{count}} tool calls without a final answer. STOP calling tools now. "
        "Synthesize your final answer to the user from the information you have already gathered. "
        "If you genuinely cannot answer with what you have, state plainly what is missing."
    ),
    "write_script_nudge": (
        "\n=== WRITE A SCRIPT ===\n"
        "You have made {{count}} bash calls against the same target ({{resource}}). Stop extracting "
        "one field at a time. Write a single script that fetches the target once and extracts "
        "everything you need in one pass, then run it once with bash."
    ),
    "step_back_examine": (
        "\n=== STEP BACK ===\n"
        "You have made {{count}} tool calls and are not converging. Stop making new calls for a moment "
        "and reason out loud:\n"
        "1. What exactly are you trying to obtain or find?\n"
        "2. What have you already tried, and why did each attempt fail?\n"
        "3. What is the actual obstacle (blocked, JS-rendered, auth, rate-limit, wrong tool)?\n"
        "Then take ONE genuinely different approach.\n"
        "You are NOT limited to what you already tried. You can CREATE the resource you need "
        "(write a script; `pip install <lib>`; install a headless browser via "
        "`pip install playwright && playwright install chromium`; `npm i`; call an API) and you can "
        "FIND resources with your tools (web_search to research how; list_tools/read_tool to reuse a "
        "tool you already built; search_memory for past work). Do not repeat an approach that failed."
    ),
    "step_back_adapt": (
        "\n=== TRY ANOTHER ANGLE ===\n"
        "You have made {{count}} tool calls and are still stuck; the previous approach did not work. "
        "Diagnose the error you are seeing.\n"
        "Now ask: has ANYONE solved this problem before? A \"known solution\" means one the world has "
        "found — NOT just your own past turns. If you can describe the problem in one sentence, search "
        "for a solution online (web_search for the technique, library, or service that solves it, e.g. "
        "\"how to scrape a javascript-rendered page\", \"headless browser CLI\"). When you find one, "
        "adapt it: install or write the tool it points to, then run it. If no known solution exists, "
        "name a categorically different approach and take it."
    ),
    "step_back_concede": (
        "\n=== CONCEDE OR ANSWER ===\n"
        "You have made {{count}} tool calls and still cannot get the key fact. Before giving up, ask: "
        "is there a resource you could CREATE to reach it (install a headless browser, write a scraper, "
        "call an API)? If you have genuinely exhausted creating and finding resources, stop and say so "
        "plainly — tell the user what you could NOT get and why. Then give them everything you DID "
        "find, with a clear note on what is missing. Do not make any more tool calls."
    ),
    "overcome_build": (
        "\n=== BUILD MODE (step {{iteration}}/{{max}}) ===\n"
        "Build the tool from your plan: write it under the tools directory using write, then test it "
        "with bash. When it works, output \"TOOL_SAVE: <name> :: <description> :: <path>\". "
        "If it fails, fix and retry — you have a limited number of build steps before you must declare the edge."
    ),
    "overcome_build_exhausted": (
        "\n=== BUILD EXHAUSTED ===\n"
        "You have used all {{max}} build attempts without a working tool. Stop building. "
        "Answer honestly: state what you could not achieve and which capability is missing "
        "(output \"DECISION: declare_edge\" and a \"MISSING:\" note)."
    ),
    "tool_failure_nudge": (
        "You have had {{count}} tool failures in a row. Stop retrying and diagnose the "
        "root cause, then switch to a fundamentally different method."
    ),
    "meta_principles_header": "\n=== META-PRINCIPLES (always apply) ===\n",
    "user_preferences_header": "\n=== USER PREFERENCES (learned from explicit requests — honor these) ===",
    "system_directives_header": "=== SYSTEM DIRECTIVES (learned from past experience) ===",
}

# Normalize: strip leading/trailing whitespace so each prompt round-trips faithfully
# through the on-disk files (the loader strips the blank-line separator between the
# frontmatter and the body). Separation whitespace is the injection call-site's job,
# not the prompt's — the two *_header prompts below get their leading newline added
# by build_context/_preferences_block.
DEFAULT_INSTRUCTIONS = {k: v.strip() for k, v in DEFAULT_INSTRUCTIONS.items()}


# Self-documentation for each prompt (the "when / vars / used_by" frontmatter that
# materialize_instructions() writes into the on-disk files). Kept in sync with
# docs/instructions.md. `vars` is a space-separated list of {{placeholders}}.
INSTRUCTION_META = {
    "explore": ("user explicitly asks for a NEW method", "", "_explore_directive"),
    "capability_edge": ("task type is a flagged edge → hard stop: build/reuse/declare", "{{problem_type}}", "_capability_directive"),
    "overcome": ("model is stuck (2 failures / 6 rounds), hard stop", "{{problem_type}} {{reason}}", "_overcome_directive"),
    "overcome_reuse": ("model chose reuse_tool — run the existing tool", "{{tool}} {{path}}", "_reuse_directive"),
    "overcome_build": ("model chose build_tool — one bounded build iteration", "{{iteration}} {{max}}", "_build_directive"),
    "overcome_build_exhausted": ("build iterations exhausted — force declare_edge", "{{max}}", "_build_exhausted_directive"),
    "synthesize_nudge": ("≥8 tool calls w/o a final answer (advisory)", "{{count}}", "_synthesize_nudge"),
    "hard_wrapup": ("repeated identical tool calls (redundancy hard stop)", "{{count}}", "_hard_wrapup_directive"),
    "write_script_nudge": ("≥5 distinct bash calls on one target (soft)", "{{count}} {{resource}}", "_write_script_nudge"),
    "step_back_examine": ("≥6 tool calls w/o answer — examine + pivot (soft)", "{{count}}", "_step_back_directive"),
    "step_back_adapt": ("≥12 tool calls w/o answer — adapt a known solution", "{{count}}", "_step_back_directive"),
    "step_back_concede": ("≥20 tool calls w/o answer — concede honestly (hard stop)", "{{count}}", "_step_back_directive"),
    "tool_failure_nudge": ("≥2 consecutive tool failures (soft, before overcome)", "{{count}}", "_tool_failure_nudge"),
    "meta_principles_header": ("always — header above the meta-principles", "", "_meta_principles_block"),
    "user_preferences_header": ("stored preferences exist", "", "_preferences_block"),
    "system_directives_header": ("saved strategies are injected", "", "build_context"),
}


# Canonical order the prompts fire in a conversation (used by the /instructions
# reference page so a user can read them top-to-bottom in the order they'd appear).
# Not every prompt fires every turn — this is the "if a conversation escalates
# fully" order. Anything not listed here (shouldn't happen) is appended last.
INSTRUCTION_ORDER = [
    "meta_principles_header",     # always — context build, every turn
    "system_directives_header",   # always — context build, every turn
    "user_preferences_header",    # always — context build, every turn
    "explore",                    # user explicitly asks for a NEW method
    "capability_edge",            # the task's problem type was previously flagged
    "tool_failure_nudge",         # ≥2 consecutive tool failures
    "write_script_nudge",         # ≥5 bash calls against the same target
    "synthesize_nudge",           # ≥8 calls w/o a final answer (advisory)
    "step_back_examine",          # ≥6 calls — examine the obstacle, pivot
    "step_back_adapt",            # ≥12 calls — find a known solution online
    "step_back_concede",          # ≥20 calls — concede honestly (hard stop)
    "hard_wrapup",                # repeated identical calls (redundancy stop)
    "overcome",                   # stuck → choose build / reuse / declare
    "overcome_reuse",             #   reuse an already-built tool
    "overcome_build",             #   build a new tool
    "overcome_build_exhausted",   #   build attempts ran out
]


def materialize_instructions():
    """Write every shipped prompt to $MNEME_CHUNK_DIR/instructions/default/<name>.txt.

    This is what makes the injected prompts READABLE and EDITABLE on disk without a
    code edit — the same ergonomics as system_prompt.md. Each file gets a short
    self-documenting frontmatter (when / vars / used_by) the loader strips, then the
    prompt body. Existing files are left untouched so user edits survive; only
    missing files are created. Deleting a file reverts to the built-in default.
    """
    default_dir = os.path.join(_instructions_dir(), "default")
    try:
        os.makedirs(default_dir, exist_ok=True)
    except OSError as e:
        print(f"  [INSTRUCTIONS][ERR] cannot create {default_dir}: {e}", flush=True)
        return
    created = 0
    for name, text in DEFAULT_INSTRUCTIONS.items():
        path = os.path.join(default_dir, name + ".txt")
        if os.path.isfile(path):
            continue  # never clobber a file the user may have edited
        when, vars_, used_by = INSTRUCTION_META.get(name, ("", "", ""))
        head = []
        if when:
            head.append(f"# when: {when}")
        if vars_:
            head.append(f"# vars: {vars_}")
        if used_by:
            head.append(f"# used_by: {used_by}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(head) + "\n\n" + text + "\n")
            created += 1
        except OSError as e:
            print(f"  [INSTRUCTIONS][ERR] cannot write {path}: {e}", flush=True)
    if created:
        print(f"  [INSTRUCTIONS] materialized {created} prompt file(s) under {default_dir}", flush=True)


def list_instructions():
    """Return the prompts in conversation order, reading the LIVE default/ files
    (with the code default as fallback). Each entry: {name, when, vars, used_by,
    content, path, model_override}. Used by the /instructions reference page."""
    ordered = list(INSTRUCTION_ORDER)
    for name in DEFAULT_INSTRUCTIONS:  # any default not in the order list (defensive)
        if name not in ordered:
            ordered.append(name)
    model_dir = _model_override_dir()
    result = []
    for name in ordered:
        when, vars_, used_by = INSTRUCTION_META.get(name, ("", "", ""))
        path = os.path.join(_instructions_dir(), "default", name + ".txt")
        body = None
        if os.path.isfile(path):
            body, _ = _parse_instruction_file(path)
        if body is None:
            body = DEFAULT_INSTRUCTIONS.get(name, "")
        model_override = ""
        if model_dir:
            mp = os.path.join(_instructions_dir(), model_dir, name + ".txt")
            if os.path.isfile(mp):
                model_override = mp
        result.append({
            "name": name,
            "when": when,
            "vars": vars_,
            "used_by": used_by,
            "content": body,
            "path": path,
            "model_override": model_override,
        })
    return result


def save_instruction(name, content):
    """Write a user-edited prompt body back to default/<name>.txt, preserving the
    self-documenting frontmatter (reconstructed from INSTRUCTION_META). Returns the
    path written; raises OSError on failure. Unknown names raise ValueError."""
    if name not in DEFAULT_INSTRUCTIONS:
        raise ValueError(f"unknown instruction name: {name}")
    when, vars_, used_by = INSTRUCTION_META.get(name, ("", "", ""))
    head = []
    if when:
        head.append(f"# when: {when}")
    if vars_:
        head.append(f"# vars: {vars_}")
    if used_by:
        head.append(f"# used_by: {used_by}")
    path = os.path.join(_instructions_dir(), "default", name + ".txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if head:
            f.write("\n".join(head) + "\n\n")
        f.write(content.rstrip("\n") + "\n")
    return path


def _load_instruction(name, default=None, vars=None):
    """Return the instruction text for `name`.

    Precedence: per-model override > default/ override > `default` (code).
    `vars` maps {{placeholder}} -> value. A missing/malformed override file
    falls back to `default` and logs; an unknown placeholder raises.
    """
    if default is None:
        default = DEFAULT_INSTRUCTIONS.get(name, "")
    text = _read_override(name)
    if text is None:
        text = default
    elif text != default:
        # Only announce a REAL edit (materialized defaults match the code default,
        # so this stays quiet unless the user actually changed the wording).
        print(f"  [INSTRUCTIONS] {name}: using edited override file", flush=True)
    return _substitute(text, vars or {})


def _read_override(name):
    """Return the override body for `name`, or None if none exists/parses."""
    for subdir in (_model_override_dir(), "default"):
        if not subdir:
            continue
        path = os.path.join(_instructions_dir(), subdir, name + ".txt")
        if not os.path.isfile(path):
            continue
        body, _ = _parse_instruction_file(path)
        if body is not None:
            return body
        print(f"  [INSTRUCTIONS][WARN] {name}: override exists but is empty/malformed "
              f"({path}) — using code default", flush=True)
    return None


def _model_override_dir():
    """Per-model override subdir name (a safe filename derived from the model)."""
    model = os.environ.get("MNEME_MODEL", "")
    if not model:
        return ""
    return re.sub(r'[^a-zA-Z0-9_.-]+', '_', model)


def _parse_instruction_file(path):
    """Parse an instruction file -> (body, frontmatter). (None, {}) on failure."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception as e:
        print(f"  [INSTRUCTIONS][ERR] cannot read {path}: {e}", flush=True)
        return None, {}
    fm = {}
    body = []
    for line in lines:
        m = _FRONTMATTER_RE.match(line)
        if m and not body:
            fm[m.group(1)] = m.group(2).strip()
        else:
            body.append(line)
    text = "\n".join(body).strip()
    return (text if text else None), fm


def _substitute(text, vars):
    """Replace {{var}} with vars[var]; raise KeyError on an unknown placeholder."""
    def repl(m):
        key = m.group(1)
        if key not in vars:
            raise KeyError(f"unknown placeholder in instruction template: {key}")
        return str(vars[key])
    return _VAR_RE.sub(repl, text)
