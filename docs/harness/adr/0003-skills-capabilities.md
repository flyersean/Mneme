# ADR 0003 — Skills, strategy versions, tool capabilities, focused context (Phases 3–4)

Status: accepted (2026-09-28).

1. **Strategy history is append-only, and lives in `mneme.db`** beside `strategies`.
   The history table is `strategy_versions`. Every save snapshots two rows: the row
   about to be replaced (`superseded`) and the new row (`saved`), each with its
   actor and reason. The table itself remains `INSERT OR REPLACE`, so current
   readers are unchanged. New provenance columns: `created_by`, `derived_from`,
   `validated_by`.
2. **Skills live in `harness.db`. Files are a *source*; the DB holds the versions.**
   - Loading an unchanged `SKILL.md` is a no-op.
   - A changed file, a runtime upsert, or a restore each create a new version.
   - A restore never rewrites history.
   - Composition works through `requires`, which is expanded at selection time.
3. **Selection is lexical by default.** It needs no embedder and costs nothing
   per step. An embedder can be bound later without changing callers. Only the top
   2 skills and top 4 tools reach the prompt (capability per unit of context).
4. **Tool permission levels are declared metadata. Unknown and MCP tools are
   `system`.** A run's grant (`permissions.grant`) is enforced inside `process_chat`
   at two points:
   - ungranted tools are not offered to the model;
   - ungranted tools are not executed.

   A call the model makes anyway falls through to "unexecutable", so the step fails.
   The default grant is **unrestricted**, the same power as chat; profiles narrow it.
5. **`_end()` is the single terminal-state choke point**, with `on_finish` hooks.
   Skill outcome stats are the first hook. Reflection and artifact capture attach
   the same way. A hook error is recorded as an event and never breaks a run.
