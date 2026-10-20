"""Degeneration (mojibake/word-salad) detector for saved chunks.

Motivation (2026-10-18 post-mortem): a high-temperature model turn emitted
mojibake word-salad that was saved to conversations verbatim and then replayed
into every later turn's context, re-triggering degeneration — a self-feeding
loop that took down both a proxy session and a swarm.

Policy: detected chunks are NOT deleted. They are stored (record kept for
debugging) but flagged via the curation remove-chunk path (removed='removed',
actor='system'), which stops them from injecting while preserving the row.

Heuristics are deliberately conservative — they should only fire on text no
human would call a normal conversation turn.
"""

import re

# U+FFFD replacement chars — evidence the text was already byte-corrupted
_REPLACEMENT = "\ufffd"

# CJK Unified Ideographs + ext A, Hiragana, Katakana, Hangul syllables
_CJK_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]"
)
# Cyrillic
_CYR_RE = re.compile("[\u0400-\u04ff]")


def _english_word_count(text):
    return len(re.findall(r"[A-Za-z]{2,}", text))


def is_degenerate_message(content):
    """Return (degenerate: bool, reason: str) for one message's content.

    Fires when the message is fundamentally broken, not merely exotic:
      1. Replacement characters present in any number >= 2.
      2. CJK/Hangul makes up >25% of the text while English words are scarce
         (<1 word per 20 CJK chars) — i.e. CJK salad, not a Chinese sentence.
      3. Same 12+ char n-gram repeated 25+ times — looped output.
    """
    if not content or len(content) < 60:
        return False, ""

    if content.count(_REPLACEMENT) >= 2:
        return True, "replacement chars (U+FFFD) in output"

    cjk = len(_CJK_RE.findall(content))

    # Script INTERLEAVING is the word-salad signature: Latin tokens and CJK
    # tokens butting against each other across the text. Real prose — Chinese,
    # English, or English quoting one Chinese term — produces very few
    # script-boundary adjacencies; mojibake salad produces many.
    _latin = r"[A-Za-z]{2,}"
    bounds = len(re.findall(_latin + r"\s*[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
                            content))
    bounds += len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]\s*" + _latin,
                             content))
    words = _english_word_count(content)
    if bounds >= 8 and (cjk + words) > 0 and bounds > max(2, words // 3):
        return True, "script-interleaved salad: %d latin-cjk boundaries, %d english words" % (bounds, words)

    # cross-script mixing: Cyrillic interlaced with Latin tokens (salad
    # signature). Dense CJK or dense Cyrillic ALONE is legitimate prose.
    cyr = len(_CYR_RE.findall(content))
    cyr_bounds = len(re.findall(r"[A-Za-z]{2,}\s*[\u0400-\u04ff]", content))                + len(re.findall(r"[\u0400-\u04ff]\s*[A-Za-z]{2,}", content))
    if cyr >= 8 and cyr_bounds >= 8:
        return True, "latin-cyrillic salad: %d boundaries, %d cyrillic chars" % (cyr_bounds, cyr)

    # CJK chars WITH replacement chars is double evidence (checked above), but
    # dense CJK alone is legitimate Chinese — do not fire on it.

    # repetition loop: any 12-char window appearing 25+ times
    step = max(1, len(content) // 400)
    seen = {}
    for i in range(0, len(content) - 12, step):
        g = content[i:i + 12]
        if not g.strip() or g.isspace():
            continue
        seen[g] = seen.get(g, 0) + 1
        if seen[g] >= 25:
            return True, "looped n-gram %r x%d" % (g, seen[g])
        if len(seen) > 2000:
            break

    return False, ""


def is_degenerate_chunk(messages):
    """Return (degenerate: bool, reason: str) for a whole chunk.

    Inspects assistant/model/tool message text only; user text is the input
    (we never sanitize what the human said — it stays on record).
    """
    for m in messages or []:
        if m.get("role") in ("assistant", "model", "tool"):
            hit, why = is_degenerate_message(m.get("content") or "")
            if hit:
                return True, "[%s] %s" % (m.get("role"), why)
    return False, ""
