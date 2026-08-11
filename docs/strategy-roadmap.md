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
4. **Belief evolution with 35B** (half day) — contradiction detection during
   archiving, superseded fact flagging
5. **Thread cards** (deferred) — revisit after 1-4 are solid

Items 1-3 are achievable without schema changes. Item 4 needs a SQLite schema
addition. Item 5 is a research project.
