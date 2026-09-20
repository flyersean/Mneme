#!/usr/bin/env python3
"""Patch the 8080 config's `models:` block with the HF-card recipe for a mode.
Usage: set_hf.py none|low|high
Preserves all other lines (comments, sampling, etc.) via a targeted block replace.
"""
import re, sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "none"
PATH = "/workspace/mneme_chunks/instances/8080/mneme.yaml"
KEY = "orcarouter/Qwen3.8-27B-Uncensored"

# HF model-card recommended sampling (from the config's own comments):
#   temperature 0.7 / top_p 0.8 / top_k 20 / repetition_penalty 1.0
#   presence_penalty: instruct ~1.5 / thinking ~0.0
SETTINGS = {
    "none": {"reasoning": "false", "presence_penalty": "1.5"},
    "low":  {"reasoning_effort": "low", "presence_penalty": "0.0"},
    "high": {"reasoning_effort": "xhigh", "presence_penalty": "0.0"},
}

base = {
    "temperature": "0.7",
    "top_p": "0.8",
    "top_k": "20",
    "repetition_penalty": "1.0",
}
base.update(SETTINGS[MODE])

block = f'models:\n  "{KEY}":\n'
for k, v in base.items():
    block += f"    {k}: {v}\n"
block = block.rstrip("\n")

with open(PATH) as f:
    content = f.read()

# Match the uncommented `models:` line + its indented children only.
pattern = re.compile(r'(?m)^models:[^\n]*(?:\n[ \t]+[^\n]*)*')
new_content, n = pattern.subn(block, content, count=1)
if n == 0:
    print("ERROR: 'models:' section not found", flush=True)
    sys.exit(1)

with open(PATH, "w") as f:
    f.write(new_content)

print(f"=== models block set for mode={MODE} ===", flush=True)
print(block, flush=True)
