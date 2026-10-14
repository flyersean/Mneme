# Mneme Skill Library

The optional, curated skill library for Mneme. This directory is **NOT loaded by
default** — it is a store of skills you can install on demand.

## The two-folder model

```
skills/          <- what Mneme actually loads (the "installed" set)
skill-library/   <- this directory: the full optional catalog (inert until installed)
```

- `skills/` ships with a few core skills and is loaded automatically by the proxy
  (`SkillRegistry.load_dir` reads it at startup).
- `skill-library/` is organized by category. Installing a skill = copying its directory
  into `skills/`. Nothing else changes.

## Install a skill

```bash
scripts/install-skill.sh <name> [proxy-port]
```

- `<name>` is the skill's directory name (e.g. `brainstorming`, `docx`).
- Without a port, the skill becomes live on the next proxy restart.
- With a port, it is also POSTed to the running proxy so it's injectable immediately.

To see what's available, read `CATALOG.md`.

## Adding a skill to the library

1. Put the skill at `skill-library/<category>/<name>/SKILL.md` (category is a grouping:
   `engineering`, `documents`, `web`, `meta`, ...).
2. It must use the Mneme SKILL.md format (frontmatter `name` + `description`, plus
   optional `tags`/`requires`/`tools`/`verify`/`failure_modes`; then a markdown body).
3. Record its license and attribution — both in the SKILL.md body AND in `CATALOG.md`.
   See the licensing rules below.

## Licensing rules (mandatory)

- **MIT / Apache-2.0 / BSD / ISC / CC0** — safe to include, with an attribution line in
  the SKILL.md body (e.g. "Adapted from <source> — <license>, Copyright <year> <holder>").
- **Proprietary / "all rights reserved" / no license / governed by a vendor's ToS** — do
  NOT copy the text. Self-author an equivalent skill in your own words, or skip it. Never
  commit a proprietary skill to this repo.
- Every skill must have a `license` entry in `CATALOG.md`. Skills authored for Mneme are
  covered by the repo's MIT LICENSE; mark them "MIT (this repo)".
