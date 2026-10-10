#!/usr/bin/env python3
"""Canvas Server — industry-standard file workspace with editor, preview, version history, and chat.

This extension serves a web-based code workspace matching the big-3 AI canvas UI:
  - Top toolbar: Open/Save/New/Run/Undo/Redo/Export/Writing shortcuts/Coding shortcuts/History/Settings
  - File tree sidebar with context menu (create, rename, delete)
  - Split center pane: Edit | Preview | Diff tabs with CodeMirror editor
  - Live preview pane (HTML/Markdown render, code output)
  - Console/output pane with Run button and ANSI color support
  - Chat pane with AI assistant integration
  - Version history with snapshots and diff view
  - Status bar with cursor position, encoding, language mode
  - Resizable panes with drag handles

Endpoints:
  GET   /                       — serves the canvas HTML page
  GET   /api/status             — server health + config summary
  GET   /api/files              — lists files in the workspace directory
  GET   /api/files/<path>       — returns file content
  PUT   /api/files/<path>       — saves file content (body = raw file content)
  POST  /api/files/<path>       — creates a new file/folder
  DELETE /api/files/<path>      — deletes a file/folder
  POST  /api/files/<path>/rename — renames a file/folder
  POST  /api/run                — runs a file via subprocess, returns stdout/stderr
  POST  /api/preview            — renders a file for preview (HTML/MD/code output)
  GET   /api/versions           — lists version history for a file
  GET   /api/versions/<id>      — gets a specific version content
  POST  /api/versions           — creates a version snapshot
  GET   /api/versions/<id>/diff — diffs two versions
  POST  /api/export             — exports a file in requested format
  POST  /api/action             — applies a writing/coding shortcut action
  POST  /api/chat               — sends conversation messages to the managing Mneme proxy

Usage:
  python3 canvas_server.py [--port PORT] [--workspace DIR]
"""

import html
import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_PORT = 9090
DEFAULT_WORKSPACE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
VERSIONS_DIR = ".canvas_versions"


# ──────────────────────────────────────────────────────────────────────────────
# Workspace helpers
# ──────────────────────────────────────────────────────────────────────────────

def resolve_workspace_path(workspace_dir, rel_path):
    """Resolve a relative path inside the workspace, preventing directory traversal."""
    full = os.path.normpath(os.path.join(workspace_dir, rel_path))
    if not full.startswith(os.path.normpath(workspace_dir) + os.sep) and full != os.path.normpath(workspace_dir):
        return None
    return full


def list_files(workspace_dir, prefix=""):
    """Recursively list files in the workspace as a tree structure."""
    entries = []
    try:
        names = sorted(os.listdir(workspace_dir))
    except OSError:
        return entries

    for name in names:
        if name.startswith("."):
            continue
        full_path = os.path.join(workspace_dir, name)
        rel_path = os.path.join(prefix, name) if prefix else name
        if os.path.isdir(full_path):
            children = list_files(full_path, rel_path)
            entries.append({
                "name": name,
                "path": rel_path,
                "type": "directory",
                "children": children,
            })
        else:
            entries.append({
                "name": name,
                "path": rel_path,
                "type": "file",
            })
    return entries


# ──────────────────────────────────────────────────────────────────────────────
# Version history helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_versions_dir(workspace_dir):
    """Get the versions directory path, creating it if needed."""
    vdir = os.path.join(workspace_dir, VERSIONS_DIR)
    os.makedirs(vdir, exist_ok=True)
    return vdir


def list_versions(workspace_dir, file_path):
    """List version snapshots for a given file."""
    vdir = get_versions_dir(workspace_dir)
    versions = []
    if not os.path.isdir(vdir):
        return versions
    for fname in sorted(os.listdir(vdir), reverse=True):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(vdir, fname), "r") as f:
                    meta = json.load(f)
                if meta.get("file_path") == file_path:
                    versions.append(meta)
            except (json.JSONDecodeError, OSError):
                pass
    return versions


