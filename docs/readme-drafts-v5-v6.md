# README drafts — items 5 & 6

For review before anything goes into README.md. Two sections:

- **5. Opening** — keeps the AI-authorship disclosure, drops "vibe-coded"
- **6. Agent workflows & swarms** — promotes the swarm from an Extensions
  footnote to a real section, and states the small-model hypothesis honestly

---

# 5. THE OPENING

Current:

```
# Mneme — conversational memory proxy

> ⚠️ **Work in progress — vibe-coded and under active development.** This works,
> but it may have bugs and it changes as it's developed. Expect rough edges and
> occasional breakage. Feedback and bug reports welcome.

## Quick start
...
```

Proposed:

```markdown
# Mneme — persistent memory and tools for AI agents

> ⚠️ **Experimental / pre-1.0 — actively developed.** Mneme is usable today, but
> the API, the config format, and the experimental features still change. Expect
> rough edges. Feedback and bug reports welcome.
>
> **Written with AI, reviewed by a human.** Mneme was built iteratively with AI
> coding assistants rather than typed line-by-line. The design decisions,
> architecture, requirements, and testing are human-driven; much of the code
> itself is AI-generated. That's worth knowing when you read the source — the
> conventions are consistent but more uniform than hand-written code usually is.

**Mneme is a persistent memory and tool proxy for AI agents.** It sits between an
OpenAI-compatible client and a model backend, archives conversations into
searchable memory, retrieves the relevant parts back into later turns, and gives
the model a persistent tool layer.

It runs against **local Ollama models** or **any hosted OpenAI-compatible
provider**. Point Pi, Open WebUI, a script, or any OpenAI-compatible app at it.

```text
        any OpenAI-compatible client
                     │
                     ▼
        ┌────────────────────────┐
        │      Mneme Proxy       │
        │                        │
        │  memory    tools       │
        │  provenance  MCP       │
        │  context budget        │
        └───────┬────────┬───────┘
                │        │
                ▼        ▼
          SQLite+FAISS   model backend
          (memory DB)    Ollama / hosted API
```

## Why Mneme is not just a chat-history database

Three design decisions separate it from "store the transcript, paste it back":

- **Retrieval has a confidence floor.** Mneme does not inject the nearest memory
  just because it is the nearest. A chunk must clear an absolute similarity
  threshold to be used at all. Irrelevant context is worse than none.

- **Memory carries provenance, not just content.** Every chunk is tagged as
  observed (user input, a fetched page, a tool result) or as a claim (model
  output). Model-generated content is re-injected with an `[UNVERIFIED]` marker
  so the model cannot quietly re-assert its own earlier output as established
  fact — which is how a hallucination, once saved, becomes permanent.

- **Bad memories are correctable.** Any chunk can be marked false and later
  restored, with every action in an audit log. A model can *propose* a chunk is
  wrong; only you can confirm it. This exists because "the model agreed with
  itself earlier" is the main way a memory system poisons itself.

See [How memory works](#how-memory-works) for the mechanism.

## Quick start
...
```

### What changed and why

- **"vibe-coded" removed, AI authorship kept.** The disclosure stays — it's
  honest and it's information a reader can use. What goes is the *label*.
  "Vibe-coded" invites a judgement about process rather than informing a
  decision; "written with AI, reviewed by a human" says the same true thing in
  terms a reader can act on. It also pre-empts the "this looks AI-generated,
  is it serious?" reaction by naming it first.
- **Title changed** from "conversational memory proxy" to name the two things
  it actually is (memory *and* tools). "Conversational" undersold it.
- **Two short paragraphs, then a diagram, then install** — the order a new
  reader needs. The current README opens straight into "run these three
  scripts".
- **The three differentiators are stated before the feature list**, so someone
  can decide whether to care before reading 17 bullets.

### Decisions for you

1. **How specific should the AI disclosure be?** The draft says "built
   iteratively with AI coding assistants". You could name them (Claude Code,
   Codex, etc.) or leave it general. Naming is more transparent; general is
   more durable as tools change. I lean general.
2. **The four differentiators** — I picked three (floor, provenance,
   correctable memory). The fourth candidate was "persistent tools". I left it
   out of the *why-care* section because tools are a feature, not a
   differentiator — many proxies have tools. Say the word if you want it in.
3. **Length.** This is ~35 lines before Quick start. If that's too long, the
   "Why Mneme is not just a chat-history database" block can move below Quick
   start and the top becomes ~15 lines.

---

# 6. AGENT WORKFLOWS & SWARMS

