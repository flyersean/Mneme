"""Per-run workspace — an identifiable working environment for every run.

    <runs_root>/<run_id>/
        input/        files handed to the run
        workspace/    scratch space the run works in
        artifacts/    results the run produced
        logs/         run-specific logs
        checkpoints/  human-readable mirror of ledger checkpoints (the ledger is authoritative)

Tools are not yet forced to use this layout; it is the abstraction later phases
(permissions, per-run tool cwd, artifact capture) build on.
"""

from __future__ import annotations

import json
import os

SUBDIRS = ("input", "workspace", "artifacts", "logs", "checkpoints")


class RunWorkspace:
    def __init__(self, root: str, run_id: str):
        if not run_id or "/" in run_id or run_id.startswith("."):
            raise ValueError(f"invalid run id for a workspace: {run_id!r}")
        self.root = os.path.abspath(os.path.expanduser(root))
        self.run_id = run_id
        self.path = os.path.join(self.root, run_id)

    def ensure(self) -> "RunWorkspace":
        for sub in SUBDIRS:
            os.makedirs(os.path.join(self.path, sub), exist_ok=True)
        return self

    def dir(self, kind: str) -> str:
        if kind not in SUBDIRS:
            raise ValueError(f"unknown workspace dir {kind!r} (one of {', '.join(SUBDIRS)})")
        return os.path.join(self.path, kind)

    def resolve(self, kind: str, relpath: str) -> str:
        """Path inside a workspace dir; refuses to escape it."""
        base = self.dir(kind)
        full = os.path.abspath(os.path.join(base, relpath))
        if os.path.commonpath([base, full]) != base:
            raise ValueError(f"path escapes the run workspace: {relpath!r}")
        return full

    def write_checkpoint_mirror(self, seq: int, state: dict) -> str:
        path = os.path.join(self.dir("checkpoints"), f"{seq:05d}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, path)
        return path

    def append_log(self, line: str) -> None:
        with open(os.path.join(self.dir("logs"), "run.log"), "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
