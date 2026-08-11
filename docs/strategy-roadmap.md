# Strategy System Analysis & Roadmap

Response to Gemini's code review suggestions, filtered through actual codebase
knowledge (read all 2,176 lines of `mneme_proxy.py`, system prompt, PROBLEMS.md,
and multiple code reviews).

---

## 1. Temporal Threading / Belief Evolution

**What Gemini gets right:** Flat vector stores ARE time-blind. FAISS doesn't know
that "Gemma is my favorite" on Monday and "Qwen is my favorite" on Friday are in
tension — both embed similarly because they're both about "favorite model." The
0% cross-conversation scores are real (PROBLEMS.md confirms this).

**What the code already has:** The DB schema already stores `created_at`
timestamps. The proxy injects chunks with timestamps visible to the model
(`2026-08-07T02:40:00`). But the *embedding* itself has no temporal signal —
arctic-embed2 never sees the date.

**What to implement:**

### Temporal Stamping (Layer 1 — Easy Win)
Before embedding, prepend the date to the text:
```
[2026-08-09] user: I switched my favorite model to Qwen
```
vs
```
[2026-08-04] user: my favorite model is Gemma
```
Now arctic-embed2 sees different text, embeddings diverge, and FAISS can
distinguish them. The display text stays the same — only the embedding input
changes. ~5 lines of code in the embedding pipeline.

