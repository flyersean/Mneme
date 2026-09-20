# Provenance & chunk-lifecycle plan

Design agreed 2026-09-20. Build order matters — do 1 and 2 before 4, because
deletion without lineage tracking means you cannot tell what you would orphan.

Two goals:

- **A.** Bulk-remove chunks by source/time, softly, with an explicit manual purge later.
- **B.** Track which chunks were derived from (or produced in the context of) a
  hallucination, so downstream contamination can be found after the fact.

---

## Current state (verified, not assumed)

**`source` is a CATEGORY, not a model name.** Values in use:

    'model'            anything containing model output
    'tool:terminal'    a tool result
    'page:<domain>'    a fetched page
    'input'            a file handed to the model (swarm read_dir)
    'user'             the user's own words

The model name is **not stored on the chunk** — only in the log line. So a filter
like "chunks from gemma4" is not expressible until the schema records the model.

**`derived_from` exists but is NEVER populated.** `record_provenance()` in
`proxy/mneme/curation.py` is called from nowhere (verified by grep). Consequences:

- `detect_self_confirmation()` can never fire on real data
- the `[SELF-CONFIRMED ...]` injection label never appears
- tests pass only because they set the field directly

This is the silent-no-op class this project has been bitten by repeatedly: a feature
that looks present and is inert. Treat item 1 as a BUG FIX, not a feature.

**Deletion today:** only `/reset` (wipes everything). A raw
`DELETE FROM chunks` corrupts three things and the failure is invisible until
retrieval misbehaves:

- FAISS index keeps the vector -> phantom hits / crashes on a missing id
- strategies reference `source_chunk` -> orphaned learning records
- curation log `derived_from` may reference the removed chunk

The model has `bash`, so it *could* run a raw DELETE and get a successful exit code.
That is the worst failure shape. It needs a real tool instead.

---

## 1. Wire up derived_from (BUG FIX — do first)

Parse `[source: mem_XXXX]` citation tags out of a chunk's messages at archive time
and write them via `record_provenance()`.

- The proxy already recognises those tags for the fabricated-citation check, so the
  parsing concept exists — it needs pointing at the archive path.
- Where: the archive path (`_archive_single_chunk` / the staging flush), after the
  chunk row is inserted and its id is known.
- Guard against self-citation and citation of ids that do not exist (the curation
  layer already tolerates missing parents — keep that).
- Test: archive a chunk whose messages cite an earlier chunk id; assert
  `derived_from` contains it and that `detect_self_confirmation` now fires when the
  cited chunk is model-generated with no independent source.

## 2. Record injected_chunk_ids (the reliable contamination signal)

`derived_from` relies on the model *citing* what it used — and models paraphrase
without citing all the time. The proxy, however, **knows** which chunks it injected
into each turn, because it decides that itself.

Record on each archived chunk the set of chunk ids that were in context when it was
produced. Then a hallucination caught later gives a definitive list of contaminated
turns with **no model cooperation required**.

- This is the more robust of the two signals; build both.
- Where: `build_context()` already computes the injected set — expose it to the
  archive path (a module-level "last injected ids" is enough, or thread it through).
- Schema: `ALTER TABLE chunks ADD COLUMN injected_chunk_ids TEXT DEFAULT '[]'`
  (guarded, additive, same style as the curation migration).
- Test: inject a known chunk, produce a turn, archive; assert the archived chunk
  records the injected id.

## 3. Lineage query

Given a chunk id, return what was built on top of it — union of descendants via
`derived_from` and via `injected_chunk_ids`, recursive.

- Endpoint: `GET /memory/lineage/<chunk_id>` returning direct children + full
  transitive closure, with each node's grade/trust/retracted state so the user can
  see the damage shape.
- Must handle cycles (a chunk citing itself transitively) with a visited set.
- This is what answers "a bad fact was saved three days ago — what did it touch?"

## 4. Soft delete + manual purge

### Soft delete (the default action)
Reuse the existing retraction machinery — it already excludes chunks from retrieval
and keeps them labelled in the injection. Add a **bulk** form:

- `POST /memory/soft_delete` with filters: `{source, model, since, until, grade, limit}`
- Sets `retracted='purge_pending'` (distinct from `'user'` / `'model'` so the reason
  is visible in the log and in `<<SETTINGS>>`-style reports).
- Writes a curation-log entry recording the FILTER, the matched ids, and the actor.
- Refuses (or requires an explicit `confirm: true`) above a threshold, so a fat
  filter cannot silently gut memory. Report the count before acting.
- Reversible: `POST /memory/restore` already exists and should clear this state.

### Manual purge (explicit second step)
- `POST /memory/purge` with either explicit ids or a filter.
- Transactionally removes: chunk rows, **their FAISS vectors** (rebuild or remove by
  id — this is the part a raw DELETE gets wrong), and orphaned strategy links.
- Keeps the curation-log entries (an audit trail should outlive the data it describes).
- Guard rail: require `confirm: true` and report exactly what will be removed first.

### Schema addition for the model filter
Your example — "remove chunks from gemma4 saved after 4pm" — needs the model name on
the chunk. Add:

    ALTER TABLE chunks ADD COLUMN model TEXT DEFAULT ''

populated at archive time from the active chat model. Without it, "from gemma4" is
not expressible.

---

## Open question (ask the user)

Soft delete via `retracted='purge_pending'` means those chunks are **hidden from
injection but still retrievable via `/search` and still counted in `/list`**. Is
that right, or should soft-deleted chunks also be hidden from `/search`? Hiding
them everywhere is cleaner conceptually; leaving them searchable makes them
findable during the review before purge. Ask before building.

---

## Ordering rationale

1 and 2 are bug fixes and are prerequisites for meaningful deletion. 3 makes the
lineage visible. 4 is the destructive operation and should only be built once 1-3
exist, so the user can see what a delete would orphan before running it.

Do NOT reorder 4 earlier, and do NOT add a raw-delete path to the model's tools —
that is the failure shape described above.
