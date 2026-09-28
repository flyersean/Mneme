"""Tool capability metadata, permission levels, and capability selection.

Tools become described capabilities: what each does, how risky it is, which
permission level it needs, what it costs, and how to verify its result. The
harness uses this to (a) show a small model only the tools relevant to the
task, and (b) refuse tools a run's profile does not grant.

Permission levels (a run grants a set of them):
    read-only         read memory, files, the tool registry
    normal            flag/unflag memory
    network           web search, fetch pages
    filesystem-write  write files
    shell             run commands
    memory-write      remove memory
    system            anything unknown (MCP / client tools) — conservative default
    self-modification change prompts, skills, profiles, config, code (evolution)
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Set

PERMISSION_LEVELS = ("read-only", "normal", "network", "filesystem-write", "shell",
                     "memory-write", "system", "self-modification")
DEFAULT_GRANT = frozenset({"read-only", "normal", "network", "filesystem-write", "shell"})

BUILTIN_TOOL_META: Dict[str, dict] = {
    "search_memory": {"permission": "read-only", "risk": "low", "cost": "low",
                      "does": "search Mneme memory (past conversations, facts, fetched pages)",
                      "verify": "results cite mem_ ids", "tags": "memory recall remember past"},
    "list_tools": {"permission": "read-only", "risk": "low", "cost": "low",
                   "does": "list tools previously built and saved", "verify": "", "tags": "tool registry reuse"},
    "read_tool": {"permission": "read-only", "risk": "low", "cost": "low",
                  "does": "read the source of a saved tool", "verify": "", "tags": "tool source reuse"},
    "read_image": {"permission": "read-only", "risk": "low", "cost": "medium",
                   "does": "view a stored image", "verify": "", "tags": "image vision picture"},
    "read_file": {"permission": "read-only", "risk": "low", "cost": "low",
                  "does": "read a file (optionally a line range)", "verify": "file exists",
                  "tags": "file code config inspect read source repository"},
    "fetch_url": {"permission": "network", "risk": "medium", "cost": "medium",
                  "does": "fetch one web page as clean text", "verify": "non-empty text, not a bot wall",
                  "tags": "web page url http article read online"},
    "web_search": {"permission": "network", "risk": "low", "cost": "medium",
                   "does": "search the web for candidate pages (snippets only)",
                   "verify": "follow with fetch_url before stating facts",
                   "tags": "web search find online research latest current"},
    "bash": {"permission": "shell", "risk": "high", "cost": "medium",
             "does": "run a shell command on the host", "verify": "check the exit code",
             "tags": "shell command run test build install git python script compute"},
    "write": {"permission": "filesystem-write", "risk": "medium", "cost": "low",
              "does": "write a file on the host", "verify": "read it back / file_exists",
              "tags": "file write save create edit code notes"},
    "flag_bad_memory": {"permission": "normal", "risk": "low", "cost": "low",
                        "does": "flag a memory chunk as suspected wrong", "verify": "", "tags": "memory wrong flag"},
    "clear_bad_memory_flag": {"permission": "normal", "risk": "low", "cost": "low",
                              "does": "clear a bad-memory flag", "verify": "", "tags": "memory flag"},
    "remove_memory": {"permission": "memory-write", "risk": "medium", "cost": "low",
                      "does": "take a memory chunk out of use", "verify": "", "tags": "memory remove"},
}
_UNKNOWN = {"permission": "system", "risk": "unknown", "cost": "unknown", "does": "", "verify": "", "tags": ""}
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def tool_meta(name: str, description: str = "") -> dict:
    meta = dict(BUILTIN_TOOL_META.get(name) or _UNKNOWN)
    if description and not meta.get("does"):
        meta["does"] = description[:200]
    meta["name"] = name
    return meta


def normalize_grant(perms: Optional[Iterable[str]]) -> Set[str]:
    if perms is None:
        return set(DEFAULT_GRANT)
    perms = {str(p).strip() for p in perms if str(p).strip()}
    bad = perms - set(PERMISSION_LEVELS) - {"*"}
    if bad:
        raise ValueError(f"unknown permission levels: {sorted(bad)} (known: {', '.join(PERMISSION_LEVELS)})")
    return set(PERMISSION_LEVELS) if "*" in perms else perms


def allowed(name: str, grant: Set[str]) -> bool:
    return tool_meta(name)["permission"] in grant


def allowed_names(names: Iterable[str], grant: Set[str]) -> Set[str]:
    return {n for n in names if allowed(n, grant)}


def select_tools(query: str, names: Iterable[str], k: int = 4) -> List[dict]:
    """The k tools whose metadata best matches the task text (lexical)."""
    q = set(_TOKEN_RE.findall((query or "").lower()))
    scored = []
    for n in names:
        m = tool_meta(n)
        words = set(_TOKEN_RE.findall(f"{n.replace('_', ' ')} {m['does']} {m['tags']}".lower()))
        score = len(q & words)
        if score:
            scored.append((score, m))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [m for _, m in scored[:k]]