### Belief Evolution (Layer 2)
Gemini's suggestion to have the 0.5B labeler detect contradictions is wrong.
The 0.5B already misreads numbers (PROBLEMS.md: "180 contact tracers labeled as
10"). Asking it to do logical contradiction detection is asking for garbage.

The right approach: use the 35B asynchronously during archiving:
> "Here are two chunks about the same topic. Does the newer one update or
> contradict the older one?" If yes, flag the old chunk as superseded rather
> than deleting it. This preserves history while preventing conflicting context
> injection.

### Thread Cards (Layer 3 — Deferred)
Requires the 35B to periodically summarize topic clusters. Defer until temporal
stamping + belief evolution are working.

**Verdict:** Temporal stamping — do it now, trivial. Belief evolution with the
35B (not 0.5B) — good target. Thread cards — later.

---

## 2. Strategy Extraction (the core differentiator)

**What the code already does:** The strategy system in `mneme_proxy.py` has a
lifecycle:
- Grade A/B → triggers a "mini-convo" asking the model what worked
- Grade C/D/F → auto-creates boilerplate, FAISS-deduplicated
- Strategies are stored as chunks in the same DB, injected alongside regular memory

Strategies ARE being created and injected. But they're treated as *memory* —
passive records of "here's what happened." They sit in the same FAISS index as
conversation chunks.

**What Gemini suggests that's genuinely better:** Instead of boilerplate records,
extract *operational rules* — imperative directives the model should follow. The
key shift is from descriptive ("I failed because I didn't check the container IP")
to prescriptive ("ALWAYS verify the container IP before routing ports").

**Why this matters:** Models treat memory and instructions differently. A memory
chunk saying "last time I forgot to check the IP" is just another piece of
context. A directive saying "SYSTEM RULE: Verify container IP before port
routing" has epistemic weight — models are more likely to follow it.

**Implementation plan:**

1. After a C/D/F grade, fire an async 35B call: "You graded this response F.
   Extract ONE imperative rule that would have prevented this failure. Be
   specific and operational."

2. Store the rule with a `type: 'strategy'` marker in SQLite, embedded
   separately from conversation chunks

3. On future injections, strategies get their own section at the TOP of context
   — above memory — as directives, not passive records

4. Periodically (every N strategies), run a dedup pass: ask the 35B to resolve
   conflicting strategies into a single coherent directive

**Critical design choice:** Strategies are *directives*, not *memories*. They go
in a different injection slot with higher priority. This is what makes it
different from the current boilerplate approach and from other memory systems.

**Conflict resolution:** Gemini misses that strategies can conflict. If one
strategy says "always verify container IP" and another says "trust Docker's
default network configuration," the model gets conflicting directives. Current
FAISS-dedup only catches near-duplicate text, not logical contradictions. The
periodic dedup pass addresses this.

---

## 3. Dynamic K-Retrieval

**What Gemini gets right:** Static `top_k` is wasteful. Injecting 5 chunks when
they're all at noise-floor level pollutes the 35B's context with noise.

**What the code already has:** Noise floor is calculated at startup. The
`route_query` function uses this for filtering. But `top_k` appears to be static.

**Implementation:** If `best_score - noise_floor > 0.3`, inject more. If
`best_score < noise_floor + 0.05`, inject zero. Configuration change, not
architecture change.

---

## Priority Stack

If the focus is making the strategy system the standout feature:

1. **Temporal stamping** (1 hour) — fix the 0% cross-conversation scores
2. **Strategy extraction as directives** (few hours) — turn [GRADE: F] into
   actionable SYSTEM RULES, not passive records
3. **Dynamic K** (30 min) — stop polluting context with noise-floor chunks
4. **Multi-writer FAISS** (1 hour) — lock + reload per operation, all proxies
   read/write same DB safely
5. **Multi-model mode** (few hours) — multiple proxy instances with different
   models, all reading/writing one shared DB; builds on #4 infrastructure
6. **Learning mode** (half day) — parameter cycling + proxy-driven iteration to
   find novel solutions and develop new tools; extracts strategies from
   A-grade responses (see `docs/learning-critical-modes.md`)
7. **Belief evolution with 35B** (half day) — contradiction detection during
   archiving, superseded fact flagging
8. **Thread cards** (deferred) — revisit after 1-7 are solid
9. **Strategy export/import** (deferred) — share strategies between DBs;
   re-embed on import, FAISS dedup, conflict flagging

Items 1-6 are achievable without schema changes. Item 7 needs a SQLite schema
addition. Items 8-9 are deferred.

---

## Multi-Writer FAISS Architecture

Allow multiple Mneme proxy instances to safely read AND write to a single
shared DB + FAISS index. Accepts the speed trade-off in exchange for
architectural simplicity.

### Approach: Lock + Reload

No background threads, no cache invalidation, no stale reads. Every FAISS
operation loads the index fresh from disk under a file lock:

```
Any FAISS operation:
  1. Acquire fcntl.flock() on /workspace/mneme_chunks/faiss.lock
  2. Load FAISS index from disk  (always current state)
  3. Do the operation  (search or rebuild)
  4. Release lock
```

- **Writes:** lock → write chunks to SQLite → rebuild FAISS on disk → unlock
- **Reads:** lock → load FAISS from disk → search → unlock

Every proxy is equal — all read, all write, they just take turns.

### Performance

| Chunks | Index size | Load time |
|---|---|---|
| 0 | empty | <1ms |
| 100 | ~400KB | ~5ms |
| 1,000 | ~4MB | ~20ms |
| 10,000 | ~40MB | ~200ms |
| 100,000 | ~400MB | ~2s |

Up to ~10K chunks, load overhead (<200ms) is invisible against Ollama
inference latency (2-5 seconds). At 100K+ chunks (months of heavy use),
add periodic index compaction.

### Code Changes (~30 lines)

1. Add `faiss_lock` context manager using `fcntl.flock()`
2. Wrap every FAISS read (search) in: lock → load → search → unlock
3. Wrap every FAISS write (rebuild) in: lock → rebuild → unlock
4. Remove forever-in-memory index — loaded and discarded per operation

### Safety Properties

- `fcntl.flock()` is kernel-enforced — released on process death (no stale locks)
- SQLite WAL mode handles concurrent DB writes (lock only protects FAISS)
- Archiving proxy holds lock during rebuild; searching proxies wait briefly
- All proxies must use the SAME embedding model (enforced by setup script)

### Setup Script UX

First run: normal setup with `MNEME_CHUNK_DIR=/workspace/mneme_chunks`.
Subsequent runs: "Existing memory DB found. Add another proxy instance?"
→ model name + port → writes start script with same chunk dir, different model.

---

## Multi-Model Mode

Run multiple proxy instances with different models, all reading and writing
to a single shared memory DB. Each model contributes its own conversations
and benefits from every other model's stored context. Builds on the
multi-writer FAISS lock+reload infrastructure (#4).

### Architecture

```
Port 8080 → proxy A (qwen3.6-35b, 129K ctx) ─┐
Port 8081 → proxy B (llama3.1-8b,  32K ctx) ─┤
Port 8082 → proxy C (gemma2-27b,    64K ctx) ─┤
                                               ├─ /workspace/mneme_chunks/
Port 8083 → proxy D (deepseek-7b,   32K ctx) ─┤    ├── mneme.db      (shared)
Port 8084 → proxy E (qwen3.6-35b-120k) ───────┘    ├── faiss.index   (shared)
                                                    └── faiss.lock    (shared)
```

All point at `MNEME_CHUNK_DIR=/workspace/mneme_chunks`. All use same
`EMBED_MODEL` and `LABEL_MODEL`. Different `MNEME_MODEL` per instance.
The lock serializes FAISS access — each proxy takes its turn.

### Models as Specialists

Different models contribute different kinds of knowledge:

| Model | Strength | Role |
|---|---|---|
| 35B (129K ctx) | Deep reasoning, strategy extraction | Primary archiver, learning mode driver |
| 35B (32K ctx, fast) | Quick iteration, grading | Learning mode iterations |
| 27B | Good reasoning, faster | Secondary archiver, critical thinking |
| 8B | Fast, cheap | Quick lookups, label verification |
| 7B | Lightweight, experimental | Testing merged prompts, small-model edge cases |

The same conversation topic might get a deep analysis from the 35B and a
quick summary from the 8B — both stored in the shared DB. Future queries
surface both perspectives, weighted by grade.

### DB Growth from Multiple Models

Each model contributing independently means faster DB growth. A rough model:
- 1 model: ~50 chunks/day (moderate use)
- 3 models: ~150 chunks/day
- At ~1KB per chunk (embedded text), that's ~150KB/day, ~5MB/month

The 10K-chunk threshold (where FAISS load time hits ~200ms) is reached in
~2 months with 3 models. Well within the "speed doesn't matter" window.

### Setup Script UX

```
Run 1: Normal setup. MNEME_CHUNK_DIR=/workspace/mneme_chunks.
       Pick model, embed model, labeler. Proxy on port 8080.

Run 2: "Existing memory DB with X chunks found. Add another model?"
       → Pick model (different from existing)
       → Port 8081
       → Embed/labeler locked to existing choices
       → Writes start script with env vars

Run 3+: Same as run 2, next available port.

Result:
  /workspace/start_proxy_8080.sh  (qwen35b)
  /workspace/start_proxy_8081.sh  (llama3.1-8b)
  /workspace/start_proxy_8082.sh  (gemma2-27b)
```

### What the Setup Script Must Enforce

- Same `MNEME_CHUNK_DIR` for all instances (non-negotiable)
- Same `EMBED_MODEL` for all instances (different vector spaces = garbage)
- Same `LABEL_MODEL` for all instances (consistent topic labels)
- Unique ports (auto-assign)
- Different models allowed (that's the whole point)
- `MNEME_INJECT_SYSTEM` per-instance (some models use merged prompts, some don't)

---

## Strategy Export/Import

Share learned strategies between Mneme databases. Enables bootstrapping a new
DB with proven strategies, sharing between users, and backing up just the
strategy layer without exporting all conversation data.

### Export

A SQL query + JSON dump. Strategies are rows with text + metadata:

```json
{
  "exported": "2026-08-11T01:00:00Z",
  "embed_model": "snowflake-arctic-embed2",
  "strategy_count": 47,
  "strategies": [
    {
      "text": "ALWAYS verify container IP before port routing",
      "type": "directive",
      "grade": "A",
      "effectiveness": 0.87,
      "uses": 12,
      "created": "2026-08-07T02:40:00Z"
    }
  ]
}
```

`POST /admin/export/strategies` → returns JSON. Trivial — one SQL query.

### Import

Runs each strategy through the same embedding pipeline as a newly-created
internal strategy: text → embed model → FAISS insert. Vectors are rebuilt
in the target's vector space, so no cross-model compatibility issues.

`POST /admin/import/strategies` with JSON body.

### Import Pipeline

```
For each strategy in import file:
  1. FAISS dedup check: search target DB for cosine_sim > 0.95
     → If match found: update existing strategy's effectiveness score
     → If no match: continue
  2. Conflict check: search for strategies with similar topic but
     contradictory directive (defer to 35B for semantic comparison)
     → If conflict: flag for review, insert with "conflict" status
  3. Embed strategy text with target's embed model
  4. Insert into SQLite + FAISS with "imported" source marker
  5. Carry over effectiveness score but mark as unearned locally
```

### Edge Cases

**Embedding model mismatch.** Export says `snowflake-arctic-embed2`, target
uses `nomic-embed-text`. Import still works because we re-embed with the
target's model. The export's `embed_model` field is metadata for the user,
not a constraint.

**Effectiveness inflation.** An imported strategy with effectiveness 0.95
from 200 uses shouldn't immediately outrank a locally-earned strategy with
0.80 from 10 uses. Imported strategies start with a "confidence discount" —
their effectiveness is scaled down until they prove themselves locally.
Say, imported effectiveness = min(original × 0.7, 0.5). After 5 local uses
with A/B grades, the discount lifts and the original score is restored.

**Circular imports.** DB A exports, DB B imports, DB B exports, DB A
imports — duplicate strategies. The FAISS dedup check (step 1) catches
this. Same text → near-identical vector → cosine_sim > 0.95 → skip.

**Community sharing.** An export file is just JSON. Could be shared via
GitHub gist, a `/strategies` directory in the repo, or a community registry
later. The format is self-describing (embed model, dates, grades) so
consumers know what they're getting.

### API Design

```
POST /admin/export/strategies
  → 200 { exported, embed_model, strategy_count, strategies: [...] }

POST /admin/import/strategies
  Body: { strategies: [...] }  (same format as export)
  → 200 { imported: 12, skipped_dup: 3, conflicts: 2, ... }

GET /admin/strategies
  → 200 { strategies: [...], filters: { type, grade, effectiveness } }
  (Browse/manage strategies without touching the DB directly)
```

### Implementation Complexity: Low
- Export: one SQL query, one JSON response
- Import: reuse existing embed + insert pipeline, add dedup check
- No schema changes needed if strategies already have a `type` column
- Deferred because it depends on strategy directives (#2) being stable first
