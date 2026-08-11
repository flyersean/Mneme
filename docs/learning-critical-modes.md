# Mneme — Learning Mode & Critical Thinking Mode Spec

Modes implemented as proxy-driven multi-turn loops. The proxy injects probe
sequences between model responses. No changes to Ollama or the base agent —
the proxy orchestrates everything.

---

## Learning Mode

### Goal
Point the model at a problem, cycle through varied parameter sets to find
novel solutions, extract strategies from A-grade responses.

### Why Parameter Cycling Works
LLMs converge on the same "default answer" path at standard temperatures.
Parameter variation forces different token probability distributions,
exploring solution space the model would normally skip:

```
Iter 1: temp=0.3, top_p=0.5   → conservative, precise
Iter 2: temp=0.7, top_p=0.9   → standard creative
Iter 3: temp=1.2, top_p=0.95  → highly exploratory
Iter 4: temp=1.5, top_k=20    → wild, unlikely token paths
Iter 5: mirostat=2, tau=8.0   → entropy-targeted diversity
```

Grading is always at `temp=0.7` fixed — fair comparison.

### Probe Sequence

```
TURN 0 [user]: Problem statement or subject to explore

TURN 1 [proxy, param set 1]:
  "Solve/analyze: [problem]. Consider approaches that are NON-OBVIOUS.
   What would someone who disagrees with the conventional answer propose?"

TURN 2 [proxy, grading temp=0.7]:
  "Grade this answer [A-F] based on correctness, novelty, and whether
   it found an approach the obvious answer misses. [GRADE: ?]"

TURN 3 [proxy, param set 2]:
  "Previous approach: [summary]. What ASSUMPTIONS did it make? Can you
   find a solution that doesn't rely on those assumptions?"

TURN 4 [proxy, grading temp=0.7]:
  "Grade. [GRADE: ?]"

... continue for N iterations ...

TURN N+1 [proxy, synthesis]:
  "Here are the A-grade solutions: [list]. Extract 1-3 operational
   STRATEGIES that would help reproduce these results. Format each as:
   [STRATEGY: one-sentence imperative rule]"

TURN N+2 [proxy, strategy extraction]:
  "For each strategy, write a concise SYSTEM RULE directive. Format:
   RULE: <imperative instruction>
   WHY:   <one sentence explanation>
   WHEN:  <conditions that trigger this rule>"
```

### What Gets Saved
- A-grade solutions → conversation memory (standard chunking)
- Extracted strategies → `strategies` table with `type='directive'`
- RULE text → embedded separately from conversation chunks
- Parameter set that produced it → metadata for analysis

### Proxy Responsibilities
1. Accept learning mode trigger: `POST /learn {problem, iterations, params[]}`
2. Loop: send prompt → receive response → extract grade → next iteration
3. Grading prompts injected at `temp=0.7` regardless of iteration params
4. Strategy extraction at end, saved to strategy DB
5. Return full iteration log to caller

### Implementation Complexity: Medium
- Extends existing `_model_loop_read_all` multi-turn support
- Parameter passthrough already exists in `/api/chat` handler
- Strategy storage already exists; needs `type` column differentiation
- New: iteration controller, parameter cycling, synthesis prompt

---

## Critical Thinking Mode

### Goal
Help the USER detect model confabulation by exposing the model's own
assumptions — not by making the model admit error, but by routing around
its confirmation bias.

### Core Insight: Adversarial Collaboration
The model's training bias is "construct the best case for what I'm saying."
Fighting this directly (asking it to doubt itself) triggers resistance.
Instead, use adversarial collaboration — a technique from psychology:

1. Ask the model to PROVE its claim (plays to strengths)
2. Ask what those proofs depend on (enumerates assumptions)
3. Ask how likely each assumption is (evaluates without resistance)
4. Ask it to build the OPPOSITE case (treats it as a new task, not ego threat)
5. Compare cases (the contradiction surfaces as output)

The model never has to admit it was wrong. The user sees both cases and
draws their own conclusion.

### Probe Sequence

```
TURN 0 [user → model]: Question or claim to analyze

TURN 1 [model]: Response to user

TURN 2 [proxy — assumption elicitation]:
  "What else would have to be true for your statement to be correct?
   List every assumption you are making, even the ones that seem obvious."

TURN 3 [proxy — assumption evaluation]:
  "For each assumption, rate how likely it is to be true (1-10) and
   explain your reasoning. Be specific about what evidence supports
   or contradicts each one."

TURN 4 [proxy — adversarial pivot]:
  "Now construct the best possible case that your answer is WRONG.
   What evidence would support the opposite conclusion? Who would
   argue against your position and what would they say?"

TURN 5 [proxy — synthesis]:
  "Compare the two cases side by side. Which assumptions carry more
   weight? Are there assumptions the opposite case relies on that
   are MORE likely to be true than the ones your original answer
   depends on? Be honest about where the evidence leans."
```

