#!/usr/bin/env python3
"""Swarm orchestrator — drives a set of models (Mneme proxies and/or raw Ollama)
in a configurable loop, reading and writing folders. Control flow (goto / if) is
programmed in the YAML config.

CONFIG (swarm_config.yaml)
  Top level:
    ollama_url   base URL for backend=ollama steps (default http://localhost:11434)
    timeout      default per-call timeout in seconds (default 600)

  Each step:
    name          optional label — used as a jump target and in logs
    backend       "mneme" (default) | "ollama"
    port          Mneme proxy port            (required for backend=mneme)
    model         Ollama model name           (required for backend=ollama)
    options       Ollama generation options   (backend=ollama only, e.g.
                  temperature / num_predict / top_p / top_k)
    system_prompt optional system message. For ollama this is the role prompt; for
                  mneme the role prompt normally lives in that proxy's own
                  system_prompt.md, so leave it unset there unless you want an
                  extra instruction prepended.
    read_dir      directory to read context from
    write_dir     directory to write the model output to (as output.txt)
    clear_dir     directory to wipe. NOT allowed together with goto/if — clearing
                  on a jump erases context the next step needs to read.
    goto          label to jump to after this step. NOT allowed with clear_dir/if.
    if            branch on THIS step's model output:
        condition    contains | equals | startswith | endswith | matches (regex)
        value        the string / pattern to match
        then         label (or END) when the condition is true
        else         label (or END) when false (optional -> fall through)
    timeout       optional per-step request timeout override

  Reserved label: END — jumping to END stops the run. There is no iteration cap;
  the loop runs until it hits END or you Ctrl-C it.
"""

import os
import re
import sys

import requests
import yaml

END = -1  # sentinel index meaning "stop the run"


