# mind-inject — scheduled mind-module injection for Mneme

A generic "swarm tick": read recent memory from a Mneme proxy, run ONE mind
module (creative, guardian, curiosity, …) through a cheap one-off model, then
inject the output into the target model's thought flow via the **force flow**
(the `steer.txt` tool-boundary piggyback).

## What it does, step by step

1. **Read** the N most recent chunks from the target proxy's memory DB
   (`GET /list` + `GET /detail/<id>`).
2. **Run** the module's prompt against a cheap uncensored model in a **one-off
   direct API call** — it never touches the proxy, so **nothing is saved to the
   DB**.
3. **Inject** the output via `POST /steer`, which writes the proxy's steer file.
   The proxy consumes it **once, at the next tool boundary**, wrapping it as a
   high-priority `[CHUNK #LIVE-USER-STEERING]` note the model already scans.

The injection is **last-write-wins**: a fresh tick overwrites any un-consumed
note. So if the target model is idle (not tool-calling), new ticks replace the
old note rather than queueing up.

## Usage

```bash
# one tick (test)
python3 mind_inject.py modules/creative.yaml

# scheduled: loop every `interval_minutes` from the config
python3 mind_inject.py modules/creative.yaml --daemon
```

To run the daemon detached (survives logout):

```bash
setsid python3 -u mind_inject.py modules/creative.yaml --daemon \
  >> /home/ubuntu/mind_inject.log 2>&1 &
```

## Scaffolding a new module

One module = one YAML file. Copy `modules/_TEMPLATE.yaml` to a new name, change
`module` + `prompt` (and the model/interval if you want), and run it. No code
changes. Example modules to scaffold: `guardian` (watch for risk), `curiosity`
(ask what's unexplained), `motivation` (keep momentum), `jane` (the
coordinating self).

## Schedule

The interval lives in the config (`interval_minutes`, default 10) — change the
number and restart the daemon. The only knob that matters: tick roughly as
often as (or slower than) the target model's tool activity, since a tick only
lands when the model next calls a tool and an idle note just gets overwritten.

## Model note (why reasoning is off by default)

A cheap uncensored model left to "think" (reasoning pass) will burn its whole
token budget on thinking and return an empty body (`finish_reason=length`).
`reasoning_enabled: false` makes it emit content directly — faster, cheaper,
and it actually answers. Flip it to `true` if you want deeper (slower, pricier)
creative output.

## Design (why it's shaped this way)

Like `extensions/swarm`, this is a **standalone program that talks to the proxy
over HTTP only**. It imports nothing from the proxy and the proxy imports
nothing from it — so it can be changed or removed without touching the proxy,
and vice versa. One extension, many modules, zero coupling.