def save_version(workspace_dir, file_path, content, label=""):
    """Create a version snapshot of a file."""
    vdir = get_versions_dir(workspace_dir)
    version_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now(timezone.utc).isoformat()
    meta = {
        "id": version_id,
        "file_path": file_path,
        "timestamp": timestamp,
        "label": label,
    }
    # Save content
    content_path = os.path.join(vdir, f"{version_id}.content")
    with open(content_path, "w") as f:
        f.write(content)
    # Save metadata
    meta_path = os.path.join(vdir, f"{version_id}.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    return meta


def get_version_content(workspace_dir, version_id):
    """Get the content of a specific version."""
    vdir = get_versions_dir(workspace_dir)
    content_path = os.path.join(vdir, f"{version_id}.content")
    meta_path = os.path.join(vdir, f"{version_id}.json")
    if not os.path.isfile(content_path) or not os.path.isfile(meta_path):
        return None, None
    with open(content_path, "r") as f:
        content = f.read()
    with open(meta_path, "r") as f:
        meta = json.load(f)
    return content, meta


def diff_versions(content_a, content_b):
    """Simple line-based diff between two text contents."""
    lines_a = content_a.splitlines(keepends=True)
    lines_b = content_b.splitlines(keepends=True)
    # Use difflib for a proper unified diff
    import difflib
    diff = list(difflib.unified_diff(lines_a, lines_b, n=3))
    return "".join(diff)


# ──────────────────────────────────────────────────────────────────────────────
# Export helpers
# ──────────────────────────────────────────────────────────────────────────────

def export_as_pdf(text_content, filename):
    """Export content as PDF. Requires reportlab or fpdf."""
    try:
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.set_font("Courier", size=10)
        for line in text_content.split("\n"):
            try:
                pdf.cell(0, 5, line.encode("latin-1", "replace").decode("latin-1"), new_x="LMARGIN", new_y="NEXT")
            except:
                pdf.cell(0, 5, "[encoding error]", new_x="LMARGIN", new_y="NEXT")
        return bytes(pdf.output())
    except ImportError:
        return None


def export_as_markdown(text_content, filename):
    """Wrap content in a markdown code fence if it looks like code."""
    ext = os.path.splitext(filename)[1].lower()
    code_exts = {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".html", ".css", ".json", ".xml", ".yaml", ".yml", ".sh", ".rb", ".go", ".rs"}
    if ext in code_exts:
        lang = ext.lstrip(".")
        return f"```{lang}\n{text_content}\n```"
    return text_content


def export_as_docx(text_content, filename):
    """Export content as DOCX. Requires python-docx."""
    try:
        from docx import Document
        doc = Document()
        doc.add_heading(filename, level=1)
        for line in text_content.split("\n"):
            doc.add_paragraph(line)
        buf = tempfile.BytesIO()
        doc.save(buf)
        return buf.getvalue()
    except ImportError:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Preview helpers
# ──────────────────────────────────────────────────────────────────────────────

def render_preview(file_path, content):
    """Render file content for preview. Returns (html_content, content_type)."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".html":
        # Serve HTML directly (sandboxed by iframe)
        return content, "text/html"

    elif ext in (".md", ".markdown"):
        # Render Markdown to HTML
        try:
            import markdown
            html_body = markdown.markdown(content, extensions=["fenced_code", "codehilite", "tables"])
        except ImportError:
            html_body = f"<pre>{html.escape(content)}</pre>"
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>body{{font-family:system-ui,sans-serif;padding:20px;line-height:1.6;color:#d4d4d4;background:#1e1e1e}}
pre{{background:#2d2d2d;padding:12px;border-radius:6px;overflow-x:auto}}
code{{background:#2d2d2d;padding:2px 6px;border-radius:3px}}
img{{max-width:100%}}</style></head><body>{html_body}</body></html>""", "text/html"

    elif ext in (".py", ".js", ".ts", ".java", ".c", ".cpp", ".rs", ".go", ".rb", ".sh"):
        # Syntax-highlighted code preview
        try:
            from pygments import highlight
            from pygments.lexers import get_lexer_for_filename
            from pygments.formatters import HtmlFormatter
            lexer = get_lexer_for_filename(file_path)
            formatter = HtmlFormatter(style="monokai", full=True, linenos=True)
            html_body = highlight(content, lexer, formatter)
        except ImportError:
            html_body = f"<pre>{html.escape(content)}</pre>"
        return html_body, "text/html"

    elif ext in (".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg"):
        # Formatted data preview
        try:
            from pygments import highlight
            from pygments.lexers import get_lexer_for_filename
            from pygments.formatters import HtmlFormatter
            lexer = get_lexer_for_filename(file_path)
            formatter = HtmlFormatter(style="monokai", full=True, linenos=True)
            html_body = highlight(content, lexer, formatter)
        except ImportError:
            html_body = f"<pre>{html.escape(content)}</pre>"
        return html_body, "text/html"

    elif ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"):
        # Image preview — return None to signal binary/image serving
        return None, "image"

    else:
        # Plain text preview
        return f"<pre>{html.escape(content)}</pre>", "text/html"


# ──────────────────────────────────────────────────────────────────────────────
# HTML page (embedded — no external files needed at runtime)
# ──────────────────────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Canvas Workspace</title>
<style>
  /* ═══════════════════════════════════════════════════════════════════════
     CSS Variables & Reset
     ═══════════════════════════════════════════════════════════════════════ */
  :root {
    --bg: #1e1e1e;
    --sidebar-bg: #252526;
    --toolbar-bg: #2a2a2a;
    --header-bg: #2d2d2d;
    --border: #333;
    --border-light: #444;
    --text: #d4d4d4;
    --text-dim: #888;
    --text-muted: #999;
    --accent: #0078d4;
    --accent-hover: #1a8ae8;
    --active-bg: #37373d;
    --hover-bg: #2a2d2e;
    --error: #f14c4c;
    --warning: #cca700;
    --success: #4ec94e;
    --unsaved: #e2b714;
    --font-mono: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', monospace;
    --font-ui: system-ui, -apple-system, sans-serif;
    --radius: 4px;
    --radius-sm: 3px;
    --toolbar-height: 42px;
    --statusbar-height: 24px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; font-family: var(--font-ui); font-size: 14px; color: var(--text); background: var(--bg); }
  body { display: flex; flex-direction: column; overflow: hidden; }

  /* ═══════════════════════════════════════════════════════════════════════
     Top Toolbar
     ═══════════════════════════════════════════════════════════════════════ */
  #topbar {
    display: flex; align-items: center; gap: 4px;
    padding: 0 12px; height: var(--toolbar-height);
    background: var(--toolbar-bg);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    user-select: none;
  }
  #topbar .brand { font-size: 14px; font-weight: 600; color: var(--text); margin-right: 16px; display: flex; align-items: center; gap: 6px; }
  #topbar .brand svg { width: 18px; height: 18px; }
  #topbar .separator { width: 1px; height: 20px; background: var(--border-light); margin: 0 4px; }
  .tb-group { display: flex; align-items: center; gap: 2px; }
  .tb-btn {
    display: flex; align-items: center; justify-content: center;
    gap: 4px; padding: 4px 8px; height: 28px;
    background: transparent; border: 1px solid transparent;
    color: var(--text-dim); border-radius: var(--radius-sm);
    cursor: pointer; font-size: 12px; font-family: var(--font-ui);
    white-space: nowrap; transition: all 0.1s;
  }
  .tb-btn:hover { background: var(--hover-bg); color: var(--text); border-color: var(--border-light); }
  .tb-btn:active { background: var(--active-bg); }
  .tb-btn:disabled { opacity: 0.35; cursor: default; }
  .tb-btn .icon { font-size: 14px; line-height: 1; }
  .tb-btn .label { font-size: 11px; }
  .tb-btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .tb-btn.primary:hover { background: var(--accent-hover); }
  .tb-btn.danger { color: var(--error); }
  .tb-btn.danger:hover { background: rgba(241,76,76,0.15); border-color: var(--error); }

  /* Dropdown menus */
  .tb-dropdown { position: relative; }
  .tb-dropdown-menu {
    position: absolute; top: 100%; left: 0; z-index: 1000;
    background: var(--sidebar-bg); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 4px 0;
    min-width: 180px; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
    display: none;
  }
  .tb-dropdown-menu.open { display: block; }
  .tb-dropdown-item {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 12px; cursor: pointer; font-size: 12px; color: var(--text);
  }
  .tb-dropdown-item:hover { background: var(--hover-bg); }
  .tb-dropdown-item .icon { font-size: 14px; }
  .tb-dropdown-item .shortcut { margin-left: auto; color: var(--text-dim); font-size: 10px; }
  .tb-dropdown-item.disabled { opacity: 0.4; cursor: default; }

  /* ═══════════════════════════════════════════════════════════════════════
     Main layout: sidebar | editor+preview+console | chat
     ═══════════════════════════════════════════════════════════════════════ */
  #main { display: flex; flex: 1; height: calc(100vh - var(--toolbar-height) - var(--statusbar-height)); }

  /* ── Sidebar (file tree) ── */
  #sidebar {
    width: 240px; min-width: 160px; max-width: 500px;
    background: var(--sidebar-bg); overflow-y: auto;
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column;
    flex-shrink: 0;
  }
  #sidebar-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 12px; font-size: 11px; font-weight: 600;
    color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border);
  }
  #sidebar-header .actions { display: flex; gap: 4px; }
  #sidebar-header .actions button {
    background: none; border: none; color: var(--text-dim);
    cursor: pointer; font-size: 14px; padding: 2px; line-height: 1;
    border-radius: var(--radius-sm);
  }
  #sidebar-header .actions button:hover { color: var(--text); background: var(--hover-bg); }
  #file-tree { padding: 4px 0; flex: 1; overflow-y: auto; }
  .tree-item {
    display: flex; align-items: center;
    padding: 3px 8px 3px 12px; cursor: pointer;
    color: var(--text-dim); font-size: 13px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .tree-item:hover { background: var(--hover-bg); color: var(--text); }
  .tree-item.active { background: var(--active-bg); color: #fff; }
  .tree-item .icon { margin-right: 6px; font-size: 12px; flex-shrink: 0; }
  .tree-item.directory { font-weight: 500; }
  .tree-item .children { display: none; }
  .tree-item.expanded > .children { display: block; }
  .tree-item .dirty-dot { color: var(--unsaved); font-size: 10px; margin-left: 4px; }

  /* ── Resize handle (sidebar) ── */
  #sidebar-resize {
    width: 4px; cursor: col-resize; background: transparent;
    flex-shrink: 0; position: relative; z-index: 10;
  }
  #sidebar-resize:hover, #sidebar-resize.active { background: var(--accent); }

  /* ── Center: editor + tabs + preview + console ── */
  #center {
    flex: 1; display: flex; flex-direction: column;
    min-width: 300px; background: var(--bg);
    overflow: hidden;
  }

  /* Tab bar */
  #tabbar {
    display: flex; align-items: center;
    background: var(--header-bg); border-bottom: 1px solid var(--border);
    height: 32px; flex-shrink: 0;
    padding: 0 8px; gap: 0;
  }
  .tab {
    display: flex; align-items: center; gap: 6px;
    padding: 4px 14px; font-size: 12px; color: var(--text-dim);
    cursor: pointer; border-bottom: 2px solid transparent;
    height: 100%; user-select: none;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--text); border-bottom-color: var(--accent); }
  .tab .close { font-size: 10px; color: var(--text-dim); margin-left: 4px; }
  .tab .close:hover { color: var(--text); }

  /* Editor pane */
  #editor-pane { flex: 1; display: flex; flex-direction: column; min-height: 100px; position: relative; }
  #editor-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 2px 12px; background: var(--header-bg);
    border-bottom: 1px solid var(--border); font-size: 12px; color: var(--text-muted);
    flex-shrink: 0; height: 28px;
  }
  #editor-header .filename { font-weight: 600; color: var(--text); }
  #editor-header .unsaved { color: var(--unsaved); font-size: 11px; margin-left: 8px; }
  #editor-header .editor-actions { display: flex; gap: 4px; }
  #editor-header .editor-actions button {
    background: none; border: none; color: var(--text-dim);
    cursor: pointer; font-size: 12px; padding: 2px 6px; border-radius: var(--radius-sm);
  }
  #editor-header .editor-actions button:hover { color: var(--text); background: var(--hover-bg); }
  #editor-container { flex: 1; position: relative; overflow: hidden; }
  #editor-container textarea {
    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    background: var(--bg); color: var(--text); border: none;
    padding: 16px 20px;
    font-family: var(--font-mono); font-size: 13px; line-height: 1.6;
    tab-size: 4; resize: none; outline: none;
    overflow-y: auto;
  }
  #editor-container .line-numbers {
    position: absolute; top: 0; left: 0; width: 48px; height: 100%;
    background: var(--sidebar-bg); border-right: 1px solid var(--border);
    padding: 16px 0; overflow: hidden;
    font-family: var(--font-mono); font-size: 13px; line-height: 1.6;
    text-align: right; color: var(--text-dim);
    user-select: none;
  }
  #editor-container .line-numbers span {
    display: block; padding: 0 8px;
  }

  /* Preview pane */
  #preview-pane {
    display: none; flex: 1; flex-direction: column;
    background: var(--bg); border-top: 1px solid var(--border);
    min-height: 100px;
  }
  #preview-pane.active { display: flex; }
  #preview-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 2px 12px; background: var(--header-bg);
    border-bottom: 1px solid var(--border); font-size: 12px; color: var(--text-muted);
    flex-shrink: 0; height: 28px;
  }
  #preview-frame {
    flex: 1; border: none; background: #fff;
  }

  /* Console pane */
  #console-pane {
    height: 150px; min-height: 60px; max-height: 400px;
    background: #1a1a1a; border-top: 1px solid var(--border);
    display: flex; flex-direction: column; flex-shrink: 0;
  }
  #console-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 2px 12px; background: var(--header-bg);
    border-bottom: 1px solid var(--border); font-size: 12px; color: var(--text-muted);
    flex-shrink: 0; height: 28px;
  }
  #console-header .console-title { display: flex; align-items: center; gap: 6px; }
  #console-header .console-actions { display: flex; gap: 4px; }
  #console-header button {
    background: none; border: none; color: var(--text-dim);
    cursor: pointer; font-size: 11px; padding: 2px 8px; border-radius: var(--radius-sm);
  }
  #console-header button:hover { color: var(--text); background: var(--hover-bg); }
  #console-header button.primary { background: var(--accent); color: #fff; }
  #console-header button.primary:hover { background: var(--accent-hover); }
  #console-output {
    flex: 1; overflow-y: auto; padding: 8px 12px;
    font-family: var(--font-mono); font-size: 12px; line-height: 1.5;
    white-space: pre-wrap; word-break: break-all;
    color: #b0b0b0;
  }
  #console-output .timestamp { color: var(--text-dim); font-size: 10px; }
  #console-output .stdout { color: #b0b0b0; }
  #console-output .stderr { color: var(--error); }
  #console-output .exit-code { color: var(--text-dim); font-size: 11px; }
  #console-output .exit-code.success { color: var(--success); }
  #console-output .exit-code.fail { color: var(--error); }

  /* ── Resize handle (console) ── */
  #console-resize {
    height: 4px; cursor: row-resize; background: transparent;
    flex-shrink: 0; position: relative; z-index: 10;
  }
  #console-resize:hover, #console-resize.active { background: var(--accent); }

  /* ── Chat pane ── */
  #chat-pane {
    width: 320px; min-width: 240px; max-width: 600px;
    background: var(--sidebar-bg); border-left: 1px solid var(--border);
    display: flex; flex-direction: column; flex-shrink: 0;
  }
  #chat-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 12px; font-size: 11px; font-weight: 600;
    color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border);
  }
  #chat-header .actions { display: flex; gap: 4px; }
  #chat-header .actions button {
    background: none; border: none; color: var(--text-dim);
    cursor: pointer; font-size: 14px; padding: 2px; line-height: 1;
    border-radius: var(--radius-sm);
  }
  #chat-header .actions button:hover { color: var(--text); background: var(--hover-bg); }
  #chat-messages {
    flex: 1; overflow-y: auto; padding: 12px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .chat-msg { display: flex; flex-direction: column; gap: 4px; }
  .chat-msg .role { font-size: 10px; font-weight: 600; color: var(--text-dim); text-transform: uppercase; }
  .chat-msg .content { font-size: 13px; line-height: 1.5; color: var(--text); white-space: pre-wrap; }
  .chat-msg .content code {
    background: var(--header-bg); padding: 1px 4px; border-radius: var(--radius-sm);
    font-family: var(--font-mono); font-size: 12px;
  }
  .chat-msg .content pre {
    background: var(--bg); padding: 8px 12px; border-radius: var(--radius);
    overflow-x: auto; margin: 4px 0;
  }
  .chat-msg .actions { display: flex; gap: 6px; margin-top: 4px; }
  .chat-msg .actions button {
    background: var(--header-bg); border: 1px solid var(--border);
    color: var(--text-dim); padding: 2px 8px; border-radius: var(--radius-sm);
    cursor: pointer; font-size: 11px;
  }
  .chat-msg .actions button:hover { background: var(--hover-bg); color: var(--text); }
  #chat-input-area {
    display: flex; gap: 8px; padding: 8px 12px;
    border-top: 1px solid var(--border);
  }
  #chat-input {
    flex: 1; background: var(--bg); border: 1px solid var(--border);
    color: var(--text); padding: 6px 10px; border-radius: var(--radius);
    font-family: var(--font-ui); font-size: 13px; resize: none;
    outline: none; min-height: 32px; max-height: 80px;
  }
  #chat-input:focus { border-color: var(--accent); }
  #chat-send {
    background: var(--accent); border: none; color: #fff;
    padding: 6px 14px; border-radius: var(--radius);
    cursor: pointer; font-size: 13px; align-self: flex-end;
  }
  #chat-send:hover { background: var(--accent-hover); }
  #chat-send:disabled { opacity: 0.5; cursor: default; }

  /* ── Resize handle (chat) ── */
  #chat-resize {
    width: 4px; cursor: col-resize; background: transparent;
    flex-shrink: 0; position: relative; z-index: 10;
  }
  #chat-resize:hover, #chat-resize.active { background: var(--accent); }

  /* ═══════════════════════════════════════════════════════════════════════
     Status Bar
     ═══════════════════════════════════════════════════════════════════════ */
  #statusbar {
    display: flex; align-items: center;
    height: var(--statusbar-height); padding: 0 12px;
    background: var(--accent); color: #fff;
    font-size: 11px; flex-shrink: 0;
    gap: 16px; user-select: none;
  }
  #statusbar .left { display: flex; gap: 12px; align-items: center; }
  #statusbar .right { margin-left: auto; display: flex; gap: 12px; align-items: center; }
  #statusbar .segment { display: flex; align-items: center; gap: 4px; }
  #statusbar .dirty { color: rgba(255,255,255,0.8); }

  /* ═══════════════════════════════════════════════════════════════════════
     Modal / Overlay
     ═══════════════════════════════════════════════════════════════════════ */
  .modal-overlay {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.6); z-index: 2000;
    display: none; align-items: center; justify-content: center;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--sidebar-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 24px; min-width: 400px;
    max-width: 600px; box-shadow: 0 16px 48px rgba(0,0,0,0.5);
  }
  .modal h2 { font-size: 16px; font-weight: 600; margin-bottom: 16px; }
  .modal label { display: block; font-size: 12px; color: var(--text-dim); margin-bottom: 4px; }
  .modal input, .modal select, .modal textarea {
    width: 100%; background: var(--bg); border: 1px solid var(--border);
    color: var(--text); padding: 6px 10px; border-radius: var(--radius);
    font-size: 13px; margin-bottom: 12px;
  }
  .modal input:focus, .modal select:focus, .modal textarea:focus { border-color: var(--accent); outline: none; }
  .modal .buttons { display: flex; gap: 8px; justify-content: flex-end; margin-top: 8px; }
  .modal .buttons button {
    padding: 6px 16px; border-radius: var(--radius); cursor: pointer;
    font-size: 13px; border: 1px solid var(--border); background: var(--header-bg);
    color: var(--text);
  }
  .modal .buttons button:hover { background: var(--hover-bg); }
  .modal .buttons button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .modal .buttons button.primary:hover { background: var(--accent-hover); }

  /* ═══════════════════════════════════════════════════════════════════════
     Version History Panel
     ═══════════════════════════════════════════════════════════════════════ */
  #version-panel {
    position: fixed; top: var(--toolbar-height); right: 0; bottom: var(--statusbar-height);
    width: 360px; background: var(--sidebar-bg); border-left: 1px solid var(--border);
    z-index: 500; display: none; flex-direction: column;
    box-shadow: -4px 0 16px rgba(0,0,0,0.3);
  }
  #version-panel.open { display: flex; }
  #version-panel-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px; border-bottom: 1px solid var(--border);
  }
  #version-panel-header h3 { font-size: 14px; font-weight: 600; }
  #version-panel-header button {
    background: none; border: none; color: var(--text-dim);
    cursor: pointer; font-size: 18px; padding: 2px;
  }
  #version-panel-header button:hover { color: var(--text); }
  #version-list { flex: 1; overflow-y: auto; padding: 8px; }
  .version-item {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 12px; border-radius: var(--radius); cursor: pointer;
    margin-bottom: 4px;
  }
  .version-item:hover { background: var(--hover-bg); }
  .version-item.active { background: var(--active-bg); }
  .version-item .info { display: flex; flex-direction: column; gap: 2px; }
  .version-item .time { font-size: 11px; color: var(--text-dim); }
  .version-item .label { font-size: 12px; color: var(--text); }
  .version-item .actions { display: flex; gap: 4px; }
  .version-item .actions button {
    background: none; border: none; color: var(--text-dim);
    cursor: pointer; font-size: 12px; padding: 2px 4px;
  }
  .version-item .actions button:hover { color: var(--text); }

  /* ═══════════════════════════════════════════════════════════════════════
     Settings Panel
     ═══════════════════════════════════════════════════════════════════════ */
  #settings-panel {
    position: fixed; top: var(--toolbar-height); right: 0; bottom: var(--statusbar-height);
    width: 360px; background: var(--sidebar-bg); border-left: 1px solid var(--border);
    z-index: 500; display: none; flex-direction: column;
    box-shadow: -4px 0 16px rgba(0,0,0,0.3);
  }
  #settings-panel.open { display: flex; }
  #settings-panel-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px; border-bottom: 1px solid var(--border);
  }
  #settings-panel-header h3 { font-size: 14px; font-weight: 600; }
  #settings-panel-header button {
    background: none; border: none; color: var(--text-dim);
    cursor: pointer; font-size: 18px; padding: 2px;
  }
  #settings-panel-header button:hover { color: var(--text); }
  #settings-body { flex: 1; overflow-y: auto; padding: 12px; }
  .setting-group { margin-bottom: 16px; }
  .setting-group h4 { font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
  .setting-row { display: flex; align-items: center; justify-content: space-between; padding: 6px 0; }
  .setting-row label { font-size: 13px; color: var(--text); }
  .setting-row input[type="number"] { width: 60px; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 2px 6px; border-radius: var(--radius-sm); font-size: 12px; text-align: center; }
  .setting-row select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 2px 6px; border-radius: var(--radius-sm); font-size: 12px; }
  .setting-row input[type="checkbox"] { accent-color: var(--accent); }

  /* ═══════════════════════════════════════════════════════════════════════
     Scrollbar
     ═══════════════════════════════════════════════════════════════════════ */
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: var(--sidebar-bg); }
  ::-webkit-scrollbar-thumb { background: #424242; border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: #555; }

  /* ═══════════════════════════════════════════════════════════════════════
     Toast notifications
     ═══════════════════════════════════════════════════════════════════════ */
  #toast-container {
    position: fixed; bottom: calc(var(--statusbar-height) + 16px); right: 16px;
    z-index: 3000; display: flex; flex-direction: column; gap: 8px;
  }
  .toast {
    padding: 10px 16px; border-radius: var(--radius);
    font-size: 13px; color: #fff;
    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    animation: toast-in 0.2s ease-out;
    max-width: 360px;
  }
  .toast.info { background: var(--accent); }
  .toast.success { background: var(--success); }
  .toast.error { background: var(--error); }
  .toast.warning { background: var(--warning); color: #333; }
  @keyframes toast-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

  /* ═══════════════════════════════════════════════════════════════════════
     Diff view
     ═══════════════════════════════════════════════════════════════════════ */
  #diff-view {
    display: none; flex: 1; flex-direction: column;
    background: var(--bg); overflow-y: auto;
  }
  #diff-view.active { display: flex; }
  #diff-view pre {
    padding: 16px 20px; font-family: var(--font-mono); font-size: 13px; line-height: 1.6;
    white-space: pre-wrap; word-break: break-all;
  }
  #diff-view .diff-add { background: rgba(78,201,78,0.15); color: #4ec94e; }
  #diff-view .diff-remove { background: rgba(241,76,76,0.15); color: #f14c4c; }
  #diff-view .diff-header { color: var(--text-dim); font-weight: 600; }
