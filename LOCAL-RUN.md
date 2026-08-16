# Mneme (OpenRouter backend) — Local Run Reference

Quick reference for running Mneme + Pi on this machine. Branch: `openrouter-backend`.
Everything is hosted on OpenRouter — no Ollama, no GPU, no local model files.

## One command to start everything

    ~/Mneme-build/launch.sh

Starts the Mneme proxy in the background, waits for it to be healthy, then launches
Pi. Exiting Pi stops the proxy. Idempotent — sources your saved API key and creates
the venv if it's missing.

## Paths

Mneme
    repo ............ /home/sean/Mneme-build
    proxy ........... /home/sean/Mneme-build/proxy/mneme_proxy.py
    launcher ........ /home/sean/Mneme-build/launch.sh
    setup wizard .... /home/sean/Mneme-build/scripts/mneme_setup_openrouter.py
    manual launcher . /home/sean/Mneme-build/scripts/run_openrouter.sh
    memory DB ....... /home/sean/mneme_chunks/
    proxy log ....... /home/sean/mneme_chunks/mneme.log   (only when using launch.sh)
    python venv ..... /home/sean/mneme-venv/
    API key file .... /home/sean/.mneme/openrouter.env    (chmod 600)

Pi
    binary .......... /home/sean/.nvm/versions/node/v22.22.2/bin/pi   (on PATH as `pi`)
    config .......... /home/sean/.pi/agent/models.json
    extensions ...... /home/sean/Mneme-build/extensions/pi/mneme-search-tool.ts
                      /home/sean/Mneme-build/extensions/pi/mneme-web-tools.ts

## Commands

Quick start (proxy + Pi together):
    ~/Mneme-build/launch.sh

One-time setup (key + models + venv; only needed if something's missing):
    python3 ~/Mneme-build/scripts/mneme_setup_openrouter.py

Proxy only (foreground, see logs live in this terminal):
    cd ~/Mneme-build
    export OPENROUTER_API_KEY=$(grep -iE '^OPENROUTER_API_KEY=' ~/.mneme/openrouter.env | cut -d= -f2-)
    scripts/run_openrouter.sh

Pi only (when the proxy is already running):
    cd ~/Mneme-build
    pi --provider mneme --model text-mneme:64k \
      --extension extensions/pi/mneme-search-tool.ts \
      --extension extensions/pi/mneme-web-tools.ts

Health check:
    curl http://localhost:8080/health

Stop the proxy:
    kill $(ss -tlnp | grep :8080 | grep -oP 'pid=\K[0-9]+')

## Models (defaults)

    Main LLM .. deepseek/deepseek-v4-flash        (thinking — slower, better on hard tasks)
    Embedder .. voyageai/voyage-4-lite            (1024-dim, matches FAISS)
    Labeler ... meta-llama/llama-3.2-3b-instruct  (non-thinking — required)

Change a model by overriding the env var before launching:
    MNEME_MODEL=deepseek/deepseek-chat ~/Mneme-build/launch.sh
    # deepseek-chat = V3, non-thinking: faster but weaker reasoning

Other overrides: EMBED_MODEL, LABEL_MODEL, MNEME_PORT, MNEME_CHUNK_DIR.

## Troubleshooting

- "No such file: ~/mneme_chunks/start_proxy.sh" — that script only exists after running
  the setup wizard. Use launch.sh or run_openrouter.sh instead.

- Pi refuses to start with a "Tool conflicts" error — the extensions are loaded twice
  (auto-discovery at ~/.pi/agent/extensions/ AND the --extension flags). Remove one.

- Feels stalled / slow — deepseek-v4-flash is a thinking model; it reasons before
  answering and can take 30s-2min on big prompts. Wait it out, or switch to
  deepseek/deepseek-chat for speed. The proxy auto-aborts a hung call after 10 minutes.

- Port 8080 already in use — stop the old proxy first (see "Stop the proxy" above).

- Health check hangs — the proxy may be mid-generation; the /health endpoint stays
  responsive because it's threaded, so a non-responding /health means the proxy is down.
