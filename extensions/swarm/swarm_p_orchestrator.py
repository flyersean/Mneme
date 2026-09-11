#!/usr/bin/env python3
"""Parallel swarm orchestrator — one new step form on top of the serial one.

Extends swarm_orchestrator.Orchestrator with a `parallel:` block that fans out a
list of independent steps concurrently (via a thread pool). Everything else —
primitives (read_dir/write_dir/append_dir/copy_dir/move_dir/swap_dir/clear_dir/
exec/goto/if), backends (mneme/ollama), options, hot-reload, max_steps — is
inherited unchanged, so a config written for swarm_orchestrator.py also runs
here, plus it may add `parallel:` blocks.

WHEN PARALLEL HELPS
  Concurrent steps only speed things up when Ollama can actually serve them at
  once: requests to the SAME model batch/parallelize (up to OLLAMA_NUM_PARALLEL),
  while requests to DIFFERENT models that don't both fit in VRAM get serialized
  by Ollama (load A, evict, load B) — no win there, just swap latency. So fan out
  steps that share one model, e.g. several critics on the same port.

CONFIG — a `parallel:` step is a list of leaf sub-steps (no goto/if inside):

    steps:
      - name: freeze
        swap_dir: input
      - parallel:
          - name: critic_structure
            backend: mneme
            port: 8080
            read_dir: input.active
            write_dir: pass1/structure.txt
            system_prompt: "You are a structure critic."
          - name: critic_prose
            backend: mneme
            port: 8080
            read_dir: input.active
            write_dir: pass1/prose.txt
            system_prompt: "You are a prose critic."
      - name: consume
        clear_dir: input.active

Run:  python3 swarm_p_orchestrator.py [config.yaml]
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor

from swarm_orchestrator import Orchestrator, END


class ParallelOrchestrator(Orchestrator):
    def _exec_sub_step(self, step, header=None):
        """Execute ONE leaf sub-step (no goto/if). Mirrors the per-step dispatch
        in Orchestrator.run(): delay -> exec -> read -> model -> write/append ->
        folder actions, and returns the model output (for serial string-if).
        Thread-safe: sub-steps share only read-only config; each writes a
        distinct output path. stdout may interleave under concurrency."""
        if header:
            print(header)
        if step.get("delay"):
            print(f"  [delay] {float(step['delay'])}s")
            time.sleep(float(step["delay"]))
        if step.get("exec"):
            self.run_exec(step["exec"])
        context = self.get_context(step.get("read_dir"))
        if step.get("read_dir"):
            src = step["read_dir"]
            srcs = src if isinstance(src, list) else [src]
            print(f"  [read] {', '.join(srcs)} ({len(context)} chars)")
        output = None
        if self._step_needs_model(step):
            backend = (step.get("backend") or "mneme").lower()
            retries = int(step.get("retry") or 0)
            if backend == "ollama":
                output = self.call_ollama(step, context, retries)
            else:
                output = self.call_mneme(step, context, retries)
        if step.get("write_dir") and output is not None:
            self.write_output(step["write_dir"], output)
        if step.get("append_dir") and output is not None:
            self.append_output(step["append_dir"], output)
        if step.get("copy_dir"):
            self.copy_dir(step["copy_dir"], step.get("copy_to"))
        if step.get("move_dir"):
            self.move_dir(step["move_dir"], step.get("move_to"))
        if step.get("swap_dir"):
            self.swap_dir(step["swap_dir"])
        if step.get("clear_dir"):
            self.clear_dir(step["clear_dir"])
        return output

    def _run_parallel(self, block):
        """Fan out a list of independent sub-steps concurrently; wait for all."""
        print(f"\n{'=' * 40}\nPARALLEL ({len(block)} steps)")
        t0 = time.time()

        def _one(sub):
            return self._exec_sub_step(sub, header=f"    [parallel] {sub.get('name') or '(sub)'}")

        with ThreadPoolExecutor(max_workers=len(block)) as pool:
            list(pool.map(_one, block))
        print(f"  [parallel] done in {time.time() - t0:.1f}s")

    def run(self):
        print("Starting Parallel Orchestrator...")
        idx = 0
        steps_run = 0
        while True:
            if idx == END or idx >= len(self.steps):
                print("\nOrchestration complete.")
                return
            if self.max_steps > 0 and steps_run >= self.max_steps:
                print(f"\n[max_steps] reached {self.max_steps} step executions — stopping.")
                return
            steps_run += 1
            idx = self._maybe_reload(idx)
            if idx == END or idx >= len(self.steps):
                print("\n[reload] flow has no more steps — stopping.")
                return
            step = self.steps[idx]

            # A `parallel:` block fans out its sub-steps concurrently.
            if "parallel" in step:
                self._run_parallel(step["parallel"])
                idx += 1
                continue

            name = step.get("name") or f"#{idx}"
            output = self._exec_sub_step(step, header=f"\n{'=' * 40}\nSTEP {name}")
            idx = self._next_index(step, output, idx)


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "swarm_config.yaml"
    ParallelOrchestrator(cfg).run()