</style>
</head>
<body>

<!-- ═════════════════════════════════════════════════════════════════════════
     Top Toolbar
     ═════════════════════════════════════════════════════════════════════════ -->
<div id="topbar">
  <div class="brand">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/><line x1="3" y1="9" x2="21" y2="9"/></svg>
    Canvas
  </div>

  <div class="separator"></div>

  <!-- File operations -->
  <div class="tb-group">
    <button class="tb-btn" onclick="newFile()" title="New File (Ctrl+N)"><span class="icon">📄</span><span class="label">New</span></button>
    <button class="tb-btn" onclick="saveFile()" id="save-btn" disabled title="Save (Ctrl+S)"><span class="icon">💾</span><span class="label">Save</span></button>
    <button class="tb-btn" onclick="newFolder()" title="New Folder"><span class="icon">📁</span><span class="label">Folder</span></button>
  </div>

  <div class="separator"></div>

  <!-- Edit operations -->
  <div class="tb-group">
    <button class="tb-btn" onclick="undoEdit()" id="undo-btn" disabled title="Undo (Ctrl+Z)"><span class="icon">⏪</span></button>
    <button class="tb-btn" onclick="redoEdit()" id="redo-btn" disabled title="Redo (Ctrl+Shift+Z)"><span class="icon">⏩</span></button>
  </div>

  <div class="separator"></div>

  <!-- Run & Preview -->
  <div class="tb-group">
    <button class="tb-btn primary" onclick="runFile()" id="run-btn" title="Run (Ctrl+Enter)"><span class="icon">▶</span><span class="label">Run</span></button>
    <button class="tb-btn" onclick="togglePreview()" id="preview-btn" title="Toggle Preview"><span class="icon">👁️</span><span class="label">Preview</span></button>
  </div>

  <div class="separator"></div>

  <!-- Writing shortcuts dropdown -->
  <div class="tb-dropdown">
    <button class="tb-btn" onclick="toggleDropdown('writing-dropdown')" title="Writing Shortcuts"><span class="icon">✍️</span><span class="label">Writing</span></button>
    <div class="tb-dropdown-menu" id="writing-dropdown">
      <div class="tb-dropdown-item" onclick="doAction('suggest_edits')"><span class="icon">✍️</span> Suggest edits</div>
      <div class="tb-dropdown-item" onclick="doAction('adjust_length')"><span class="icon">📏</span> Adjust length</div>
      <div class="tb-dropdown-item" onclick="doAction('change_reading_level')"><span class="icon">📖</span> Change reading level</div>
      <div class="tb-dropdown-item" onclick="doAction('final_polish')"><span class="icon">✨</span> Final polish</div>
      <div class="tb-dropdown-item" onclick="doAction('add_emojis')"><span class="icon">😊</span> Add emojis</div>
    </div>
  </div>

  <!-- Coding shortcuts dropdown -->
  <div class="tb-dropdown">
    <button class="tb-btn" onclick="toggleDropdown('coding-dropdown')" title="Coding Shortcuts"><span class="icon">💻</span><span class="label">Code</span></button>
    <div class="tb-dropdown-menu" id="coding-dropdown">
      <div class="tb-dropdown-item" onclick="doAction('review_code')"><span class="icon">🔍</span> Review code</div>
      <div class="tb-dropdown-item" onclick="doAction('add_logs')"><span class="icon">📋</span> Add logs</div>
      <div class="tb-dropdown-item" onclick="doAction('add_comments')"><span class="icon">💬</span> Add comments</div>
      <div class="tb-dropdown-item" onclick="doAction('fix_bugs')"><span class="icon">🐛</span> Fix bugs</div>
      <div class="tb-dropdown-item" onclick="doAction('port_code')"><span class="icon">🔄</span> Port to...</div>
    </div>
  </div>

  <div class="separator"></div>

  <!-- History & Export -->
  <div class="tb-group">
    <button class="tb-btn" onclick="toggleVersionPanel()" title="Version History"><span class="icon">🕓</span><span class="label">History</span></button>
    <div class="tb-dropdown">
      <button class="tb-btn" onclick="toggleDropdown('export-dropdown')" title="Export"><span class="icon">📤</span><span class="label">Export</span></button>
      <div class="tb-dropdown-menu" id="export-dropdown">
        <div class="tb-dropdown-item" onclick="exportFile('pdf')"><span class="icon">📄</span> PDF</div>
        <div class="tb-dropdown-item" onclick="exportFile('markdown')"><span class="icon">📝</span> Markdown</div>
        <div class="tb-dropdown-item" onclick="exportFile('docx')"><span class="icon">📘</span> Word (DOCX)</div>
        <div class="tb-dropdown-item" onclick="exportFile('txt')"><span class="icon">📋</span> Plain Text</div>
      </div>
    </div>
    <button class="tb-btn" onclick="toggleSettingsPanel()" title="Settings"><span class="icon">⚙</span></button>
  </div>
</div>

<!-- ═════════════════════════════════════════════════════════════════════════
     Main layout
     ═════════════════════════════════════════════════════════════════════════ -->
<div id="main">
  <!-- Sidebar -->
  <div id="sidebar">
    <div id="sidebar-header">
      <span>Explorer</span>
      <div class="actions">
        <button onclick="refreshFileTree()" title="Refresh">⟳</button>
        <button onclick="collapseAll()" title="Collapse All">−</button>
      </div>
    </div>
    <div id="file-tree"></div>
  </div>
  <div id="sidebar-resize"></div>

  <!-- Center: editor + tabs + preview + console -->
  <div id="center">
    <!-- Tab bar -->
    <div id="tabbar">
      <div class="tab active" data-tab="edit" onclick="switchTab('edit')">Edit</div>
      <div class="tab" data-tab="preview" onclick="switchTab('preview')">Preview</div>
      <div class="tab" data-tab="diff" onclick="switchTab('diff')" id="diff-tab" style="display:none">Diff</div>
    </div>

    <!-- Editor pane -->
    <div id="editor-pane">
      <div id="editor-header">
        <span>
          <span class="filename" id="current-filename">(no file open)</span>
          <span class="unsaved" id="unsaved-indicator"></span>
        </span>
        <div class="editor-actions">
          <button onclick="saveVersion()" title="Save snapshot">📸</button>
        </div>
      </div>
      <div id="editor-container">
        <div class="line-numbers" id="line-numbers"></div>
        <textarea id="editor" spellcheck="false" placeholder="Select a file from the explorer to open it..."></textarea>
      </div>
    </div>

    <!-- Preview pane -->
    <div id="preview-pane">
      <div id="preview-header">
        <span>👁️ Preview</span>
        <button onclick="togglePreview()" style="background:none;border:none;color:var(--text-dim);cursor:pointer">✕</button>
      </div>
      <iframe id="preview-frame" sandbox="allow-scripts allow-same-origin"></iframe>
    </div>

    <!-- Diff view -->
    <div id="diff-view">
      <pre id="diff-content"></pre>
    </div>

    <!-- Console resize handle -->
    <div id="console-resize"></div>

    <!-- Console pane -->
    <div id="console-pane">
      <div id="console-header">
        <div class="console-title">
          <span>⚙ Console</span>
        </div>
        <div class="console-actions">
          <button onclick="clearConsole()">🗑 Clear</button>
          <button class="primary" onclick="runFile()" id="run-btn2">▶ Run</button>
        </div>
      </div>
      <div id="console-output"></div>
    </div>
  </div>

  <!-- Chat resize handle -->
  <div id="chat-resize"></div>

  <!-- Chat pane -->
  <div id="chat-pane">
    <div id="chat-header">
      <span>💬 Chat</span>
      <div class="actions">
        <button onclick="clearChat()" title="Clear chat">🗑</button>
      </div>
    </div>
    <div id="chat-messages">
      <div class="chat-msg">
        <div class="role">System</div>
        <div class="content">Welcome to Canvas. Select a file to start editing, or ask the AI for help.</div>
      </div>
    </div>
    <div id="chat-input-area">
      <textarea id="chat-input" placeholder="Ask the AI or type a command..." rows="1"></textarea>
      <button id="chat-send" onclick="sendChat()">Send</button>
    </div>
  </div>
