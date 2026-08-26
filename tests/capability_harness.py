"""
Hidden Capability Tasks — a live-model benchmark for Mneme's self-improvement loop.

The goal of the loop is NOT "build a tool". It is: get the task done, fastest and
best. A missing capability may be obtained by BUILDING a tool, FINDING one
(web / pip / the laptop), or ADAPTING an existing one — all equivalent as far as
correct output is concerned, and discovery is usually the cheaper road.

This harness therefore scores the OUTCOME, not the means:

  solved       — does the answer match a deterministic oracle?
  efficiency   — wall time, token count, and number of failed tool attempts.
  reused       — does a related-but-different second task reuse the acquisition
                 (a tool, or the method: search/install-first) from task 1?
  unnecessary  — did it build/search when a direct answer or an already-available
                 tool would have done?  ("unnecessary WORK", not just tool creation.)
  method       — did task 2 transfer the METHOD (find-a-parser, not the parser
                 itself) across a DIFFERENT format?

Each environment is data-first and deterministic: a generator writes a small set
of files with a weird/structured format plus a fixed oracle. Trajectories are
classified A..G, matching the agreed scale:

  A  solved directly          task -> answer
  B  solved with existing tools  task -> existing tool -> answer
  C  failed / gave up         task -> attempts -> "can't do it"
  D  recognized the gap       task -> failure -> "I need a tool for X"
  E  built a tool, failed verify  build -> broken -> give up
  F  built/found a working tool  build/find -> test -> use -> answer
  G  acquired + reused/generalized  task2 reuses task1's tool OR method

The harness is split so the CLASSIFICATION and SCORING logic is testable with
synthetic traces (no live model) — see tests/test_capability_harness.py.
"""

from __future__ import annotations

import json
import math
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import List, Optional


# ─── Tool categories (how a tool call maps to the acquisition story) ────────
_DISCOVERY_TOOLS = {"web_search", "fetch_url", "search_memory"}
_BUILD_TOOLS = {"write"}          # native write == authoring a script/tool
_REUSE_TOOLS = {"list_tools", "read_tool"}

# A lightweight failure heuristic, independent of Mneme internals. Objective
# failure shapes only — same spirit as mneme/tool_trail._FAILURE_MARKERS.
_FAILURE_MARKERS = (
    "no results", "no matching", "nothing found", "0 results", "no matches",
    "not found", "no data", "blocked", "captcha", "cloudflare", "access denied",
    "forbidden", "too many requests", "rate limit", "rate-limited", "challenge",
    "timed out", "timeout", "connection refused", "connection reset",
    "command not found", "no such file", "permission denied", "cancelled",
    "error", "failed", "exception", "traceback", "syntax error",
)


def _looks_like_failure(result_text) -> bool:
    """Heuristic: does a tool RESULT string look like an objective failure?"""
    if not isinstance(result_text, str):
        return False
    t = result_text.strip().lower()
    if not t:
        return True  # empty result == failure
    return any(m in t for m in _FAILURE_MARKERS)


# ─── Run result ──────────────────────────────────────────────────────────────
@dataclass
class RunResult:
    answer: str = ""
    tools: List[str] = field(default_factory=list)       # tool names, in order
    results: List[str] = field(default_factory=list)      # tool result strings, aligned with tools
    elapsed: float = 0.0
    tokens: Optional[dict] = None

    @property
    def failures(self) -> int:
        return sum(1 for r in self.results if _looks_like_failure(r))

    @property
    def built(self) -> bool:
        return any(t in _BUILD_TOOLS for t in self.tools)

    @property
    def discovered(self) -> bool:
        return any(t in _DISCOVERY_TOOLS for t in self.tools)

    @property
    def reused(self) -> bool:
        return any(t in _REUSE_TOOLS for t in self.tools)

    def search_before_build(self) -> bool:
        """Did the run search (discover) BEFORE any write/build? The cheap path."""
        if not self.discovered:
            return False
        if not self.built:
            return True  # discovered, never built -> clearly discovery-first
        first_disc = min(i for i, t in enumerate(self.tools) if t in _DISCOVERY_TOOLS)
        first_build = min(i for i, t in enumerate(self.tools) if t in _BUILD_TOOLS)
        return first_disc < first_build


