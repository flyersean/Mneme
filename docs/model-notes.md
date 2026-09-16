# Model Notes

Consolidated notes on the models we've run through Mneme — what they are, how
they're set up, and what we measured. Intended as the living reference for
"which model, with what settings, and how did it behave."

---

## Qwen 3.8-27B (orcarouter/Qwen3.8-27B-Uncensored)

Current test model. A native **thinking** model (reasoning ON by default) that is
overthink-prone. 27B, 256K native context (we run 120K).

### Key facts / quirks

- **`think:false` only works on the native `/api/chat` endpoint, not `/v1`.** On
  `/v1/chat/completions` the thinking toggle is broken (still emits reasoning).
  Mneme uses `/api/chat`, so this is fine — but don't benchmark via raw `/v1` and
  expect the toggle to hold.
- **"Empty" responses on old Ollama were a version bug** — 0.34.x works.
- **HF model-card sampling** (the recommended recipe):
  - instruct (no thinking): `temperature 0.7 / top_p 0.8 / top_k 20 / presence_penalty 1.5 / repetition_penalty 1.0`
  - thinking: same except `presence_penalty 0.0`
- **`reasoning_effort`** (`low|medium|xhigh`) is supported natively, but the
  mapping is unreliable — `low` over-thinks MORE than `xhigh` in practice. Qwen's
  effort/budget knobs are treated as a *goal*, not a cap.

### Measured: large-input (19.6k-token document → extract critical findings)

| | Before chunking fix | After fix |
|---|---|---|
| Result | 20 min churn → empty (22 searches, 286 pending-embed) | 95.6s, correct (26/26 findings) |

The root cause was **not the model** — it was `_chunk_large_messages` silently
replacing the large input with an unembedded chunk index and telling the model to
`search_memory` for it (which returned nothing). Fixed by (a) only chunking inputs
that genuinely exceed the context budget and (b) adding a keyword fallback to
`search_memory` for unembedded chunks. Qwen 3.8 was uniquely *bad* here only
because it dutifully obeys the "use search_memory" instruction and grinds, where
3.6 and others answer from previews or give up gracefully.

### Measured: weather task (1–2 tools) latency across thinking levels

Question varied (different phrasing) per run; memory reset between levels; proxy
on 120K ctx. Values are wall-clock seconds, single-run each (noisy).

**Run 1 — default sampling (temp 0.2 / top_p 0.9 / top_k 64 / presence_penalty 0):**

| Level | Fresh (no-injection) | Injected |
|---|---|---|
| No thinking | 40.8s | 26.5s |
| Low thinking | 59.2s | 38.8s |
| High thinking | 48.4s | 42.2s |

**Run 2 — HF-card sampling (temp 0.7 / top_p 0.8 / top_k 20 / presence_penalty 1.5/0.0):**

| Level | Fresh | Injected |
|---|---|---|
| No thinking | 39.6s | 38.3s |
| Low thinking | 70.9s | 37.5s |
| High thinking | 72.4s | 42.6s |

Takeaways:

- **No-thinking is fastest and most consistent in every config** (~40s fresh).
- **Thinking adds latency**; the HF-card's higher temp makes thinking *slower*
  (high: 48.4s → 72.4s). "low" and "high" converge to ~71–72s under the card
  recipe — the "low is slowest" ordering from run 1 was a low-temp artifact.
- **Injection is inconsistently trusted.** Only 2 of 6 injection runs answered
  directly from the injected chunk; the other 4 re-fetched via `fetch_url`/
  `search_memory`. Worse with thinking on (it second-guesses the injection). The
  higher temp (run 2) further erodes the injection speedup for no-thinking.

### Measured: injected made-up facts (not look-up-able)

Saved fabricated personal details (name "Zephyr Quarrington", a lighthouse address,
dog "Biscuit", safe code "9-2-7-4"), restarted the proxy, then fresh-asked. Result:
**all three extracted correctly with `[source: mem_…]` citations, no hallucination.**

