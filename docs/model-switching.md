# Switching models without breaking Mneme

Read this **before** you change the `model:` line. A model switch that changes only
the model string — but leaves the previous model's template and penalties in place —
is the #1 way Mneme "breaks" for a new user. This doc turns that debugging session
into a 3-line edit.

## The failure modes (what "broken" looks like)

Two distinct problems show up when settings from one model leak onto another. Both
are **settings problems, not "the model is broken"** — the model is fine, it's being
fed another model's recipe.

1. **Word salad / repetition** — the model loops the same phrase. Cause: a too-low
   temperature, or a `frequency_penalty` / `presence_penalty` recipe that doesn't fit
   the model. Seen on GLM-5.3 (needs `temperature 1.0` + a 0.7 frequency penalty).

2. **Early end-of-turn** — the model emits a bare tool call (`think=0`, `content=2`)
   and finalizes instead of working. Cause: a reasoning model sent
   `reasoning: {enabled: false}` (told "don't think"), so it can't plan the multi-step
   task. Seen on DeepSeek-V4-Pro and GLM-5.3.

## The settings stack (highest precedence first)

```
environment variable  >  models.<name>.*  >  model template  >  mneme.yaml sampling  >  built-in default
```

- **Built-in defaults** — in `proxy/mneme_proxy.py` (the `opts = {...}` dict in
  `_query_model_impl`). Deliberately neutral: `temperature 0.7`, `top_p 0.95`,
  `frequency_penalty 0.0`, `presence_penalty 0.0`.
- **`mneme.yaml` `sampling:` block** — your per-instance settings.
- **`model_template:`** — points at a named recipe in `model_templates.yaml`. Template
  values **win** over the `sampling:` block.
- **`models.<name>.*`** — per-model overrides (in the template's `models:` block, or the
  config's `models:` block). These beat the template's own sampling.
- **environment variables** — `MNEME_*` (e.g. `MNEME_TEMPERATURE`,
  `MNEME_FREQUENCY_PENALTY`, `MNEME_MANDATORY_REASONING_BUDGET`). Highest of all.

To see what is *actually* in force, type `<<SETTINGS>>` in the chat — it prints the
merged result, not what you think you set.

## The known-good templates (`model_templates.yaml`)

| template | model | temperature | top_p | penalties | notes |
|---|---|---|---|---|---|
| `deepseek-v4` | DeepSeek V4 Pro/Flash | 1.0 | 1.0 | none | reasoning model — thinking stays on (bounded) |
| `glm5.3-loop` | GLM-5.3 | 1.0 | 0.95 | freq 0.7 / pres 0.3 | GLM's anti-loop recipe |
| `qwen3.8-xhigh` / `qwen3.8-nothink` | Qwen 3.8 | — | — | — | see `docs/model-notes.md` |
| `muse-glimmer` / `gemma4-repeat` | — | — | — | — | see `docs/model-notes.md` |

## Reasoning models vs the "reasoning off" default

Mneme defaults to `sampling.reasoning_enabled: 0` (thinking OFF) to stop a reasoning
model from runaway-thinking on a trivial ask. **Reasoning models (DeepSeek-V4, GLM-5.3)
must not have thinking disabled** — that's the early-end-of-turn failure above.

The `_MANDATORY_REASONING_MODELS` list in `proxy/mneme_proxy.py` marks models whose
endpoint can't be told "don't think". For those, "reasoning off" becomes a *bounded*
budget instead of a disable:

- budget = `MNEME_MANDATORY_REASONING_BUDGET` (default 2000; we run 8000), capped at
  `max_tokens/2`.
- Change it in `start_proxy.sh` and restart.
- To opt a reasoning model fully in: `sampling.reasoning_enabled: 1`.

## How to switch a model, correctly (the checklist)

1. `model:` — the new model id (provider-qualified if needed).
2. `model_template:` — point at the matching template, or delete the line for neutral.
3. `sampling:` — confirm no leftover penalties from the previous model
   (`frequency_penalty` / `presence_penalty`).
4. If switching *providers* too: `backend.type` (`openai` for any OpenAI-compatible
   host, `openrouter` for OpenRouter), `backend.provider`, and confirm `start_proxy.sh`
   isn't hardcoding `MNEME_BACKEND`.
5. Restart, then `<<SETTINGS>>` to confirm.

## Switching providers (e.g. OpenRouter → DeepSeek direct)

The chat model can run on a different vendor than the embedder/labeler. To move chat to
DeepSeek direct while keeping embed/label on OpenRouter:

```yaml
backend:
  type: openai            # openai = any OpenAI-compatible host
  provider: deepseek
model: "deepseek-v4-pro"  # the id from DeepSeek's own /models
embed_model: "qwen/qwen3-embedding-8b"
label_model: "meta-llama/llama-3.2-3b-instruct"
```

`DEEPSEEK_API_KEY` must be in the env file the start script sources. Watch for the
generated `start_proxy.sh` hardcoding `MNEME_BACKEND` — that overrides `backend.type`
and is a known foot-gun.

## The issues we hit (history, so you don't re-live them)

- **GLM-5.3 word salad** → `temperature 1.0` + the `glm5.3-loop` template
  (frequency 0.7 / presence 0.3).
- **DeepSeek-V4 early-end** → it wasn't in `_MANDATORY_REASONING_MODELS`, so it got
  `reasoning.enabled=false` → `think=0` → bare tool calls. Fixed by adding it to the
  mandatory list + neutral penalties.
- **A "reasoning-strip" fix once shipped with invalid template keys** (`frequency_penalty`
  in `sampling`, `strip_reasoning` in `models`) that the validator rejected — the
  feature was dead code. If a template key silently does nothing, check it against
  `ALLOWED_SAMPLING_KEYS` / `ALLOWED_MODEL_KEYS` in `proxy/mneme/templates.py`.
- **Built-in code defaults once shipped `frequency_penalty 0.5` / `presence_penalty 0.3`**
  (GPT-family values) that leaked to every model. Now 0.0 — model-specific penalties
  belong only in that model's template.

---

## Maintenance & future work

### Maintenance requirement: model info goes stale

`model_templates.yaml`, `_MANDATORY_REASONING_MODELS`, and `PROVIDER_CATALOG` are
hand-curated. **They are wrong the moment a new model ships.** When you add or adopt a
model, update all three together:

1. Add a template with its known-good settings (or confirm neutral defaults are right).
2. If it's a reasoning model, add its slug to `_MANDATORY_REASONING_MODELS`.
3. If it's a new provider, add it to `PROVIDER_CATALOG` (base_url + key_env).

Treat this as a standing requirement, not a one-off. A model with no template and no
mandatory-reasoning entry silently gets neutral defaults + `reasoning.enabled=false` —
the exact combination that produces the early-end failure. New models release constantly;
this table is only as good as the last time someone updated it.

### TODO: auto-select the template by model slug

Currently `model_template:` is pinned manually; changing `model:` doesn't follow it —
the root cause of the word-salad/early-end trap this doc exists to prevent. The intended
fix is to make template selection automatic (match the model slug, like
`_MANDATORY_REASONING_MODELS` already does), with a neutral fallback and a visible
"no known-good settings" notice in `<<SETTINGS>>`. Not yet implemented; recorded here as
the standing improvement.

