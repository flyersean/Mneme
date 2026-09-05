# Muse Glimmer 30B (abliterated) — issue & restore reference

> **Status: not wired into the current code.** The setup wizard no longer offers
> Muse, and the proxy has no Muse-specific path. This file is a *reference* for
> re-adding Muse later if we decide to. It documents the exact bug, the exact
> workaround, and how to restore it.
>
> The original auto-provisioning (`setup_muse()` in `scripts/mneme_setup.py`)
> was removed in `e09de81` ("unified install → setup → connect"), and the original
> version of this doc was deleted in `1234e0a` ("prep repo for public sharing —
> prune internal artifacts"). Both predate the current `main`. The content below
> is reconstructed from git history (commit `98dbfa8`).

## What the model is

- Source: `Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF` (Hugging Face)
- Base: Meta **Muse Glimmer 30B** (dense ~29.6B, 128K context, agentic + multimodal)
- Abliteration: Blackfrost-AI (not the crude `huihui_ai` method)
- Why we tried it: it was the intended replacement for the `gemma4` abliterated
  model, which had a severe "way-" tic (every noun prefixed with "way-") from a
  crude abliteration. Muse is Meta-architecture, not Gemma, and had no such tic.

## The issue — Ollama's auto-detected template is wrong for this model

Ollama's template auto-detection for the Muse GGUF produced a template that
**stalls generation at ~3 tokens and returns empty output.** There were two bugs
in the auto-detected template:

1. It hardcoded `assistant to=user` in the assistant prefix — but Muse's Harmony
   channel format emits a `to=self` *reasoning* turn FIRST, then `to=user` for the
   actual answer. Forcing the recipient breaks the sequence.
2. It added premature stop tokens (`<|start|>`, `<|message|>`), which cut the
   output off almost immediately.

### Channel / reasoning split

The Harmony format works like this:

- `to=self` turn → reasoning, captured by Ollama in the `thinking` field
- `to=user` turn → the answer, captured in the `content` field

Because Ollama already splits these into `thinking` vs `content`, the proxy's
existing "fall back to `thinking` when `content` is empty" logic handles Muse
with no proxy changes — the only thing that needed fixing was the template.

## The workaround — a corrected Modelfile

Create `muse-glimmer:30b` with this template instead of relying on auto-detection.
The assistant prefix is just `<|start|>assistant` (no forced recipient), and the
only stop tokens are the ones that actually end a turn.

```Modelfile
FROM hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M
TEMPLATE """{{ if .System }}<|begin_of_text|><|start|>system<|message|>{{ .System }}

Reasoning strength: high.

# Valid recipients: "self", "user".<|eot|>{{ end }}{{ if .Prompt }}<|start|>user<|message|>{{ .Prompt }}<|eot|>{{ end }}<|start|>assistant"""
PARAMETER stop "<|eot|>"
PARAMETER stop "<|start|>user<|message|>"
PARAMETER num_ctx 32768
```

The system-prompt section injects the Harmony "valid recipients" preamble the
model expects (`Reasoning strength: high.` / `# Valid recipients: "self", "user".`).

## How to restore it

### Option A — manual (one-off, no code changes)

```bash
# Pull the GGUF (Q5_K_M ≈ 19.8GB; also fetches the ~2GB mmproj vision projector).
ollama pull hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M

# Write the corrected Modelfile (content above) and create the model.
cat > /tmp/Modelfile.muse <<'EOF'
FROM hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M
TEMPLATE """{{ if .System }}<|begin_of_text|><|start|>system<|message|>{{ .System }}

Reasoning strength: high.

# Valid recipients: "self", "user".<|eot|>{{ end }}{{ if .Prompt }}<|start|>user<|message|>{{ .Prompt }}<|eot|>{{ end }}<|start|>assistant"""
PARAMETER stop "<|eot|>"
PARAMETER stop "<|start|>user<|message|>"
PARAMETER num_ctx 32768
EOF
ollama create muse-glimmer:30b -f /tmp/Modelfile.muse
```

Then point the proxy at it:

```bash
MNEME_MODEL=muse-glimmer:30b
```

(instead of the `mneme-chat-...` derived name).

### Option B — re-add `setup_muse()` to the setup wizard

The original function (from `98dbfa8`) is small and self-contained. To restore the
wizard menu option, re-add these constants and function, then re-add the menu entry:

```python
MUSE_MODEL_NAME = "muse-glimmer:30b"
MUSE_SOURCE = "hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M"
MUSE_MODELFILE = '''FROM hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M
TEMPLATE """{{ if .System }}<|begin_of_text|><|start|>system<|message|>{{ .System }}

Reasoning strength: high.

# Valid recipients: "self", "user".<|eot|>{{ end }}{{ if .Prompt }}<|start|>user<|message|>{{ .Prompt }}<|eot|>{{ end }}<|start|>assistant"""
PARAMETER stop "<|eot|>"
PARAMETER stop "<|start|>user<|message|>"
PARAMETER num_ctx 32768
'''

def setup_muse():
    """Pull the Muse GGUF and create muse-glimmer:30b with the corrected template."""
    pull_model(MUSE_SOURCE)
    with open("/tmp/Modelfile.muse", "w") as f:
        f.write(MUSE_MODELFILE)
    r = run(f"ollama create {MUSE_MODEL_NAME} -f /tmp/Modelfile.muse", timeout=180)
    if r.returncode != 0:
        print(f"  Warning: muse create failed: {(r.stderr or '')[-200:]}")
    else:
        print(f"  ✓ {MUSE_MODEL_NAME} created with corrected template")
    return MUSE_MODEL_NAME
```

Other per-model knobs to apply (from the config example):

```yaml
muse-glimmer:30b:
  temperature: 1.0
  top_k: 64
  num_ctx: 32768
  reasoning_field: thinking
```

## Is this still needed? — likely an upstream bug that may be fixed

We tested Muse when it was **brand new**, so the wrong-template stall is almost
certainly an Ollama-side auto-detection bug for the then-new Harmony channel
format, not something inherent to the model. A later Ollama release may well
have corrected the auto-detected template — which would make this whole
workaround obsolete.

**Before re-adding the workaround, verify it's still needed** against the current
Ollama version:

```bash
# Pull fresh and create WITHOUT our template (use Ollama's auto-detected one).
ollama pull hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M
ollama create muse-glimmer:test -f <(echo "FROM hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M")

# If this returns a real answer instead of stalling at ~3 tokens, the bug is fixed
# and the corrected template above is no longer needed.
ollama run muse-glimmer:test "Say hello in one sentence."
```

If the auto-detected template generates normally, drop the custom template entirely —
just pull the model and use it directly (the proxy needs no changes either way).

## Notes

- The proxy still contains two generic, model-agnostic pieces of Muse-era handling
  that are harmless and not Muse-specific:
  - a comment at the strategy-extraction site noting `muse-glimmer`'s `to=self`
    reasoning turn makes JSON-grammar extraction unreliable (hence text+regex), and
  - the "Muse-template workaround" where `search_memory` results are delivered as a
    user message (the Muse template had no tool rendering).
- These are not setup; they don't need to be restored. They only matter if Muse is
  actually in use.
