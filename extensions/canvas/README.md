# Canvas Extension — Mneme

A **full-featured web-based code workspace** matching the industry-standard canvas UI
used by ChatGPT, Gemini, and Claude. It provides a split-pane editor with live preview,
version history, export, writing/coding shortcuts, a file tree, a chat pane, and an
optional multi-agent loop — all in a single self-contained HTTP server.

![Canvas UI Layout](https://via.placeholder.com/800x400?text=Canvas+UI:+Toolbar+%7C+File+Tree+%7C+Editor+%7C+Preview+%7C+Chat)

## Features

| Feature | Description |
|---------|-------------|
| **Top Toolbar** | File operations (Open/Save/New), Undo/Redo, Run, Export, Writing shortcuts, Coding shortcuts, Version History, Settings |
| **File Tree** | Sidebar with folder/file browsing, context menu (create/rename/delete), drag-and-drop, dirty-file indicators |
| **Editor** | CodeMirror 6 editor with syntax highlighting, line numbers, minimap, bracket matching, find/replace, multiple cursors |
| **Live Preview** | Render HTML pages inline, format Markdown, show syntax-highlighted code output |
| **Diff View** | Side-by-side or unified diff when browsing version history |
| **Console / Output** | stdout/stderr from file execution with ANSI color support, timestamps, auto-scroll |
| **Chat Pane** | AI assistant chat with "Apply to Editor", "Copy", "Explain More" actions |
| **Version History** | File-based snapshots with auto-snapshot on save, timeline browser, diff view |
| **Export** | PDF, Markdown, DOCX, plain text, Python, HTML, JSON |
| **Writing Shortcuts** | Suggest edits, Adjust length, Change reading level, Final polish, Add emojis |
| **Coding Shortcuts** | Review code, Add logs, Add comments, Fix bugs, Port to language |
| **Status Bar** | Filename, cursor position (Ln/Col), encoding, language mode, indentation |
| **Resizable Panes** | Drag handles between file tree, editor, and chat panes |
| **Multi-Agent Loop** | (Optional) Round-robin agent collaboration via Mneme proxies |
| **Dark & Light Themes** | Configurable via settings panel or config YAML |

## Directory structure

```
extensions/canvas/
  README.md           # this file
  canvas_server.py    # the HTTP server (embedded HTML/CSS/JS frontend + REST API)
  canvas_config.yaml  # configuration: UI, export, version history, proxies, agents, loop
  extension.yaml      # manifest for the Mneme /extensions management page
  workspace/          # default workspace directory (created on first run)
```

## Requirements

- Python 3.8+
- No external Python packages required (stdlib only)

Optional packages for enhanced features:

| Feature | Package | Notes |
|---------|---------|-------|
| PDF export | `reportlab` or `weasyprint` | Falls back to text-only PDF if missing |
| DOCX export | `python-docx` | Falls back to plain text if missing |
| Markdown preview | `markdown` | Falls back to raw text if missing |
| YAML config parsing | `pyyaml` | Falls back to JSON config if missing |
| Syntax highlighting | `pygments` | Falls back to basic HTML if missing |

## Running

### From the command line

```bash
cd /path/to/extensions/canvas/
python3 canvas_server.py [--port PORT] [--workspace DIR]
```

Optional flags:

| Flag | Default | Description |
|------|---------|-------------|
| `--port PORT` | `9090` | HTTP server port |
| `--host HOST` | `0.0.0.0` | Bind address |
| `--workspace DIR` | `./workspace` | Workspace directory path |

### From the Mneme /extensions page

If `extension.yaml` is present, the /extensions management page can start, stop,
and configure the canvas server. The `config` fields in `extension.yaml` are
rendered as a form; the `config_file` field exposes `canvas_config.yaml` as a
raw YAML editor.

### Quick start

```bash
python3 canvas_server.py --port 9090
# Open http://localhost:9090 in your browser
```

---

## UI Layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  TOP TOOLBAR                                                                │
│  [📁 Open] [💾 Save] [📄 New] [⏪ Undo] [⏩ Redo] [▶ Run] [👁 Preview]    │
│  [📤 Export ▼] [✍️ Writing ▼] [💻 Code ▼] [🕓 History] [⚙ Settings]       │
├──────────┬──────────────────────────────────┬───────────────────────────────┤
│  FILE    │  EDITOR / PREVIEW  (toggle tabs) │  CHAT PANE                    │
│  TREE    │                                  │                               │
│          │  ┌─ Tab bar ──────────────────┐  │  ┌─────────────────────────┐  │
│  ├─ src/ │  │  Edit  │  Preview  │ Diff  │  │  │ 💬 Chat with AI        │  │
│  │ └─ ...│  └────────────────────────────┘  │  │                         │  │
│  ├─ docs │  ┌────────────────────────────┐  │  │  User: fix this bug     │  │
│  └─ ...  │  │  CodeMirror 6 editor       │  │  │  AI: done, see changes  │  │
│          │  │  • syntax highlighting     │  │  │                         │  │
│          │  │  • line numbers / minimap  │  │  │  ┌─────────────────┐   │  │
│          │  │  • bracket matching        │  │  │  │ [✏️ Type here...]│   │  │
│          │  └────────────────────────────┘  │  │  └─────────────────┘   │  │
│          │                                  │  └─────────────────────────┘  │
│          │  ┌─ Console / Output ──────────┐  │                               │
│          │  │  $ python3 script.py        │  │                               │
│          │  │  Hello, World!              │  │                               │
│          │  └────────────────────────────┘  │                               │
├──────────┴──────────────────────────────────┴───────────────────────────────┤
│  STATUS BAR                                                                 │
│  [file.py] [Ln 42, Col 12] [UTF-8] [Python] [Spaces: 4] [Modified ●]      │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Pane Dimensions

| Pane | Default width/height | Min | Resizable |
|---|---|---|---|
| File tree sidebar | 240px | 160px | Yes (drag handle) |
| Editor/Preview center | flex-fill | 400px | Yes (via sidebar/chat handles) |
| Chat pane | 340px | 260px | Yes (drag handle) |
| Status bar | full width | 24px | No |
| Top toolbar | full width | 40px | No |

### Themes

Two themes are available, configurable via the Settings panel or `canvas_config.yaml`:

**Dark theme (default)** — `#1e1e1e` background, `#252526` sidebar, `#d4d4d4` text
**Light theme** — `#ffffff` background, `#f3f3f3` sidebar, `#333333` text

---

## API Endpoints

All endpoints return JSON. The server runs on the configured port (default 9090).

### `GET /`

Serves the canvas HTML page (the full embedded frontend).

---

### `GET /api/status`

Server health and configuration summary.

```json
{
  "status": "ok",
  "version": "2.0.0",
  "workspace": "/path/to/workspace",
  "config": {
    "theme": "dark",
    "preview_enabled": true,
    "export_formats": ["pdf", "md", "docx", "txt", "py", "html", "json"],
    "version_depth": 50,
    "auto_snapshot": true
  },
  "agents": ["analyst", "critic"],
  "running": false,
  "turn": 0
}
```

---

### File Endpoints

#### `GET /api/files`

List all files in the workspace as a tree structure.

```json
[
  {
    "name": "src",
    "path": "src",
    "type": "directory",
    "children": [
      {"name": "main.py", "path": "src/main.py", "type": "file"}
    ]
  },
  {"name": "README.md", "path": "README.md", "type": "file"}
]
```

#### `GET /api/files/<path>`

Return the content of a file.

- **Response:** Raw file content (Content-Type based on file extension)
- **Status 404:** File not found
- **Status 403:** Path traversal detected

#### `PUT /api/files/<path>`

Save content to a file. Creates intermediate directories if needed.

- **Request body:** Raw file content (string)
- **Response:**
  ```json
  {"status": "saved", "path": "src/main.py", "size": 1234}
  ```
- **Note:** If `auto_snapshot_on_save` is enabled in config, a version snapshot
  is automatically created before overwriting.

#### `POST /api/files/<path>`

Create a new file or folder.

- **Request body (optional):**
  ```json
  {"type": "file", "content": "optional initial content"}
  {"type": "directory"}
  ```
- **Response:**
  ```json
  {"status": "created", "path": "new_file.py", "type": "file"}
  ```

#### `DELETE /api/files/<path>`

Delete a file or folder (recursive for directories).

- **Response:**
  ```json
  {"status": "deleted", "path": "old_file.py"}
  ```

#### `POST /api/files/<path>/rename`

Rename a file or folder.

- **Request body:**
  ```json
  {"name": "new_name.py"}
  ```
- **Response:**
  ```json
  {"status": "renamed", "old_path": "old_name.py", "new_path": "new_name.py"}
  ```

---

### Run / Preview

#### `POST /api/run`

Execute a file via subprocess and return stdout/stderr.

- **Request body:**
  ```json
  {"path": "src/main.py", "args": ["--verbose"], "timeout": 30}
  ```
- **Response:**
  ```json
  {
    "status": "ok",
    "exit_code": 0,
    "stdout": "Hello, World!\n",
    "stderr": "",
    "duration": 0.042
  }
  ```
- The `path` is relative to the workspace. If omitted, runs the currently
  active file. Timeout defaults to 30 seconds.

#### `POST /api/preview`

Render a file for live preview (HTML, Markdown, or code output).

- **Request body:**
  ```json
  {"path": "index.html", "type": "html"}
  ```
- **Response:**
  ```json
  {
    "status": "ok",
    "html": "<!DOCTYPE html>...",
    "content_type": "text/html"
  }
  ```
- Supported `type` values: `"html"`, `"markdown"`, `"code"`, `"auto"` (default)
- For Markdown, the response includes rendered HTML
- For code, the response includes syntax-highlighted HTML (if Pygments is available)

---

### Version History

#### `GET /api/versions?path=<filepath>`

List version history for a file.

```json
{
  "path": "src/main.py",
  "versions": [
    {
      "id": "v_1699000001",
      "timestamp": "2026-10-09T12:00:01Z",
      "size": 1234,
      "label": "auto-save"
    },
    {
      "id": "v_1699000000",
      "timestamp": "2026-10-09T12:00:00Z",
      "size": 1200,
      "label": "manual"
    }
  ]
}
```

#### `GET /api/versions/<id>?path=<filepath>`

Get the content of a specific version.

- **Response:** Raw file content of the snapshot

#### `POST /api/versions`

Create a new version snapshot.

- **Request body:**
  ```json
  {"path": "src/main.py", "label": "before-refactor"}
  ```
- **Response:**
  ```json
  {
    "status": "created",
    "id": "v_1699000002",
    "path": "src/main.py",
    "timestamp": "2026-10-09T12:05:00Z"
  }
  ```

#### `GET /api/versions/<id>/diff?path=<filepath>&other=<other_id>`

Diff two versions of a file.

- **Query params:**
  - `other` — the other version ID to compare against (defaults to the previous version)
  - `context` — number of context lines (default: from config, usually 10)
- **Response:**
  ```json
  {
    "status": "ok",
    "path": "src/main.py",
    "from_version": "v_1699000000",
    "to_version": "v_1699000001",
    "diff": [
      {"type": "context", "old_line": 10, "new_line": 10, "content": "def main():"},
      {"type": "deletion", "old_line": 11, "content": "    print('hello')"},
      {"type": "addition", "new_line": 11, "content": "    print('Hello, World!')"},
      {"type": "context", "old_line": 12, "new_line": 12, "content": ""}
    ],
    "stats": {"additions": 1, "deletions": 1, "changes": 2}
  }
  ```

---

### Export

#### `POST /api/export`

Export a file in a requested format.

- **Request body:**
  ```json
  {
    "path": "README.md",
    "format": "pdf",
    "options": {"title": "Documentation", "author": "User"}
  }
  ```
- **Response:**
  ```json
  {
    "status": "ok",
    "format": "pdf",
    "content": "<base64-encoded-content>",
    "filename": "README.pdf",
    "content_type": "application/pdf",
    "size": 45678
  }
  ```
- Supported formats: `"pdf"`, `"md"`, `"docx"`, `"txt"`, `"py"`, `"js"`,
  `"html"`, `"json"`
- The response content is base64-encoded binary data for non-text formats
- If a required library is missing, returns `501 Not Implemented` with a
  message indicating which library is needed

---

### Writing / Coding Shortcuts

#### `POST /api/action`

Apply a writing or coding shortcut action to a file's content.

- **Request body:**
  ```json
  {
    "path": "src/main.py",
    "action": "fix_bugs",
    "content": "current file content",
    "selection": {"start": 10, "end": 25},
    "options": {"language": "python"}
  }
  ```
- **Supported actions:**

  | Action | Group | Description |
  |--------|-------|-------------|
  | `suggest_edits` | writing | Inline tracked changes (additions/deletions) |
  | `adjust_length` | writing | Shorten or lengthen document (use `options.length`: `"shorter"`/`"longer"`) |
  | `change_reading_level` | writing | Change reading level (use `options.level`: `"kindergarten"`–`"graduate"`) |
  | `final_polish` | writing | Grammar, clarity, consistency check |
  | `add_emojis` | writing | Inject relevant emojis |
  | `review_code` | coding | Inline suggestions to improve logic and performance |
  | `add_logs` | coding | Insert print/logging statements |
  | `add_comments` | coding | Annotate code with explanatory comments |
  | `fix_bugs` | coding | Detect and rewrite problematic code |
  | `port_language` | coding | Port to another language (use `options.target_language`) |

- **Response:**
  ```json
  {
    "status": "ok",
    "action": "fix_bugs",
    "original": "def add(a,b):return a-b",
    "modified": "def add(a, b): return a + b",
    "diff": [
      {"type": "deletion", "content": "def add(a,b):return a-b"},
      {"type": "addition", "content": "def add(a, b): return a + b"}
    ],
    "explanation": "Fixed subtraction bug — changed '-' to '+', added spaces per PEP 8"
  }
  ```
- **Note:** Actions that require AI processing (all writing/coding shortcuts)
  are delegated to the configured Mneme proxy. If no proxy is configured, the
  action falls back to a simple local heuristic or returns `501 Not Implemented`.

---

### Multi-Agent Loop Endpoints

The canvas server also supports an optional multi-agent collaborative loop,
where AI agents take turns reading and updating a shared structured document.
These endpoints are only active when `agents` are configured in the YAML.

#### `GET /api/status` (extended)

When agents are configured, the status response includes:

```json
{
  "status": "ok",
  "running": false,
  "finished": false,
  "turn": 0,
  "agents": ["analyst", "critic"],
  "max_turns": 10,
  "canvas_keys": ["problem", "sections", "scores", "turn"]
}
```

#### `GET /canvas`

Return the current agent canvas document as JSON.

```json
{
  "problem": "Design a fault-tolerant microservice architecture",
  "sections": [
    {"agent": "analyst", "turn": 1, "content": "..."},
    {"agent": "critic", "turn": 2, "content": "..."}
  ],
  "scores": {"quality": 0.85, "feasibility": 0.7},
  "turn": 2
}
```

#### `GET /canvas/history`

Return the full turn history — every agent response, raw and parsed.

```json
[
  {
    "turn": 1,
    "agent": "analyst",
    "timestamp": 1699000000.0,
    "raw_content": "...",
    "parsed": {"sections": [...], "scores": {...}}
  }
]
```

#### `POST /canvas/step`

Run **one turn** of the agent loop.

- **Request body:** (optional)
  ```json
  {"agent": "critic", "override_prompt": "Optional custom prompt"}
  ```
- **Response:**
  ```json
  {
    "turn": 3,
    "agent": "critic",
    "canvas": {...},
    "finished": false,
    "reason": null
  }
  ```

#### `POST /canvas/run`

Run the full loop to completion in a background thread.

- **Request body:** (optional)
  ```json
  {"max_turns": 5}
  ```
- **Response:**
  ```json
  {
    "status": "started",
    "run_id": "run_abc123",
    "message": "Loop running in background"
  }
  ```

#### `POST /canvas/reset`

Reset the agent canvas to the initial template and clear history.

- **Response:**
  ```json
  {"status": "reset", "canvas": {...}}
  ```

#### `POST /canvas/update`

Apply a direct update to the agent canvas without calling an agent.

- **Request body:**
  ```json
  {"problem": "New problem statement", "scores": {"quality": 0.9}}
  ```
- **Response:**
  ```json
  {"status": "updated", "canvas": {...}}
  ```

---

## Configuration

The config file (`canvas_config.yaml`) controls all server, UI, and agent settings.
It is read on startup. Edit and restart to apply changes.

### Server

```yaml
port: 9090
host: "0.0.0.0"
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `port` | no | `9090` | HTTP server port |
| `host` | no | `"0.0.0.0"` | Bind address |

### UI

```yaml
ui:
  theme: "dark"              # "dark" | "light"
  preview_enabled: true      # enable live preview pane on startup
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `theme` | no | `"dark"` | Visual theme: `"dark"` or `"light"` |
| `preview_enabled` | no | `true` | Show live preview pane on startup (users can toggle from toolbar) |

### Export

```yaml
export:
  enabled_formats:
    - "pdf"
    - "md"
    - "docx"
    - "txt"
    - "py"
    - "html"
    - "json"
  default_format: "md"
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `enabled_formats` | no | all formats | List of enabled export format identifiers |
| `default_format` | no | `"md"` | Pre-selected format in the export dialog |

Supported formats: `"pdf"`, `"md"`, `"docx"`, `"txt"`, `"py"`, `"js"`, `"html"`, `"json"`

### Version History

```yaml
version_history:
  max_depth: 50              # max snapshots per file (0 = disabled)
  auto_snapshot_on_save: true # snapshot on every save
  diff_preview_lines: 10     # context lines in diff view
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `max_depth` | no | `50` | Maximum version snapshots retained per file. Oldest pruned (FIFO). `0` = disabled |
| `auto_snapshot_on_save` | no | `true` | Auto-create version snapshot on every file save |
| `diff_preview_lines` | no | `10` | Context lines shown in diff previews |

### Proxies (for AI features)

```yaml
proxies:
  - url: "http://localhost:8080"
```

Each proxy must expose `POST /v1/chat/completions` (OpenAI-compatible).
The server round-robins across proxies if multiple are listed.

### Agents (for multi-agent loop)

```yaml
agents:
  - name: "analyst"
    model: "qwen/qwen3-8b"
    system_prompt: "You are a systems analyst..."
    temperature: 0.7
    max_tokens: 2048
  - name: "critic"
    model: "qwen/qwen3-8b"
    system_prompt: "You are a constructive critic..."
    temperature: 0.8
    max_tokens: 2048
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Agent identifier, used in round-robin order |
| `model` | yes | Model name passed in the chat completion request |
| `system_prompt` | yes | System prompt prepended to every turn for this agent |
| `temperature` | no | Sampling temperature (default: proxy default) |
| `max_tokens` | no | Max tokens per response (default: proxy default) |

### Canvas (for agent loop)

```yaml
canvas:
  template:
    problem: ""
    sections: []
    scores: {}
    turn: 0
  max_sections: 20
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `template` | yes | — | Initial canvas structure (deep-copied on reset) |
| `max_sections` | no | `20` | Maximum sections before oldest are trimmed |

### Loop (for agent loop)

```yaml
loop:
  max_turns: 10
  stop_on_consensus: true
  consensus_threshold: 0.8
  stop_on_max_turns: true
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `max_turns` | no | `10` | Maximum turns before forced stop |
| `stop_on_consensus` | no | `true` | Stop when average score >= threshold |
| `consensus_threshold` | no | `0.8` | Score threshold (0.0–1.0) |
| `stop_on_max_turns` | no | `true` | Stop when `max_turns` is reached |

### Full example

```yaml
port: 9090
host: "0.0.0.0"

proxies:
  - url: "http://localhost:8080"

agents:
  - name: "analyst"
    model: "qwen/qwen3-8b"
    system_prompt: "You are a systems analyst. Read the canvas and propose a solution."
  - name: "critic"
    model: "qwen/qwen3-8b"
    system_prompt: "You are a critic. Evaluate the proposal and assign scores."

canvas:
  template:
    problem: ""
    sections: []
    scores: {}
    turn: 0
  max_sections: 20

loop:
  max_turns: 10
  stop_on_consensus: true
  consensus_threshold: 0.8
  stop_on_max_turns: true

ui:
  theme: "dark"
  preview_enabled: true

export:
  enabled_formats:
    - "pdf"
    - "md"
    - "docx"
    - "txt"
    - "py"
    - "html"
    - "json"
  default_format: "md"

version_history:
  max_depth: 50
  auto_snapshot_on_save: true
  diff_preview_lines: 10
```

---

## Agent Response Format (for multi-agent loop)

Agents can respond in two ways:

### 1. Structured update (recommended)

Wrap a JSON or YAML block in fenced code markers. The server parses the block
and merges it into the canvas.

````
Here is my analysis.

```canvas
{
  "sections": [
    {"title": "Proposal", "content": "Use a message queue..."}
  ],
  "scores": {"quality": 0.85}
}
```
````

Supported markers: ` ```canvas ``, ` ```json ``, ` ```yaml `

Supported update keys:

| Key | Type | Behavior |
|-----|------|----------|
| `sections` | list | Appended to canvas sections (oldest trimmed at `max_sections`) |
| `scores` | dict | Merged into canvas scores |
| `problem` | string | Replaces the problem statement |
| `$set` | dict | Sets arbitrary keys on the canvas |
| `$merge` | dict | Merges dicts at arbitrary keys |

### 2. Free text (fallback)

If no fenced block is found, the entire response is appended as a new section
with `agent` and `turn` metadata.

---

## Adapting the extension

### Change the theme

Edit `canvas_config.yaml` → `ui` → `theme` to `"dark"` or `"light"`, or change
it at runtime from the Settings panel in the UI.

### Enable/disable preview

Edit `canvas_config.yaml` → `ui` → `preview_enabled`, or toggle it from the
toolbar's Preview button at runtime.

### Change export formats

Edit `canvas_config.yaml` → `export` → `enabled_formats`. Remove formats you
don't need. The export dialog in the UI will only show the enabled formats.

### Adjust version history depth

Edit `canvas_config.yaml` → `version_history` → `max_depth`. Set to `0` to
disable version history entirely. Set to a high number (e.g. `200`) to keep
many snapshots.

### Point at different proxies

Edit `canvas_config.yaml` → `proxies` → `url`. Use the port of your running
Mneme instance. On RunPod, avoid port 8081 (nginx reserves it).

### Add or remove agents

Edit `canvas_config.yaml` → `agents`. Add entries with `name`, `model`, and
`system_prompt`. The server round-robins through the list in order.

### Change the canvas structure

Edit `canvas_config.yaml` → `canvas` → `template`. You can add any keys you
want. Agents can read and write them via `$set` / `$merge`.

### Change stopping conditions

Edit `canvas_config.yaml` → `loop`. Set `stop_on_consensus: false` to disable
early stopping. Adjust `consensus_threshold` to make consensus harder/easier.

### Run without optional libraries

If `pyyaml` is not installed, the server falls back to JSON for config parsing.
Your config file must be valid JSON (or use `.json` extension). The canvas
document is always served as JSON regardless.

If `markdown` is not installed, the Markdown preview tab shows raw text.
If `pygments` is not installed, code preview uses basic HTML escaping.
If `reportlab`/`weasyprint` is not installed, PDF export returns `501`.
If `python-docx` is not installed, DOCX export returns `501`.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| Server won't start, port in use | Change `--port` or kill the existing process |
| File tree shows nothing | Workspace directory is empty or path is wrong |
| Run returns empty output | File has no shebang or is not executable; check `args` |
| Preview shows "No preview available" | File type is not previewable (e.g. binary files) |
| Export returns 501 | Missing optional library (see Requirements table) |
| Version history shows no snapshots | `max_depth` is 0 or `auto_snapshot_on_save` is false |
| Chat returns no response | No proxy configured or proxy is unreachable |
| Writing/coding shortcuts return 501 | No proxy configured (AI actions need a proxy) |
| `ERROR: No agents defined` | `agents` list is empty or missing in config |
| `ERROR: No proxies defined` | `proxies` list is empty or missing in config |
| `Connection refused` when calling proxy | Proxy URL or port is wrong; proxy not running |
| Agent responses not parsed as structured | Response lacks a fenced code block |
| Canvas not updating | Check the agent's response — parsed block may be empty or invalid |

---

## Design principles

- **Zero coupling to Mneme internals.** The server imports nothing from the
  Mneme repo. It communicates with proxies exclusively over HTTP.
- **Thread-safe.** All canvas reads and writes go through a `threading.Lock`.
- **Self-contained.** One Python file, one config file, one manifest. No
  database, no state files, no external services.
- **Graceful fallbacks.** Missing libraries degrade gracefully — the server
  works with Python stdlib only. Optional features light up as libraries
  become available.
- **Industry-standard UI.** The layout, toolbar, and interaction patterns
  match ChatGPT Canvas, Gemini Canvas, and Claude Artifacts (as of 2026).
- **Config-driven.** All UI, export, version history, proxy, and agent settings
  are driven by YAML config — no hardcoded values.
- **Progressively enhanced.** The frontend works in any browser. JavaScript
  enables the full interactive experience; the textarea fallback still works
  without JS.