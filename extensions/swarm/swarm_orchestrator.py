#!/usr/bin/env python3
"""Swarm orchestrator — a config-driven loop over Mneme proxies and/or raw Ollama.

This file is an EXAMPLE of how to write an extension that *consumes* the Mneme
proxy stack. It is deliberately self-contained: it talks to proxies ONLY over
HTTP (`http://localhost:<port>/v1/chat/completions`) and has no import, path, or
code dependency on the Mneme repo. You could write a serial driver, a parallel
fan-out, a cron job, or a full UI against the same endpoints without touching
proxy code. That is the integration contract Mneme exposes.

CONFIG (swarm_config.yaml)
  Top level:
    ollama_url   base URL for backend=ollama steps (default http://localhost:11434)
    timeout      default per-call timeout in seconds (default 600)
    max_steps    safety cap on the TOTAL number of step executions before the
                 orchestrator stops (default 0 = no limit). Catches an infinite
                 goto/if loop that never reaches END.

  Each step may carry any of the fields below. A step calls a model ONLY when it
  has `write_dir`, `append_dir`, or a STRING `if` condition (branches on output).
  A step with only folder actions (swap_dir / copy_dir / move_dir / clear_dir /
  goto / a folder-state `if`) never calls a model — use those to sequence the
  loop without burning a generation.

    name          optional label — used as a jump target and in logs
    backend       "mneme" (default) | "ollama"
    port          Mneme proxy port            (required for backend=mneme)
    model         Ollama model name           (required for backend=ollama)
    options       generation overrides for THIS call only (see below)
    system_prompt optional system message. For ollama this is the role prompt; for
                  mneme the role prompt normally lives in that proxy's own
                  system_prompt.md, so leave it unset there unless you want an
                  extra instruction prepended.
    retry         re-issue the model call up to this many EXTRA times on a
                  transient failure (connection/timeout error or HTTP 5xx), with
                  exponential backoff. Permanent errors (HTTP 4xx, malformed
                  response) still stop the run immediately. Default 0 (no retry).
    delay         pause this many seconds BEFORE the step runs (rate-limit a busy
                  proxy, or throttle the loop by putting it on the loop step).

    read_dir      directory (or a LIST of directories) to read context from. When
                  a list is given, the contents of every directory are read and
                  concatenated into one blob; each file is headed by
                  `--- <dir>/<relpath> ---` so the model can tell which source it
                  came from. (Single-directory reads keep the plain `--- path ---`
                  header for backward compatibility.)
    write_dir     where to write the model output (OVERWRITE). If the path ends in
                  a file extension (e.g. "pass1/a2_synthesis.txt") it is treated as
                  a full file path and the output is written to that exact file;
                  otherwise it is treated as a directory and output.txt is written
                  inside it.
    append_dir    like write_dir, but APPENDS to the target instead of overwriting
                  — for a running log or a story that grows across ticks.

    copy_dir      source file or directory to COPY elsewhere. Pair with `copy_to`.
                  Copies the source OUTSIDE the freeze/consume loop — e.g. snapshot
                  `input.active` into a persistent `buffer/` that survives the
                  tick's consume step. A directory source copies its CONTENTS into
                  the destination (merging; same-name files are overwritten); a
                  file source is copied into the destination folder under its own
                  name.
    copy_to       destination FOLDER for `copy_dir` (required with copy_dir).
    move_dir      source file or directory to MOVE (rename) elsewhere. Pair with
                  `move_to`. Atomic (os.rename) when source and destination share
                  a filesystem — the usual "promote draft -> final" primitive.
    move_to       destination FOLDER for `move_dir` (required with move_dir). The
                  source is moved INTO it under its own basename.

    clear_dir     directory (or a LIST of directories) to wipe — everything under
                  it is deleted, the directory itself is kept. NOT allowed together
                  with goto/if (clearing on a jump erases context the next step
                  needs to read). A list lets one step reset several boards at once.
    swap_dir      directory to freeze for this tick (atomic inbox swap): rename it
                  to <dir>.active and recreate a fresh empty <dir> for new writes.
                  Readers point read_dir at <dir>.active; a later clear_dir on
                  <dir>.active consumes the snapshot. Use on an action-only step
                  (no write_dir/if) so you control WHEN the freeze happens.
    goto          label to jump to after this step. NOT allowed with clear_dir/if.
    if            branch — either on THIS step's model output, or on filesystem
                  state (see below). Both forms have `then` / `else` labels.
    timeout       optional per-step request timeout override

  `if` has two forms:

    A) Branch on the model's output (requires a model call):
        if:
          condition: contains | equals | startswith | endswith | matches (regex)
          value:     the string / pattern to match
          then:      label (or END) when true
          else:      label (or END) when false (optional -> fall through)

    B) Branch on FILESYSTEM state (no model call — action-only):
        if:
          condition: count_ge | count_lt | empty | exists
          dir:       the directory (or path, for `exists`) to inspect
          value:     integer file count (required for count_ge / count_lt)
          then:      label when true
          else:      label when false (optional)
        count_ge / count_lt: number of files under `dir` vs `value`.
        empty:  `dir` has no files (or does not exist).
        exists: the path `dir` exists.

  Per-step `options` (overrides the backend's global settings for this call only):
    backend=mneme:  OpenAI-style — { temperature, top_p, top_k, max_tokens }
                    (the proxy maps these onto its own generation settings)
    backend=ollama: Ollama options — { temperature, top_p, top_k, num_predict }

  Reserved label: END — jumping to END stops the run. The loop otherwise runs
  until it hits END, the step list ends, the max_steps cap trips, or you Ctrl-C.
"""

