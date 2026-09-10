#!/usr/bin/env python3
"""Write the current date/time to a file (system local time by default).

A tiny utility the swarm orchestrator can invoke via its `exec` step so the
models it drives share a single, consistent "now" to anchor on. Different models
have different training cutoffs, and small models otherwise assume current
events are made up — giving them a real timestamp in their context fixes that.
Point `read_dir` at the file this writes to make it part of a step's input.

The target follows the SAME rule as the orchestrator's `write_dir`, with one
improvement: an EXISTING directory is always treated as a directory.
  - an existing directory -> "timestamp.txt" inside it (even a dotted name like
    "raw.active");
  - a path WITH a file extension -> written to exactly (e.g. "board/now.txt");
  - a path WITHOUT one -> a new directory, "timestamp.txt" inside it (e.g. "board").

Usage:
    now.py [OUTPUT]           # file (with extension) or directory (timestamp.txt)
    now.py -u [OUTPUT]        # UTC instead of local time
"""
import os
import sys
import datetime


def resolve(out):
    """Return the concrete file path. An EXISTING directory is always treated as
    a directory (so a dotted name like `raw.active` still gets timestamp.txt
    inside it); otherwise the extension rule applies (extension = file, none =
    directory)."""
    if os.path.isdir(out):
        return os.path.join(out, "timestamp.txt")
    if os.path.splitext(out)[1]:
        parent = os.path.dirname(out)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return out
    os.makedirs(out, exist_ok=True)
    return os.path.join(out, "timestamp.txt")


def main():
    args = sys.argv[1:]
    utc = False
    if args and args[0] == "-u":
        utc = True
        args = args[1:]
    out = resolve(args[0]) if args else "timestamp.txt"

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