Subtlety: auto-injection only fired for the *specific* query ("dog + safe combo");
the generic queries ("my name?", "where do I live?") fell below the
`inject_min_sim` floor and the model instead called `search_memory` — which worked
only because of the keyword-fallback fix. Net: 3.8's memory extraction is solid;
its earlier "re-fetch" behavior on weather is *verification when a fetch path
exists*, not a failure to extract.

### Verdict

Reasoning-off + default (low) sampling is the best latency/consistency trade for
these 1–2 tool tasks. The HF-card temp 0.7 buys variety, not speed. Use 3.8 when
you want a diligent reasoning model and accept it will verify aggressively.

---

## Muse Glimmer 30B (muse-glimmer:30b)

Full detail (exact Modelfile, restore steps): see `docs/muse-glimmer-model.md`.

- **Source:** `Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M` — Meta
  Muse Glimmer 30B base (dense ~29.6B, 128K context, agentic + multimodal),
  abliterated by Blackfrost-AI (not the crude `huihui_ai` method).
- **Setup required a custom Modelfile.** Ollama's auto-detected template was wrong
  (stalls at ~3 tokens, empty output) because Muse uses the **Harmony channel
  format**: a `to=self` reasoning turn, then `to=user` for the answer. The corrected
  Modelfile drops the forced `to=user` recipient and the premature stop tokens.
  (Likely an Ollama auto-detection bug on the then-new Harmony format — verify it's
  still needed on current Ollama before re-adding.)
- **Quirks:**
  - The `to=self` reasoning turn conflicts with **all Ollama JSON grammars**
    (`format='json'` and schemas) → `peg-native format` errors / empty replies.
    Workaround: text-format prompts + regex, no JSON grammar. (Reported fixed in
    Ollama 0.32.13+.)
  - The Modelfile has **no tool/response rendering** — deliver `search_memory`
    results as a *user* message, not a `tool` message.
- **Sampling we used:** `temperature 1.0 / top_k 64 / num_ctx 32768 /
  reasoning_field: thinking`.
- **Latency:** it's a reasoning model and over-generates — measured **5,316 tokens
  for a ~500-token page summary (≈3.3 min pure generation)** because the `to=self`
  reasoning + provenance grading (which rewards many citable claims) both push
  verbosity. Embed/FAISS/labeling were all sub-second; the model's own output
  volume was the bottleneck.
- **Status:** not currently wired into the wizard; the doc above is the restore
  reference. Was the intended replacement for Gemma 4.

---

## Qwen 3.6-35B-A3B (fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive)

The **default** chat model in `proxy/mneme_proxy.py` (`MODEL` at module top).

- **What it is:** Qwen 3.6 35B MoE (**A3B** = ~3B active of 35B total), 22 GB,
  aggressively uncensored abliteration ("HauhauCS-Aggressive").
- **Why we run it:** the reliable workhorse. It handled the same large-input /
  tool tasks that choked Qwen 3.8, answering in "a few min max" with no empty-
  response churn. Not a thinking model in the same sense as 3.8/Muse.
- **Pairing:** used with `snowflake-arctic-embed2` (embed) and, historically,
  `qwen:0.5b` as the classifier, later `dagbs/qwen2.5-coder-1.5b-instruct-abliterated`
  as the labeler.
- **Known issue (historical):** early on, Ollama/Qwen didn't recognize the OpenAI
  tool-call schema we were sending (`search_memory` ignored). Worth re-verifying on
  current Ollama before assuming tool-calling works out of the box.

---

## Gemma 4 26b (abliterated)

- The original Mneme chat default, since replaced.
- **Notable:** a severe **"way-" tic** — nearly every noun prefixed with "way-"
  — from a *crude* abliteration. That's the concrete lesson it contributed: cheap
  abliteration methods leave lexical tics, so prefer cleaner abliterations
  (Blackfrost-AI style) or the abliteration is worse than no abliteration.
- Replaced by Muse Glimmer 30B for this reason.
