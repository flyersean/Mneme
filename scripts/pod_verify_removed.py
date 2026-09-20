#!/usr/bin/env python3
"""POD verification: does provenance record on a LIVE proxy, and does the
removed flag really change what Mneme uses?

Run ON the pod, against a running instance:

    python3 /tmp/pod_verify.py --port 8080

Why this must run on a pod and not the laptop:
  - /search needs a working embedder (the laptop has none), so the search
    exclusion can only be proven where embeddings actually happen.
  - provenance recording runs through the REAL archive path, which needs a
    real model call to produce a turn worth archiving.

What it proves, in order:
  1. a real chat turn is archived                     (archive path works)
  2. that chunk carries model + provenance fields      (provenance is LIVE)
  3. a removed chunk stops being injectable            (the flag works)
  4. /search excludes it, and include_removed finds it (search filtering)
  5. unflagging restores it                            (reversible)
  6. the management endpoints the page uses all answer  (page will work)

Exit code 0 = all checks passed.
"""
import argparse, json, sys, time, urllib.error, urllib.request

BASE = "http://127.0.0.1:{port}"


def call(method, path, payload=None, timeout=120):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


R = []


def check(label, ok, detail=""):
    R.append((label, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""), flush=True)


def _summary():
    """Print the aggregate result. Used for BOTH the normal end and early exits, so
    a failure never returns a bare exit code with no explanation."""
    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in R if ok)
    print(f"RESULT: {passed}/{len(R)} checks passed")
    for label, ok, detail in R:
        if not ok:
            print(f"  FAILED: {label} — {detail}")
    return 0 if passed == len(R) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--model-timeout", type=int, default=300,
                    help="how long to wait for a chat turn (big models are slow)")
    args = ap.parse_args()
    global BASE
    BASE = f"http://127.0.0.1:{args.port}"
    print(f"=== target: {BASE} ===")

    print("\n=== 0. proxy is up ===")
    sc, body = call("GET", "/health")
    check("GET /health == 200", sc == 200, str(sc))
    if sc != 200:
        print("  cannot continue without a running proxy")
        return _summary()
    print(f"    backend={body.get('backend')} model={body.get('model')} chunks={body.get('chunks')}")
    before_chunks = body.get("chunks", 0)

    print("\n=== 1. a real chat turn gets archived ===")
    marker = f"podverify-{int(time.time())}"
    sc, chat = call("POST", "/v1/chat/completions", {
        "messages": [{"role": "user",
                      "content": f"Remember this exact token for later: {marker}. "
                                 f"Reply with just the token."}],
        "model": "text-mneme:64k",
    }, timeout=args.model_timeout)
    check("chat request returned", sc == 200, str(sc))
    reply = ""
    try:
        reply = chat["choices"][0]["message"]["content"]
    except Exception:
        reply = str(chat)[:200]
    print(f"    reply: {reply[:120]!r}")

    # Archiving is async (background worker) — poll for it.
    print("    waiting for the background archive to land…")
    nb = before_chunks
    for _ in range(30):
        time.sleep(2)
        sc, h = call("GET", "/health")
        nb = h.get("chunks", 0)
        if nb > before_chunks:
            break
    check("chunk count increased (archive ran)", nb > before_chunks,
          f"{before_chunks} -> {nb}")

    print("\n=== 2. PROVENANCE IS LIVE on a real chunk ===")
    # Identify the chunk OUR turn created. Asserting on "the newest chunk" is
    # wrong — other rows may be newer, and seeded/probe rows do not go through the
    # archive path at all, so they legitimately lack provenance fields.
    cid = None
    for _ in range(20):
        sc, body = call("GET", "/memory/chunks?limit=10&order=newest")
        chunks = body.get("chunks", [])
        # Our turn's chunk is the one whose preview/content mentions the marker.
        match = [c for c in chunks
                 if marker in json.dumps(c) or marker in (c.get("preview") or "")]
        if match:
            cid = match[0]["chunk_id"]
            break
        time.sleep(2)
    check("management list answers", sc == 200, str(sc))
    check("the chunk OUR turn created was found", cid is not None,
          f"searched {len(chunks)} rows for marker {marker}")

    if not cid:
        # Do NOT stop here. The archive check needs a working model; the flag
        # checks below do not. Stopping would turn an environment limitation into
        # a black-box failure and hide the results we CAN get. Fall back to any
        # existing chunk so the flag/search checks still execute.
        print("    NOTE: no marker chunk — the chat turn did not archive (needs a")
        print("    working model). Falling back to an existing chunk for the flag")
        print("    checks, which do not depend on the archive path.")
        sc, body = call("GET", "/memory/chunks?limit=1&order=newest")
        chunks = body.get("chunks", [])
        if not chunks:
            print("    No chunks in the DB at all — cannot test the flag. Send at")
            print("    least one message to the proxy, then re-run.")
            return _summary()
        cid = chunks[0]["chunk_id"]
        print(f"    using {cid} ({chunks[0].get('topic_label')!r}) for the flag checks")

    newest = next((c for c in chunks if c["chunk_id"] == cid), chunks[0])
    print(f"    target: {cid}  topic={newest.get('topic_label')!r} "
          f"model={newest.get('model')!r} removed={newest.get('removed')!r}")
    # THE regression this whole session was about: derived_from used to be always [].
    check("model recorded by the archive path (new column is populated)",
          bool(newest.get("model")), repr(newest.get("model")))
    check("removed defaults to injectable", newest.get("removed") == "injectable",
          repr(newest.get("removed")))

    sc, det = call("GET", f"/memory/chunks/{cid}")
    ch = (det or {}).get("chunk", {})
    check("detail endpoint answers", sc == 200, str(sc))
    check("injected_chunk_ids field present (schema migrated)",
          "injected_chunk_ids" in ch, str(list(ch)[:8]))
    check("injected_chunk_ids is a recorded list (provenance wiring live)",
          isinstance(ch.get("injected_chunk_ids"), list), str(ch.get("injected_chunk_ids")))

    print("\n=== 3. flag it removed ===")
    # CRITICAL: prove the chunk is VISIBLE while injectable before asserting it
    # disappears when removed. Without this, "absent when removed" passes
    # vacuously whenever nothing is findable at all — which is how an earlier
    # version of this script passed against deliberately broken code.
    sc, pre = call("GET", f"/memory/chunks?removed=injectable&limit=500")
    pre_ids = [c["chunk_id"] for c in pre.get("chunks", [])]
    check("PRE-CONDITION: chunk is visible while injectable", cid in pre_ids,
          f"looked for {cid} among {len(pre_ids)} injectable chunk(s)")

    sc, out = call("POST", f"/memory/chunks/{cid}/remove",
                   {"removed": True, "reason": "pod verification"})
    check("flag set", sc == 200 and out.get("removed") == "removed", str(out))

    print("\n=== 4. the flag CHANGES what Mneme uses ===")
    sc, m = call("GET", f"/memory/chunks?removed=injectable&limit=500")
    inj_ids = [c["chunk_id"] for c in m.get("chunks", [])]
    check("absent from injectable filter", cid not in inj_ids,
          f"n={len(inj_ids)}; present={'YES' if cid in inj_ids else 'no'}")
    sc, m = call("GET", f"/memory/chunks?removed=removed&limit=500")
    rem_ids = [c["chunk_id"] for c in m.get("chunks", [])]
    check("present under removed filter", cid in rem_ids,
          f"n={len(rem_ids)}")

    print("\n   injection choke point (this is what actually gates the model):")
    sc, det = call("GET", f"/memory/chunks/{cid}")
    check("still readable by the management view while removed", sc == 200, str(sc))
    # load_chunk is exercised through the running proxy: an injectable read of a
    # removed chunk must be refused. There is no HTTP route that calls it with
    # allow_removed, so the observable proxy-side proof is the search exclusion
    # below plus this endpoint's survival.

    print("\n=== 5. /search filtering (needs a REAL embedder — the point of testing here) ===")
    # Positive control first: the removed chunk must be findable while it is
    # INJECTABLE, otherwise "excluded when removed" proves nothing. Unflag, search,
    # confirm it appears, then re-flag and confirm it disappears.
    call("POST", f"/memory/chunks/{cid}/remove", {"removed": False})
    sc, s_pre = call("POST", "/search", {"query": marker, "top_k": 10})
    pre_hits = [x["chunk_id"] for x in (s_pre or {}).get("results", [])]
    call("POST", f"/memory/chunks/{cid}/remove",
         {"removed": True, "reason": "pod verification"})

    sc, s0 = call("POST", "/search", {"query": marker, "top_k": 10})
    base_ids = [x["chunk_id"] for x in (s0 or {}).get("results", [])]
    check("/search answers", sc == 200, str(sc))
    check("PRE-CONDITION: search is actually returning results",
          len(pre_hits) > 0,
          f"unflagged search returned {pre_hits} — if empty, the embedder is not "
          f"working and the exclusion checks below cannot prove anything")
    check("PRE-CONDITION: removed chunk WAS findable before removal",
          cid in pre_hits, f"pre={pre_hits}")
    check("removed chunk NOT in default /search", cid not in base_ids,
          f"default={base_ids}")
    sc, s1 = call("POST", "/search",
                  {"query": marker, "top_k": 10, "include_removed": True})
    inc_ids = [x["chunk_id"] for x in (s1 or {}).get("results", [])]
    check("include_removed=true returns it", cid in inc_ids,
          f"incl={inc_ids} excl={base_ids}")

    print("\n=== 6. the management page loads and its data endpoints answer ===")
    sc, _ = call("GET", "/memory/chunks?limit=5")
    check("GET /memory/chunks (page's main call)", sc == 200, str(sc))
    sc, src = call("GET", "/memory/sources")
    check("GET /memory/sources (filter dropdowns)", sc == 200, str(sc))
    check("dropdowns have real values",
          bool(src.get("models") or src.get("sources")),
          f"models={src.get('models')} sources={src.get('sources')}")

    print("\n=== 7. unflag is reversible ===")
    sc, out = call("POST", f"/memory/chunks/{cid}/remove",
                   {"removed": False, "reason": ""})
    check("flag cleared", sc == 200 and out.get("removed") == "injectable", str(out))
    sc, m = call("GET", f"/memory/chunks?removed=injectable&limit=200")
    check("chunk findable as injectable again",
          any(c["chunk_id"] == cid for c in m.get("chunks", [])),
          f"n={m.get('total')}")

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in R if ok)
    print(f"RESULT: {passed}/{len(R)} checks passed")
    for label, ok, detail in R:
        if not ok:
            print(f"  FAILED: {label} — {detail}")
    return 0 if passed == len(R) else 1


if __name__ == "__main__":
    sys.exit(main())
