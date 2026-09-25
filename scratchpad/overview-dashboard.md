# Multi-proxy overview dashboard + reverse proxy

Status: idea
Date: 2026-09-24

## Problem

Mneme supports multiple proxy instances on one pod — one shared memory DB, each
instance a different chat model on its own port. Every instance binds to
`127.0.0.1` (no auth, by design) and is reached from a laptop through an SSH
tunnel per port. With more than one proxy running:

- The human has no single page that links out to every proxy's pages
  (`/`, `/chat`, `/memory`, `/instructions`, `/templates`).
- A fan-out harness (multiple models at once) needs N tunnels — one per model.

Opening N tunnels works but is clunky.

## Idea

An "overview dashboard" on one reserved port that:

1. Links out to every running proxy's web pages.
2. Optionally reverse-proxies the OpenAI API so a fan-out harness reaches all
   models through ONE tunnel and ONE base URL.

## Consumers (two, distinct)

- Human browser → web pages (`/`, `/chat`, `/memory`, `/instructions`, `/templates`).
- Harness → OpenAI API (`/v1/chat/completions`, base `localhost:<port>/v1`).

These are independent; the overview affects them differently.

## Discovery (already free)

Every instance writes `instances/<port>/mneme.yaml` + `start_proxy.sh`, and the
shared dir has `setup_config.json`. An overview enumerates proxies by listing
`instances/<port>/` and reading each one's model name — no registration
handshake, auto-updates when instances are added/removed.

## Options

- **A — link-list (cheapest).** Overview reads `instances/` and renders
  `localhost:<port>` links. Still needs N tunnels; doesn't help the harness.
- **B — reverse proxy (clean).** Overview on one port (e.g. 8090) serves the
  page AND reverse-proxies `/<port>/…` → `localhost:<port>/…` (web UI and `/v1`).
  One tunnel for everything. This is the recommended shape.
- **C — status-only.** Overview hits each `/health` server-side and shows a
  status table + links. Good for monitoring; still multi-tunnel unless combined
  with B.

## Key decisions / open questions

- **Port:** reserve one; avoid RunPod nginx ports 8081 / 3001 / 7270 / 7861 /
  8001 / 9091. Make it configurable (e.g. `MNEME_OVERVIEW_PORT`).
- **Harness addressing:** N base URLs vs one URL + per-request model. Decides
  whether B is needed at all (see stopgap below).
- **Route by port or by model name?** Port = trivial (just reverse-proxy on the
  port). Model name = cleaner for the harness but needs the overview to read
  each instance's config to map model → port.

## Stopgap (no code)

Stacked SSH forwards collapse N tunnels into ONE command/process:

    ssh -N \
      -L 8080:localhost:8080 \
      -L 8081:localhost:8081 \
      -p <SSH_PORT> -i ~/.ssh/id_ed25519 root@<POD_IP>

Sufficient if the harness holds N base URLs (one per model). Only the reverse
proxy (B) collapses N ports into one URL.

## Gotchas

- **Streaming is the #1 make-or-break.** The API is SSE/streaming and a fan-out
  harness depends on it (first-token detection, tool-call passthrough, the
  empty-answer→continue loop). The reverse proxy must pass streaming through
  UNBUFFERED, or every worker stalls.
- **Single point of failure.** Overview down = all web UI + all harness
  connections break at once, even though the proxies stay healthy.
- **No auth** (unchanged from today). The overview concentrates the
  unauthenticated surface into one port; add a token if ever exposed directly.
- **Extra localhost hop** on the pod: negligible.

## Recommendation

1. Use the stacked `-L` tunnel now (a habit, not a build).
2. Build Option A (link-list) for the human UI — cheap, gives the link-out.
3. Build Option B only if the harness truly needs a single URL.
