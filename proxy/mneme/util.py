"""Shared utilities with no heavy dependencies.

Anything in here must be importable by any module in the package without
pulling in the DB, the embedder, or Flask — so keep it pure (no module-level
side effects that touch the outside world).
"""

import os
from datetime import datetime, timezone


def _extract_text(content) -> str:
    """Extract text from message content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    parts.append("[IMAGE: " + block.get("image_url", {}).get("url", "unknown") + "]")
        return "\n".join(parts)
    return str(content)


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