# ─── Environment ─────────────────────────────────────────────────────────────
@dataclass
class Environment:
    id: str
    task1: str
    task2: str                       # related-but-different (method-generalization probe)
    oracle1: str
    oracle2: str
    capability: str                  # human label of the missing capability
    discoverable: bool               # is there an existing tool to find (vs must build)?
    files: dict = field(default_factory=dict)  # filename -> content, placed in a scratch dir


# ─── Oracle: is the answer correct? ──────────────────────────────────────────
def check_oracle(answer: str, oracle: str) -> bool:
    """Deterministic correctness. Compares the first number in the answer to the
    oracle number (whitespace/commas/signs stripped); falls back to substring
    match for non-numeric oracles."""
    a = (answer or "").strip()
    o = (oracle or "").strip()
    if not o:
        return False
    # numeric oracle: extract the first number from the answer
    try:
        target = float(o.replace(",", ""))
    except ValueError:
        return o.lower() in a.lower()
    import re
    m = re.search(r"-?\d[\d,]*\.?\d*", a)
    if not m:
        return False
    try:
        got = float(m.group(0).replace(",", ""))
    except ValueError:
        return False
    return math.isclose(got, target, rel_tol=1e-9)


# ─── Trajectory classification (single task) ─────────────────────────────────
def classify_single(rr: RunResult, oracle: str) -> str:
    """Classify one task's trajectory A..F. G is assessed across the pair."""
    solved = check_oracle(rr.answer, oracle)
    if solved:
        if rr.built:
            return "F"      # built a tool and it worked
        if rr.discovered:
            return "B"      # found/used an existing approach, solved
        if rr.tools:
            return "B"      # solved with existing tools
        return "A"          # solved directly, no tools
    # not solved
    if rr.built:
        return "E"          # built something but it didn't pass the oracle
    if rr.discovered and not tools_beyond_discovery(rr):
        return "C"          # searched but still gave up
    # recognized the gap vs plain gave up: "I need a tool / can't do this"
    low = rr.answer.lower()
    if any(k in low for k in ("need a tool", "i need", "requires a", "no tool", "missing",
                              "don't have a tool", "build a tool", "capability")):
        return "D"
    return "C"


def tools_beyond_discovery(rr: RunResult) -> bool:
    return any(t not in _DISCOVERY_TOOLS and t not in _REUSE_TOOLS for t in rr.tools)


# ─── Method-reuse probe (across the pair) ────────────────────────────────────
def method_reuse_probe(rr1: RunResult, rr2: RunResult) -> dict:
    """Did task 2 transfer the METHOD from task 1 (find-a-parser / search-install-
    first) rather than re-learning it the hard way (bash-fail-first)?

    Signals, all cheap and observable:
      fewer failures   — task2 failed fewer times than task1 before succeeding.
      discovery-first  — task2 searched/installed before writing.
      direct            — task2 had no failures at all (method fully internalized).
    """
    return {
        "fewer_failures": rr2.failures < rr1.failures,
        "discovery_first": rr2.search_before_build() and not rr2.built,
        "no_failures": rr2.failures == 0,
        "transferred": rr2.failures < rr1.failures,
    }


# ─── Unnecessary-work detector ───────────────────────────────────────────────
def unnecessary_work(rr: RunResult, solved: bool) -> bool:
    """Flag waste: building a tool (write) or searching (web_search/fetch_url) when
    the task was actually solvable directly with the tools already present. A
    crude proxy for 'built a tool for a one-liner'."""
    if not solved:
        return False  # only flag waste on tasks that were ultimately solved
    return rr.built and (not rr.discovered) and rr.failures == 0


