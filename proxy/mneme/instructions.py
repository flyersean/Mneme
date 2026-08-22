"""Externalized instruction loader — every injected prompt as an editable file.

code-default + disk-override + graceful fallback. This is the prompt analog of
mneme.yaml: a user can tune wording for a quirky model or an edge case without
a code edit, and a bad file degrades to the shipped default instead of breaking
an injection.

Directory layout (under $MNEME_CHUNK_DIR/instructions/):

    default/<name>.txt       — override for the code default
    <model-dir>/<name>.txt   — per-model override (wins over default/)

Each file carries optional frontmatter (self-documenting + used by the sync
test), commented lines that the loader strips before substitution:

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
        "\n=== CAPABILITY EDGE ===\n"
        "You have previously failed or performed poorly on tasks of type '{{problem_type}}'.\n"
        "Do NOT attempt this again from memory alone. Either (a) propose the exact tool, "
        "command, or script that would answer it correctly, or (b) state clearly that you "
        "cannot answer it and what capability is missing."
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
    else:
        print(f"  [INSTRUCTIONS] {name}: using override file", flush=True)
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