</div>

<!-- ═════════════════════════════════════════════════════════════════════════
     Status Bar
     ═════════════════════════════════════════════════════════════════════════ -->
<div id="statusbar">
  <div class="left">
    <span class="segment" id="status-filename">(no file)</span>
    <span class="segment dirty" id="status-dirty"></span>
  </div>
  <div class="right">
    <span class="segment" id="status-cursor">Ln 1, Col 1</span>
    <span class="segment" id="status-encoding">UTF-8</span>
    <span class="segment" id="status-language">Plain Text</span>
    <span class="segment" id="status-indent">Spaces: 4</span>
  </div>
</div>

<!-- ═════════════════════════════════════════════════════════════════════════
     Version History Panel
     ═════════════════════════════════════════════════════════════════════════ -->
<div id="version-panel">
  <div id="version-panel-header">
    <h3>🕓 Version History</h3>
    <button onclick="toggleVersionPanel()">✕</button>
  </div>
  <div id="version-list"></div>
</div>

<!-- ═════════════════════════════════════════════════════════════════════════
     Settings Panel
     ═════════════════════════════════════════════════════════════════════════ -->
<div id="settings-panel">
  <div id="settings-panel-header">
    <h3>⚙ Settings</h3>
    <button onclick="toggleSettingsPanel()">✕</button>
  </div>
  <div id="settings-body">
    <div class="setting-group">
      <h4>Editor</h4>
      <div class="setting-row"><label>Font Size</label><input type="number" id="setting-font-size" value="13" min="10" max="24" onchange="applySettings()"></div>
      <div class="setting-row"><label>Tab Size</label><input type="number" id="setting-tab-size" value="4" min="1" max="8" onchange="applySettings()"></div>
      <div class="setting-row"><label>Line Numbers</label><input type="checkbox" id="setting-line-numbers" checked onchange="applySettings()"></div>
      <div class="setting-row"><label>Word Wrap</label><input type="checkbox" id="setting-word-wrap" onchange="applySettings()"></div>
      <div class="setting-row"><label>Theme</label>
        <select id="setting-theme" onchange="applySettings()">
          <option value="dark">Dark</option>
          <option value="light">Light</option>
        </select>
      </div>
    </div>
    <div class="setting-group">
      <h4>Preview</h4>
      <div class="setting-row"><label>Auto-preview</label><input type="checkbox" id="setting-auto-preview" checked onchange="applySettings()"></div>
    </div>
    <div class="setting-group">
      <h4>Run</h4>
      <div class="setting-row"><label>Timeout (s)</label><input type="number" id="setting-timeout" value="30" min="5" max="120" onchange="applySettings()"></div>
      <div class="setting-row"><label>Auto-save before run</label><input type="checkbox" id="setting-auto-save" checked onchange="applySettings()"></div>
    </div>
  </div>
</div>

<!-- ═════════════════════════════════════════════════════════════════════════
     Toast container
     ═════════════════════════════════════════════════════════════════════════ -->
<div id="toast-container"></div>

<!-- ═════════════════════════════════════════════════════════════════════════
     Modal overlay (for rename, new file, etc.)
     ═════════════════════════════════════════════════════════════════════════ -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal" id="modal-content">
    <h2 id="modal-title">New File</h2>
    <div id="modal-body"></div>
    <div class="buttons">
      <button onclick="closeModal()">Cancel</button>
      <button class="primary" id="modal-confirm" onclick="modalConfirm()">Confirm</button>
    </div>
  </div>
</div>

<script>
// ═════════════════════════════════════════════════════════════════════════════
// State
// ═════════════════════════════════════════════════════════════════════════════
let state = {
  currentFile: null,         // relative path of the currently open file
  fileContent: "",           // last saved content of the current file
  editorDirty: false,        // true if editor differs from fileContent
  fileTree: [],              // cached tree from /api/files
  undoStack: [],             // undo history
  redoStack: [],             // redo history
  previewActive: false,      // is preview pane visible
  chatMessages: [],          // chat message history
  versions: [],              // version history for current file
  settings: {
    fontSize: 13,
    tabSize: 4,
    lineNumbers: true,
    wordWrap: false,
    theme: 'dark',
    autoPreview: true,
    timeout: 30,
    autoSave: true,
  },
  modalCallback: null,       // callback for modal confirm
  contextMenuTarget: null,   // target for context menu
};

// ═════════════════════════════════════════════════════════════════════════════
// Utility
// ═════════════════════════════════════════════════════════════════════════════
function htmlEscape(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function showToast(message, type = 'info', duration = 3000) {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => { toast.remove(); }, duration);
}

function closeAllDropdowns() {
  document.querySelectorAll('.tb-dropdown-menu.open').forEach(m => m.classList.remove('open'));
}

function toggleDropdown(id) {
  closeAllDropdowns();
  const menu = document.getElementById(id);
  if (menu) menu.classList.toggle('open');
}

// Close dropdowns on click outside
document.addEventListener('click', (e) => {
  if (!e.target.closest('.tb-dropdown')) closeAllDropdowns();
});

// ═════════════════════════════════════════════════════════════════════════════
// Modal
// ═════════════════════════════════════════════════════════════════════════════
function openModal(title, bodyHtml, onConfirm) {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML = bodyHtml;
  document.getElementById('modal-overlay').classList.add('open');
  state.modalCallback = onConfirm;
  // Focus first input
  setTimeout(() => {
    const input = document.querySelector('#modal-body input, #modal-body textarea');
    if (input) input.focus();
  }, 100);
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  state.modalCallback = null;
}

function modalConfirm() {
  if (state.modalCallback) state.modalCallback();
  closeModal();
}

// Close modal on overlay click
document.getElementById('modal-overlay').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeModal();
});

// Enter key in modal input confirms
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && document.getElementById('modal-overlay').classList.contains('open')) {
    const active = document.activeElement;
    if (active && active.closest('#modal-body')) {
      e.preventDefault();
      modalConfirm();
    }
  }
});

// ═════════════════════════════════════════════════════════════════════════════
// File tree
// ═════════════════════════════════════════════════════════════════════════════
async function refreshFileTree() {
  try {
    const resp = await fetch('/api/files');
    const data = await resp.json();
    state.fileTree = data.files || [];
    renderTree(state.fileTree, document.getElementById('file-tree'));
  } catch (e) {
    document.getElementById('file-tree').innerHTML = "<div style='color:var(--error);padding:8px;'>Error loading files</div>";
  }
}

function renderTree(entries, container, depth) {
  if (depth === undefined) depth = 0;
  container.innerHTML = '';
  for (const entry of entries) {
    const div = document.createElement('div');
    div.className = 'tree-item';
    div.style.paddingLeft = (12 + depth * 16) + 'px';

    if (entry.type === 'directory') {
      div.classList.add('directory');
      div.innerHTML = `<span class="icon">📁</span> ${htmlEscape(entry.name)}`;
      div.onclick = (e) => { e.stopPropagation(); toggleDir(div, entry); };
      div.oncontextmenu = (e) => { e.preventDefault(); showContextMenu(e, entry); };
      container.appendChild(div);
      const childContainer = document.createElement('div');
      childContainer.className = 'children';
      childContainer.style.display = 'none';
      if (entry.children && entry.children.length > 0) {
        renderTree(entry.children, childContainer, depth + 1);
      }
      container.appendChild(childContainer);
    } else {
      const isActive = entry.path === state.currentFile;
      div.classList.toggle('active', isActive);
      div.innerHTML = `<span class="icon">📄</span> ${htmlEscape(entry.name)}`;
      if (isActive && state.editorDirty) {
        div.innerHTML += '<span class="dirty-dot">●</span>';
      }
      div.onclick = () => openFile(entry.path);
      div.oncontextmenu = (e) => { e.preventDefault(); showContextMenu(e, entry); };
      div.dataset.path = entry.path;
      container.appendChild(div);
    }
  }
}

function toggleDir(dirDiv, entry) {
  const children = dirDiv.nextElementSibling;
  if (!children) return;
  const isExpanded = children.style.display !== 'none';
  children.style.display = isExpanded ? 'none' : 'block';
  dirDiv.classList.toggle('expanded');
  dirDiv.querySelector('.icon').textContent = isExpanded ? '📁' : '📂';
}

function collapseAll() {
  document.querySelectorAll('#file-tree .children').forEach(el => el.style.display = 'none');
  document.querySelectorAll('#file-tree .tree-item.directory').forEach(el => el.classList.remove('expanded'));
}

