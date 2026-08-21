"""Shared utilities with no heavy dependencies.

Anything in here must be importable by any module in the package without
pulling in the DB, the embedder, or Flask — so keep it pure (no module-level
side effects that touch the outside world).
"""


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