class Orchestrator:
    def __init__(self, config_path="swarm_config.yaml"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f) or {}
        self.steps = self.config.get("steps") or []
        self.ollama_url = self.config.get("ollama_url", "http://localhost:11434")
        self.timeout = int(self.config.get("timeout", 600))
        self.name_to_index = {}
        self._resolve_labels()
        self._validate_flow()

    # ---- startup validation (fail loud, before any model is called) ----

    def _resolve_labels(self):
        for i, s in enumerate(self.steps):
            nm = s.get("name")
            if not nm:
                continue
            if nm == "END":
                raise SystemExit(f"step {i}: 'END' is reserved — pick another name")
            if nm in self.name_to_index:
                raise SystemExit(
                    f"duplicate step name '{nm}' (steps {self.name_to_index[nm]} and {i})"
                )
            self.name_to_index[nm] = i

    def _validate_flow(self):
        targets = set(self.name_to_index) | {"END"}
        for i, s in enumerate(self.steps):
            nm = s.get("name") or f"#{i}"
            has_clear = bool(s.get("clear_dir"))
            has_goto = bool(s.get("goto"))
            has_if = bool(s.get("if"))
            if has_clear and (has_goto or has_if):
                raise SystemExit(
                    f"step {nm}: clear_dir cannot be combined with goto/if — "
                    "clearing on a jump erases context the next step needs to read"
                )
            backend = (s.get("backend") or "mneme").lower()
            if backend not in ("mneme", "ollama"):
                raise SystemExit(f"step {nm}: unknown backend '{s.get('backend')}'")
            if backend == "mneme" and not s.get("port"):
                raise SystemExit(f"step {nm}: backend=mneme requires 'port'")
            if backend == "ollama" and not s.get("model"):
                raise SystemExit(f"step {nm}: backend=ollama requires 'model'")
            if has_goto and s.get("goto") not in targets:
                raise SystemExit(f"step {nm}: goto target '{s.get('goto')}' not found")
            ifc = s.get("if")
            if ifc:
                cond = ifc.get("condition")
                if cond not in ("contains", "equals", "startswith", "endswith", "matches"):
                    raise SystemExit(f"step {nm}: bad if.condition '{cond}'")
                if cond != "matches" and ifc.get("value") in (None, ""):
                    raise SystemExit(f"step {nm}: if.condition '{cond}' needs a 'value'")
                if "then" not in ifc and "else" not in ifc:
                    raise SystemExit(f"step {nm}: if block needs 'then' and/or 'else'")
                for key in ("then", "else"):
                    t = ifc.get(key)
                    if t is not None and t not in targets:
                        raise SystemExit(f"step {nm}: if.{key} target '{t}' not found")

    # ---- folder IO ----

    def get_context(self, dir_path):
        """Recursively read all files under dir_path into one text blob."""
        if not dir_path or not os.path.exists(dir_path):
            return "NO_INPUT"
        files = []
        for root, dirs, names in os.walk(dir_path):
            # Skip hidden files/dirs (dot-prefixed, e.g. .ipynb_checkpoints, .DS_Store)
            # — they are editor/tooling artifacts, not story content.
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in names:
                if n.startswith("."):
                    continue
                files.append(os.path.join(root, n))
        files.sort()
        parts = []
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    parts.append(f"--- {os.path.relpath(fp, dir_path)} ---\n{f.read()}")
            except (OSError, UnicodeDecodeError) as e:
                print(f"  [read] error reading {fp}: {e}")
        return "\n\n".join(parts) if parts else "NO_INPUT"

    def write_output(self, dir_path, content):
        if not dir_path:
            return
        os.makedirs(dir_path, exist_ok=True)
        out = os.path.join(dir_path, "output.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  [write] {out}")

    def clear_dir(self, dir_path):
        """Recursively delete everything under dir_path (keeps the dir itself)."""
        if not dir_path or not os.path.exists(dir_path):
            return
        for root, dirs, files in os.walk(dir_path, topdown=False):
            for n in files:
                os.remove(os.path.join(root, n))
            for n in dirs:
                os.rmdir(os.path.join(root, n))
        print(f"  [clear] {dir_path}")

    # ---- backends ----

    def call_mneme(self, step, context):
        port = step["port"]
        url = f"http://localhost:{port}/v1/chat/completions"
        messages = []
        if step.get("system_prompt"):
            messages.append({"role": "system", "content": step["system_prompt"]})
        messages.append({"role": "user", "content": context})
        payload = {"model": "default", "messages": messages}
        timeout = step.get("timeout") or self.timeout
        try:
            r = requests.post(url, json=payload, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise SystemExit(f"mneme :{port} request failed: {e}")
        if r.status_code != 200:
            raise SystemExit(f"mneme :{port} HTTP {r.status_code}: {r.text[:400]}")
        try:
            return r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise SystemExit(f"mneme :{port} unexpected response: {r.text[:400]}")

    def call_ollama(self, step, context):
        model = step["model"]
        messages = []
        if step.get("system_prompt"):
            messages.append({"role": "system", "content": step["system_prompt"]})
        messages.append({"role": "user", "content": context})
        payload = {"model": model, "stream": False, "messages": messages}
        if step.get("options"):
            payload["options"] = step["options"]
        url = f"{self.ollama_url.rstrip('/')}/api/chat"
        timeout = step.get("timeout") or self.timeout
        try:
            r = requests.post(url, json=payload, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise SystemExit(f"ollama request failed: {e}")
        if r.status_code != 200:
            raise SystemExit(f"ollama HTTP {r.status_code}: {r.text[:400]}")
        try:
            return r.json()["message"]["content"]
        except (KeyError, TypeError):
            raise SystemExit(f"ollama unexpected response: {r.text[:400]}")

    # ---- control flow ----

    def _match(self, output, condition, value):
        out = output or ""
        if condition == "contains":
            return value in out
        if condition == "equals":
            return out.strip() == (value or "").strip()
        if condition == "startswith":
            return out.strip().startswith(value or "")
        if condition == "endswith":
            return out.strip().endswith(value or "")
        if condition == "matches":
            return re.search(value or "", out, re.DOTALL) is not None
        return False

    def _resolve_target(self, target):
        if target == "END":
            return END
        return self.name_to_index[target]

    def _next_index(self, step, output, cur):
        ifc = step.get("if")
        if ifc:
            hit = self._match(output, ifc["condition"], ifc.get("value", ""))
            print(f"  [if] {ifc['condition']} {ifc.get('value', '')!r} -> {hit}")
            target = ifc.get("then") if hit else ifc.get("else")
            if target is None:
                return cur + 1
            return self._resolve_target(target)
        goto = step.get("goto")
        if goto:
            print(f"  [goto] {goto}")
            return self._resolve_target(goto)
        return cur + 1

    # ---- main loop ----

    def run(self):
        print("Starting Orchestrator...")
        idx = 0
        while True:
            if idx == END or idx >= len(self.steps):
                print("\nOrchestration complete.")
                return
            step = self.steps[idx]
            name = step.get("name") or f"#{idx}"
            print(f"\n{'=' * 40}\nSTEP {name}")
            context = self.get_context(step.get("read_dir"))
            if step.get("read_dir"):
                print(f"  [read] {step['read_dir']} ({len(context)} chars)")

            # A model call is needed when we write output OR when we branch on it.
            needs_output = bool(step.get("write_dir")) or bool(step.get("if"))
            output = None
            if needs_output:
                backend = (step.get("backend") or "mneme").lower()
                if backend == "ollama":
                    output = self.call_ollama(step, context)
                else:
                    output = self.call_mneme(step, context)

            if step.get("write_dir") and output is not None:
                self.write_output(step["write_dir"], output)
            if step.get("clear_dir"):
                self.clear_dir(step["clear_dir"])

            idx = self._next_index(step, output, idx)


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "swarm_config.yaml"
    Orchestrator(cfg).run()
