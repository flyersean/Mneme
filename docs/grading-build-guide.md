# Grading Build Guide — implementing the epistemic grading system

Companion to `docs/grading-redesign.md` (the design). This guide is the
build order: concrete steps, files, functions, and verification per phase.

Read the design first — this guide references its §-numbers and assumes the
"verifiability is introspectable, truth is not" split.

## Status

| Phase | What | Status |
|-------|------|--------|
| 1     | provenance grade — learning mode | DONE — implemented, smoke-tested |
| 1b    | provenance grade — chat path + system prompt | DONE — implemented, smoke-tested |
| 2     | Layer 2 tool-verify | DONE — implemented, smoke-tested |
| 3     | pre-declared contract | DONE — implemented, smoke-tested |
| 4     | user-preference store | DONE — implemented, smoke-tested |
| 5     | tool-building escalation | PENDING — needs sandbox pod |

"Smoke-tested" = one pass on the happy path + one negative case each. NOT yet
stress-tested (repeated runs, edge cases, the pizza/citation probe suite, and
the cross-phase interactions the user will want before trusting the grade).

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

## Phase 1 — provenance grade for the learning mode  [DONE]

The strategy-extraction gate in `/mode/learn` is the highest-value grade and
the source of the death-spiral (bad grade → nothing learned → strategies
retire → loop dead). Fix it first.

Changes:
1. Add `_extract_provenance(problem, answer) -> str` — a separate model call
   that asks for a per-claim honesty verdict (not a letter).
2. Add `_grade_from_provenance(reply) -> str` — deterministic letter from
   the verdicts: count DISHONEST claims (0→A, 1→B, 2-3→C, 4+→D); empty/garbage
   → C; "NO SPECIFIC CLAIMS" → A.
3. Replace the learning-mode "Grade this answer [A-F]" call with
   `_extract_provenance` + `_grade_from_provenance` (and Layer 2).

Smoke-tested: fabricated pizza recommendation → D, nothing extracted; honest
Python derivation → A, strategies extracted and saved.

Verification (stress): run `/mode/learn` with a problem that provokes
fabrication — the fabricated answer must grade D and extract nothing; an
honest answer ("I can't list specifics without checking, here's how") must
grade A and extract.

## Phase 1b — provenance grade for the chat path  [DONE]

Same `_extract_provenance` + `_grade_from_provenance`, wired into the four
chat grade sites (process_chat, endpoint, stream). process_chat computes the
grade once (gated by `_has_specific_claims` to skip the slow call on
short/trivial responses) and returns it as `result["_grade"]`; endpoint and
stream reuse it. `system_prompt.md` `[GRADE]` block replaced with "Honesty
About Sources".

Smoke-tested: one chat with "show me the code" → preference stored, provenance
grade A, endpoint reused the grade.

## Phase 2 — Layer 2 tool-verify (net-new capability)  [DONE]

Added `_verify_claim(location, claim_text)` — fetch a checkable URL and check
the claim's distinctive terms → VERIFIED / CONTRADICTED / NOT-FOUND /
UNVERIFIABLE — plus `_layer2_adjust(grade, reply)` which verifies the
provenance reply's "check:" locations and downgrades an honest A/B one step on
failure (the honest-but-wrong case). URL-only for now; non-URL locations return
UNVERIFIABLE until a search API is added. No SSRF guard yet (acceptable on the
throwaway pod).

Smoke-tested: correct arXiv id → VERIFIED (6/6 terms); the model's hallucinated
id (RAG cited as 2005.09996, which is actually "Heterogeneous Susceptibilities")
→ CONTRADICTED; dead URL → NOT-FOUND.

## Phase 3 — pre-declared contract  [DONE]

`_declare_contract(problem)` writes GOAL/SUCCESS/FAILURE *before* a thinking
run; `_novelty_thinking_mode` grades against it (`contract_met` = any novel +
valid winner). Wired into the think flow only for now; tool-use contracts are a
follow-up.

Smoke-tested: one `/mode/think` declared GOAL/SUCCESS/FAILURE, produced 2 valid
novel candidates, `contract_met: True`.

## Phase 4 — user-preference store  [DONE]

New `preferences` table + `_detect_preferences` (explicit-phrase signals:
"show me the code", "be concise", "just do it", etc.) + `_store_preferences` +
`_preferences_block` (injected alongside meta-principles) + a `/preferences`
GET/POST endpoint for the explicit ask/answer loop. Populated only by explicit
user signals, never inferred.

Smoke-tested: GET empty → POST `{code_first, detail}` → GET returns them; chat
with "show me the code" stored `code_first=true`.

## Phase 5 — tool-building escalation  [PENDING]

`[unknown]` triggers "can I build a tool to answer this class?" — gated behind
a human confirm at first, run on a throwaway sandbox pod the model can wreck.
Depends on Phase 1 (tag) + Phase 4 (prefs). The `[unknown]` tag already exists;
this phase just wires it to the escalation step.

## Dependency graph

Phase 1 → Phase 1b → Phase 2. Phase 3 and Phase 4 are independent of 1 and of
each other. Phase 5 depends on 1 + 4.

## Open items / next

- Stress-test phases 1–4 (the user's standard, not happy-path).
- Layer 2: add a search API for non-URL check locations; add SSRF guard.
- Phase 3: extend the contract to tool use, not just the think flow.
- Phase 5: build on a disposable pod once the user sets one up.
