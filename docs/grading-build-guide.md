# Grading Build Guide — implementing the epistemic grading system

Companion to `docs/grading-redesign.md` (the design). This guide is the
build order: concrete steps, files, functions, and verification per phase.

Read the design first — this guide references its §-numbers and assumes the
"verifiability is introspectable, truth is not" split.

## Guiding constraints (from the experiments)

- **No JSON grammar with muse-glimmer.** All extraction is text-format +
  regex. The `to=self` reasoning turn conflicts with Ollama JSON grammars
  ("peg-native format" errors).
- **Separate extraction call, not inline self-tagging.** muse-glimmer does
  not reliably emit `[GRADE]` inline, but a *separate* focused call (the
  Probe 2 "source specificity" shape) reliably produces honest provenance.
- **Grade stays a letter A-F in Phase 1** so the six existing consumers
  (`GRADE_PRIORITY`, indexing gate, strategy extraction, lifecycle,
  telemetry, suspect-grade) keep working unchanged. Phase 1 changes only
  *how the letter is computed*, not its consumers.

## Phase 1 — provenance grade for the learning mode (this session)

The strategy-extraction gate in `/mode/learn` is the highest-value grade and
the source of the death-spiral (bad grade → nothing learned → strategies
retire → loop dead). Fix it first.

Changes:
1. Add `_extract_provenance(problem, answer) -> str` — a separate model call
   that asks for a per-claim honesty verdict (not a letter).
2. Add `_grade_from_provenance(reply) -> str` — deterministic letter from
   the verdicts: count DISHONEST claims (0→A, 1→B, 2-3→C, 4+→D); empty/garbage
   → C; "NO SPECIFIC CLAIMS" → A.
3. Replace the learning-mode "Grade this answer [A-F]" call
   (`_run_learning_mode`, the grade extraction near line 1879) with
   `_extract_provenance` + `_grade_from_provenance`.

Verification: run `/mode/learn` with a problem that provokes fabrication
(e.g. "recommend a pizza place in a small town") — the fabricated answer must
grade D and extract nothing; an honest answer ("I can't list specifics without
checking, here's how") must grade A and extract.

## Phase 1b — provenance grade for the chat path

Same `_extract_provenance` + `_grade_from_provenance`, wired into the four
chat grade sites (process_chat, endpoint, stream, and the learning-mode
replacement). This adds ~1 call/turn latency; gate it: only run when the
answer contains specific claims (a cheap pre-filter). Update the
`system_prompt.md` `[GRADE]` block to the provenance framing.

## Phase 2 — Layer 2 tool-verify (net-new capability)

The proxy has no web/network verification today. Add `_verify_claim(location)`
— fetch/search the named checkable location, return VERIFIED / NOT-FOUND /
CONTRADICTED. Feeds a Layer-2 adjustment on top of the Layer-1 letter: an
honest-but-wrong sourced claim (Layer 1 A, verified CONTRADICTED) becomes a
different, lower outcome. This is the biggest new component; defer until
Phase 1 is proven.

## Phase 3 — pre-declared contract

`to=self` goal/success/failure before tool use and novel-thinking runs; grade
against the contract ("this should be one easy call" vs nine retries).
Independent of Phase 1; can be built in parallel.

## Phase 4 — user-preference store

New `preferences` table + an ask-the-user loop + injection in `build_context`.
Populated only by explicit user answers (detail level, code-first vs
explanation-first, action vs planning). Independent; parallel with Phase 3.

## Phase 5 — tool-building escalation

`[unknown]` triggers "can I build a tool to answer this class?" — gated behind
a human confirm at first. Depends on Phase 1 (tag) + Phase 4 (prefs).

## Dependency graph

Phase 1 → Phase 1b → Phase 2. Phase 3 and Phase 4 are independent of 1 and of
each other. Phase 5 depends on 1 + 4.
