# Muse Glimmer 30B (abliterated) — model notes

Replacement for the gemma4 abliterated model, which had a severe "way-" tic
(every noun prefixed with "way-") caused by huihui_ai's crude abliteration.

## Model

- Source: `Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF` (Hugging Face)
- Base: Meta Muse Glimmer 30B (dense 29.6B, 128K context, agentic + multimodal)
- Abliteration: from Blackfrost-AI (not the crude huihui_ai method)
- No "way" tic — Meta architecture, not gemma.

## Pull command (Ollama, needs 0.32.x)

```
ollama pull hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M
```

Sizes: Q4_K_M 16.9GB / Q5_K_M 19.8GB / Q6_K 22.9GB / Q8_0 29.6GB.
The Q5_K_M pull also fetches the 2GB mmproj vision projector automatically.

## Template fix (REQUIRED)

Ollama's auto-detected template is wrong for this model and stalls generation
at 3 tokens with empty output. Two bugs:
1. It hardcodes `assistant to=user` in the assistant prefix — but the model
   emits a `to=self` reasoning turn FIRST, then `to=user` for the answer.
2. It adds premature stop tokens (`<|start|>`, `<|message|>`).

Correct template — the assistant prefix is just `<|start|>assistant` (no forced
recipient), and the only stop tokens are `<|eot|>` and `<|start|>user<|message|>`.

Modelfile:

```
FROM hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M
TEMPLATE """{{ if .System }}<|begin_of_text|><|start|>system<|message|>{{ .System }}

Reasoning strength: high.

# Valid recipients: "self", "user".<|eot|>{{ end }}{{ if .Prompt }}<|start|>user<|message|>{{ .Prompt }}<|eot|>{{ end }}<|start|>assistant"""
PARAMETER stop "<|eot|>"
PARAMETER stop "<|start|>user<|message|>"
PARAMETER num_ctx 32768
```

Create the model:

```
ollama create muse-glimmer:30b -f /path/to/Modelfile
```

## Reasoning / content separation

The model's Harmony channel format emits `to=self` reasoning then `to=user`
answer. Ollama captures reasoning in the `thinking` field and the answer in
`content` — so the proxy's existing "fall back to thinking when content is
empty" logic already handles it.

## Wire into the proxy

Start the proxy with:

```
MNEME_MODEL=muse-glimmer:30b
```

(instead of `mneme-chat:latest`).
