# Mneme Installation Guide for AI Agents

You are setting up Mneme, a conversational memory proxy between an AI agent and Ollama. Follow these steps exactly. Run every verification command. Do not skip steps. If a step fails, stop and report the error — do not continue.

## Prerequisites

- Linux (Ubuntu 22.04+ recommended) or macOS
- NVIDIA GPU with 24GB+ VRAM (A40, A100, 4090) OR CPU-only for small models
- Python 3.10+
- An AI agent interface that connects to OpenAI-compatible endpoints (Hermes recommended)

## Step 1: Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Verify:
```bash
ollama --version
# Should print: ollama version 0.x.x
```

If on a GPU pod without systemd, start Ollama manually:
```bash
nohup ollama serve > /tmp/ollama.log 2>&1 &
sleep 5
curl -s http://localhost:11434/api/tags
# Should return: {"models":[]}
```

## Step 2: Pull Required Models

```bash
# Main chat model (35B, Q4_K_M, ~22GB VRAM)
ollama pull fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest

# Embedding model (567M, ~1.2GB VRAM)
ollama pull snowflake-arctic-embed2

# Labeling model (494M, ~400MB VRAM)
ollama pull qwen2.5:0.5b
```

Verify:
```bash
ollama list
# Should show all three models
```

If you have less VRAM, replace the chat model with a smaller one:
```bash
ollama pull qwen2.5:7b
```
Then set `MNEME_MODEL=qwen2.5:7b` before starting the proxy.

## Step 3: Clone and Checkout

```bash
git clone https://github.com/flyersean/Mneme.git /tmp/mneme
cd /tmp/mneme
git checkout dev-chunks
```

Verify:
```bash
git branch
# Should show: * dev-chunks
ls proxy/mneme_proxy.py
# Should exist
```

## Step 4: Install Python Dependencies

```bash
pip install flask flask-cors faiss-cpu numpy requests
```

On GPU pods with CUDA FAISS:
```bash
pip install faiss-gpu
```

Verify:
```bash
python3 -c "import flask, faiss, numpy; print('deps ok')"
# Should print: deps ok
```

## Step 5: Configure Ollama for Parallel Requests

Required for the LLM labeling step. Set before starting Ollama:
```bash
export OLLAMA_NUM_PARALLEL=4
export OLLAMA_MAX_LOADED_MODELS=3
```

If Ollama is already running, restart it:
```bash
pkill ollama
sleep 3
nohup ollama serve > /tmp/ollama.log 2>&1 &
sleep 10
```

## Step 6: Start the Mneme Proxy

```bash
# Copy files to workspace (the proxy runs from /workspace)
mkdir -p /workspace/proxy
cp proxy/mneme_proxy.py proxy/system_prompt.md /workspace/proxy/
cp restart_proxy.sh /workspace/

# Start
cd /workspace
bash restart_proxy.sh
```

Verify:
```bash
curl -s http://localhost:8080/health
# Should return: {"backend":"...","chunks":0,"model":"text-mneme:64k","status":"ok"}
```

## Step 7: Configure Your Agent Interface (Hermes)

Create a Hermes profile pointing at Mneme:

```bash
# Option A: Create a new profile
hermes profile create mneme \
  --provider custom \
  --base-url http://localhost:8080/v1 \
  --model text-mneme:64k

# Disable Hermes memory (Mneme handles it)
hermes config set memory.memory_enabled false
hermes config set memory.user_profile_enabled false
```

Verify:
```bash
hermes chat --profile mneme --message "Hello, what model are you running?"
# Should respond with a greeting
```

### Option B: Manual Hermes Config

Add to `~/.hermes/config.yaml` under the `mneme` profile:
```yaml
model:
  default: text-mneme:64k
  provider: custom
  base_url: http://localhost:8080/v1
  api_key: none
memory:
  memory_enabled: false
  user_profile_enabled: false
```

Then use:
```bash
hermes chat --profile mneme
```

### Option C: Any OpenAI-Compatible Client

Mneme speaks the OpenAI `/v1/chat/completions` protocol. Any client works:
```python
import openai
client = openai.OpenAI(base_url="http://localhost:8080/v1", api_key="none")
response = client.chat.completions.create(
    model="text-mneme:64k",
    messages=[{"role": "user", "content": "Hello"}]
)
```

## Step 8: Verify End-to-End

```bash
# 1. Send a message that will be remembered
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"text-mneme:64k","stream":false,"messages":[{"role":"user","content":"Remember: the capital of France is Paris."}]}'

# 2. Force save to archive
curl -s -X POST http://localhost:8080/save

# 3. Search for the saved fact
curl -s -X POST http://localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"query":"capital of France","top_k":5}'
# Should return results with topic labels

# 4. List recent chunks
curl -s http://localhost:8080/list
# Should show at least 1 chunk

# 5. Health check
curl -s http://localhost:8080/health
# chunks should be > 0
```

## Troubleshooting

**CUDA error: illegal memory access**
The Qwen 35B model crashes above ~4,600 chars total prompt. The proxy trims automatically. If crashes persist, switch to a smaller model:
```bash
export MNEME_MODEL=qwen2.5:7b
bash restart_proxy.sh
```

**FAISS dimension mismatch**
Delete the old database and restart:
```bash
rm -rf /workspace/mneme_chunks
bash restart_proxy.sh
```

**Save endpoint timeouts**
Normal during model generation. Data is saved on the next archive cycle (every 6 turns or manual `/save`).

**Ollama not starting on GPU pod**
```bash
apt-get install -y zstd psmisc
# Then reinstall ollama
```

**Proxy port conflict**
```bash
fuser -k 8080/tcp
bash restart_proxy.sh
```

## Config Reference

Environment variables for the proxy:
| Variable | Default | Description |
|----------|---------|-------------|
| `MNEME_MODEL` | `fredrezones55/...` | Ollama model name |
| `MNEME_CHUNK_DIR` | `/workspace/mneme_chunks` | Database directory |

Tunable constants in `proxy/mneme_proxy.py`:
| Constant | Default | Description |
|----------|---------|-------------|
| `ROUTE_THRESHOLD` | 0.08 | Minimum normalized FAISS score |
| `MAX_INJECTED_TOKENS` | 2048 | Token budget for injected memory |
| `STAGING_TURNS` | 6 | User turns before auto-archive |
| `AGE_DECAY_DAYS` | 7 | Recency half-life in save cycles |

## Quick Start (All-in-One)

Run this on a fresh pod or local machine:
```bash
curl -fsSL https://ollama.com/install.sh | sh
nohup ollama serve > /tmp/ollama.log 2>&1 &
sleep 5
ollama pull fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest
ollama pull snowflake-arctic-embed2
ollama pull qwen2.5:0.5b
git clone https://github.com/flyersean/Mneme.git /tmp/mneme
cd /tmp/mneme && git checkout dev-chunks
pip install flask flask-cors faiss-cpu numpy requests
mkdir -p /workspace/proxy
cp proxy/mneme_proxy.py proxy/system_prompt.md /workspace/proxy/
cp restart_proxy.sh /workspace/
cd /workspace && bash restart_proxy.sh
curl -s http://localhost:8080/health
```
