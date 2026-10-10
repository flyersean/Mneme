#!/usr/bin/env python3
"""Insert a mid-stream repetition-loop guard into the OpenRouter stream reader.

Patch target: proxy/mneme_proxy.py — the OpenAI/OpenRouter streaming loop
where content deltas append to content_parts. On detecting N consecutive
identical content deltas (a degenerate repetition loop), abort the stream,
keep the partial output, and return done_reason="loop".
"""
import re
import sys

PATH = "/home/ubuntu/mneme/repo/proxy/mneme_proxy.py"

APPEND_LINE = "                content_parts.append(delta[\"content\"])\n"

GUARD = '''                content_parts.append(delta["content"])
                # [LOOP-GUARD] mid-stream repetition detector: if the last
                # LOOP_WINDOW content deltas are byte-identical (and non-
                # trivial), the model is stuck in a repetition loop (seen with
                # GLM at low temperature). Abort the stream, keep the partial
                # output, and let the retry loop re-query. Cheap: only compares
                # small recent deltas, never the whole buffer.
                _d = delta["content"]
                if len(_d) > 1:
                    _recent.append(_d)
                if len(_recent) > _LOOP_WINDOW:
                    _recent.popleft()
                if (len(_recent) == _LOOP_WINDOW
                        and len(set(_recent)) == 1
                        and sum(len(x) for x in _recent) >= 24):
                    print(f"  [LOOP-GUARD] {len(content_parts)} content chunks "
                          f"in, last {_LOOP_WINDOW} deltas identical — aborting "
                          f"repetition loop, keeping partial", flush=True)
                    return {"content": "".join(content_parts),
                            "thinking": "".join(reasoning_parts),
                            "tool_calls": [], "eval_count": 0,
                            "done_reason": "loop", "provider": provider}
'''

SETUP = '''    content_parts = []
    # [LOOP-GUARD] state: recent content deltas for repetition detection
    _LOOP_WINDOW = 8
    _recent = collections.deque()
'''

src = open(PATH).read()

if "[LOOP-GUARD]" in src:
    print("already patched — nothing to do")
    sys.exit(0)

# 1) insert the guard right after the content append inside the stream loop
if APPEND_LINE not in src:
    print("FATAL: content append line not found verbatim", file=sys.stderr)
    sys.exit(1)
src = src.replace(APPEND_LINE, GUARD, 1)

# 2) add the deque setup next to content_parts = [] in the same function
#    (first occurrence is the OpenRouter streaming reader at ~3065)
anchor = "    content_parts = []\n"
if anchor not in src:
    print("FATAL: content_parts anchor not found", file=sys.stderr)
    sys.exit(1)
src = src.replace(anchor, "    content_parts = []\n    # [LOOP-GUARD] state: recent content deltas for repetition detection\n    _LOOP_WINDOW = 8\n    _recent = collections.deque()\n", 1)

open(PATH, "w").write(src)
print("patched OK")