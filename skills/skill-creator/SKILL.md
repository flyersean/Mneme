---
name: skill-creator
description: How to write a good Mneme skill — the SKILL.md shape (frontmatter + body), how retrieval selects it, trigger conditions, numbered steps, pitfalls, and verification. Load when asked to create, improve, or restructure a skill, or when a reusable procedure should be captured so it can be injected on later tasks.
tags: [skills, meta, authoring, procedures, retrieval]
---

# Skill: writing a Mneme skill

A skill is a named, versioned procedure the harness injects when it looks relevant to
the current task. It is NOT a strategy (a strategy is a one-line learned directive
distilled from a bad turn; a skill is a fuller playbook). Skills live on disk at
`skills/<name>/SKILL.md` and are loaded by `SkillRegistry.load_dir` (in
`proxy/mneme/harness/skills.py`). Every edit that changes content is a new version;
old versions are kept in `skill_versions`.

Source of truth: `proxy/mneme/harness/skills.py` (the schema and retrieval logic) and
`skills/swarm-creation/SKILL.md` (the reference example). If this skill and the code
disagree, the code wins.

---

## 1. The SKILL.md shape

```markdown
---
name: <name>            # required — lowercase, digits, _ . - (regex ^[a-z0-9][a-z0-9_.-]{0,63}$)
description: <text>     # required — this is what retrieval scores against; make it precise
tags: [a, b, c]         # optional list — extra retrieval tokens
requires: [other-skill] # optional — those skills get injected alongside this one
tools: [bash, write]    # optional — the tools this skill assumes
verify: [check, ...]    # optional — how to confirm the skill's own work
failure_modes: [...]    # optional — known ways it goes wrong
---
# body (markdown)
```

Only `name` and `description` are required. Fill the list fields when they genuinely
help; an empty optional field is better than a wrong one.

## 2. Ground rules

- **General, or Mneme-tuned.** A skill must never teach a task in terms of another
  tool's internals (Hermes, OpenCode, Jan). If a procedure is only meaningful inside
  one of those, rewrite it against Mneme's actual toolset: `search_memory`, `read_file`,
  `fetch_url`, `web_search`, `bash`, `write`, `list_tools`/`read_tool`,
  `flag_bad_memory`/`clear_bad_memory_flag`, `start_run`/`inspect_run`, `search_skills`.
- **The description is the retrieval hook.** Retrieval is lexical overlap on
  name/description/tags (or embedding cosine when an embedder is bound). A vague
  description means the skill is never selected. Put the *when-to-use* trigger and the
  *what-it-does* in the first sentence.
- **The body teaches the procedure, not the motivation.** Numbered steps with exact
  commands win over prose. If a step has a command, show it.
- **Every skill needs a verification section** — how would you prove the skill was
  followed correctly? A skill with no way to check its own work is a story, not a skill.
- **Pitfalls are the high-value half.** A "known failure modes" section is what makes a
  skill outperform a model's default guess. Mine your actual failures for it.
- **Code is the source of truth.** If the skill describes behavior that the code
  implements differently, the code wins — and the skill should be fixed to match.

## 3. Procedure

1. **Name the trigger.** Write down the exact user phrasings / situations that should
   load this skill ("when asked to X", "when Y fails"). These words go into the
   `description` so lexical retrieval finds them.
2. **Write the description first, then check it.** Imagine the search query the model
   will issue. Does this description share tokens with it? Tighten until it does.
3. **Draft the body** with these sections, in this order:
   - One-paragraph definition of the thing being taught (and its "source of truth").
   - Ground rules that must never be broken.
   - Numbered procedure with exact commands.
   - Pitfalls / failure modes.
   - Verification checklist.
   - (optional) templates / worked examples.
4. **List `requires` only for real dependencies.** `requires` pulls another skill in
   every time this one is selected — don't use it for "nice to have".
5. **Save to `skills/<name>/SKILL.md`.** The directory name and the `name:` field must
   agree (the loader falls back to the directory name, but keep them identical).
6. **Validate:** the name matches `^[a-z0-9][a-z0-9_.-]{0,63}$`, the description is
   non-empty, frontmatter is valid YAML, and every command in the body actually runs.

## 4. Pitfalls

- **Description too vague** ("general advice about debugging") → never retrieved. Be
  concrete about the trigger.
- **Copying another tool's skill verbatim.** `search_files`/`read_file`/`delegate_task`
  are Hermes tools; Mneme has `bash` + `read_file` + `start_run`. A verbatim copy
  references tools the model doesn't have and produces confident wrong behavior.
- **No verification section.** A procedure with no check is unverifiable; add one.
- **A skill that restates the obvious.** If the body would be identical to what a model
  writes unprompted, the skill adds retrieval value only if the *pitfalls* are non-obvious.
  Focus effort there.
- **Drift.** A skill describing code that changed becomes a lie that convinces future
  sessions. When you find drift, fix the skill in the same change as the code.

## 5. Verification checklist

1. `name` matches the regex and equals the directory name.
2. `description` names the trigger and the action in one sentence.
3. No other-tool tool names in the body (search for `search_files`, `delegate_task`,
   `hermes`, `opencode` etc.).
4. Every command in the body runs as written (or is clearly a placeholder the user sets).
5. A pitfalls section exists with at least one non-obvious failure mode.
6. A verification section exists.