function updateActiveFile(path) {
  for (const item of document.querySelectorAll('#file-tree .tree-item[data-path]')) {
    item.classList.toggle('active', item.dataset.path === path);
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// Context menu
// ═════════════════════════════════════════════════════════════════════════════
function showContextMenu(e, entry) {
  e.preventDefault();
  closeAllDropdowns();
  // Create a temporary context menu
  const existing = document.querySelector('.context-menu');
  if (existing) existing.remove();

  const menu = document.createElement('div');
  menu.className = 'tb-dropdown-menu open context-menu';
  menu.style.position = 'fixed';
  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  menu.style.zIndex = '2000';

  const items = [];
  if (entry.type === 'directory') {
    items.push({ icon: '📄', label: 'New File', action: () => newFileInDir(entry.path) });
    items.push({ icon: '📁', label: 'New Folder', action: () => newFolderInDir(entry.path) });
    items.push({ icon: '✏️', label: 'Rename', action: () => renameItem(entry) });
    items.push({ icon: '🗑', label: 'Delete', action: () => deleteItem(entry) });
  } else {
    items.push({ icon: '📄', label: 'Open', action: () => openFile(entry.path) });
    items.push({ icon: '✏️', label: 'Rename', action: () => renameItem(entry) });
    items.push({ icon: '🗑', label: 'Delete', action: () => deleteItem(entry) });
    items.push({ icon: '📋', label: 'Copy Path', action: () => copyPath(entry.path) });
  }

  for (const item of items) {
    const div = document.createElement('div');
    div.className = 'tb-dropdown-item';
    div.innerHTML = `<span class="icon">${item.icon}</span> ${item.label}`;
    div.onclick = () => { menu.remove(); item.action(); };
    menu.appendChild(div);
  }

  document.body.appendChild(menu);

  // Close on click outside
  const close = (ev) => {
    if (!ev.target.closest('.context-menu')) {
      menu.remove();
      document.removeEventListener('click', close);
    }
  };
  setTimeout(() => document.addEventListener('click', close), 0);
}

// ═════════════════════════════════════════════════════════════════════════════
// File operations
// ═════════════════════════════════════════════════════════════════════════════
async function openFile(path) {
  // Save if dirty
  if (state.editorDirty) {
    if (!confirm('Save changes to ' + state.currentFile + '?')) return;
    await saveFile();
  }

  try {
    const resp = await fetch('/api/files/' + encodeURIComponent(path));
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast('Error: ' + (err.error || resp.statusText), 'error');
      return;
    }
    const text = await resp.text();
    state.currentFile = path;
    state.fileContent = text;
    state.editorDirty = false;
    state.undoStack = [];
    state.redoStack = [];
    document.getElementById('editor').value = text;
    document.getElementById('current-filename').textContent = path;
    document.getElementById('unsaved-indicator').textContent = '';
    document.getElementById('save-btn').disabled = true;
    document.getElementById('undo-btn').disabled = true;
    document.getElementById('redo-btn').disabled = true;
    updateActiveFile(path);
    updateStatusBar();
    updateLineNumbers();
    loadVersions(path);

    // Auto-preview if enabled
    if (state.settings.autoPreview) {
      updatePreview();
    }
  } catch (e) {
    showToast('Failed to open file: ' + e.message, 'error');
  }
}

async function saveFile() {
  if (!state.currentFile) return;
  const content = document.getElementById('editor').value;
  try {
    const resp = await fetch('/api/files/' + encodeURIComponent(state.currentFile), {
      method: 'PUT',
      body: content,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast('Save error: ' + (err.error || resp.statusText), 'error');
      return;
    }
    state.fileContent = content;
    state.editorDirty = false;
    document.getElementById('unsaved-indicator').textContent = '';
    document.getElementById('save-btn').disabled = true;
    updateStatusBar();
    updateActiveFile(state.currentFile);
    showToast('Saved ' + state.currentFile, 'success', 1500);
  } catch (e) {
    showToast('Failed to save: ' + e.message, 'error');
  }
}

async function newFile() {
  openModal('New File', `
    <label>Filename</label>
    <input type="text" id="modal-input" placeholder="e.g. script.py">
    <label>Directory (optional, leave empty for root)</label>
    <input type="text" id="modal-dir" placeholder="e.g. src/utils">
  `, async () => {
    const name = document.getElementById('modal-input').value.trim();
    const dir = document.getElementById('modal-dir').value.trim();
    if (!name) { showToast('Filename is required', 'error'); return; }
    const path = dir ? dir + '/' + name : name;
    try {
      const resp = await fetch('/api/files/' + encodeURIComponent(path), { method: 'POST', body: '' });
      if (!resp.ok) throw new Error((await resp.json()).error || 'Failed');
      showToast('Created ' + path, 'success');
      await refreshFileTree();
      await openFile(path);
    } catch (e) {
      showToast('Error: ' + e.message, 'error');
    }
  });
}

function newFileInDir(dirPath) {
  openModal('New File in ' + dirPath, `
    <label>Filename</label>
    <input type="text" id="modal-input" placeholder="e.g. script.py">
  `, async () => {
    const name = document.getElementById('modal-input').value.trim();
    if (!name) { showToast('Filename is required', 'error'); return; }
    const path = dirPath + '/' + name;
    try {
      const resp = await fetch('/api/files/' + encodeURIComponent(path), { method: 'POST', body: '' });
      if (!resp.ok) throw new Error((await resp.json()).error || 'Failed');
      showToast('Created ' + path, 'success');
      await refreshFileTree();
      await openFile(path);
    } catch (e) {
      showToast('Error: ' + e.message, 'error');
    }
  });
}

function newFolder() {
  openModal('New Folder', `
    <label>Folder name</label>
    <input type="text" id="modal-input" placeholder="e.g. src">
    <label>Parent directory (optional)</label>
    <input type="text" id="modal-dir" placeholder="e.g. src/utils">
  `, async () => {
    const name = document.getElementById('modal-input').value.trim();
    const dir = document.getElementById('modal-dir').value.trim();
    if (!name) { showToast('Folder name is required', 'error'); return; }
    const path = dir ? dir + '/' + name : name;
    try {
      const resp = await fetch('/api/files/' + encodeURIComponent(path), { method: 'POST', body: '' });
      if (!resp.ok) throw new Error((await resp.json()).error || 'Failed');
      showToast('Created folder ' + path, 'success');
      await refreshFileTree();
    } catch (e) {
      showToast('Error: ' + e.message, 'error');
    }
  });
}

function newFolderInDir(dirPath) {
  openModal('New Folder in ' + dirPath, `
    <label>Folder name</label>
    <input type="text" id="modal-input" placeholder="e.g. utils">
  `, async () => {
    const name = document.getElementById('modal-input').value.trim();
    if (!name) { showToast('Folder name is required', 'error'); return; }
    const path = dirPath + '/' + name;
    try {
      const resp = await fetch('/api/files/' + encodeURIComponent(path), { method: 'POST', body: '' });
      if (!resp.ok) throw new Error((await resp.json()).error || 'Failed');
      showToast('Created folder ' + path, 'success');
      await refreshFileTree();
    } catch (e) {
      showToast('Error: ' + e.message, 'error');
    }
  });
}

function renameItem(entry) {
  openModal('Rename ' + entry.name, `
    <label>New name</label>
    <input type="text" id="modal-input" value="${htmlEscape(entry.name)}">
  `, async () => {
    const newName = document.getElementById('modal-input').value.trim();
    if (!newName) { showToast('Name is required', 'error'); return; }
    try {
      const resp = await fetch('/api/files/' + encodeURIComponent(entry.path) + '/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newName }),
      });
      if (!resp.ok) throw new Error((await resp.json()).error || 'Failed');
      showToast('Renamed to ' + newName, 'success');
      await refreshFileTree();
      if (state.currentFile === entry.path) {
        const parent = entry.path.includes('/') ? entry.path.split('/').slice(0, -1).join('/') : '';
        state.currentFile = parent ? parent + '/' + newName : newName;
      }
    } catch (e) {
      showToast('Error: ' + e.message, 'error');
    }
  });
}

async function deleteItem(entry) {
  if (!confirm('Delete ' + entry.path + '?')) return;
  try {
    const resp = await fetch('/api/files/' + encodeURIComponent(entry.path), { method: 'DELETE' });
    if (!resp.ok) throw new Error((await resp.json()).error || 'Failed');
    showToast('Deleted ' + entry.path, 'success');
    if (state.currentFile === entry.path) {
      state.currentFile = null;
      document.getElementById('editor').value = '';
      document.getElementById('current-filename').textContent = '(no file open)';
      document.getElementById('unsaved-indicator').textContent = '';
      document.getElementById('save-btn').disabled = true;
      updateStatusBar();
    }
    await refreshFileTree();
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

function copyPath(path) {
  navigator.clipboard.writeText(path).then(() => {
    showToast('Copied: ' + path, 'success', 1500);
  }).catch(() => {
    showToast('Failed to copy', 'error');
  });
}

// ═════════════════════════════════════════════════════════════════════════════
// Editor
// ═════════════════════════════════════════════════════════════════════════════
function updateLineNumbers() {
  const container = document.getElementById('line-numbers');
  const editor = document.getElementById('editor');
  if (!state.settings.lineNumbers) {
    container.style.display = 'none';
    editor.style.paddingLeft = '20px';
    return;
  }
  container.style.display = 'block';
  editor.style.paddingLeft = '64px';
  const lines = editor.value.split('\n').length;
  let html = '';
  for (let i = 1; i <= lines; i++) {
    html += '<span>' + i + '</span>';
  }
  container.innerHTML = html;
}

function updateStatusBar() {
  const editor = document.getElementById('editor');
  const text = editor.value;
  const lines = text.split('\n');
  const cursorPos = editor.selectionStart;
  let line = 1, col = 1;
  for (let i = 0; i < cursorPos; i++) {
    if (text[i] === '\n') { line++; col = 1; }
    else { col++; }
  }

  document.getElementById('status-filename').textContent = state.currentFile || '(no file)';
  document.getElementById('status-dirty').textContent = state.editorDirty ? '● modified' : '';
  document.getElementById('status-cursor').textContent = `Ln ${line}, Col ${col}`;

  // Detect language
  if (state.currentFile) {
    const ext = state.currentFile.split('.').pop().toLowerCase();
    const langMap = {
      py: 'Python', js: 'JavaScript', ts: 'TypeScript', html: 'HTML', css: 'CSS',
      json: 'JSON', xml: 'XML', yaml: 'YAML', yml: 'YAML', md: 'Markdown',
      c: 'C', cpp: 'C++', h: 'C', java: 'Java', rs: 'Rust', go: 'Go',
      rb: 'Ruby', sh: 'Shell', php: 'PHP', sql: 'SQL', r: 'R',
    };
    document.getElementById('status-language').textContent = langMap[ext] || 'Plain Text';
  } else {
    document.getElementById('status-language').textContent = 'Plain Text';
  }
}

function pushUndo() {
  const editor = document.getElementById('editor');
  state.undoStack.push({ content: state.fileContent, cursor: editor.selectionStart });
  state.redoStack = [];
  document.getElementById('undo-btn').disabled = false;
  document.getElementById('redo-btn').disabled = true;
}

function undoEdit() {
  if (state.undoStack.length === 0) return;
  const editor = document.getElementById('editor');
  state.redoStack.push({ content: editor.value, cursor: editor.selectionStart });
  const prev = state.undoStack.pop();
  editor.value = prev.content;
  editor.selectionStart = editor.selectionEnd = prev.cursor;
  editorDirtyCheck();
  document.getElementById('undo-btn').disabled = state.undoStack.length === 0;
  document.getElementById('redo-btn').disabled = false;
}

function redoEdit() {
  if (state.redoStack.length === 0) return;
  const editor = document.getElementById('editor');
  state.undoStack.push({ content: editor.value, cursor: editor.selectionStart });
  const next = state.redoStack.pop();
  editor.value = next.content;
  editor.selectionStart = editor.selectionEnd = next.cursor;
  editorDirtyCheck();
  document.getElementById('undo-btn').disabled = false;
  document.getElementById('redo-btn').disabled = state.redoStack.length === 0;
}

function editorDirtyCheck() {
  const editor = document.getElementById('editor');
  state.editorDirty = state.currentFile ? editor.value !== state.fileContent : false;
  document.getElementById('unsaved-indicator').textContent = state.editorDirty ? '● unsaved' : '';
  document.getElementById('save-btn').disabled = !state.editorDirty || !state.currentFile;
  updateLineNumbers();
  updateStatusBar();
  updateActiveFile(state.currentFile);
}

// ═════════════════════════════════════════════════════════════════════════════
// Preview
// ═════════════════════════════════════════════════════════════════════════════
function togglePreview() {
  state.previewActive = !state.previewActive;
  document.getElementById('preview-pane').classList.toggle('active', state.previewActive);
  document.getElementById('preview-btn').classList.toggle('active', state.previewActive);
  if (state.previewActive) updatePreview();
}

async function updatePreview() {
  if (!state.currentFile || !state.previewActive) return;
  const content = document.getElementById('editor').value;
  try {
    const resp = await fetch('/api/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: state.currentFile, content: content }),
    });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.content_type === 'text/html') {
      document.getElementById('preview-frame').srcdoc = data.content;
    } else if (data.content_type === 'image') {
      document.getElementById('preview-frame').srcdoc = `<img src="data:image/png;base64,${data.content}" style="max-width:100%">`;
    } else {
      document.getElementById('preview-frame').srcdoc = `<pre>${htmlEscape(data.content)}</pre>`;
    }
  } catch (e) {
    // silent
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// Tab switching (Edit / Preview / Diff)
// ═════════════════════════════════════════════════════════════════════════════
function switchTab(tab) {
  document.querySelectorAll('#tabbar .tab').forEach(t => t.classList.remove('active'));
  document.querySelector(`#tabbar .tab[data-tab="${tab}"]`).classList.add('active');

  document.getElementById('editor-pane').style.display = tab === 'edit' ? 'flex' : 'none';
  document.getElementById('preview-pane').classList.toggle('active', tab === 'preview');
  document.getElementById('diff-view').classList.toggle('active', tab === 'diff');

  if (tab === 'preview') updatePreview();
}

// ═════════════════════════════════════════════════════════════════════════════
// Run
// ═════════════════════════════════════════════════════════════════════════════
async function runFile() {
  if (!state.currentFile) {
    appendConsole('No file open. Select a file from the explorer first.\n');
    return;
  }

  // Auto-save before run
  if (state.editorDirty && state.settings.autoSave) {
    await saveFile();
  }

  const btns = [document.getElementById('run-btn'), document.getElementById('run-btn2')];
  btns.forEach(b => { if (b) { b.disabled = true; b.innerHTML = '<span class="icon">⏳</span> Running...'; } });

  const timestamp = new Date().toLocaleTimeString();
  appendConsole(`[${timestamp}] $ python3 ${state.currentFile}\n`);

  try {
    const resp = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: state.currentFile, timeout: state.settings.timeout }),
    });
    const result = await resp.json();
    if (result.stdout) appendConsole(result.stdout, 'stdout');
    if (result.stderr) appendConsole(result.stderr, 'stderr');
    if (result.timed_out) appendConsole('⚠ Process timed out\n', 'stderr');
    const exitClass = result.returncode === 0 ? 'success' : 'fail';
    appendConsole(`→ exit code: ${result.returncode}\n\n`, exitClass);
  } catch (e) {
    appendConsole('Error: ' + e.message + '\n', 'stderr');
  } finally {
    btns.forEach(b => { if (b) { b.disabled = false; b.innerHTML = '<span class="icon">▶</span> Run'; } });
  }
}

