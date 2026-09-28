"""Harness control commands — typed by the user, handled by the harness, never the model.

Same authority model as the existing <<SAVE>> / <<SETTINGS>> commands: a command
controls the system, so it comes from the human (chat, CLI, a gateway), not from
content the model read. Unknown "/words" return None so ordinary messages that
happen to start with "/" still go to the model.

Run references accept a full id, a unique id suffix/prefix, or "last".
"""

from __future__ import annotations

import json
from typing import Callable, Dict, Optional

from mneme.harness.ledger import InvalidTransition, LedgerError

HELP = """Harness commands (typed by you, handled by Mneme — the model never sees them):
  /run <goal>                 start a planned run            /runs [status]      list runs
  /status [run]               run status (default: last)     /plan <run>         current plan
  /tasks <run>                tasks + state                  /log <run> [n]      last n events
  /files <run>                artifacts                      /metrics            harness metrics
  /pause|/resume|/cancel|/retry <run>
  /replan <run> [reason]      replan the remaining work
  /approve <run> [note]       /reject <run> [reason]         approve/reject a waiting task
  /skills [query]             /tools                         /profiles
  /strategies                 /memory|/search <query>        /evolution [status]
  /approve-change <id>        /reject-change <id>            self-improvement proposals
  /jobs                       /config                        /models
  /help"""


def _resolve(engine, ref: str) -> str:
    ref = (ref or "last").strip()
    if ref == "last":
        runs = engine.ledger.list_runs(limit=1)
        if not runs:
            raise LedgerError("no runs yet")
        return runs[0]["run_id"]
    if engine.ledger.get_run(ref):
        return ref
    hits = [r["run_id"] for r in engine.ledger.list_runs(limit=500)
            if r["run_id"].endswith(ref) or r["run_id"].startswith(ref)]
    if len(hits) == 1:
        return hits[0]
    raise LedgerError(f"no such run: {ref}" if not hits else f"ambiguous run ref {ref!r} ({len(hits)} matches)")


def _run_line(r: dict) -> str:
    return f"{r['run_id']}  [{r['status']}]  {r['goal'][:70]}"


def _status(engine, rid: str) -> str:
    d = engine.ledger.run_detail(rid)
    r = d["run"]
    tasks = d["tasks"]
    done = sum(t["status"] == "completed" for t in tasks)
    u = r.get("usage") or {}
    lines = [_run_line(r),
             f"  tasks {done}/{len([t for t in tasks if t['status'] != 'skipped'])} done · steps {u.get('steps', 0)}"
             f" · tool calls {u.get('tool_calls', 0)} · replans {u.get('replans', 0)} · failures {u.get('failures', 0)}"]
    cur = next((t for t in tasks if t["task_id"] == r.get("current_task_id")), None)
    if cur and r["status"] not in ("completed", "failed", "cancelled"):
        lines.append(f"  current: {cur['title']}")
    if r["status"] == "awaiting_approval":
        req = json.loads(r.get("approval_state") or "{}")
        lines.append(f"  WAITING FOR APPROVAL: {req.get('title', '?')}  → /approve {rid[-8:]}  or  /reject {rid[-8:]}")
    if r.get("error"):
        lines.append(f"  error: {r['error'][:300]}")
    if r["status"] == "completed" and r.get("result"):
        lines.append("  result: " + r["result"][:600])
    return "\n".join(lines)


