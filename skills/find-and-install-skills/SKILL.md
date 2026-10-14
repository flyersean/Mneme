---
name: find-and-install-skills
description: Find a missing skill and install it into Mneme so it becomes injectable. Use when asked to do something you can't already do, when the user says "find/install a skill for X", or when you suspect a skill exists. Check the Mneme skill library (skill-library/ + CATALOG.md) FIRST; fall back to agenticskills.io and the open agent-skills ecosystem for anything not yet in the library.
tags: [skills, install, discovery, catalog, meta, capabilities, library, agenticskills]
requires: [skill-creator]
---

# Skill: finding and installing a Mneme skill

This is the meta-skill for extending Mneme mid-task. When you need a capability you
don't have, don't stop and hand the user a list — find the skill, adapt it, and install
it so it's injectable from then on.

Mneme only injects skills as-needed (top-k by lexical/embedding overlap), so adding
skills is cheap — a large catalog costs nothing until a skill is actually selected.

## 0. Check the library FIRST

Mneme ships a skill library in the repo at `skill-library/` (NOT loaded by default) with
a manifest `skill-library/CATALOG.md`. Before touching the web:

1. Read `skill-library/CATALOG.md` (or `find skill-library -maxdepth 2 -type d`) to see
   what's available.
2. If the skill you need is there, install it — no network, no conversion:

```bash
scripts/install-skill.sh <name> [<proxy-port>]
```

This copies `skill-library/<category>/<name>/` into `skills/<name>/` (the loaded dir).
Pass the proxy port to also POST it so it's injectable immediately; without it, it goes
live on the next proxy restart.

Only if the library does NOT have the skill, go to the web (§1).

## 1. Fallback: search the open ecosystem (agenticskills.io)

`agenticskills.io` is the directory for the open agent-skills ecosystem: 190+ skills and
190+ MCP servers built around `SKILL.md`, backed by the `skills.sh` index and the
`vercel-labs/skills` CLI (`npx skills`). Use it for skills the library doesn't cover yet.

Search, in order of preference:

1. `fetch_url("https://agenticskills.io/skills")` to browse, or
   `fetch_url("https://agenticskills.io/category/<name>")` for a category (agents, ai-ml,
   backend, database, data-science, design, devops, documents, marketing, mobile,
   official, productivity, security, seo, testing, web-development).
2. `npx skills find "<keywords>"` (needs Node) — searches `skills.sh`.
3. The site's own ⌘K search if you're driving a browser.

Each skill page shows: name/author/category/platforms/stars, an Install block, the raw
GitHub URL, "when to use / when not to use", and the full SKILL.md inline.

## 2. Fetch the skill

```bash
# the raw GitHub SKILL.md (from the page's Manual install block)
curl -sL https://raw.githubusercontent.com/<owner>/<repo>/<branch>/skills/<name>/SKILL.md

# or read the rendered page (Mneme-native)
#   fetch_url("https://agenticskills.io/skills/<slug>")
```

`npx skills add <owner>/<repo>@<name>` installs for Claude/Cursor/Codex — use it only to
fetch content, never as the Mneme install step.

## 3. License gate (do this BEFORE installing)

Read the skill's license line (frontmatter `license:` or a LICENSE file on its GitHub):

- **MIT / Apache-2.0 / CC0 / BSD / ISC** → safe; add an attribution line.
- **Proprietary / "all rights reserved" / no license / vendor ToS** → DO NOT copy. Self-
  author an equivalent in your own words, or skip. Never paste proprietary text into a
  repo that may be shared.

## 4. Convert to Mneme format

Mneme's schema is narrower than the ecosystem's. See `skill-creator` for the full spec;
the essentials:

- Keep `name` (lowercase `[a-z0-9_.-]`, ≤64 chars, == directory name) and `description`.
- Drop `license`, `allowed-tools`, `metadata`, and other non-Mneme frontmatter keys.
- Add `tags:` (5-10 lowercase tokens) and `requires:` where the skill depends on another.
- **Rewrite tool references to Mneme's toolset** (the agent has ONLY these):
  `search_memory`, `read_file`, `write`, `bash`, `fetch_url`, `web_search`,
  `list_tools`/`read_tool`, `flag_bad_memory`/`clear_bad_memory_flag`/`remove_memory`,
  `start_run`/`inspect_run`, `search_skills`.
  Map "dispatch a subagent / Task tool" → `start_run(goal=...)`; Read/Glob/Grep →
  `read_file` or `bash` grep; Write/Edit → `write` or `bash` sed; WebFetch/WebSearch →
  `fetch_url`/`web_search`.
- Delete sections that only make sense in another harness. Keep the methodology.
- Add an attribution line as the first body line.

## 5. Install (durable + immediate) and grow the library

Write the converted skill into the LIBRARY (so it's available to every future session),
then install it:

```bash
mkdir -p skill-library/<category>/<name>
# write SKILL.md to skill-library/<category>/<name>/SKILL.md
scripts/install-skill.sh <name> [<proxy-port>]
```

`install-skill.sh` copies it into `skills/<name>/` (durable) and optionally POSTs it to
the live registry. Then add a row to `skill-library/CATALOG.md` so it's discoverable.

If you can't write to the repo (read-only scope), fall back to writing directly to
`skills/<name>/SKILL.md` and POSTing `/skills` — but prefer the library path so the skill
isn't lost on the next fresh clone.

## 6. Verify it's installed and injectable

```bash
curl -s "http://localhost:<proxy-port>/skills?q=<name>"   # should return the skill
read_file "skills/<name>/SKILL.md"                        # confirm the file exists
```

Confirm the frontmatter parses:

```bash
python3 -c "import sys; sys.path.insert(0,'proxy'); from mneme.harness.skills import parse_skill_md,_NAME_RE; m=parse_skill_md(open('skills/<name>/SKILL.md').read()); n=str(m.get('name') or '<name>').lower(); assert _NAME_RE.match(n); assert (m.get('description') or '').strip(); print('OK', n)"
```

## Pitfalls

- **Skipping the library and going to the web for a skill that's already in
  `skill-library/`.** Check `CATALOG.md` first — it's faster and already converted.
- **Installing a proprietary skill verbatim.** The ecosystem mixes MIT and "all rights
  reserved" skills (Anthropic's skills are proprietary). Check the license; self-author
  when it's not clean.
- **`npx skills add` installs for Claude/Cursor/Codex, NOT Mneme.** It writes to
  `.claude/skills/` etc. Use it only to fetch content.
- **Tool references leak.** A skill saying "use the Read tool" / "dispatch a subagent" is
  describing another harness. Every tool mention must be Mneme's.
- **File installed but not loaded.** `load_dir` runs at startup; a newly written file
  isn't injectable until restart unless you also `POST /skills`.
- **Name mismatch.** `name:` and the directory name must match, and the name must be
  lowercase `[a-z0-9_.-]`, or the loader rejects it.
- **Installing to `skills/` but not the library.** The skill then vanishes on a fresh
  clone. Save to `skill-library/` too and record it in `CATALOG.md`.

## Verification checklist

1. `CATALOG.md` was checked first (or the library was absent and the web was justified).
2. The skill's license was checked and is clean (or you self-authored an equivalent).
3. The SKILL.md has Mneme frontmatter (name + description + tags) and Mneme tool names only.
4. It's saved to `skill-library/<category>/<name>/` AND installed into `skills/<name>/`.
5. `CATALOG.md` has a row for it with license + attribution.
6. `GET /skills?q=<name>` returns it, and the parse check prints OK.