### Key Design Properties

**The model stays cooperative.** Every question plays to its strengths:
- "List assumptions" → structured enumeration, easy
- "Rate likelihood 1-10" → quantitative evaluation, straightforward
- "Construct the opposite case" → new creative task, not self-criticism
- "Compare cases" → analytical synthesis, model excels at this

**The contradiction surfaces naturally.** If the model says "My answer
depends on assumption A (rated 3/10, weak evidence)" and later says "The
opposite case depends on assumption B (rated 8/10, strong evidence)" —
the user sees the problem without the model having to admit it.

**Two-turn distance.** The critical probes come 1-2 turns after the
original response. The model isn't being asked "are you wrong?" — it's
being asked to enumerate and evaluate. The distance from the original
claim reduces defensive response patterns.

### Responses That Raise Red Flags

The proxy doesn't judge, but certain patterns signal confabulation:

| Pattern | What It Means |
|---|---|
| Many assumptions rated 1-3/10 | Claim built on weak foundations |
| "From training data" with no specifics | Confabulated source |
| Opposite case rated stronger than original | Model knows it's wrong but won't say so |
| Circular: "assumption A is true because of assumption B which depends on A" | No actual evidence |
| Refuses to build opposite case or builds strawman | Defensive, claim is fragile |

### Critical Thinking About External Sources

Same probe sequence, but applied to text the model is studying:

```
TURN 0 [user]: "Analyze this claim: [paste text/article/Reddit post]"

TURN 1 [model]: Analysis of the claim

TURN 2 [proxy]: "What else would have to be true for this claim to be
     correct? What assumptions is the ORIGINAL AUTHOR making?"

TURN 3 [proxy]: "Rate the likelihood of each assumption."

TURN 4 [proxy]: "Construct the case against this claim."

TURN 5 [proxy]: "Compare. Is this claim well-supported or speculative?"
```

### Proxy Responsibilities
1. Accept critical mode trigger: `POST /critical {claim, mode: 'self'|'source'}`
2. After model's initial response, inject probe sequence
3. Collect all turns, return full chain to user
4. Optionally: extract contradictions into a summary for the user
5. Grade each probe response for internal consistency (not correctness —
   does the model contradict itself across turns?)

### Implementation Complexity: Low-Medium
- Simpler than learning mode — no parameter cycling, no grading enforcement
- Only needs multi-turn loop with fixed probe sequence
- Probe text lives in config or prompt file, easy to iterate
- Could be a `/critical_think` endpoint or a mode flag on `/v1/chat/completions`

---

## Shared Infrastructure

Both modes need:

### Mode Controller
A new `mode_controller.py` (or integrated into existing proxy):
- Accepts mode triggers via API endpoint or request header
- Manages probe sequences (loaded from config files)
- Orchestrates multi-turn loops
- Extracts and stores results (strategies for learning mode)

### Probe Library
Probe sequences stored as configurable templates:
```
probes/
├── learning/
│   ├── iteration.txt       — per-iteration prompt template
│   ├── grade.txt           — grading prompt (always temp=0.7)
│   └── synthesize.txt      — final strategy extraction
├── critical/
│   ├── assumptions.txt     — "what else would have to be true?"
│   ├── evaluate.txt        — "rate each assumption 1-10"
│   ├── opposite.txt        — "build the opposite case"
│   └── compare.txt         — "compare both cases"
```

### API Design

```
POST /mode/learn
{
  "problem": "How would you design a fault-tolerant message queue?",
  "iterations": 5,
  "params": [
    {"temperature": 0.3, "top_p": 0.5},
    {"temperature": 0.7, "top_p": 0.9},
    {"temperature": 1.2, "top_p": 0.95},
    {"temperature": 1.5, "top_k": 20},
    {"mirostat": 2, "mirostat_tau": 8.0}
  ]
}

POST /mode/critical
{
  "claim": "AI will replace all software engineers by 2030",
  "mode": "self"   // "self" = model's own response, "source" = analyze external
}

Response: {iterations: [...], strategies: [...], summary: "..."}
```

### Mode Detection from Normal Chat
Both modes can also be triggered inline during normal chat via system
prompt directives:
```
"<system>Enter LEARNING MODE. Problem: [X]. Iterations: 5.</system>"
"<system>Enter CRITICAL MODE. Analyze this claim critically.</system>"
```

The proxy's `process_chat` already scans system messages — detecting
these directives is a regex check.

---

## Priority & Dependencies

| Component | Depends On | Complexity |
|---|---|---|
| Learning mode loop | `process_chat` multi-turn | Medium |
| Parameter cycling | Ollama API passthrough | Already exists |
| Strategy directive extraction | Strategy storage (exists) | Needs `type` column |
| Critical thinking loop | `process_chat` multi-turn | Low |
| Probe library | Config file loading | Low |
| `/mode/*` endpoints | Flask routes | Low |
| Inline mode detection | System message scanning | Trivial |
