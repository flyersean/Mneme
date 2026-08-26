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
    commands: List[str] = field(default_factory=list)     # the key arg (command/query/path) per tool call
    elapsed: float = 0.0
    tokens: Optional[dict] = None

    def used(self, substr: str) -> bool:
        """Did any tool call's key arg (command/query/path) mention this?"""
        return any(substr in (c or "") for c in self.commands)

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
    def consulted_tools(self) -> bool:
        """Called list_tools/read_tool. WEAK signal — the registry may be empty, so
        this means 'looked for a tool', NOT 'reused a tool'."""
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
# Generic shell/stdio tokens that are always present and never represent an
# ACQUIRED capability — excluded when fingerprinting the tools a run used.
_GENERIC_TOKENS = {
    "bash", "sh", "python3", "python", "pip", "pip3", "cd", "ls", "cat", "echo",
    "grep", "awk", "sed", "which", "apt", "apt-get", "export", "head", "tail",
    "wc", "sort", "uniq", "cut", "tr", "printf", "mkdir", "cp", "mv", "rm",
    "exit", "command", "type", "set", "curl", "wget", "env", "xargs", "find",
    "tee", "readlink", "realpath", "basename", "dirname",
}


def _tool_tokens(rr: RunResult) -> set:
    """Fingerprint the specific tools/executables a run used — the 'acquired
    capability' tokens: pdftotext, pypdf, pandoc, unzip, a pip package, etc."""
    import re
    tokens = set()
    for c in (rr.commands or []):
        if not c:
            continue
        low = c.lower()
        for m in re.findall(r"pip(?:3)?\s+install\s+([a-z0-9_.\-]+)", low):
            tokens.add(m)
        for seg in c.split("&&"):
            seg = seg.strip()
            parts = seg.split()
            if not parts:
                continue
            exe = parts[0].split("/")[-1].strip().lower()
            if exe and exe not in _GENERIC_TOKENS and not exe.startswith("cd"):
                tokens.add(exe)
        for m in re.findall(r"(pdftotext|pypdf|pdfplumber|pandoc|unzip|tesseract|"
                            r"ffmpeg|convert|identify|docx2txt|mammoth|pymupdf|fitz|"
                            r"poppler|pdfinfo)", low):
            tokens.add(m)
    return tokens


def reused_tool(rr1: RunResult, rr2: RunResult) -> bool:
    """Did task2 reuse a tool/command that task1 introduced (discovered/built/
    installed)? True when the two runs share a non-generic tool token (e.g. both
    ran pdftotext). This is the honest reuse signal — it does NOT fire on an
    empty list_tools call."""
    t1 = _tool_tokens(rr1)
    t2 = _tool_tokens(rr2)
    return bool(t1 and t1 & t2)


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
    """Deterministic correctness. Numeric oracles: any number in the answer may
    match (the answer often leads with "field 3 ... 58.76", so a first-number
    grab is wrong). Non-numeric: case-insensitive substring."""
    a = (answer or "").strip()
    o = (oracle or "").strip()
    if not o:
        return False
    try:
        target = float(o.replace(",", ""))
    except ValueError:
        return o.lower() in a.lower()
    import re
    for m in re.findall(r"-?\d[\d,]*\.?\d*", a):
        try:
            got = float(m.replace(",", ""))
        except ValueError:
            continue
        if math.isclose(got, target, rel_tol=1e-9):
            return True
    return False


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
        "reused_tool": reused_tool(rr1, rr2),
        "consulted_tools": rr2.consulted_tools,
        "method_transferred": probe["transferred"],
        "method_probe": probe,
        "unnecessary_work_1": unnecessary_work(rr1, s1),
        "unnecessary_work_2": unnecessary_work(rr2, s2),
        # headline: persistent capability gain = task2 solved AND reused a tool
        # task1 introduced (a real reuse, not an empty list_tools call)
        "persistent_capability_gain": s2 and reused_tool(rr1, rr2),
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
    commands = []
    for t in trace:
        args = t.get("args") or {}
        key = ""
        if isinstance(args, dict):
            for k in ("command", "query", "path", "file_path", "name", "url"):
                if args.get(k) is not None:
                    key = str(args[k])
                    break
        commands.append(key)
    return RunResult(answer=msg.get("content", ""), tools=tools, results=results,
                     commands=commands, elapsed=elapsed, tokens=d.get("usage"))


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