Proposed as a top-level section, placed after "How memory works" and before
"Configuration". Currently the swarm is one `###` under "Extensions".

```markdown
## Agent workflows & swarms

Mneme includes a **declarative agent workflow engine** (`extensions/swarm`).

Rather than writing a Python driver for every multi-agent task, you describe the
workflow in `swarm_config.yaml`: which agents run, what each one reads, where it
writes its result, how the flow branches, when it retries, and what runs in
parallel.

The result is a small workflow language for repeatable agent pipelines — with the
orchestration *outside* the models rather than negotiated between them.

```text
                      swarm_config.yaml
                             │
                             ▼
                    ┌─────────────────┐
                    │   Orchestrator  │
                    │                 │
                    │ flow / branches │
                    │ retries         │
                    │ file state      │
                    │ parallel steps  │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
           Mneme           Mneme          Ollama
            agent          agent           model
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                    filesystem state
                     / shared board
```

### Agents communicate through artifacts, not conversation

The workflow uses the filesystem as a shared blackboard. One agent writes a file,
the next reads it, and a later stage can consume the whole directory.

```text
input/
   │
   ▼
┌──────────────────────────┐
│ parallel critics         │
│   structure.txt          │
│   prose.txt              │
│   factual.txt            │
└────────────┬─────────────┘
             ▼
         synthesis
             ▼
           draft
             ▼
          review
         /      \
    approve     revise
       │           │
       ▼           └──────► review
    publish
```

This is the design choice that matters most: **the workflow defines the
structure; the models perform the cognitive steps.** An agent doesn't need to
understand the whole pipeline — it gets "here is the material, you are the
critic" and writes to a path. Intermediate work is left on disk, so it is
visible and inspectable while the run is happening and after it finishes.

It is easier to reason about than a swarm of agents negotiating with each other,
and it fails in ways you can see.

### What a workflow can express

- sequential model steps, and `goto` loops
- branching on model output (`if: contains/equals/startswith`) or on filesystem state
- retries on transient model failure, and per-step `delay` pacing
- **per-step generation settings** — `options:` overrides temperature/top_p/top_k
  and backend-specific keys for that one call, without touching the proxy config
- file and directory primitives
- `swap_dir` — an **atomic input snapshot** (see below)
- cycle throttling (`every`) and `skip_if_empty`
- action-only steps that do not spend a model call

Granularity is deliberate: a step that freezes a directory, moves a file, or
checks state costs nothing, because it never touches a model.

### Atomic workflow snapshots

`swap_dir` gives an iterative workflow a transaction-like boundary:

```text
input/
   │
   │ swap_dir
   ▼
input.active/     ← frozen snapshot: what this iteration works on
input/            ← fresh: new work can accumulate for the next iteration
```

Agents in the current cycle see a stable input set, while new material keeps
arriving for the next one. This sounds small and removes a genuinely awkward
problem in iterative agent systems: *what exactly is the input to this pass?*

### Parallel swarms

The parallel orchestrator (`swarm_p_orchestrator.py`) adds one step form:

```yaml
- parallel:
    - name: structure
      backend: mneme
      port: 8080
      read_dir: input.active
      write_dir: pass1/structure.txt
    - name: prose
      backend: ollama
      model: qwen2.5:14b
      read_dir: input.active
      write_dir: pass1/prose.txt
