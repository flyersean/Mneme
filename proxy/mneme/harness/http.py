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


def register(app, get_engine: Callable[[], Optional[object]], respond: Callable):
    from flask import request

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
            else:  # checkpoint
                eng.ledger.require_run(run_id)
                return respond({"checkpoint": eng.checkpoint(run_id, reason=str(data.get("reason") or "manual"))})
            return respond({"run": run})
        return _guard(go)

    for _action in ("pause", "cancel", "resume", "retry", "checkpoint"):
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