def _make_pdf(text: str) -> bytes:
    """Minimal single-page PDF with the given text, in a FlateDecode-compressed
    content stream. stdlib-only to GENERATE, but extracting the text needs a real
    PDF parser (poppler/pypdf) — `strings`/`grep` can't see compressed text, so
    this is a genuine 'requires a tool' gap."""
    import zlib

    esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 14 Tf 72 720 Td ({esc}) Tj ET".encode("latin-1")
    cstream = zlib.compress(content)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"),
        (b"<< /Length " + str(len(cstream)).encode()
         + b" /Filter /FlateDecode >>\nstream\n" + cstream + b"\nendstream"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    return bytes(out)


def make_env_pdf_text(seed: int = 9) -> Environment:
    """PDF text extraction — a REAL gap: Python stdlib has no PDF parser, and the
    text is FlateDecode-compressed so `strings`/`grep` can't read it. The model
    must FIND a tool (pdftotext) or INSTALL one (pypdf) to succeed."""
    rng = __import__("random").Random(seed)
    total1 = rng.randint(1000, 9999)
    total2 = rng.randint(1000, 9999)
    pdf1 = _make_pdf(f"Order summary. TOTAL: {total1}")
    pdf2 = _make_pdf(f"Order summary. TOTAL: {total2}")
    return Environment(
        id="pdf_text",
        task1="Extract the text from data/report.pdf and report the amount after 'TOTAL:'.",
        task2="Extract the text from data/report2.pdf and report the amount after 'TOTAL:'.",
        oracle1=str(total1),
        oracle2=str(total2),
        capability="PDF text extraction",
        discoverable=True,  # pdftotext (poppler) is present; pypdf installable
        files={"report.pdf": pdf1, "report2.pdf": pdf2},
    )


def _make_png(width: int, height: int) -> bytes:
    """Minimal valid 8-bit RGB PNG (stdlib only) — enough for `file`/Pillow to
    report dimensions, and for a hand-rolled IHDR parser to read width/height."""
    import struct
    import zlib

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))  # filter0, black
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_docx(text: str) -> bytes:
    """Minimal valid .docx (a ZIP of WordprocessingML XML), stdlib-only, with the
    text in a DEFLATE-compressed `word/document.xml` so `strings`/`grep` can't see
    it. `pdftotext` cannot read it — this is the 'similar task, wrong tool' case."""
    import io
    import zipfile

    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>')
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def make_env_docx_text(seed: int = 13) -> Environment:
    """Text extraction from a .docx — same task shape as pdf_text, but pdftotext
    does NOT work here. The model must use `unzip`/python-docx, NOT the PDF tool.
    Used as the NEGATIVE case for 'does the saved rule fire on the wrong task?'."""
    rng = __import__("random").Random(seed)
    sub1 = rng.randint(1000, 9999)
    sub2 = rng.randint(1000, 9999)
    return Environment(
        id="docx_text",
        task1="Extract the text from data/notes.docx and report the amount after 'SUBTOTAL:'.",
        task2="Extract the text from data/notes2.docx and report the amount after 'SUBTOTAL:'.",
        oracle1=str(sub1),
        oracle2=str(sub2),
        capability="docx text extraction",
        discoverable=True,  # unzip / python-docx; NOT pdftotext
        files={"notes.docx": _make_docx(f"Invoice. SUBTOTAL: {sub1}"),
               "notes2.docx": _make_docx(f"Invoice. SUBTOTAL: {sub2}")},
    )


def make_env_png_dims(seed: int = 5) -> Environment:
    """A genuinely DISCOVERABLE gap: extracting image dimensions from a PNG. The
    model has no stdlib call that does this in one line — it must FIND a tool
    (`file`, ImageMagick `identify`, `pip install Pillow`) or BUILD a tiny IHDR
    parser. bash+python3-stdlib alone cannot read a PNG's width trivially."""
    rng = __import__("random").Random(seed)
    w1 = rng.randint(50, 400); h1 = rng.randint(50, 400)
    w2 = rng.randint(50, 400); h2 = rng.randint(50, 400)
    return Environment(
        id="png_dims",
        task1="What is the pixel width of data/image.png?",
        task2="What is the pixel height of data/image2.png?",
        oracle1=str(w1),
        oracle2=str(h2),
        capability="image metadata extraction",
        discoverable=True,  # `file` / Pillow / identify all report dimensions
        files={"image.png": _make_png(w1, h1), "image2.png": _make_png(w2, h2)},
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


ALL_ENVIRONMENTS = [make_env_records, make_env_binary, make_env_timestamps, make_env_png_dims, make_env_pdf_text, make_env_docx_text]
