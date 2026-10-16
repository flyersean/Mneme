---
name: mneme-schema-migrations
description: Safely change Mneme's shared SQLite schema (chunks table etc.) under concurrent multi-proxy access using versioned + serialized migrations.
tags: [mneme, schema, migration, sqlite, shared-db, concurrency]
---

# Mneme schema migrations (shared-DB safe)

## When to use
Adding/removing a column or table on the chunks DB, or fixing "no such column: X" crashes that come from divergent schemas across proxies sharing one DB.

## Core pattern
The chunks DB is shared by multiple proxies that can start concurrently. Never use an ad-hoc `db.executescript` + `for ALTER ... try/except: pass` loop. Use `_SCHEMA_MIGRATIONS` (a list of `(version, description, [SQL...])` tuples) plus a `schema_migrations` table in `proxy/mneme_proxy.py`.

## Steps
1. Append a new `(N, "desc", [sql...])` tuple to `_SCHEMA_MIGRATIONS`. Never fold a branch-specific column into the base `CREATE TABLE` — give it its own version (dev's `instance_origin` is version 3, not part of version 1).
2. Each migration runs exactly once, under `BEGIN IMMEDIATE` (SQLite's cross-process write lock), recorded in `schema_migrations`.
3. Treat "already exists"/"duplicate column" as idempotent no-ops (skip). Any OTHER error is fatal + rollback — never silently swallow it.
4. `_migrate_schema()` runs at startup before any other schema work.

## Pitfalls
- Shared memory (multiple proxies -> one DB) is CORE to Mneme — serialize, don't block, concurrent access.
- The `no such column` crash class is exactly what this kills: a dev-branch proxy half-migrating a main DB.
- Keep branch divergence in separate versions so main/dev can share a DB safely (main opening a dev DB ignores dev's extra version; dev opening a main DB applies its own).

## Verify
- Use `bash` to boot the module against a temp DB (`python3 -c "..."` with `MNEME_DB_PATH=/tmp/x.db`) and confirm the full schema + versions record.
- Confirm an existing DB copy preserves chunk count + vectors exactly (idempotent, zero loss).
- Read the code with `read_file` around `_SCHEMA_MIGRATIONS` and `_migrate_schema()`.
