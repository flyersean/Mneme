# Provenance & chunk lifecycle — status

Originally a plan (2026-09-20). Rewritten 2026-09-21 to record **what actually
shipped**, because the plan as written now describes an abandoned design and work
that is finished. Where a decision changed during the build, the change is noted —
those are the useful parts later.

---

## What shipped

### 1. Provenance recording (was the critical bug)

`record_provenance()` existed, was tested, and **was called from nowhere**. So on a
live proxy `derived_from` stayed `[]` forever, `detect_self_confirmation()` could
never fire, and the `[SELF-CONFIRMED]` injection label never appeared. The tests
passed only because they set the field directly.

Now wired into the archive path, which parses `[source: mem_XXXX]` citations out of
a chunk's own messages. It reuses `grading._extract_mem_ids` — the same parser the
fabricated-citation check uses — so the two cannot drift.

Verified live on a pod: a real 14B model cited a real chunk and the citation was
recorded and resolved.

**Two bugs found while doing it**, both worth remembering:

- `_INLINE_MEM_RE` was `mem_[a-zA-Z0-9]+`, which stops at an underscore. Real ids
  are digits so it was invisible, but it would truncate `mem_a_b` to `mem_a`.
- Citations were recorded **verbatim**, so a model emitting a truncated or invented
  id created a `derived_from` edge pointing at nothing. Observed live: a 3B model
  cited `mem_1789944977`, which matched no chunk. Fixed — only citations that
  resolve to a real chunk are recorded. A dangling edge is worse than no edge when
  tracing contamination, because it looks like a real lead.

### 2. `injected_chunk_ids` — the reliable contamination signal

`derived_from` needs the model to *cite* what it used, and models paraphrase without
citing constantly. The proxy, by contrast, **knows** which chunks it injected,
because it chose them. Each archived chunk now records that set.

Threaded explicitly from the caller, snapshotted at enqueue time — **not** via a
module global. Archiving runs on a worker thread after the request returns, so a
global would be overwritten by a later turn and stamp a chunk with *another* turn's
context. Wrong provenance is worse than absent here.

### 3. `lineage()` + `GET /memory/lineage/<id>`

Answers "what was built on top of this chunk?", transitively, over both relations:

    cites — that chunk's messages named this one as a source
    saw   — that chunk was in context when this one was produced

Cycle-safe (visited set), depth-limited, and each node carries grade/trust/retracted
state so the damage shape is visible.

### 4. The removed flag + the management page

This replaced the original goal A (bulk delete by source/time with a manual purge).
**What was built instead is a flag, not a deletion** — nothing is ever deleted, so
there is no purge step and none of the orphan hazards the original plan worried
about (FAISS vectors, strategy links, dangling refs) apply at all.

    chunks.removed = 'injectable' (default) | 'removed'

Enforced at one choke point, `load_chunk()`, which is where retrieval results become
usable chunks — so injection and the model's memory search are covered by one rule
rather than by every caller remembering. `/search` excludes removed chunks unless
asked. The management page reads them via `allow_removed=True`.

### 5. The bad-chunk marker

A second, weaker flag: this chunk is *suspected* wrong. It changes nothing about
what Mneme uses. Set by the model (`flag_bad_memory`) or by the user (the button on
the page), and rendered amber for model / red for user so the transcript says who
decided. Clicking toggles; there is no approval step.

Flagged chunks still inject — that is what "changes nothing" means — but they carry
a label so the model knows:

    [FLAGGED as suspected-wrong by the model — treat with suspicion, verify
     before relying on it (reason: ...)]

---

## Decisions that CHANGED during the build

Worth keeping, because each was a deliberate reversal:

**Bulk delete → a flag.** The original plan was selective deletion with a manual
purge. Rejected once it was clear that deletion creates orphans (FAISS, strategies,
lineage) while a flag gets the same practical effect with none of the risk. There is
no purge system, by design.

**The approval flow → nothing.** There *was* a model-proposes / user-confirms /
chunk-retracted flow, with `/memory/proposals` and confirm/deny endpoints. Removed
entirely. It modelled a two-party workflow that does not exist and should not: the
model cannot remove anything, and the page's Remove button is the only thing that
changes what the model sees.

**Tool names were lying.** `retract_memory` / `restore_memory` promised an action the
model cannot take, and the model acted on the promise — it told users "once you
confirm, I will proceed with retracting it". Three separate wordings fed this: the
tool *name*, the tool *description*, and the tool *result text* (which said the flag
was "pending their confirmation"). Now `flag_bad_memory` / `clear_bad_memory_flag`,
with the description and result stating plainly that nothing changes.

**`ignore_removed` config setting → dropped.** Was going to let the model see removed
chunks during review. Unnecessary once the management page did review model-free.

**Stored descendant tags → computed.** The original plan stored `descendant_of` on
each chunk. Not built; the page queries `lineage()` live instead. Removes a whole
class of bug (stale tags, cascade maintenance, un-flag cleanup) for no loss.

---

## What is NOT built

- **Deletion / purge.** Deliberately. Nothing removes rows; `removed` is a flag.
- **Automatic ranking of removal candidates.** The fields exist (`grade`,
  `self_confirm`, `independent_sources`, `trust`), and the page filters on them, but
  nothing scores or recommends. A manual-review tool, not an advisor.
- **`/memory/retract` and `/memory/restore` are still live** but unused by the page
  and unreachable from any model tool. They are the last remnant of the retraction
  model otherwise dismantled. Decide: fold into `removed`, or document them.

## Known limits, stated plainly

- Per-chunk review is **damage limitation, not repair.** You are judging chunks
  against content you may not remember the truth of. The reliable move on a polluted
  DB is the **date filter**: if a bad fact landed at a known time, everything saved
  after it is the contamination window.
- `lineage()` shows what was *influenced by* a bad chunk, not what is *wrong*.
  Content that merely sat in context may be entirely correct. A triage list, not a
  verdict.
- Provenance only records citations that resolve, so a model that misremembers an id
  leaves no trace. The fabricated-citation grader handles that case separately.
