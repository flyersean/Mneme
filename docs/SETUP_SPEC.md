# Mneme Setup — Specification

## Overview

Two components: a pod-side setup wizard (`mneme_setup.py`) and a lightweight entry script (`setup.sh`). Together they provide a zero-dependency, one-command deployment of the Mneme memory proxy on any Linux machine with a GPU.

**One-line invocation:**
```
curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/setup.sh | bash
```

## User Experience

### Entry Point (`setup.sh`)

1. Clones the `dev-chunks` branch from GitHub (or downloads raw proxy files as fallback).
2. Copies proxy code to `/workspace/proxy/`.
3. Downloads the latest setup wizard to `/tmp/mneme_setup.py` and runs it with terminal stdin (`python3 < /dev/tty`).

### Setup Wizard (`mneme_setup.py`)

**Zero external dependencies.** Uses only Python standard library (`input()`, `subprocess`, `os`, `shutil`, `json`, `time`). No `questionary`, no `pip install` at wizard level.

**Four interactive steps:**

| Step | Question | Options |
|------|----------|---------|
| 1 | Choose main model | Auto-detected pulled models + "129K Modelfile" + "Custom" |
| 2 | Context window size | 32K, 129K, 264K, Custom |
| 3 | Chat interface | Pi, Skip (proxy-only), Both |
| 4 | Embedding model | arctic-embed2, nomic-embed-text, Custom |

**Pre-step guidance displayed:**
```
Model recommendations:
  • Hermes: 120K+ context (Hermes sends ~78KB of system prompt)
  • Pi:     32K context is plenty (Pi's prompt is tiny)
  • Any OpenAI-compatible model works. Pull models first with: ollama pull <name>
  • For large context, create a Modelfile with PARAMETER num_ctx, or use 'Custom' below
```

### Installation Phase (Automated)

After user confirms choices:

1. **GPU detection:** `nvidia-smi` for VRAM capacity.
2. **Ollama install:** `apt-get zstd curl`, then `curl ollama.com/install.sh | sh`, verify with `which ollama`. Starts `ollama serve` in background.
3. **Python deps:** Tries `pip install` (plain, then `--break-system-packages`), falls back to `apt-get python3-flask`. Prints actual errors on failure.
4. **Model pulling:** Pulls chosen model, embedding model, and `qwen2.5:0.5b` (labeler). Skips already-pulled models.
5. **120K Modelfile:** If context > 32K, creates `FROM <model>\nPARAMETER num_ctx <size>` and runs `ollama create mneme-chat -f Modelfile`. Uses any already-pulled model as base rather than downloading a new one.
6. **Proxy start:** Spawns `mneme_proxy.py` via `subprocess.Popen(start_new_session=True)` with env vars `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`. Polls `/health` for 15s to confirm startup.
7. **Pi install (optional):** Installs Node.js 22, `npm install -g @earendil-works/pi-coding-agent`, writes `~/.pi/agent/models.json` pointing at `localhost:8080/v1`, downloads `mneme-search-tool.ts` extension.

### Output

```
✓ Mneme is running! (0 chunks, mneme-chat:latest)

  Proxy:    http://localhost:8080
  Health:   http://localhost:8080/health
  API base: http://localhost:8080/v1

  Connect from this machine or via SSH:
    OpenAI client → http://localhost:8080/v1
    Pi agent:     pi --provider mneme --model text-mneme:64k --extension /workspace/mneme-search-tool.ts

  Logs: tail -f /tmp/mneme.log
```

## Architecture Decisions

### Why `input()` not `questionary`?

`curl | bash` closes stdin. `questionary` opens `/dev/tty` internally and works, but adds a dependency that must be pip-installed before the wizard can start. `input()` reads from whatever stdin is available. The `setup.sh` script redirects Python's stdin to `/dev/tty` before launching (`python3 /tmp/mneme_setup.py < /dev/tty`), so `input()` sees the terminal directly.

### Why no hardcoded model presets?

Model names on Ollama's registry change frequently (tags appear, disappear, get renamed). Hardcoding them creates a maintenance burden and confusing failures ("model not found"). Instead:

1. Run `ollama list` (poll up to 20s for server readiness).
2. Show every pulled model as an option under "── Pulled models ──".
3. Offer two generic options: "129K Modelfile" and "Custom".
4. Display model selection guidance before the prompt.

Users control what they pull. The wizard shows what's available.

### Why `subprocess.Popen` not `nohup`?

The proxy must survive the wizard process exiting. `nohup python3 ... &` inside `subprocess.run(shell=True, capture_output=True)` sometimes gets killed when the shell closes. `Popen(start_new_session=True)` creates a new process group that the wizard's exit doesn't affect.

### Why `--break-system-packages`?

RunPod and many cloud GPU images use Ubuntu 22.04+ with PEP 668 ("externally managed environment"). Standard `pip install` is blocked. `--break-system-packages` bypasses this. The apt fallback (`python3-flask`) is tried for systems where pip is completely locked down.

### Why poll Ollama server?

`ollama serve` takes 2–8 seconds to become ready. Simple `time.sleep(2)` is unreliable. The model detection function polls `ollama list` up to 20 times before giving up.

## Edge Cases

| Case | Behavior |
|------|----------|
| No GPU | Shows "Unknown (0GB VRAM)", continues |
| Ollama already running | Skips install, starts new serve anyway (Ollama handles it) |
| Git not installed | Falls back to curl-download of raw proxy files |
| All models already pulled | Skips `ollama pull` for all, proceeds to Modelfile/proxy |
| No pulled models | Only "Modelfile" and "Custom" shown; Modelfile asks for base model |
| Pip blocked | Tries `--break-system-packages`, then apt |
| Flask still not importable | Prints warning + manual fix command, does not abort |
| Proxy fails to start | Polls 15s, prints "timeout — check /tmp/mneme.log" if not responding |
| 120K Modelfile with already-pulled model | Uses first pulled model as base, skips re-download |
| `pkill -f mneme_proxy` fails (no process) | Ignored, continues |
| `curl | bash` stdin closed | `setup.sh` redirects `python3 < /dev/tty` before wizard |

## Script Locations

| File | Purpose | Served From |
|------|---------|-------------|
| `setup.sh` | Entry point, clones repo, launches wizard | GitHub raw (`dev-chunks`) |
| `scripts/mneme_setup.py` | Interactive wizard | GitHub raw (`dev-chunks`) |
| `scripts/mneme_connect.py` | Local laptop SSH tunnel + agent launcher | GitHub raw (`dev-chunks`) |
| `proxy/mneme_proxy.py` | The proxy itself | Cloned repo or raw download |
| `proxy/system_prompt.md` | v2 persona-free prompt | Cloned repo or raw download |
| `extensions/pi/mneme-search-tool.ts` | Pi search_memory extension | Downloaded during Pi install |

## Future Improvements

- **Headless mode:** `--non-interactive` flag accepting model/context/embedding as CLI args for CI/CD pipelines.
- **Config persistence:** Save choices to `/workspace/mneme_config.json` so re-running reuses previous selections.
- **Multi-model support:** Option to pull and configure multiple models with different ports.
- **Health dashboard:** Post-setup curl to `/health` with friendlier formatting than raw JSON.
- **Version pinning:** Lock proxy code to a release tag instead of always pulling `dev-chunks`.
- **Disk space check:** Warn before pulling models if available space < 30GB.
