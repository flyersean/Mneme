---
name: mneme-system-monitor
description: Prevent proxy crashes from OOM / credit-exhaustion / disk-full — swap, credit low-balance floor, brownout, host status line, DB ownership marker.
tags: [mneme, ops, oom, credits, disk, monitoring]
---

# Mneme system monitoring (crash prevention)

## When to use
Preventing a proxy from being OOM-killed, burning OpenRouter credits to zero, or filling the disk with runaway logs.

## Components (all in `proxy/mneme_proxy.py`)
- **Swapfile**: 4GB swap on the VPS is the cheapest OOM buffer. 0 swap = the kernel OOM-killer has no runway (an OOM kill at ~5.7GB RSS came from this). `sudo fallocate -l 4G /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` + persist in `/etc/fstab`.
- **Credit low-balance guard**: `_fetch_credit_balance()` (GET openrouter.ai/api/v1/credits, cached ~60s) + `MNEME_CREDIT_FLOOR` (default $5). The re-embed drain halts when balance < floor. This is a LOW-WATER MARK, NOT a rate limit — burning credits fast when useful is fine; the floor leaves headroom to wind down gracefully instead of dying at $0.10 with open processes.
- **Brownout**: `MNEME_BROWNOUT_RAM_PCT` (default 80) — `_memory_pressure()` halts the re-embed drain when RAM is high.
- **Host status line** in the injected turn header: `[HOST: ram X% | disk Y% | credits $Z @ HH:MMZ]`.
- **DB ownership marker**: `<db>.owner` records pid/port/checkout/schema — warns loudly when a DIFFERENT checkout/schema opens the same DB (8083-class footgun). Audit marker, NOT a lock (shared memory stays allowed).

## Pitfalls
- Fail-open: if the credit balance can't be read (no key / network down), halt nothing.
- Timestamp every number surfaced (`measured_at` on memory counts, `at` on host) — a bare stale integer is indistinguishable from live.
- Proxies run via `nohup setsid`, NOT systemd — MemoryHigh/MemoryMax needs systemd adoption first.
- The re-embed drain is the biggest background credit/CPU consumer; gate it there.

## Verify
- `bash`: `curl -s http://127.0.0.1:8080/health` shows credits {balance, floor, low} + host {ram_pct, disk_pct, load, at}.
- `bash`: `free -h` and `swapon --show` confirm the swap.