import os
import re
import shutil
import sys
import time

import requests
import yaml

END = -1  # sentinel index meaning "stop the run"

# if-conditions that branch on FILESYSTEM state (no model call) vs the string
# conditions (contains/equals/startswith/endswith/matches) that branch on the
# step's model output.
_FOLDER_CONDITIONS = {"count_ge", "count_lt", "empty", "exists"}


class Orchestrator:
    def __init__(self, config_path="swarm_config.yaml"):
        self._config_path = config_path
        self._config_mtime = self._file_mtime()
        self._load_config()

    def _file_mtime(self):
        try:
            return os.path.getmtime(self._config_path)
        except OSError:
            return 0.0

    def _load_config(self):
        """Read the config file and (re)build the flow state. Fails loud on an
        invalid flow at startup; _maybe_reload() wraps this to keep the last good
        flow when a mid-run edit is still broken."""
        with open(self._config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f) or {}
        self.steps = self.config.get("steps") or []
        self.ollama_url = self.config.get("ollama_url", "http://localhost:11434")
        self.timeout = int(self.config.get("timeout", 600))
        self.max_steps = int(self.config.get("max_steps", 0) or 0)  # 0 = no cap
        self.name_to_index = {}
        self._resolve_labels()
        self._validate_flow()

    def _maybe_reload(self, idx):
        """Hot-reload: if the config file changed on disk, rebuild the flow and
        re-anchor the current position by step name, so an edit takes effect on the
        NEXT step with no restart. A broken/partial edit keeps the last good flow."""
        if not self._config_path:
            return idx
        try:
            mtime = os.path.getmtime(self._config_path)
        except OSError:
            return idx
        if mtime == self._config_mtime:
            return idx
        self._config_mtime = mtime
        anchor = self.steps[idx].get("name") if 0 <= idx < len(self.steps) else None
        snap = (self.config, self.steps, self.name_to_index,
                self.ollama_url, self.timeout, self.max_steps)
        try:
            self._load_config()
        except (SystemExit, yaml.YAMLError, ValueError, TypeError) as e:
            (self.config, self.steps, self.name_to_index,
             self.ollama_url, self.timeout, self.max_steps) = snap
            print(f"  [reload] config reload failed ({e}) — keeping current flow", flush=True)
            return idx
        if anchor is not None and anchor in self.name_to_index:
            print(f"  [reload] config reloaded ({len(self.steps)} steps)", flush=True)
            return self.name_to_index[anchor]
        print(f"  [reload] config reloaded — step '{anchor}' gone, restarting flow", flush=True)
        return 0

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

    def _step_needs_model(self, step):
        """True if this step calls a model: it writes/append output, or branches
        on output (a STRING if-condition). Folder-state if-conditions don't."""
        if step.get("write_dir") or step.get("append_dir"):
            return True
        ifc = step.get("if")
        return bool(ifc) and ifc.get("condition") not in _FOLDER_CONDITIONS

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
            # Two-part actions (copy/move) need source AND destination together.
            for src_key, dst_key in (("copy_dir", "copy_to"), ("move_dir", "move_to")):
                if bool(s.get(src_key)) != bool(s.get(dst_key)):
                    raise SystemExit(
                        f"step {nm}: {src_key} needs BOTH '{src_key}' (source) and "
                        f"'{dst_key}' (destination folder)"
                    )
            backend = (s.get("backend") or "mneme").lower()
            if backend not in ("mneme", "ollama"):
                raise SystemExit(f"step {nm}: unknown backend '{s.get('backend')}'")
            # A model is only called when the step writes/append output OR branches
            # on output — action-only steps (folder actions, folder-state if, goto,
            # read-only) never touch a backend, so requiring port/model there is
            # wrong and rejects otherwise-valid action-only configs.
            if self._step_needs_model(s):
                if backend == "mneme" and not s.get("port"):
                    raise SystemExit(f"step {nm}: backend=mneme requires 'port'")
                if backend == "ollama" and not s.get("model"):
                    raise SystemExit(f"step {nm}: backend=ollama requires 'model'")
            rd = s.get("read_dir")
            if rd is not None and not isinstance(rd, (str, list)):
                raise SystemExit(f"step {nm}: read_dir must be a string or a list of strings")
            for num_key in ("retry", "delay"):
                v = s.get(num_key)
                if v is not None:
                    try:
                        float(v)
                    except (TypeError, ValueError):
                        raise SystemExit(f"step {nm}: {num_key} must be a number")
            if has_goto and s.get("goto") not in targets:
                raise SystemExit(f"step {nm}: goto target '{s.get('goto')}' not found")
            ifc = s.get("if")
            if ifc:
                cond = ifc.get("condition")
                if cond in _FOLDER_CONDITIONS:
                    if not ifc.get("dir"):
                        raise SystemExit(f"step {nm}: if.condition '{cond}' needs a 'dir' to inspect")
                    if cond in ("count_ge", "count_lt") and ifc.get("value") in (None, ""):
                        raise SystemExit(f"step {nm}: if.condition '{cond}' needs a 'value' (integer count)")
                else:
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

    def _read_dir(self, dir_path):
        """Return a sorted list of (relative_path, text) for every non-hidden file
        under dir_path. Hidden files/dirs (dot-prefixed — editor/tooling artifacts
        like .ipynb_checkpoints, .DS_Store) are skipped."""
        files = []
        for root, dirs, names in os.walk(dir_path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in names:
                if n.startswith("."):
                    continue
                fp = os.path.join(root, n)
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        files.append((os.path.relpath(fp, dir_path), f.read()))
                except (OSError, UnicodeDecodeError) as e:
                    print(f"  [read] error reading {fp}: {e}")
        files.sort(key=lambda t: t[0])
        return files

    def get_context(self, dir_path):
        """Read one directory (or a LIST of directories) into one text blob.

        Each file is headed by `--- path ---`. When multiple directories are
        supplied, the path is prefixed with the directory's basename (e.g.
        `--- buffer/story.txt ---`) so the model can tell which source each file
        came from. A single directory keeps the plain `--- path ---` header.
        """
        if not dir_path:
            return "NO_INPUT"
        dirs = [dir_path] if isinstance(dir_path, str) else list(dir_path)
        multi = len(dirs) > 1
        parts = []
        for d in dirs:
            if not d or not os.path.exists(d):
                continue
            base = os.path.basename(d.rstrip(os.sep)) or d
            for rel, text in self._read_dir(d):
                label = os.path.join(base, rel) if multi else rel
                parts.append(f"--- {label} ---\n{text}")
        return "\n\n".join(parts) if parts else "NO_INPUT"

    def _resolve_output_path(self, dir_path):
        """Turn a write_dir/append_dir value into a concrete file path.

        A path with a file extension (e.g. "pass1/a2_synthesis.txt") is a full
        file path; a path without one is a directory, and output.txt is written
        inside it.
        """
        if os.path.splitext(dir_path)[1]:
            out = dir_path
            os.makedirs(os.path.dirname(out), exist_ok=True)
        else:
            os.makedirs(dir_path, exist_ok=True)
            out = os.path.join(dir_path, "output.txt")
        return out

    def write_output(self, dir_path, content):
        if not dir_path:
            return
        out = self._resolve_output_path(dir_path)
        with open(out, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  [write] {out}")

    def append_output(self, dir_path, content):
        if not dir_path:
            return
        out = self._resolve_output_path(dir_path)
        with open(out, "a", encoding="utf-8") as f:
            f.write(content)
        print(f"  [append] {out}")

    def copy_dir(self, source, dest):
        """Copy a file or directory into a destination folder.

        This is the "snapshot" primitive: it moves a COPY of some content OUTSIDE
        the freeze/consume loop, so it survives the tick even after the original
        is consumed. For example, copying `input.active` into `buffer/` before the
        consume step lets a later model read the frozen story after `input.active`
        has been cleared.

        - source is a directory -> its CONTENTS are copied into `dest` (recursively,
          merging with what is already there; same-name files are overwritten).
        - source is a file       -> copied into `dest` under its own name.
        To start from a clean slate, `clear_dir: dest` first (or on a prior step).
        """
        if not source or not dest:
            return
        if not os.path.exists(source):
            print(f"  [copy] source missing: {source}")
            return
        os.makedirs(dest, exist_ok=True)
        if os.path.isdir(source):
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(source, dest)
        print(f"  [copy] {source} -> {dest}")

    def move_dir(self, source, dest):
        """Move (rename) a file or directory into a destination folder.

        The "promote draft -> final" primitive. Atomic (os.rename) when source and
        destination are on the same filesystem, so a reader never sees a
        half-moved file. The source is moved INTO `dest` under its own basename
        (a file keeps its name; a directory moves as a whole).
        """
        if not source or not dest:
            return
        if not os.path.exists(source):
            print(f"  [move] source missing: {source}")
            return
        os.makedirs(dest, exist_ok=True)
        dst = os.path.join(dest, os.path.basename(source.rstrip(os.sep)))
        os.rename(source, dst)
        print(f"  [move] {source} -> {dst}")

    def clear_dir(self, dir_path):
        """Delete everything under dir_path (the directory itself is kept).

        Accepts a single directory or a LIST of directories, so one action-only
        step can reset several boards at the end of a tick.
        """
        if not dir_path:
            return
        targets = [dir_path] if isinstance(dir_path, str) else list(dir_path)
        for d in targets:
            if not d or not os.path.exists(d):
                continue
            for root, subdirs, files in os.walk(d, topdown=False):
                for n in files:
                    os.remove(os.path.join(root, n))
                for n in subdirs:
                    os.rmdir(os.path.join(root, n))
            print(f"  [clear] {d}")

    def swap_dir(self, dir_path):
        """Freeze dir_path for the current tick (atomic inbox swap).

        Renames dir_path -> dir_path + ".active" (the frozen snapshot readers use)
        and recreates a fresh empty dir_path so new writes land in the inbox for
        the NEXT tick. Any stale "<dir>.active" from a previous tick is removed
        first. The rename is atomic on a single filesystem, so there is no window
        where a reader sees a half-updated inbox.
        """
        if not dir_path:
            return
        active = dir_path + ".active"
        if os.path.exists(active):
            shutil.rmtree(active)  # stale snapshot from a previous tick
        if not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
        os.rename(dir_path, active)
        os.makedirs(dir_path, exist_ok=True)
        print(f"  [swap] {dir_path} -> {active}")

    def _count_files(self, dir_path):
        """Number of non-hidden files under dir_path (0 if missing)."""
        if not dir_path or not os.path.exists(dir_path):
            return 0
        return len(self._read_dir(dir_path))

    # ---- backends ----

    def _post_json(self, url, payload, timeout, retries, label):
        """POST `payload` to `url`, retrying transient failures.

        A transient failure is a connection/timeout error or an HTTP 5xx. Those are
        retried up to `retries` extra times with exponential backoff. Permanent
        errors (HTTP 4xx, a non-200 that isn't 5xx) stop the run immediately, as
        does exhausting the retries.
        """
        for attempt in range(retries + 1):
            try:
                r = requests.post(url, json=payload, timeout=timeout)
            except requests.exceptions.RequestException as e:
                if attempt < retries:
                    print(f"  [retry] {label} {type(e).__name__} — retrying ({attempt + 1}/{retries})", flush=True)
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"{label} request failed: {e}")
            if r.status_code == 200:
                return r
            if r.status_code >= 500 and attempt < retries:
                print(f"  [retry] {label} HTTP {r.status_code} — retrying ({attempt + 1}/{retries})", flush=True)
                time.sleep(2 ** attempt)
                continue
            raise SystemExit(f"{label} HTTP {r.status_code}: {r.text[:400]}")

    def call_mneme(self, step, context, retries=0):
        """Call a Mneme proxy over its OpenAI-compatible chat endpoint.

        This is the ONLY integration point with Mneme: a plain HTTP POST to
        http://localhost:<port>/v1/chat/completions with an OpenAI-style body.
        The proxy does the memory retrieval/injection, tool loop, and grading on
        its own — the orchestrator just sends messages and reads the reply.

        Two Mneme-specific details worth knowing when writing a consumer:
          - `options` must be nested under the top-level "options" key (not spread
            as bare fields) — the proxy ignores bare temperature/top_p/etc.
          - the proxy injects its OWN system prompt, so a per-step `system_prompt`
            here is an EXTRA instruction prepended, not a replacement.
        """
        port = step["port"]
        url = f"http://localhost:{port}/v1/chat/completions"
        messages = []
        if step.get("system_prompt"):
            messages.append({"role": "system", "content": step["system_prompt"]})
        messages.append({"role": "user", "content": context})
        payload = {"model": "default", "messages": messages}
        if step.get("options"):
            payload["options"] = step["options"]   # nested, opt-in (proxy ignores bare fields)
        timeout = step.get("timeout") or self.timeout
        r = self._post_json(url, payload, timeout, retries, f"mneme :{port}")
        try:
            return r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise SystemExit(f"mneme :{port} unexpected response: {r.text[:400]}")

    def call_ollama(self, step, context, retries=0):
        """Call a raw Ollama model over its native /api/chat endpoint."""
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
        r = self._post_json(url, payload, timeout, retries, "ollama")
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

    def _eval_folder_condition(self, condition, dir_path, value):
        if condition == "exists":
            return os.path.exists(dir_path or "")
        if condition == "empty":
            return self._count_files(dir_path) == 0
        n = self._count_files(dir_path)
        if condition == "count_ge":
            return n >= int(value)
        if condition == "count_lt":
            return n < int(value)
        return False

    def _resolve_target(self, target):
        if target == "END":
            return END
        return self.name_to_index[target]

    def _next_index(self, step, output, cur):
        ifc = step.get("if")
        if ifc:
            cond = ifc["condition"]
            if cond in _FOLDER_CONDITIONS:
                hit = self._eval_folder_condition(cond, ifc.get("dir"), ifc.get("value"))
                print(f"  [if] {cond} {ifc.get('dir')!r} {ifc.get('value')!r} -> {hit}")
            else:
                hit = self._match(output, cond, ifc.get("value", ""))
                print(f"  [if] {cond} {ifc.get('value', '')!r} -> {hit}")
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
        steps_run = 0
        while True:
            if idx == END or idx >= len(self.steps):
                print("\nOrchestration complete.")
                return
            if self.max_steps > 0 and steps_run >= self.max_steps:
                print(f"\n[max_steps] reached {self.max_steps} step executions — stopping.")
                return
            steps_run += 1
            # Hot-reload: pick up edits to the config file without a restart.
            idx = self._maybe_reload(idx)
            if idx == END or idx >= len(self.steps):
                print("\n[reload] flow has no more steps — stopping.")
                return
            step = self.steps[idx]
            name = step.get("name") or f"#{idx}"
            print(f"\n{'=' * 40}\nSTEP {name}")

            # Optional pacing delay before the step runs.
            if step.get("delay"):
                print(f"  [delay] {float(step['delay'])}s")
                time.sleep(float(step["delay"]))

            # 1. Read input context (single dir or a list of dirs).
            context = self.get_context(step.get("read_dir"))
            if step.get("read_dir"):
                src = step["read_dir"]
                srcs = src if isinstance(src, list) else [src]
                print(f"  [read] {', '.join(srcs)} ({len(context)} chars)")

            # 2. Call a model only when we write/append output OR branch on output.
            output = None
            if self._step_needs_model(step):
                backend = (step.get("backend") or "mneme").lower()
                retries = int(step.get("retry") or 0)
                if backend == "ollama":
                    output = self.call_ollama(step, context, retries)
                else:
                    output = self.call_mneme(step, context, retries)

            # 3. Apply folder actions in a fixed, predictable order.
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

            idx = self._next_index(step, output, idx)


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "swarm_config.yaml"
    Orchestrator(cfg).run()
