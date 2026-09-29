"""/runs HTTP API — harness run control over the proxy's existing Flask app.

    POST /runs                      create {goal, tasks?, budget?, profile?, session_id?,
                                            parent_run_id?, meta?, start?=true}
    GET  /runs                      list  ?status=a,b &limit= &offset= &parent=
    GET  /runs/<id>                 detail (run, tasks, steps, tool_calls, artifacts,
                                            checkpoints, children) ?events=1 to include events
    GET  /runs/<id>/events          event stream ?after=<event_id> &limit= &types=a,b
    GET  /runs/<id>/checkpoints     checkpoint list; /checkpoints/<cp_id> for one state
    POST /runs/<id>/pause | cancel | retry | checkpoint
    POST /runs/<id>/resume          {checkpoint_id?} — optionally roll back first
    POST /runs/<id>/artifacts       {path, kind?, description?} — register a produced file

Errors: 404 unknown run, 409 invalid state transition, 400 bad request,
503 harness disabled.
"""

from __future__ import annotations

from typing import Callable, Optional

from mneme.harness.ledger import InvalidTransition, LedgerError


def register(app, get_engine: Callable[[], Optional[object]], respond: Callable,
             extras: Optional[Callable[[], dict]] = None, static_dir: Optional[str] = None):
    from flask import request
    import os as _os

    def _engine():
        eng = get_engine()
        if eng is None:
            return None, respond({"error": "harness disabled (harness.enabled: false) or failed to start"}, 503)
        return eng, None

    def _guard(fn):
        try:
            return fn()
        except InvalidTransition as e:
            return respond({"error": str(e)}, 409)
        except LedgerError as e:
            msg = str(e)
            return respond({"error": msg}, 404 if msg.startswith("no such run") else 400)

    def _body():
        return request.get_json(force=True, silent=True) or {}

    @app.route("/runs", methods=["POST"])
    def harness_runs_create():
        eng, err = _engine()
        if err:
            return err
        data = _body()

        def go():
            tasks = data.get("tasks")
            if tasks is not None and not isinstance(tasks, list):
                raise LedgerError("tasks must be a list")
            run = eng.create(
                data.get("goal") or "", tasks, budget=data.get("budget") or {},
                profile=str(data.get("profile") or ""), session_id=str(data.get("session_id") or ""),
                parent_run_id=str(data.get("parent_run_id") or ""), meta=data.get("meta") or {},
                permissions=data.get("permissions") or {},
                created_by=str(data.get("created_by") or "user"),
                start=bool(data.get("start", True)),
                plan=(None if data.get("plan") is None else bool(data.get("plan"))),
            )
            return respond({"run": run}, 201)
        return _guard(go)

    @app.route("/runs", methods=["GET"])
    def harness_runs_list():
        eng, err = _engine()
        if err:
            return err
        status = [s for s in (request.args.get("status") or "").split(",") if s] or None
        runs = eng.ledger.list_runs(status=status, limit=int(request.args.get("limit", 50)),
                                    offset=int(request.args.get("offset", 0)),
                                    parent_run_id=request.args.get("parent"))
        return respond({"runs": runs})

    @app.route("/runs/<run_id>", methods=["GET"])
    def harness_run_detail(run_id):
        eng, err = _engine()
        if err:
            return err
        detail = eng.ledger.run_detail(run_id, include_events=request.args.get("events") == "1")
        if detail is None:
            return respond({"error": f"no such run: {run_id}"}, 404)
        detail["executing"] = eng.is_executing(run_id)
        return respond(detail)

    @app.route("/runs/<run_id>/events", methods=["GET"])
    def harness_run_events(run_id):
        eng, err = _engine()
        if err:
            return err
        if eng.ledger.get_run(run_id) is None:
            return respond({"error": f"no such run: {run_id}"}, 404)
        types = [t for t in (request.args.get("types") or "").split(",") if t] or None
        events = eng.ledger.events(run_id, after_id=int(request.args.get("after", 0)),
                                   limit=int(request.args.get("limit", 1000)), types=types)
        return respond({"events": events,
                        "last_event_id": events[-1]["event_id"] if events else int(request.args.get("after", 0))})

    @app.route("/runs/<run_id>/checkpoints", methods=["GET"])
    def harness_run_checkpoints(run_id):
        eng, err = _engine()
        if err:
            return err
        if eng.ledger.get_run(run_id) is None:
            return respond({"error": f"no such run: {run_id}"}, 404)
        return respond({"checkpoints": eng.ledger.list_checkpoints(run_id)})

    @app.route("/runs/<run_id>/checkpoints/<checkpoint_id>", methods=["GET"])
    def harness_run_checkpoint(run_id, checkpoint_id):
        eng, err = _engine()
        if err:
            return err
        cp = eng.ledger.get_checkpoint(checkpoint_id)
        if cp is None or cp["run_id"] != run_id:
            return respond({"error": "no such checkpoint"}, 404)
        return respond({"checkpoint": cp})

    def _control(run_id, action):
        eng, err = _engine()
        if err:
            return err
        data = _body()
        actor = str(data.get("actor") or "user")

        def go():
            if action == "pause":
                run = eng.pause(run_id, actor=actor)
            elif action == "cancel":
                run = eng.cancel(run_id, actor=actor)
            elif action == "resume":
                run = eng.resume(run_id, checkpoint_id=data.get("checkpoint_id") or None, actor=actor)
            elif action == "retry":
                run = eng.retry(run_id, actor=actor)
            elif action == "approve":
                run = eng.approve(run_id, actor=actor, note=str(data.get("note") or ""))
            elif action == "reject":
                run = eng.reject(run_id, actor=actor, reason=str(data.get("reason") or ""))
            else:  # checkpoint
                eng.ledger.require_run(run_id)
                return respond({"checkpoint": eng.checkpoint(run_id, reason=str(data.get("reason") or "manual"))})
            return respond({"run": run})
        return _guard(go)

    for _action in ("pause", "cancel", "resume", "retry", "checkpoint", "approve", "reject"):
        app.add_url_rule(f"/runs/<run_id>/{_action}", f"harness_run_ctl_{_action}",
                         (lambda a: lambda run_id: _control(run_id, a))(_action), methods=["POST"])

    # ── skills (Phase 3) ──
    def _skills():
        eng, err = _engine()
        if err:
            return None, err
        if getattr(eng, "skills", None) is None:
            return None, respond({"error": "no skill registry"}, 503)
        return eng.skills, None

    @app.route("/skills", methods=["GET"])
    def harness_skills_list():
        reg, err = _skills()
        if err:
            return err
        q = request.args.get("q")
        items = reg.select(q, k=int(request.args.get("k", 5)), min_score=0.0) if q else \
            reg.list(include_inactive=request.args.get("all") == "1")
        return respond({"skills": [{k: v for k, v in s.items() if k != "body"} for s in items]})

    @app.route("/skills", methods=["POST"])
    def harness_skills_upsert():
        reg, err = _skills()
        if err:
            return err
        d = _body()
        return _guard(lambda: respond({"skill": reg.upsert(
            d.get("name") or "", d.get("description") or "", d.get("body") or "",
            actor=str(d.get("actor") or "user"), reason=str(d.get("reason") or ""),
            source=str(d.get("source") or "user"),
            **{k: d[k] for k in ("tools", "requires", "strategies", "verify", "failure_modes", "tags") if k in d})}, 201))

    @app.route("/skills/<name>", methods=["GET"])
    def harness_skill_detail(name):
        reg, err = _skills()
        if err:
            return err
        sk = reg.get(name)
        if sk is None:
            return respond({"error": f"no such skill: {name}"}, 404)
        return respond({"skill": sk, "history": reg.history(name)})

    @app.route("/skills/<name>/restore", methods=["POST"])
    def harness_skill_restore(name):
        reg, err = _skills()
        if err:
            return err
        return _guard(lambda: respond({"skill": reg.restore_version(name, int(_body().get("version") or 0))}))

    # ── evolution (Phase 6) ──
    def _evo():
        eng, err = _engine()
        if err:
            return None, err
        if getattr(eng, "evolution", None) is None:
            return None, respond({"error": "self-improvement is not configured"}, 503)
        return eng.evolution, None

    @app.route("/evolution", methods=["GET"])
    def harness_evolution_list():
        evo, err = _evo()
        if err:
            return err
        a = request.args
        if a.get("kind") and a.get("target"):
            return respond({"proposals": evo.changes_to(a["kind"], a["target"])})
        return respond({"proposals": evo.list(status=a.get("status"), kind=a.get("kind"),
                                              limit=int(a.get("limit", 100)))})

    @app.route("/evolution", methods=["POST"])
    def harness_evolution_propose():
        evo, err = _evo()
        if err:
            return err
        d = _body()
        content = d.get("content")
        if isinstance(content, (dict, list)):
            import json as _json
            content = _json.dumps(content)
        return _guard(lambda: respond({"proposal": evo.propose(
            str(d.get("kind") or ""), str(d.get("target") or ""), content,
            reason=str(d.get("reason") or ""), evidence=d.get("evidence") or [], tests=d.get("tests"),
            created_by=str(d.get("created_by") or "user"), level=d.get("level"))}, 201))

    @app.route("/evolution/<pid>", methods=["GET"])
    def harness_evolution_detail(pid):
        evo, err = _evo()
        if err:
            return err
        p = evo.get(pid)
        if p is None:
            return respond({"error": f"no such proposal: {pid}"}, 404)
        return respond({"proposal": p, "log": evo.history(pid)})

    def _evo_action(pid, action):
        evo, err = _evo()
        if err:
            return err
        d = _body()
        actor = str(d.get("actor") or "user")

        def go():
            if action == "test":
                return respond({"proposal": evo.test(pid, actor=actor)})
            if action == "approve":
                return respond({"proposal": evo.approve(pid, actor=actor)})
            if action == "reject":
                return respond({"proposal": evo.reject(pid, actor=actor, reason=str(d.get("reason") or ""))})
            return respond({"proposal": evo.rollback(pid, actor=actor)})
        return _guard(go)

    for _a in ("test", "approve", "reject", "rollback"):
        app.add_url_rule(f"/evolution/<pid>/{_a}", f"harness_evolution_{_a}",
                         (lambda a: lambda pid: _evo_action(pid, a))(_a), methods=["POST"])

    # ── profiles (Phase 7) ──
    @app.route("/profiles", methods=["GET"])
    def harness_profiles_list():
        eng, err = _engine()
        if err:
            return err
        if eng.profiles is None:
            return respond({"error": "profiles not configured"}, 503)
        return respond({"profiles": eng.profiles.list()})

    @app.route("/profiles", methods=["POST"])
    def harness_profiles_upsert():
        eng, err = _engine()
        if err:
            return err
        if eng.profiles is None:
            return respond({"error": "profiles not configured"}, 503)
        d = _body()
        return _guard(lambda: respond({"profile": eng.profiles.upsert(
            d.get("name") or "", d.get("spec") or {}, actor=str(d.get("actor") or "user"),
            reason=str(d.get("reason") or ""))}, 201))

    @app.route("/profiles/<name>", methods=["GET"])
    def harness_profile_detail(name):
        eng, err = _engine()
        if err:
            return err
        p = eng.profiles.get(name) if eng.profiles else None
        if p is None:
            return respond({"error": f"no such profile: {name}"}, 404)
        return respond({"profile": p, "history": eng.profiles.history(name)})

    # ── control plane (Phase 8) ──
    @app.route("/harness/command", methods=["POST"])
    def harness_command():
        eng, err = _engine()
        if err:
            return err
        from mneme.harness.commands import handle
        d = _body()
        text = str(d.get("text") or "")
        reply = handle(text if text.startswith("/") else "/" + text, eng,
                       extras=(extras() if extras else None), actor=str(d.get("actor") or "user"))
        if reply is None:
            return respond({"error": "unknown command — try /help"}, 400)
        return respond({"reply": reply})

    @app.route("/harness/metrics", methods=["GET"])
    def harness_metrics():
        eng, err = _engine()
        if err:
            return err
        from mneme.harness.metrics import compute
        return respond(compute(eng.ledger, eng.skills, eng.evolution))

    @app.route("/runs/ui", methods=["GET"])
    def harness_runs_ui():
        path = _os.path.join(static_dir or "", "runs.html")
        try:
            with open(path, encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
        except OSError:
            return respond({"error": "runs UI not found"}, 404)

    # ── jobs (Phase 9) ──
    def _jobs():
        eng, err = _engine()
        if err:
            return None, err
        if getattr(eng, "jobs", None) is None:
            return None, respond({"error": "jobs not configured"}, 503)
        return eng.jobs, None

    @app.route("/jobs", methods=["GET"])
    def harness_jobs_list():
        jobs, err = _jobs()
        return err or respond({"jobs": jobs.list()})

    @app.route("/jobs", methods=["POST"])
    def harness_jobs_create():
        jobs, err = _jobs()
        if err:
            return err
        d = _body()
        return _guard(lambda: respond({"job": jobs.create(
            str(d.get("name") or ""), str(d.get("goal") or ""), d.get("interval_s"), tasks=d.get("tasks"),
            profile=str(d.get("profile") or ""), budget=d.get("budget") or {},
            start_in_s=float(d.get("start_in_s") or 0), overlap=bool(d.get("overlap")),
            created_by=str(d.get("created_by") or "user"))}, 201))

    @app.route("/jobs/<job_id>", methods=["GET"])
    def harness_job_detail(job_id):
        jobs, err = _jobs()
        if err:
            return err
        j = jobs.get(job_id)
        if j is None:
            return respond({"error": f"no such job: {job_id}"}, 404)
        return respond({"job": j, "log": jobs.log(job_id)})

    def _job_action(job_id, action):
        jobs, err = _jobs()
        if err:
            return err
        return _guard(lambda: respond({"job": jobs.trigger(job_id) if action == "trigger"
                                       else jobs.set_enabled(job_id, action == "enable")}))

    for _ja in ("enable", "disable", "trigger"):
        app.add_url_rule(f"/jobs/<job_id>/{_ja}", f"harness_job_{_ja}",
                         (lambda a: lambda job_id: _job_action(job_id, a))(_ja), methods=["POST"])

    # ── external drivers (Phase 10): an extension records ITS OWN run ──
    import re as _re
    _EVENT_TYPE = _re.compile(r"^[a-z][a-z0-9_]{0,48}$")

    def _external_run(eng, run_id):
        run = eng.ledger.require_run(run_id)
        if not (run.get("meta") or {}).get("external"):
            raise InvalidTransition(f"run {run_id} is harness-driven — extensions may only write to "
                                    "runs they created with meta.external")
        return run

    @app.route("/runs/<run_id>/events", methods=["POST"])
    def harness_run_add_event(run_id):
        eng, err = _engine()
        if err:
            return err
        d = _body()

        def go():
            run = _external_run(eng, run_id)
            etype = str(d.get("type") or "")
            if not _EVENT_TYPE.match(etype):
                raise LedgerError("event type must match [a-z][a-z0-9_]* (max 49 chars)")
            eid = eng.ledger.emit(run_id, etype, d.get("data") or {}, task_id=str(d.get("task_id") or ""),
                                  actor=str(d.get("actor") or f"extension:{run['meta']['external']}"))
            return respond({"event_id": eid}, 201)
        return _guard(go)

    @app.route("/runs/<run_id>/status", methods=["POST"])
    def harness_run_set_status(run_id):
        eng, err = _engine()
        if err:
            return err
        d = _body()
        return _guard(lambda: respond({"run": eng.external_transition(
            run_id, str(d.get("status") or ""), result=str(d.get("result") or ""),
            error=str(d.get("error") or ""), actor=str(d.get("actor") or "extension"))}))

    @app.route("/runs/<run_id>/artifacts", methods=["POST"])
    def harness_run_add_artifact(run_id):
        eng, err = _engine()
        if err:
            return err
        data = _body()

        def go():
            eng.ledger.require_run(run_id)
            if not data.get("path"):
                raise LedgerError("artifact needs a 'path'")
            art = eng.ledger.add_artifact(run_id, str(data["path"]), kind=str(data.get("kind") or "file"),
                                          description=str(data.get("description") or ""),
                                          task_id=str(data.get("task_id") or ""),
                                          provenance=data.get("provenance") or {})
            return respond({"artifact": art}, 201)
        return _guard(go)
