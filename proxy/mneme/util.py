"""Shared utilities with no heavy dependencies.

Anything in here must be importable by any module in the package without
pulling in the DB, the embedder, or Flask — so keep it pure (no module-level
side effects that touch the outside world).
"""

import base64
import hashlib
import os
from datetime import datetime, timezone


def _extract_text(content) -> str:
    """Extract text from message content (str, list of blocks, or None)."""
    if content is None:
        return ""  # None content (e.g. an assistant tool-call turn) must NOT become "None"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "unknown")
                    if isinstance(url, str) and url.startswith("data:"):
                        # A data URL carries the raw base64 — never dump that into a
                        # text path (staging / retrieval query / token estimate). Show
                        # only the MIME; the bytes live in the image store / backend.
                        _m = url[5:].split(";", 1)[0] or "image"
                        url = f"data:{_m};base64,<...>"
                    parts.append("[IMAGE: " + url + "]")
        return "\n".join(parts)
    return str(content)


def _split_content(content):
    """Split message content into (text, image_blocks).

    Handles a plain string or an OpenAI multimodal array. The returned text is the
    concatenated text blocks WITHOUT any image placeholder; `image_blocks` is the
    list of `{"type":"image_url","image_url":{...}}` dicts. Used to convert to a
    backend's native image format (Ollama `images`, OpenAI passthrough)."""
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []
    texts = []
    images = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            texts.append(block.get("text", ""))
        elif t == "image_url":
            images.append(block)
    return "\n".join(texts), images


def _image_bytes_from_block(block):
    """Resolve an image_url block to (bytes, mime) — or (None, None) on failure.

    Handles both a `data:` URL (base64) and an http(s) URL (fetched). This is the
    single place image bytes are materialized, so Ollama conversion and the
    content-addressed image store share the same resolution logic."""
    try:
        url = (block.get("image_url") or {}).get("url", "")
    except Exception:
        url = ""
    if not url:
        return None, None
    if url.startswith("data:"):
        meta, _, b64 = url.partition(",")
        mime = None
        m = meta[5:]  # strip "data:"
        if ";" in m:
            m = m.split(";", 1)[0]
        if m:
            mime = m
        try:
            return base64.b64decode(b64), mime
        except Exception:
            return None, None
    try:
        import requests
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            return None, None
        mime = (r.headers.get("Content-Type", "") or "").split(";", 1)[0].strip() or None
        return r.content, mime
    except Exception:
        return None, None


def _sniff_mime(data):
    """Detect MIME type from magic bytes (fallback None)."""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:12] == b"\x00\x00\x00\x0cjP  \r\n\x87\n":
        return "image/jpeg2000"
    return None


def _mime_to_ext(mime):
    """Canonical file extension for a MIME type (dedupe-consistent naming)."""
    m = (mime or "").lower().split(";", 1)[0].strip()
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/gif": "gif",
        "image/bmp": "bmp",
        "image/webp": "webp",
        "image/jpeg2000": "jp2",
        "image/svg+xml": "svg",
        "image/tiff": "tiff",
    }.get(m, "bin")


def _image_token_estimate(block):
    """Rough token cost of an image (heuristic — only for context-budget math).

    OpenAI charges 85 tokens for a low-res image and up to ~1440 for a high-res
    one (tiles). We can't know resolution without decoding, so estimate from the
    base64 byte count for data URLs and use a conservative default for http URLs."""
    try:
        url = (block.get("image_url") or {}).get("url", "")
    except Exception:
        url = ""
    if url.startswith("data:"):
        _, _, b64 = url.partition(",")
        raw = max(0, len(b64) * 3 // 4)
        return max(85, min(1440, raw // 512))
    return 1000  # unknown-size http image — conservative


def _to_ollama_messages(msgs):
    """Convert proxy messages to Ollama's native chat format.

    Ollama takes `content` as a plain string and images as a separate `images`
    list of base64 (no data-URL prefix). An OpenAI multimodal `content` array is
    split accordingly; string content passes through unchanged."""
    out = []
    for m in msgs:
        text, imgs = _split_content(m.get("content", ""))
        nm = {"role": m["role"], "content": text or ""}
        b64s = []
        for b in imgs:
            data, _mime = _image_bytes_from_block(b)
            if data:
                b64s.append(base64.b64encode(data).decode("ascii"))
        if b64s:
            nm["images"] = b64s
        if m.get("tool_calls"):
            nm["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            nm["tool_call_id"] = m["tool_call_id"]
        if m.get("name"):
            nm["name"] = m["name"]
        out.append(nm)
    return out


def _log_error(where: str, e: Exception):
    """Append 'timestamp | where | type | message' to errors.log. Never raises."""
    try:
        cd = os.environ.get("MNEME_CHUNK_DIR", "/workspace/mneme_chunks")
        os.makedirs(cd, exist_ok=True)
        with open(os.path.join(cd, "errors.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} | {where} | "
                    f"{type(e).__name__} | {e}\n")
    except Exception:
        pass  # error log must never itself crash the proxy
    try:
        print(f"  [ERR][{where}] {type(e).__name__}: {str(e)[:200]}", flush=True)
    except Exception:
        pass
