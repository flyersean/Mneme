#!/usr/bin/env python3
"""jane_bridge — notify the main model (8090) when Sean posts to the Jane
side-chat (8095), so it can look at the message and reply mid-turn.

This is the interaction channel for the looping-model experiment. The main model
is NOT a turn-based chat — it's a continuous thought loop. When Sean writes to
Jane (/home/ubuntu/message/chat.jsonl), this bridge detects the new message and
force-injects it into the main model as a [LIVE INTERRUPT] user turn. The model
then decides what to do with it: reply to Sean via Jane, incorporate it, or
ignore it. Nothing here is fast or guaranteed — that's the point.

Usage: python3 jane_bridge.py [--target http://127.0.0.1:8090] [--poll 3]
"""

import argparse
import json
import time

import requests

JANE_URL = "http://127.0.0.1:8095/api/janechat"
JANE_LOG = "/home/ubuntu/message/chat.jsonl"


def _read_messages():
    try:
        r = requests.get(JANE_URL, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    # fallback: read the log file directly
    try:
        with open(JANE_LOG) as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return []


def _force_inject(target, text):
    """Abort the in-flight turn, then resubmit the text as a live interrupt."""
    try:
        requests.post(f"{target}/cancel", timeout=5)
    except Exception:
        pass
    try:
        requests.post(
            f"{target}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": f"[LIVE INTERRUPT]: {text}"}]},
            timeout=30,
        )
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="http://127.0.0.1:8090")
    ap.add_argument("--poll", type=int, default=3, help="seconds between polls")
    args = ap.parse_args()

    msgs = _read_messages()
    last_seen = len(msgs)
    print(f"jane_bridge: watching {JANE_LOG} ({last_seen} msgs so far) -> {args.target}", flush=True)

    while True:
        try:
            msgs = _read_messages()
            if len(msgs) > last_seen:
                for m in msgs[last_seen:]:
                    who = (m.get("from") or "?").strip().lower()
                    text = (m.get("text") or "").strip()
                    if who == "sean" and text:
                        print(f"new message from sean: {text[:80]!r}", flush=True)
                        _force_inject(args.target, f"[JANE — new message from Sean]: {text}")
                last_seen = len(msgs)
        except Exception as e:
            print(f"jane_bridge error: {e}", flush=True)
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
