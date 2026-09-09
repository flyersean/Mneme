#!/usr/bin/env python3
"""Write the current date/time to a file (system local time by default).

A tiny utility the swarm orchestrator can invoke via its `exec` step so the
models it drives share a single, consistent "now" to anchor on. Different models
have different training cutoffs, and small models otherwise assume current
events are made up — giving them a real timestamp in their context fixes that.
Point `read_dir` at the file this writes to make it part of a step's input.

Usage:
    now.py [OUTPUT_FILE]        # default: timestamp.txt (system local time)
    now.py -u [OUTPUT_FILE]     # UTC instead of local time
"""
import sys
import datetime


def main():
    args = sys.argv[1:]
    utc = False
    if args and args[0] == "-u":
        utc = True
        args = args[1:]
    out = args[0] if args else "timestamp.txt"

    if utc:
        now = datetime.datetime.now(datetime.timezone.utc)
    else:
        now = datetime.datetime.now().astimezone()
    line = now.strftime("%Y-%m-%d %H:%M:%S %Z (%z)")
    with open(out, "w", encoding="utf-8") as f:
        f.write(line + "\n")
    print(f"{line} -> {out}")


if __name__ == "__main__":
    main()
