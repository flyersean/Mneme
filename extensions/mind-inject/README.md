# mind-inject — a mind module runs through its own Mneme proxy

One mind module (creative, guardian, curiosity, ...) becomes ONE proxy instance
— its own port, config, and isolated DB — running the same proxy code as the
main instance. The extension is just the control plane: it creates the proxy,
drives the cadence, and injects the module's output into the main model's
thought flow.

## Design (why proxy-through)

Running the module through a proxy instead of a direct API call gives it, for
free: the built-in **novelty mode** (`/mode/think` — diverge -> forbid ->
measure -> judge), the full config surface (temperature, sampling, etc.), and a
clean system-prompt boundary. The module's model is often tool-free (e.g.
`gemma-4-31b-cognitive-unshackled` has `function_call: false`), so the extension
does the memory retrieval against the MAIN proxy over HTTP and hands the model a
plain context. The module's own DB is isolated, so its output never pollutes the
main memory.

## Commands

```bash
python3 mind_inject.py setup modules/creative.yaml     # create + launch the proxy
python3 mind_inject.py run   modules/creative.yaml     # cadence loop (auto-setups if needed)
python3 mind_inject.py teardown modules/creative.yaml  # kill + remove the instance
```

## Path rule (deliberate — no multi-proxy confusion)

- The port number IS the directory name: `chunks/instances/<port>/`.
- The module's DB is isolated: `chunks/instances/<port>/mneme.db`.
- The main proxy is read over HTTP (`/list`, `/detail`, `/search`) and injected
  via `/steer` — never written to.

Each module YAML pins a fixed, unique `port`. Free-port auto-allocation is
deliberately avoided — a fixed port in the config means a fixed dir name, so
"which dir is which worker" never drifts.

## Scaffolding a new module

Copy `modules/_TEMPLATE.yaml`, change `module` + `port` + `prompt`, run
`setup`. The other mind modules: guardian (watch for risk), curiosity (ask
what's unexplained), motivation (keep momentum), jane (the coordinating self).

## The creative module (reference)

creative.yaml uses `gemma-4-31b-cognitive-unshackled` on Routeway (~$0.28/M,
good at creative work) at temperature 1.2 with novelty mode on. Its prompt is
the "current + past -> speculate future" frame: look at where things are now,
find a similar past situation and its outcome, judge whether the current
trajectory will succeed, and if not, invent a genuinely new idea (technical or
artistic).
