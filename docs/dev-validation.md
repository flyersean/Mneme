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
   embedder). The script surface the live `embed.ok`/`embed.error` line so
   a down embedder can never pass silently. A negative control —
   deliberately breaking the embed API key and confirming `/health` flips
   non-ok — is a MANUAL step (documented below); it is deliberately not
   automated. Related machine-readable endpoint: `GET /system/status`
   (instance inventory: ports, models, running flags).

3. **Save-then-semantic-search round trip works through the API.**
   Post a trivial chat completion (this stages a conversation), `POST /save`
   to flush staging (the script prints the `/save` response including the
   stored-chunk count), then `POST /search` with a query drawn from that
   conversation. The marker hit must be **scored** (carry a `similarity`)
   and have a **non-empty `instance_origin`** — a bare substring grep could
   match an echoed payload from a mock/fallback backend, while a scored hit
   retrieved after the flush proves both the round trip and FAISS-index
   consistency. An empty-but-clean result is a WARN during a cold-start
   boot (marked MANUAL by the script); on a warm DB it should return
   results. A hit with an empty `instance_origin` is a WARN, not a FAIL (the
   column is `TEXT DEFAULT ''`, so presence alone is not a pass).

4. **Chat completion responds within 30 s.** A trivial
   `POST /v1/chat/completions` must complete in < 30 s wall-clock.

5. **All migrations apply cleanly on a copy of an old-format DB.**
   Boot a dev proxy against a COPY of an old-format `mneme.db` on a scratch
   port and confirm: boot succeeds (and the process stays alive — the
   script checks pid liveness before issuing a verdict), the pre-boot
   `PRAGMA table_info(chunks)` did NOT yet contain the new column, and the
   post-boot PRAGMA does (e.g. `instance_origin`). The script prints the
   pre-boot columns as a positive control: a copy that already had the
   column does not vacuously pass, it is flagged. Prefer a **real
   pre-migration backup** via `OLD_DB=<path>` over the synthesized one —
   `--synth-old-db` writes a minimal legacy-shaped schema that exercises
   the ALTER path but not every legacy column shape. Never migration-test
   a live DB in place.

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

## Manual steps (not automated on purpose)

- **Negative control on `/health` inversion** (check 2): temporarily point
  the staging proxy at a bad embed API key, confirm `/health` reports the
  embedder as NOT ok (and the memory `pending` figure grows), then restore.
  Automated breakage of a live backend is out of the script's scope.
- **Origin verification on a real instance DB after its next boot**: run
  `PRAGMA table_info(chunks)` and `SELECT DISTINCT instance_origin FROM
  chunks LIMIT 5` on the instance's own DB file post-boot - the column can
  arrive **present-but-always-empty** (`TEXT DEFAULT ''`), which would
  silently defeat its audit purpose. (7979 was live-verified healthy
  during the round-4 handoff, but its origin stamp only lands on its next
  restart - do not record "instance_origin verified on 7979" until this
  check runs.)