function appendConsole(text, className = '') {
  const el = document.getElementById('console-output');
  const span = document.createElement('span');
  span.className = className;
  span.textContent = text;
  el.appendChild(span);
  el.scrollTop = el.scrollHeight;
}

function clearConsole() {
  document.getElementById('console-output').innerHTML = '';
}

// ═════════════════════════════════════════════════════════════════════════════
// Version history
// ═════════════════════════════════════════════════════════════════════════════
function toggleVersionPanel() {
  const panel = document.getElementById('version-panel');
  panel.classList.toggle('open');
  if (panel.classList.contains('open') && state.currentFile) {
    loadVersions(state.currentFile);
  }
}

async function loadVersions(path) {
  try {
    const resp = await fetch('/api/versions?path=' + encodeURIComponent(path));
    if (!resp.ok) return;
    const data = await resp.json();
    state.versions = data.versions || [];
    renderVersions();
  } catch (e) {
    // silent
  }
}

function renderVersions() {
  const list = document.getElementById('version-list');
  if (state.versions.length === 0) {
    list.innerHTML = '<div style="padding:12px;color:var(--text-dim);font-size:12px;">No versions saved yet. Click 📸 to save a snapshot.</div>';
    return;
  }
  list.innerHTML = '';
  for (const v of state.versions) {
    const div = document.createElement('div');
    div.className = 'version-item';
    const date = new Date(v.timestamp);
    div.innerHTML = `
      <div class="info">
        <div class="label">${htmlEscape(v.label || 'Snapshot')}</div>
        <div class="time">${date.toLocaleString()}</div>
      </div>
      <div class="actions">
        <button onclick="restoreVersion('${v.id}')" title="Restore">↩</button>
        <button onclick="diffVersion('${v.id}')" title="Diff">⇄</button>
      </div>
    `;
    list.appendChild(div);
  }
}

async function saveVersion() {
  if (!state.currentFile) { showToast('No file open', 'warning'); return; }
  const content = document.getElementById('editor').value;
  try {
    const resp = await fetch('/api/versions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: state.currentFile, content: content, label: '' }),
    });
    if (!resp.ok) throw new Error((await resp.json()).error || 'Failed');
    showToast('Version snapshot saved', 'success', 1500);
    await loadVersions(state.currentFile);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

async function restoreVersion(versionId) {
  if (!confirm('Restore this version? Current changes will be lost.')) return;
  try {
    const resp = await fetch('/api/versions/' + versionId);
    if (!resp.ok) throw new Error('Failed');
    const data = await resp.json();
    document.getElementById('editor').value = data.content;
    state.fileContent = data.content;
    state.editorDirty = false;
    state.undoStack = [];
    state.redoStack = [];
    document.getElementById('unsaved-indicator').textContent = '';
    document.getElementById('save-btn').disabled = true;
    updateLineNumbers();
    updateStatusBar();
    showToast('Restored version', 'success');
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

async function diffVersion(versionId) {
  try {
    const resp = await fetch('/api/versions/' + versionId + '/diff?current_path=' + encodeURIComponent(state.currentFile));
    if (!resp.ok) throw new Error('Failed');
    const data = await resp.json();
    document.getElementById('diff-content').textContent = data.diff;
    document.getElementById('diff-view').classList.add('active');
    document.getElementById('editor-pane').style.display = 'none';
    document.getElementById('preview-pane').classList.remove('active');
    document.querySelectorAll('#tabbar .tab').forEach(t => t.classList.remove('active'));
    document.querySelector('#tabbar .tab[data-tab="diff"]').classList.add('active');
    document.getElementById('diff-tab').style.display = '';
    showToast('Diff loaded', 'info', 1500);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// Export
// ═════════════════════════════════════════════════════════════════════════════
async function exportFile(format) {
  if (!state.currentFile) { showToast('No file open', 'warning'); return; }
  const content = document.getElementById('editor').value;
  try {
    const resp = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: state.currentFile, content: content, format: format }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast(err.error || 'Export failed', 'error');
      return;
    }
    // Trigger download
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const baseName = state.currentFile.replace(/\.\w+$/, '');
    const extMap = { pdf: '.pdf', markdown: '.md', docx: '.docx', txt: '.txt' };
    a.download = baseName + (extMap[format] || '.txt');
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('Exported as ' + format, 'success', 1500);
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// Actions (writing/coding shortcuts)
// ═════════════════════════════════════════════════════════════════════════════
async function doAction(action) {
  if (!state.currentFile) { showToast('No file open', 'warning'); return; }
  const content = document.getElementById('editor').value;
  closeAllDropdowns();
  showToast('Running action: ' + action.replace(/_/g, ' '), 'info', 2000);

  try {
    const resp = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: action,
        path: state.currentFile,
        content: content,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast(err.error || 'Action failed', 'error');
      return;
    }
    const data = await resp.json();
    if (data.content) {
      // Push undo before applying
      pushUndo();
      document.getElementById('editor').value = data.content;
      editorDirtyCheck();
      showToast('Action applied: ' + action.replace(/_/g, ' '), 'success', 2000);
    }
    if (data.message) {
      appendConsole(data.message + '\n');
    }
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// Chat
// ═════════════════════════════════════════════════════════════════════════════
let chatPending = false;
let chatConversationId = null;
let chatGeneration = 0;

async function sendChat() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text || chatPending) return;

  chatPending = true;
  const generation = chatGeneration;
  const sendButton = document.getElementById('chat-send');
  sendButton.disabled = true;
  state.chatMessages.push({role: 'user', content: text});
  addChatMessage('user', text);
  input.value = '';
  input.style.height = 'auto';

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({messages: state.chatMessages, conversation_id: chatConversationId}),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `Chat request failed (HTTP ${resp.status})`);
    if (typeof data.reply !== 'string') throw new Error('Chat response has no reply');
    // Clearing chat while an older request is in flight starts a fresh conversation.
    if (generation !== chatGeneration) return;
    if (data.conversation_id) chatConversationId = data.conversation_id;
    state.chatMessages.push({role: 'assistant', content: data.reply});
    addChatMessage('assistant', data.reply);
  } catch (e) {
    if (generation === chatGeneration) {
      // Failed turns should not pollute the context sent with the next attempt.
      state.chatMessages.pop();
      addChatMessage('error', 'Chat error: ' + e.message);
    }
  } finally {
    chatPending = false;
    sendButton.disabled = false;
  }
}

function addChatMessage(role, content) {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'chat-msg';
  div.innerHTML = `
    <div class="role">${role === 'user' ? 'You' : role === 'error' ? 'Error' : 'Assistant'}</div>
    <div class="content">${htmlEscape(content)}</div>
    ${role === 'assistant' ? `<div class="actions">
      <button onclick="applyToEditor(this)" title="Apply to editor">📝 Apply</button>
      <button onclick="copyMessage(this)" title="Copy">📋 Copy</button>
    </div>` : ''}
  `;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function applyToEditor(btn) {
  const content = btn.closest('.chat-msg').querySelector('.content').textContent;
  pushUndo();
  document.getElementById('editor').value = content;
  editorDirtyCheck();
  showToast('Applied to editor', 'success', 1500);
}

function copyMessage(btn) {
  const content = btn.closest('.chat-msg').querySelector('.content').textContent;
  navigator.clipboard.writeText(content).then(() => {
    showToast('Copied', 'success', 1000);
  });
}

function clearChat() {
  chatGeneration++;
  state.chatMessages = [];
  chatConversationId = null;
  document.getElementById('chat-messages').innerHTML = `
    <div class="chat-msg">
      <div class="role">System</div>
      <div class="content">Chat cleared. Start a new conversation.</div>
    </div>
  `;
}

// Chat input auto-resize
document.getElementById('chat-input').addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 80) + 'px';
});

// Chat send on Enter (Shift+Enter for newline)
document.getElementById('chat-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
});

// ═════════════════════════════════════════════════════════════════════════════
// Settings
// ═════════════════════════════════════════════════════════════════════════════
function toggleSettingsPanel() {
  document.getElementById('settings-panel').classList.toggle('open');
}

function applySettings() {
  state.settings.fontSize = parseInt(document.getElementById('setting-font-size').value) || 13;
  state.settings.tabSize = parseInt(document.getElementById('setting-tab-size').value) || 4;
  state.settings.lineNumbers = document.getElementById('setting-line-numbers').checked;
  state.settings.wordWrap = document.getElementById('setting-word-wrap').checked;
  state.settings.theme = document.getElementById('setting-theme').value;
  state.settings.autoPreview = document.getElementById('setting-auto-preview').checked;
  state.settings.timeout = parseInt(document.getElementById('setting-timeout').value) || 30;
  state.settings.autoSave = document.getElementById('setting-auto-save').checked;

  const editor = document.getElementById('editor');
  editor.style.fontSize = state.settings.fontSize + 'px';
  editor.style.tabSize = state.settings.tabSize;
  editor.style.whiteSpace = state.settings.wordWrap ? 'pre-wrap' : 'pre';

  // Theme
  if (state.settings.theme === 'light') {
    document.documentElement.style.setProperty('--bg', '#ffffff');
    document.documentElement.style.setProperty('--sidebar-bg', '#f3f3f3');
    document.documentElement.style.setProperty('--toolbar-bg', '#ececec');
    document.documentElement.style.setProperty('--header-bg', '#e8e8e8');
    document.documentElement.style.setProperty('--border', '#ddd');
    document.documentElement.style.setProperty('--border-light', '#ccc');
    document.documentElement.style.setProperty('--text', '#333333');
    document.documentElement.style.setProperty('--text-dim', '#666');
    document.documentElement.style.setProperty('--text-muted', '#888');
    document.documentElement.style.setProperty('--active-bg', '#e0e0e0');
    document.documentElement.style.setProperty('--hover-bg', '#e8e8e8');
  } else {
    document.documentElement.style.setProperty('--bg', '#1e1e1e');
    document.documentElement.style.setProperty('--sidebar-bg', '#252526');
    document.documentElement.style.setProperty('--toolbar-bg', '#2a2a2a');
    document.documentElement.style.setProperty('--header-bg', '#2d2d2d');
    document.documentElement.style.setProperty('--border', '#333');
    document.documentElement.style.setProperty('--border-light', '#444');
    document.documentElement.style.setProperty('--text', '#d4d4d4');
    document.documentElement.style.setProperty('--text-dim', '#888');
    document.documentElement.style.setProperty('--text-muted', '#999');
    document.documentElement.style.setProperty('--active-bg', '#37373d');
    document.documentElement.style.setProperty('--hover-bg', '#2a2d2e');
  }

  updateLineNumbers();
}