```

It is a **map → reduce** for agents: fan out independent work, then let the next
step read `pass1/` and synthesize.

Worth being precise about "parallel", because it depends on the backend. Against
a **hosted** provider, requests genuinely run concurrently and fan-out is free.
On a **single-GPU Ollama** box, different models serialize anyway — VRAM forces a
model swap per step — so fan-out only helps for the *same* model. The thread
pool doesn't pretend threads equal GPU parallelism.

### Mneme is optional

The orchestrator talks to Mneme over `/v1/chat/completions`. It does not import
the proxy or depend on its internals. So a single workflow can mix:

- Mneme-backed agents (memory + tools)
- raw Ollama models (`backend: ollama`, no memory)
- different models for different roles
- several Mneme instances sharing one memory DB
- hosted and local inference in the same run

```text
          ┌──────────────┐
          │  Researcher  │
          │    Mneme     │
          └──────┬───────┘
                 │
          research/*.txt
                 │
       ┌─────────┴─────────┐
       ▼                   ▼
   Critic A             Critic B
    Mneme                Ollama
   (memory)            (no memory)
       │                   │
       └─────────┬─────────┘
                 ▼
             Synthesizer
               Mneme
                 │
                 ▼
              final/
```

### Why the swarm exists

This is not a general-purpose distributed workflow platform, and it isn't trying
to be. It exists to test a specific idea.

**The hypothesis:** can a collection of relatively small, locally runnable
models — given persistent memory, tools, specialized roles, and a structured
workflow — complete work that would normally require one much larger model?

The approach is to move work that would otherwise have to happen *inside* a
large model into the surrounding system:

```text
        large-model approach          Mneme approach

        ┌──────────────────┐          ┌──────────────────┐
        │                  │          │    small LLM     │
        │    large LLM     │          │  local / hosted  │
        │                  │          └────────┬─────────┘
        │  reasoning       │                   │
        │  memory          │      ┌────────────┼────────────┐
        │  tools           │      ▼            ▼            ▼
        │  planning        │   memory       tools       workflow
        │  context         │      │            │            │
        └──────────────────┘      └────────────┼────────────┘
                                               ▼
                                        other agents
```

The workflow engine is the control layer for this. Instead of asking one model to
perform every part of a task, a workflow splits it across specialized calls and
combines the results.

### This is not demonstrated yet

**The hypothesis above has not been shown quantitatively.** Mneme provides the
infrastructure to investigate it; it does not claim the question is settled.

In practice the system is sensitive to configuration, and being honest about that
is more useful than overselling it:

- Model choice, context size, retrieval threshold, generation settings, prompts,
  tool availability, and workflow design all interact.
- Some combinations work well. Some work poorly. **Some model/configuration
  combinations do not work at all.**
- There is no single configuration that works equally well across models and
  tasks. "Model-agnostic" does not mean "model-independent".

If you install Mneme, pair a random 7B with an untuned retrieval threshold, and
get poor results — that is the expected outcome of an untuned configuration, not
necessarily a verdict on the approach. Tuning is currently part of using it.

**Model templates** (see [`model_templates.yaml`](model_templates.yaml)) exist to
reduce that cost: named, known-good settings for specific models, selected during
setup, so you don't start from scratch. They are a starting point, not a
guarantee.

Some open questions this exists to ask:

- Can several small specialized models outperform one small general-purpose model?
- How much does persistent memory actually improve long-running work?
- Which tasks benefit from parallel specialists, and which don't?
- When is a large model genuinely necessary?
- Can tools compensate for a capability a model lacks?

---

## Swarm (`extensions/swarm`)  ← what remains under Extensions

The reference material stays where it is:

- `extensions/swarm/README.md` — worked example
- `extensions/swarm/SWARM_REFERENCE.md` — full field-by-field spec
- `extensions/swarm/swarm_orchestrator.py` — serial driver
- `extensions/swarm/swarm_p_orchestrator.py` — parallel driver
```

### What changed and why

- **Promoted to a top-level section.** One `###` under Extensions reads as "side
  project". It's a workflow engine and it's the most novel thing in the repo.
- **Retitled.** "Swarm" alone makes people expect agents chatting with each
  other, which is explicitly *not* the design. "Agent workflows & swarms" sets
  the right expectation.
- **Leads with the architectural idea**, not the feature list: artifacts as the
  communication channel, workflow outside the models.
- **The hypothesis is stated as a hypothesis.** Separate subsection, explicit
  "not demonstrated yet", and the failure modes named — including the
  "combinations that do not work at all" one. This is the difference between a
  credible research direction and an overclaim, and it inoculates you against
  the "I tried it with a 7B and it was bad" review.
- **Templates are mentioned as the mitigation** for the tuning cost, which links
  the two features instead of leaving them unrelated.

### Decisions for you

1. **How blunt should "this is not demonstrated" be?** I've gone fairly direct,
   including the "do not work at all" line. You could soften it. I'd argue
   against: the failure mode is a reader concluding the *approach* is broken
   when they actually just have an untuned config, and stating it pre-empts that.
2. **Should the hypothesis lead the section** rather than close it? Leading with
   "can small models do big-model work" is more compelling but reads as a claim
   before you've explained the mechanism. I chose mechanism-then-hypothesis.
3. **`swap_dir`** — I gave it its own subsection because the snapshot-as-
   transaction idea is genuinely interesting, but it may be more detail than a
   README wants. Could collapse into the bullet list.

---

## Suggested ordering of the whole README after 5 & 6

```
Opening (memory + tools, why care, diagram)        ← 5
Quick start
Security                                            ← added today
Model selection
Agent workflows & swarms                            ← 6
How memory works
Usage and connecting clients
Configuration
API endpoints
Testing
Architecture
Extensions
Branches
```

Swarm sits above "How memory works" deliberately: a new reader should understand
what the project *does* before reading how retrieval is implemented.