def handle(text: str, engine, extras: Optional[Dict[str, Callable]] = None, actor: str = "user") -> Optional[str]:
    """Return the reply for a harness command, or None if `text` is not one."""
    text = (text or "").strip()
    if not text.startswith("/") or engine is None:
        return None
    word, _, arg = text[1:].partition(" ")
    word, arg = word.lower(), arg.strip()
    extras = extras or {}
    first, _, rest = arg.partition(" ")
    try:
        if word == "help":
            return HELP
        if word == "run":
            if not arg:
                return "usage: /run <goal>"
            r = engine.create(arg, start=True, created_by=actor)
            return f"started {r['run_id']} — /status {r['run_id'][-8:]}"
        if word == "runs":
            runs = engine.ledger.list_runs(status=[arg] if arg else None, limit=20)
            return "\n".join(_run_line(r) for r in runs) or "no runs"
        if word == "status":
            return _status(engine, _resolve(engine, arg))
        if word == "plan":
            r = engine.ledger.require_run(_resolve(engine, arg))
            p = r.get("plan") or {}
            return (f"plan v{p.get('version', 0)} ({p.get('source', '?')})\n"
                    + "\n".join(f"  {i + 1}. {t}" for i, t in enumerate(p.get("tasks") or [])))
        if word == "tasks":
            ts = engine.ledger.list_tasks(_resolve(engine, arg))
            return "\n".join(f"  [{t['status']:<9}] {t['title']}" + (f" — {t['error'][:120]}" if t["error"] else "")
                             for t in ts) or "no tasks yet"
        if word == "log":
            rid = _resolve(engine, first)
            n = int(rest) if rest.isdigit() else 15
            evs = engine.ledger.events(rid, limit=100_000)[-n:]
            return "\n".join(f"  {e['created_at'][11:19]} {e['type']}"
                             + (f" {json.dumps(e['data'])[:120]}" if e["data"] else "") for e in evs)
        if word == "files":
            arts = engine.ledger.list_artifacts(_resolve(engine, arg))
            return "\n".join(f"  {a['path']}  ({a['size']} bytes, sha256 {a['sha256'][:12]})" for a in arts) or "no artifacts"
        if word in ("pause", "resume", "cancel", "retry"):
            rid = _resolve(engine, arg)
            getattr(engine, word)(rid, actor=actor)
            return _status(engine, rid)
        if word == "replan":
            rid = _resolve(engine, first)
            engine.request_replan(rid, rest or "requested by user", actor=actor)
            return f"replan requested for {rid}"
        if word == "approve":
            rid = _resolve(engine, first)
            engine.approve(rid, actor=actor, note=rest)
            return _status(engine, rid)
        if word == "reject":
            rid = _resolve(engine, first)
            engine.reject(rid, actor=actor, reason=rest)
            return _status(engine, rid)
        if word == "skills":
            reg = engine.skills
            if reg is None:
                return "no skill registry"
            items = reg.select(arg, k=5, min_score=0.0) if arg else reg.list()
            return "\n".join(f"  {s['name']} v{s['version']} — {s['description'][:90]} (uses {s['uses']})"
                             for s in items) or "no skills"
        if word == "tools":
            from mneme.harness import capabilities as caps
            names = list(extras["tools"]()) if "tools" in extras else list(caps.BUILTIN_TOOL_META)
            return "\n".join(f"  {m['name']} [{m['permission']}, risk {m['risk']}] {m['does']}"
                             for m in (caps.tool_meta(n) for n in names))
        if word == "profiles":
            if engine.profiles is None:
                return "profiles not configured"
            return "\n".join(f"  {p['name']} v{p['version']} — {p['spec'].get('description', '')}"
                             for p in engine.profiles.list())
        if word == "evolution":
            evo = engine.evolution
            if evo is None:
                return "self-improvement not configured"
            return "\n".join(f"  {p['proposal_id']} L{p['level']} {p['kind']}:{p['target']} [{p['status']}] "
                             f"{p['reason'][:60]}" for p in evo.list(status=arg or None, limit=20)) or "no proposals"
        if word in ("approve-change", "reject-change"):
            evo = engine.evolution
            if evo is None:
                return "self-improvement not configured"
            p = evo.approve(first, actor=actor) if word == "approve-change" else evo.reject(first, actor=actor, reason=rest)
            return f"{p['proposal_id']} -> {p['status']}"
        if word == "metrics":
            from mneme.harness.metrics import compute
            return json.dumps(compute(engine.ledger, engine.skills, engine.evolution), indent=2)
        if word == "jobs":
            jobs = getattr(engine, "jobs", None)
            if jobs is None:
                return "jobs not configured"
            return "\n".join(f"  {j['job_id']} [{'on' if j['enabled'] else 'off'}] every {j['interval_s']}s "
                             f"— {j['name']} (last run {j['last_run_id'] or '-'})" for j in jobs.list()) or "no jobs"
        if word in ("strategies", "memory", "search", "config", "models"):
            fn = extras.get("search" if word == "memory" else word)
            if fn is None:
                return f"/{word} is not available here"
            return fn(arg)
    except (LedgerError, InvalidTransition) as e:
        return f"/{word}: {e}"
    return None  # not a harness command — let the model have it
