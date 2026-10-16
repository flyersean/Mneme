# A→B dev validation handoff protocol

Before handing dev code to instance B (port 7979) for testing — or before
claiming a dev change is "done" — run every item on this checklist. The bash
script `scripts/validate-dev.sh` executes the mechanical parts; anything it
marks MANUAL must be checked by hand. Run the script against a **staging
instance** (a proxy booted on dev code at a scratch port), never against the
live 8080 or 7979 proxies.

## Checklist

1. **Proxy boots on dev code.** A proxy process started from the dev checkout
   reaches `/health` and returns HTTP 200 with `"status":"ok"`.
   (Script: `--staging` mode boots one on a scratch port.)

2. **`/health` reports per-backend status AND memory health figures.**
   The `/health` body includes `embed`/`chat`/`label` backend status plus
   the `memory` object with total/embedded/pending counts (see the Oct 10-14
   outage postmortem: `/health` used to return ok without checking the
   embedder). Also sanity-check `GET /status` per-provider ok/error fields.

3. **Save-then-semantic-search round trip works through the API.**
   Post a trivial chat completion (this stages a conversation), `POST /save`
   to flush staging, then `POST /search` with a query drawn from that
   conversation. The call must not error, and any results returned must
   carry the `instance_origin` provenance column. An empty-but-clean result
   is a WARN during a cold-start boot; on a warm DB it should return results.

4. **Chat completion responds within 30 s.** A trivial
   `POST /v1/chat/completions` must complete in < 30 s wall-clock.

5. **All migrations apply cleanly on a copy of an old-format DB.**
   Copy an existing `mneme.db` (or synthesize an old-schema one with
   `scripts/validate-dev.sh --synth-old-db`), boot the dev proxy against
   the copy on a scratch port, and confirm: boot succeeds and
   `PRAGMA table_info(chunks)` on the migrated copy shows the new columns
   (e.g. `instance_origin`). Never migration-test a live DB in place.

## How to run

```bash
# Full staging run (boots a scratch-port proxy on dev code first):
scripts/validate-dev.sh --staging

# Manual mode against an already-running staging proxy:
BASE_URL=http://127.0.0.1:8090 scripts/validate-dev.sh

# Migration check on a synthesized old-format DB:
scripts/validate-dev.sh --synth-old-db /tmp/old-mneme.db
```

Exit code 0 = all checks passed; non-zero = at least one FAILED.
Under `--staging` the script starts and stops its own proxy on a scratch
port (default 8090) — it never touches ports 8080 or 7979.
