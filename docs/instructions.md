# Mneme injected instructions — the map

Every prompt Mneme injects into the model is externalized: it ships with a
hardcoded default (so a fresh clone works) and can be overridden by an editable
file under `$MNEME_CHUNK_DIR/instructions/`:

    instructions/
      default/<name>.txt      — generic override (wins over the code default)
      <model-name>/<name>.txt — per-model override (wins over default/)

A missing or malformed file falls back to the code default and logs loudly — a
bad instruction file degrades to the shipped default, never to a broken
injection. Placeholders use `{{var}}`; an unknown placeholder fails loudly.

## The Tier-1 instructions

| name                    | when injected                                        | vars                          | used_by                |
|-------------------------|------------------------------------------------------|-------------------------------|------------------------|
| `explore`               | user explicitly asks for a NEW method                | —                             | `_explore_directive`   |
| `capability_edge`       | task's problem type is a flagged capability edge     | `{{problem_type}}`            | `_capability_directive`|
| `overcome`              | model is stuck (2 failures / 6 rounds), hard stop    | `{{problem_type}}`, `{{reason}}` | `_overcome_directive` |
| `overcome_build`        | model chose build_tool — one bounded build iteration | `{{iteration}}`, `{{max}}`    | `_build_directive`     |
| `overcome_build_exhausted` | build iterations exhausted — force declare_edge    | `{{max}}`                     | `_build_exhausted_directive` |
| `overcome_reuse`       | model chose reuse_tool — run the existing tool       | `{{tool}}`, `{{path}}`         | `_reuse_directive`     |
| `synthesize_nudge`     | ≥4 successful tool calls w/o a final answer          | `{{count}}`                   | `_synthesize_nudge`    |
| `hard_wrapup`          | ≥6 tool rounds w/o a final answer (tools stripped)   | `{{count}}`                   | `_hard_wrapup_directive`|
| `tool_failure_nudge`    | ≥2 consecutive tool failures (soft, before overcome) | `{{count}}`                   | `_tool_failure_nudge`  |
| `meta_principles_header`| always — header above the meta-principles            | —                             | `_meta_principles_block`|
| `user_preferences_header`| stored preferences exist                             | —                             | `_preferences_block`   |
| `system_directives_header`| saved strategies are injected                      | —                             | `build_context`        |

## How to override one

Create `$MNEME_CHUNK_DIR/instructions/default/overcome.txt` with optional
frontmatter (self-documenting; the loader strips it) and the body:

    # when: injected when the model is stuck and must stop
    # vars: {{problem_type}} {{reason}}
    # used_by: _overcome_directive

    STOP. You have failed on {{problem_type}} because: {{reason}}.
    Decide: build a tool or declare the edge.

The frontmatter lines (`# when:`, `# vars:`, `# used_by:`) are comments to the
loader but are read by the sync test to keep this README honest.