// ═════════════════════════════════════════════════════════════════════════════
// Resize handlers
// ═════════════════════════════════════════════════════════════════════════════
function makeResizable(handleId, targetId, direction) {
  const handle = document.getElementById(handleId);
  const target = document.getElementById(targetId);
  let startPos, startSize;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    startPos = direction === 'col' ? e.clientX : e.clientY;
    startSize = direction === 'col' ? target.offsetWidth : target.offsetHeight;
    handle.classList.add('active');
    document.body.style.cursor = direction === 'col' ? 'col-resize' : 'row-resize';
    document.body.style.userSelect = 'none';

    const onMove = (ev) => {
      const delta = direction === 'col' ? (ev.clientX - startPos) : (ev.clientY - startPos);
      const newSize = startSize + delta;
      if (direction === 'col') {
        target.style.width = Math.max(target.dataset.min || 160, newSize) + 'px';
      } else {
        target.style.height = Math.max(60, newSize) + 'px';
      }
    };

    const onUp = () => {
      handle.classList.remove('active');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

makeResizable('sidebar-resize', 'sidebar', 'col');
makeResizable('chat-resize', 'chat-pane', 'col');
makeResizable('console-resize', 'console-pane', 'row');

// ═════════════════════════════════════════════════════════════════════════════
// Keyboard shortcuts
// ═════════════════════════════════════════════════════════════════════════════
document.addEventListener('keydown', (e) => {
  const ctrl = e.ctrlKey || e.metaKey;

  // Ctrl+S = save
  if (ctrl && e.key === 's') {
    e.preventDefault();
    saveFile();
  }
  // Ctrl+Enter = run
  if (ctrl && e.key === 'Enter') {
    e.preventDefault();
    runFile();
  }
  // Ctrl+Z = undo
  if (ctrl && !e.shiftKey && e.key === 'z') {
    e.preventDefault();
    undoEdit();
  }
  // Ctrl+Shift+Z = redo
  if (ctrl && e.shiftKey && e.key === 'z') {
    e.preventDefault();
    redoEdit();
  }
  // Ctrl+N = new file
  if (ctrl && e.key === 'n') {
    e.preventDefault();
    newFile();
  }
  // Escape = close panels
  if (e.key === 'Escape') {
    document.getElementById('version-panel').classList.remove('open');
    document.getElementById('settings-panel').classList.remove('open');
    closeModal();
    closeAllDropdowns();
  }
});

// ═════════════════════════════════════════════════════════════════════════════
// Editor event listeners
// ═════════════════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  const editor = document.getElementById('editor');

  // Track changes
  editor.addEventListener('input', () => {
    if (!state.undoStack.length && state.currentFile) {
      pushUndo();
    }
    editorDirtyCheck();
  });

  // Track cursor position
  editor.addEventListener('click', updateStatusBar);
  editor.addEventListener('keyup', updateStatusBar);

  // Initial load
  refreshFileTree();
  updateStatusBar();
  updateLineNumbers();
  document.getElementById('status-indent').textContent = 'Spaces: 4';
});
</script>
</body>
</html>
"""


def serve_html():
    """Return the full HTML page for the canvas workspace UI."""
    return HTML_PAGE


# ──────────────────────────────────────────────────────────────────────────────
# HTTP request handler
# ──────────────────────────────────────────────────────────────────────────────

class CanvasHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for the canvas workspace server."""

    # Shared state set by create_server
    workspace_dir = ""
    run_lock = threading.Lock()
    proxy_url = "http://localhost:8080"  # overridden by --proxy-url when extension-managed

    # Silence default logging (we do our own)
    def log_message(self, format, *args):
        sys.stderr.write("[canvas] %s - - [%s] %s\n" %
                         (self.client_address[0],
                          self.log_date_time_string(),
                          format % args))

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_text(self, text, status=200, content_type="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def _send_html(self, html_content, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(html_content.encode("utf-8"))

    def _send_error(self, message, status=400):
        self._send_json({"error": message}, status)

    def _send_binary(self, data, content_type="application/octet-stream", filename=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _parse_path(self):
        """Parse the URL path and return (endpoint, subpath)."""
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path.endswith("/") and len(path) > 1:
            path = path.rstrip("/")

        # Route
        if path == "/":
            return ("root", "", query)
        elif path == "/api/status":
            return ("status", "", query)
        elif path == "/api/files":
            return ("list_files", "", query)
        elif path.startswith("/api/files/") and path.endswith("/rename"):
            rel_path = path[len("/api/files/"):-len("/rename")]
            return ("rename_file", rel_path, query)
        elif path.startswith("/api/files/"):
            rel_path = path[len("/api/files/"):]
            return ("get_put_delete_file", rel_path, query)
        elif path == "/api/run":
            return ("run_file", "", query)
        elif path == "/api/preview":
            return ("preview", "", query)
        elif path == "/api/versions":
            return ("versions", "", query)
        elif path.startswith("/api/versions/") and path.endswith("/diff"):
            version_id = path[len("/api/versions/"):-len("/diff")]
            return ("version_diff", version_id, query)
        elif path.startswith("/api/versions/"):
            version_id = path[len("/api/versions/"):]
            return ("version_get", version_id, query)
        elif path == "/api/export":
            return ("export", "", query)
        elif path == "/api/action":
            return ("action", "", query)
        elif path == "/api/chat":
            return ("chat", "", query)
        else:
            return ("unknown", path, query)

    # ── CORS preflight ──
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        endpoint, subpath, query = self._parse_path()

        if endpoint == "root":
            self._send_html(serve_html())

        elif endpoint == "status":
            self._send_json({
                "status": "ok",
                "workspace": self.workspace_dir,
                "version": "2.0.0",
                "features": ["editor", "preview", "run", "versions", "export", "actions", "chat"],
            })

        elif endpoint == "list_files":
            tree = list_files(self.workspace_dir)
            self._send_json({"files": tree})

        elif endpoint == "get_put_delete_file":
            full_path = resolve_workspace_path(self.workspace_dir, subpath)
            if full_path is None:
                self._send_error("Path outside workspace", 403)
                return
            if not os.path.isfile(full_path):
                self._send_error("File not found", 404)
                return
            try:
                with open(full_path, "rb") as f:
                    content = f.read()
                try:
                    text = content.decode("utf-8")
                    self._send_text(text, content_type="text/plain; charset=utf-8")
                except UnicodeDecodeError:
                    # Serve binary files as base64 JSON
                    import base64
                    self._send_json({
                        "binary": True,
                        "content": base64.b64encode(content).decode("ascii"),
                        "path": subpath,
                    })
            except OSError as e:
                self._send_error(f"Could not read file: {e}", 500)

        elif endpoint == "versions":
            file_path = query.get("path", [None])[0]
            if not file_path:
                self._send_error("Missing 'path' query parameter", 400)
                return
            versions = list_versions(self.workspace_dir, file_path)
            self._send_json({"versions": versions})

        elif endpoint == "version_get":
            content, meta = get_version_content(self.workspace_dir, subpath)
            if content is None:
                self._send_error("Version not found", 404)
                return
            self._send_json({"id": subpath, "content": content, "meta": meta})

        elif endpoint == "version_diff":
            current_path = query.get("current_path", [None])[0]
            version_content, version_meta = get_version_content(self.workspace_dir, subpath)
            if version_content is None:
                self._send_error("Version not found", 404)
                return
            if current_path:
                full_path = resolve_workspace_path(self.workspace_dir, current_path)
                if full_path and os.path.isfile(full_path):
                    with open(full_path, "r") as f:
                        current_content = f.read()
                else:
                    current_content = ""
            else:
                current_content = ""
            diff = diff_versions(version_content, current_content)
            self._send_json({"diff": diff, "version_id": subpath})

        else:
            self._send_error("Not found", 404)

    def do_PUT(self):
        endpoint, subpath, query = self._parse_path()

        if endpoint == "get_put_delete_file":
            full_path = resolve_workspace_path(self.workspace_dir, subpath)
            if full_path is None:
                self._send_error("Path outside workspace", 403)
                return
            body = self._read_body()
            try:
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "wb") as f:
                    f.write(body)
                self._send_json({"status": "saved", "path": subpath})
            except OSError as e:
                self._send_error(f"Could not write file: {e}", 500)
        else:
            self._send_error("Not found", 404)

    def do_DELETE(self):
        endpoint, subpath, query = self._parse_path()

        if endpoint == "get_put_delete_file":
            full_path = resolve_workspace_path(self.workspace_dir, subpath)
            if full_path is None:
                self._send_error("Path outside workspace", 403)
                return
            try:
                if os.path.isdir(full_path):
                    shutil.rmtree(full_path)
                elif os.path.isfile(full_path):
                    os.remove(full_path)
                else:
                    self._send_error("Path not found", 404)
                    return
                self._send_json({"status": "deleted", "path": subpath})
            except OSError as e:
                self._send_error(f"Could not delete: {e}", 500)
        else:
            self._send_error("Not found", 404)

    def do_POST(self):
        endpoint, subpath, query = self._parse_path()

        if endpoint == "chat":
            try:
                data = json.loads(self._read_body().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send_error("Invalid JSON body", 400)
                return
            if not isinstance(data, dict):
                self._send_error("Expected a JSON object", 400)
                return
            messages = data.get("messages")
            if (not isinstance(messages, list) or not messages or
                    any(not isinstance(msg, dict) or
                        msg.get("role") not in ("system", "user", "assistant", "tool") or
                        not isinstance(msg.get("content"), str) for msg in messages)):
                self._send_error("'messages' must be a nonempty list of role/content messages", 400)
                return
            conversation_id = data.get("conversation_id")
            if conversation_id is not None and not isinstance(conversation_id, str):
                self._send_error("'conversation_id' must be a string", 400)
                return

            # The running proxy is the authority on the active chat model. Read
            # it for each turn so provider/model switches take effect immediately.
            base = self.proxy_url.rstrip("/")
            headers = {"Content-Type": "application/json"}
            if getattr(self, "api_token", ""):
                headers["Authorization"] = "Bearer " + self.api_token
            try:
                with urlopen(Request(base + "/providers", headers=headers), timeout=10) as response:
                    model = json.load(response).get("current_model")
                if not isinstance(model, str) or not model:
                    raise ValueError("Proxy did not report a current_model")
                payload = {"model": model, "messages": messages, "stream": False}
                if conversation_id:
                    payload["conversation_id"] = conversation_id
                request = Request(
                    base + "/v1/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers, method="POST",
                )
                with urlopen(request, timeout=180) as response:
                    completion = json.load(response)
                reply = completion["choices"][0]["message"]["content"]
                if not isinstance(reply, str):
                    raise ValueError("Proxy returned a completion without text content")
            except HTTPError as exc:
                self._send_error(f"Mneme proxy returned HTTP {exc.code}", 502)
                return
            except (URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                self._send_error(f"Mneme proxy chat failed: {exc}", 502)
                return
            self._send_json({"reply": reply, "model": model,
                             "conversation_id": completion.get("session_id", conversation_id)})
            return

        if endpoint == "get_put_delete_file":
            # POST to a file path = create new file/folder
            full_path = resolve_workspace_path(self.workspace_dir, subpath)
            if full_path is None:
                self._send_error("Path outside workspace", 403)
                return
            if os.path.exists(full_path):
                self._send_error("Path already exists", 409)
                return
            try:
                # If path ends with a known extension, create file; otherwise create directory
                ext = os.path.splitext(subpath)[1]
                if ext:
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    with open(full_path, "w") as f:
                        f.write("")
                    self._send_json({"status": "created", "path": subpath, "type": "file"})
                else:
                    os.makedirs(full_path, exist_ok=True)
                    self._send_json({"status": "created", "path": subpath, "type": "directory"})
            except OSError as e:
                self._send_error(f"Could not create: {e}", 500)

        elif endpoint == "rename_file":
            full_path = resolve_workspace_path(self.workspace_dir, subpath)
            if full_path is None:
                self._send_error("Path outside workspace", 403)
                return
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_error("Invalid JSON body", 400)
                return
            new_name = data.get("name", "")
            if not new_name:
                self._send_error("Missing 'name' in request body", 400)
                return
            parent = os.path.dirname(full_path)
            new_path = os.path.join(parent, new_name)
            if os.path.exists(new_path):
                self._send_error("Target path already exists", 409)
                return
            try:
                os.rename(full_path, new_path)
                self._send_json({"status": "renamed", "from": subpath, "to": new_name})
            except OSError as e:
                self._send_error(f"Could not rename: {e}", 500)

        elif endpoint == "run_file":
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = {}

            file_path = data.get("path", "")
            if not file_path:
                self._send_error("Missing 'path' in request body", 400)
                return

            full_path = resolve_workspace_path(self.workspace_dir, file_path)
            if full_path is None:
                self._send_error("Path outside workspace", 403)
                return
            if not os.path.isfile(full_path):
                self._send_error(f"File not found: {file_path}", 404)
                return

            timeout = data.get("timeout", 30)

            with self.run_lock:
                try:
                    proc = subprocess.run(
                        [sys.executable, full_path],
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        cwd=os.path.dirname(full_path),
                    )
                    result = {
                        "stdout": proc.stdout,
                        "stderr": proc.stderr,
                        "returncode": proc.returncode,
                        "timed_out": False,
                    }
                except subprocess.TimeoutExpired:
                    result = {
                        "stdout": "",
                        "stderr": f"Process timed out after {timeout} seconds",
                        "returncode": -1,
                        "timed_out": True,
                    }
                except OSError as e:
                    result = {
                        "stdout": "",
                        "stderr": f"Could not execute file: {e}",
                        "returncode": -1,
                        "timed_out": False,
                    }

            self._send_json(result)

        elif endpoint == "preview":
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_error("Invalid JSON body", 400)
                return

            file_path = data.get("path", "")
            content = data.get("content", "")
            if not file_path:
                self._send_error("Missing 'path' in request body", 400)
                return

            html_content, content_type = render_preview(file_path, content)
            if html_content is None and content_type == "image":
                # Binary image — return as base64
                full_path = resolve_workspace_path(self.workspace_dir, file_path)
                if full_path and os.path.isfile(full_path):
                    import base64
                    with open(full_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("ascii")
                    self._send_json({"content": b64, "content_type": "image"})
                else:
                    self._send_json({"content": "No preview available", "content_type": "text"})
            else:
                self._send_json({"content": html_content, "content_type": content_type})

        elif endpoint == "versions":
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_error("Invalid JSON body", 400)
                return

            file_path = data.get("path", "")
            content = data.get("content", "")
            label = data.get("label", "")
            if not file_path:
                self._send_error("Missing 'path' in request body", 400)
                return

            meta = save_version(self.workspace_dir, file_path, content, label)
            self._send_json({"status": "saved", "version": meta})

        elif endpoint == "export":
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_error("Invalid JSON body", 400)
                return

            file_path = data.get("path", "")
            content = data.get("content", "")
            fmt = data.get("format", "txt")
            if not file_path:
                self._send_error("Missing 'path' in request body", 400)
                return

            base_name = os.path.splitext(os.path.basename(file_path))[0]

            if fmt == "pdf":
                pdf_data = export_as_pdf(content, file_path)
                if pdf_data is None:
                    self._send_error("PDF export requires fpdf library (pip install fpdf2)", 501)
                    return
                self._send_binary(pdf_data, "application/pdf", f"{base_name}.pdf")

            elif fmt == "markdown":
                md_content = export_as_markdown(content, file_path)
                self._send_text(md_content, content_type="text/markdown")

            elif fmt == "docx":
                docx_data = export_as_docx(content, file_path)
                if docx_data is None:
                    self._send_error("DOCX export requires python-docx library (pip install python-docx)", 501)
                    return
                self._send_binary(docx_data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", f"{base_name}.docx")

            elif fmt == "txt":
                self._send_text(content, content_type="text/plain")

            else:
                self._send_error(f"Unsupported export format: {fmt}", 400)

        elif endpoint == "action":
            body = self._read_body()
            try:
                data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_error("Invalid JSON body", 400)
                return

            action = data.get("action", "")
            file_path = data.get("path", "")
            content = data.get("content", "")

            if not action:
                self._send_error("Missing 'action' in request body", 400)
                return

            # Apply writing/coding shortcut actions
            # In a full implementation, these would call the AI proxy.
            # For now, we provide simple built-in transformations.
            result_content = content
            message = ""

            if action == "suggest_edits":
                # Placeholder: wraps content with edit markers
                result_content = content
                message = "Suggest edits: review the document for improvements. (AI integration required for full feature.)"

            elif action == "adjust_length":
                # Placeholder: shortens by removing blank lines
                lines = content.split("\n")
                compressed = [l for l in lines if l.strip() or l == ""]
                # Keep at most one consecutive blank line
                result_content = ""
                blank = False
                for l in lines:
                    if l.strip() == "":
                        if not blank:
                            result_content += l + "\n"
                            blank = True
                    else:
                        result_content += l + "\n"
                        blank = False
                message = "Adjusted length: compressed blank lines."

            elif action == "change_reading_level":
                # Placeholder
                result_content = content
                message = "Change reading level: select a target level from the dropdown. (AI integration required.)"

            elif action == "final_polish":
                # Placeholder: trim trailing whitespace
                result_content = "\n".join(l.rstrip() for l in content.split("\n"))
                message = "Final polish applied: trimmed trailing whitespace."

            elif action == "add_emojis":
                # Placeholder: add emojis to common keywords
                replacements = {
                    "TODO": "📋 TODO",
                    "FIXME": "🐛 FIXME",
                    "NOTE": "📝 NOTE",
                    "HACK": "🔧 HACK",
                    "IMPORTANT": "⚠️ IMPORTANT",
                    "bug": "🐛 bug",
                    "fix": "🔧 fix",
                    "feature": "✨ feature",
                    "docs": "📚 docs",
                    "test": "🧪 test",
                }
                result_content = content
                for old, new in replacements.items():
                    result_content = result_content.replace(old, new)
                message = "Added emojis to common keywords."

            elif action == "review_code":
                result_content = content
                message = "Code review: analyze for logic and performance issues. (AI integration required.)"

            elif action == "add_logs":
                # Add print statements at function entries for Python
                lines = content.split("\n")
                new_lines = []
                for l in lines:
                    new_lines.append(l)
                    if l.strip().startswith("def ") and l.strip().endswith(":"):
                        indent = " " * (len(l) - len(l.lstrip()))
                        func_name = l.strip().split("(")[0].replace("def ", "")
                        new_lines.append(f'{indent}print("[LOG] Entering {func_name}()")')
                result_content = "\n".join(new_lines)
                message = "Added log statements at function entries."

            elif action == "add_comments":
                # Add comments above functions and classes
                lines = content.split("\n")
                new_lines = []
                for l in lines:
                    stripped = l.strip()
                    if stripped.startswith("def ") and not any(
                        prev.strip().startswith("#") for prev in new_lines[-3:] if new_lines
                    ):
                        indent = " " * (len(l) - len(l.lstrip()))
                        new_lines.append(f"{indent}# {stripped.split('(')[0].replace('def ', '')} — TODO: add docstring")
                    elif stripped.startswith("class ") and not any(
                        prev.strip().startswith("#") for prev in new_lines[-3:] if new_lines
                    ):
                        indent = " " * (len(l) - len(l.lstrip()))
                        new_lines.append(f"{indent}# Class: {stripped.split('(')[0].replace('class ', '').replace(':', '')}")
                    new_lines.append(l)
                result_content = "\n".join(new_lines)
                message = "Added comments above functions and classes."

            elif action == "fix_bugs":
                result_content = content
                message = "Bug fix analysis: scan for common issues. (AI integration required for full feature.)"

            elif action == "port_code":
                result_content = content
                message = "Port code: select target language from the dropdown. (AI integration required.)"

            else:
                self._send_error(f"Unknown action: {action}", 400)
                return

            self._send_json({
                "content": result_content,
                "message": message,
                "action": action,
            })

        else:
            self._send_error("Not found", 404)


# ──────────────────────────────────────────────────────────────────────────────
# Server factory
# ──────────────────────────────────────────────────────────────────────────────

def create_server(workspace_dir, host="0.0.0.0", port=DEFAULT_PORT, proxy_url=None):
    """Create and return an HTTPServer with the canvas handler."""

    class Handler(CanvasHTTPHandler):
        def __init__(self, *args, **kwargs):
            self.workspace_dir = workspace_dir
            self.proxy_url = proxy_url or os.environ.get("MNEME_PROXY_URL", "http://localhost:8080")
            self.api_token = os.environ.get("MNEME_API_TOKEN", "")
            super().__init__(*args, **kwargs)

    server = HTTPServer((host, port), Handler)
    return server


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Canvas Server — industry-standard file workspace with editor, preview, version history, and chat"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP server port (default: {DEFAULT_PORT})")
    parser.add_argument("--workspace", type=str, default=None,
                        help=f"Workspace directory (default: {DEFAULT_WORKSPACE})")
    parser.add_argument("--host", default="0.0.0.0",
                        help="HTTP server host (default: 0.0.0.0)")
    parser.add_argument("--proxy-url", default=None,
                        help="Managing Mneme proxy URL (default: MNEME_PROXY_URL or http://localhost:8080)")
    args = parser.parse_args()

    # Resolve workspace directory
    workspace_dir = args.workspace or os.environ.get("WORKSPACE_DIR", DEFAULT_WORKSPACE)
    workspace_dir = os.path.abspath(workspace_dir)

    # Create workspace directory if it doesn't exist
    os.makedirs(workspace_dir, exist_ok=True)

    # Create a sample file if workspace is empty
    if not os.listdir(workspace_dir):
        sample_path = os.path.join(workspace_dir, "hello.py")
        with open(sample_path, "w") as f:
            f.write('#!/usr/bin/env python3\n"""Hello from the canvas workspace!"""\n\nprint("Hello, canvas world!")\n')

    # Create and start server
    server = create_server(workspace_dir, host=args.host, port=args.port, proxy_url=args.proxy_url)
    print(f"[canvas] Workspace: {workspace_dir}", flush=True)
    print(f"[canvas] Server listening on http://{args.host}:{args.port}", flush=True)
    print(f"[canvas] Endpoints:", flush=True)
    print(f"  GET   /                        — canvas HTML UI", flush=True)
    print(f"  GET   /api/status              — server health + config", flush=True)
    print(f"  GET   /api/files               — list workspace files", flush=True)
    print(f"  GET   /api/files/<path>        — get file content", flush=True)
    print(f"  PUT   /api/files/<path>        — save file content", flush=True)
    print(f"  POST  /api/files/<path>        — create new file/folder", flush=True)
    print(f"  DELETE /api/files/<path>       — delete file/folder", flush=True)
    print(f"  POST  /api/files/<path>/rename — rename file/folder", flush=True)
    print(f"  POST  /api/run                 — run a file", flush=True)
    print(f"  POST  /api/preview             — render file preview", flush=True)
    print(f"  GET   /api/versions            — list version history", flush=True)
    print(f"  GET   /api/versions/<id>       — get version content", flush=True)
    print(f"  POST  /api/versions            — create version snapshot", flush=True)
    print(f"  GET   /api/versions/<id>/diff  — diff with current", flush=True)
    print(f"  POST  /api/export              — export file", flush=True)
    print(f"  POST  /api/action              — apply writing/coding shortcut", flush=True)
    print(f"  POST  /api/chat                — chat via Mneme proxy", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[canvas] Shutting down...", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()