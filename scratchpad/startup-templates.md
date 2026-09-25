# Startup templates — save + replay the entire setup

Status: idea (pinned)
Date: 2026-09-24

## Problem

The setup wizard walks through ~10 questions (backend, chat/embed/label models,
model template, port, inject, hot-reload, MCP servers, Pi, DB path). Recreating a
known-good setup means re-answering them all by hand.

## Idea

A "startup template" snapshots the ENTIRE setup so it launches with one choice:

1. Wizard start: "Use a startup template?" → pick → fast-forward (or pre-fill)
   the remaining steps.
2. Templates page: "Save current setup as template" (snapshot the running
   instance once it's tested and working).

## What it captures

The wizard's full answer set, not just generation settings (that's what
`model_templates.yaml` already covers):

    backend, chat model + ctx, embedder, labeler, model_template,
    port, inject, hot_reload, mcp_servers, pi, memory_dir (see decisions)

## Distinction from model templates

- **Model templates** = generation settings (sampling / timeouts / per-model /
  modelfile), a runtime defaults layer merged into config.
- **Startup templates** = a wizard-time replay of the whole setup.

Different schema, different merge semantics, different lifecycle. Keep them in
separate namespaces — a startup template jammed into `model_templates.yaml`
would be rejected by its validator, and for good reason.

## Notes / facts

- The wizard already persists ~90% of a snapshot (`setup_config.json` +
  `instances/<port>/mneme.yaml`). This feature is mostly "name it, save it,
  replay it."
- Secrets: the OpenRouter key lives in `~/mneme/env`, never in config — a
  template must reference it, not embed it.
- Model availability: replaying an Ollama template must still pull the models.
- Pi is a side-effect (Node install), not a config line; replay re-runs
  `setup_pi`.
- Separate catalogue file (e.g. `setup_templates.yaml`) next to the shared DB,
  with its own fail-loud validation (unknown keys error, not silently drop).

## Key decisions / open questions

- **Name:** "startup template" vs "setup template". Avoid "session" — it
  collides with the DB's conversation-session field.
- **DB path:** keep OUT of the template (ask fresh at load — portable across
  machines) vs store-and-override. Recommend ask-fresh.
- **Launch UX:** true one-choice (skip all steps) vs pre-filled wizard (confirm
  each). Pre-filled = less code + keeps confirm-before-launch safety.
- **Save source:** `/templates` page (snapshot the running instance) vs also a
  wizard-end "save this setup" step.