# ─── Scoring (the outcome-first metrics) ─────────────────────────────────────
def score(env: Environment, rr1: RunResult, rr2: RunResult) -> dict:
    s1 = check_oracle(rr1.answer, env.oracle1)
    s2 = check_oracle(rr2.answer, env.oracle2)
    probe = method_reuse_probe(rr1, rr2)
    return {
        "env": env.id,
        "task1_solved": s1,
        "task1_class": classify_single(rr1, env.oracle1),
        "task2_solved": s2,
        "task2_class": classify_single(rr2, env.oracle2),
        "task1_failures": rr1.failures,
        "task2_failures": rr2.failures,
        "task1_elapsed": round(rr1.elapsed, 1),
        "task2_elapsed": round(rr2.elapsed, 1),
        "task1_tools": rr1.tools,
        "task2_tools": rr2.tools,
        "reused_tool": rr2.reused,
        "method_transferred": probe["transferred"],
        "method_probe": probe,
        "unnecessary_work_1": unnecessary_work(rr1, s1),
        "unnecessary_work_2": unnecessary_work(rr2, s2),
        # headline: persistent capability gain = task2 solved via reuse/transfer
        "persistent_capability_gain": s2 and (rr2.reused or probe["transferred"]),
    }


# ─── Live runner (HTTP to the proxy) ─────────────────────────────────────────
def run_task(base_url: str, content: str, timeout: int = 300) -> RunResult:
    body = json.dumps({
        "model": "text-mneme:64k",
        "messages": [{"role": "user", "content": content}],
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    elapsed = time.time() - t0
    msg = d.get("choices", [{}])[0].get("message", {})
    trace = d.get("tool_trace", []) or []
    tools = [t.get("tool", "?") for t in trace]
    results = [str(t.get("result", "") or "") for t in trace]
    return RunResult(answer=msg.get("content", ""), tools=tools, results=results,
                     elapsed=elapsed, tokens=d.get("usage"))


# ─── Deterministic environment generators ────────────────────────────────────
def make_env_records(seed: int = 7) -> Environment:
    """Custom multi-char delimiter, with genuinely malformed (corrupted) lines to
    skip. Field 3 is a number; task2 switches delimiter AND field. Real parsing
    logic (filter + extract + aggregate), not a one-liner."""
    rng = __import__("random").Random(seed)
    lines, zx_vals = [], []
    for i in range(40):
        a = rng.randint(1, 9)
        b = rng.randint(1, 9)
        c = rng.randint(1, 100)
        code = "ZX" if i % 2 == 0 else "QQ"
        if i % 7 == 3:
            # corrupted: missing the code field (3 fields instead of 4) — visibly
            # malformed so a parser (and the oracle) can reject it by field count.
            lines.append(f"{a}@@{b}@@{c}")
        else:
            lines.append(f"{a}@@{b}@@{c}@@{code}")
            if code == "ZX":
                zx_vals.append(c)
    lines.insert(5, "broken line")  # a genuinely malformed record
    data1 = "\n".join(lines) + "\n"

    avg = round(sum(zx_vals) / len(zx_vals), 4)

    # task2: different delimiter |;| and a count of field 5 > 400
    lines2, cnt = [], 0
    for i in range(30):
        w = rng.randint(1, 9); x = rng.randint(1, 9); y = rng.randint(1, 9)
        f5 = rng.randint(0, 900)
        lines2.append(f"{w}|;|{x}|;|{y}|;|0|;|{f5}")
        if f5 > 400:
            cnt += 1
    data2 = "\n".join(lines2) + "\n"

    return Environment(
        id="records_avg",
        task1=("In data/records.dat each record uses the @@ delimiter. Field 3 is a number. "
               "Find the average of field 3 across all records where the code field is ZX, "
               "ignoring any corrupted records."),
        task2=("In data/sales.dat each record uses the |;| delimiter. Count how many records "
               "have field 5 greater than 400."),
        oracle1=str(avg),
        oracle2=str(cnt),
        capability="custom-delimiter record parsing",
        discoverable=True,  # awk/cut/python stdlib all handle this
        files={"records.dat": data1, "sales.dat": data2},
    )


def make_env_binary(seed: int = 11) -> Environment:
    """A binary header + fixed records. Needs a real parser (struct.unpack), not
    a text one-liner — a genuine build-it gap for a bash-only agent. Task 2 uses
    DIFFERENT records (same layout) so reuse is generalization, not memorization."""
    import struct
    rng = __import__("random").Random(seed)
    n = 24
    header_ts = 1_700_000_000 + rng.randint(0, 1_000_000)
    recs = [rng.randint(0, 1000) for _ in range(n)]
    blob = struct.pack("<II", header_ts, n) + b"".join(struct.pack("<I", v) for v in recs)
    avg = round(sum(recs) / n, 4)

    n2 = 24
    recs2 = [rng.randint(0, 1000) for _ in range(n2)]
    blob2 = struct.pack("<II", header_ts, n2) + b"".join(struct.pack("<I", v) for v in recs2)

    return Environment(
        id="binary_avg",
        task1=("data/metrics.bin is a little-endian binary: a 4-byte unsigned header timestamp, "
               "a 4-byte unsigned record count, then that many 4-byte unsigned records. "
               "What is the mean of the records?"),
        task2=("data/metrics2.bin has the same layout. What is the maximum record value?"),
        oracle1=str(avg),
        oracle2=str(max(recs2)),
        capability="binary record parsing",
        discoverable=False,  # must actually write a parser
        files={"metrics.bin": blob, "metrics2.bin": blob2},
    )


def make_env_timestamps(seed: int = 3) -> Environment:
    """Weird timestamp strings -> normalized ISO. Discoverable (date / a known
    lib) but fiddly enough that a naive parse fails. Task 2 switches the field
    ORDER (MM/DD vs DD/MM) to probe method transfer, not memorization."""
    rng = __import__("random").Random(seed)

    def rnd():
        return (rng.randint(2019, 2024), rng.randint(1, 12), rng.randint(1, 28),
                rng.randint(0, 23), rng.randint(0, 59))

    rows1 = [rnd() for _ in range(12)]
    # DD/MM/YYYY HH:MM
    data1 = "\n".join(f"{d:02d}/{mo:02d}/{y} {h:02d}:{mi:02d}"
                      for (y, mo, d, h, mi) in rows1) + "\n"
    earliest = min(f"{y:04d}-{mo:02d}-{d:02d}" for (y, mo, d, h, mi) in rows1)

    rows2 = [rnd() for _ in range(12)]
    # MM/DD/YYYY HH:MM  (different field order than task 1)
    data2 = "\n".join(f"{mo:02d}/{d:02d}/{y} {h:02d}:{mi:02d}"
                      for (y, mo, d, h, mi) in rows2) + "\n"
    latest = max(f"{y:04d}-{mo:02d}-{d:02d}" for (y, mo, d, h, mi) in rows2)

    return Environment(
        id="timestamps_norm",
        task1=("data/log.txt holds timestamps in DD/MM/YYYY HH:MM order. Normalize them all "
               "to ISO-8601 (YYYY-MM-DD) and report the earliest date."),
        task2=("data/log2.txt holds timestamps in MM/DD/YYYY HH:MM order. Normalize them "
               "and report the latest date (YYYY-MM-DD)."),
        oracle1=earliest,
        oracle2=latest,
        capability="timestamp normalization",
        discoverable=True,
        files={"log.txt": data1, "log2.txt": data2},
    )


ALL_ENVIRONMENTS = [make_env_records, make_env_binary, make_env_timestamps]
