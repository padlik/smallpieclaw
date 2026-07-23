"""The ``schedule`` built-in tool (list/add/remove/pause/resume/run_now).

Leaf module: reads exactly one collaborator (``scheduler``), passed in as a
plain parameter rather than via an owner back-reference — no state to own.
"""

from __future__ import annotations


def exec_schedule(scheduler, args: dict, caller_depth: int = 0) -> dict:
    """Run the ``schedule`` built-in tool's ``action`` against *scheduler*.

    ``args["action"]`` selects the operation (default ``"list"``):
    ``list`` (enumerate jobs), ``add`` (requires ``tag``; see ``Scheduler.add_job``
    for the remaining fields), ``remove``/``pause``/``resume``/``run_now``
    (each requires ``tag``). Returns the standard built-in result dict; an
    unrecognized action or a missing ``scheduler`` both return
    ``success=False`` with a descriptive ``error``.

    ``caller_depth`` is the depth of the invoking AgentController (0 = main
    agent, 1 = sub-agent). Mutating actions (``add``/``remove``/``pause``/
    ``resume``/``run_now``) are blocked at ``caller_depth >= 1`` so sub-agents
    cannot create, delete, or alter scheduled jobs that persist beyond the
    sub-agent's lifetime. The read-only ``list`` action remains available.
    """
    if not scheduler:
        return {"success": False, "output": "", "error": "Scheduler not available.", "exit_code": -1}
    action = str(args.get("action", "list")).lower()
    tag = str(args.get("tag", "")).strip()

    # Depth guard — sub-agents cannot manipulate scheduled jobs. The read-only
    # ``list`` action is allowed; all mutating actions are hard-blocked.
    if caller_depth >= 1 and action != "list":
        return {
            "success": False,
            "output": "",
            "error": (
                f"schedule: sub-agents cannot modify scheduled jobs "
                f"(action '{action}' blocked at depth {caller_depth}). "
                f"Only 'list' is available to sub-agents."
            ),
            "exit_code": -1,
            "error_type": "fundamentally_wrong_approach",
            "recoverable": False,
            "suggestion": (
                "Ask the parent agent to modify scheduled jobs; sub-agents may "
                "only list them."
            ),
        }

    if action == "list":
        jobs = scheduler.list_jobs()
        if not jobs:
            return {"success": True, "output": "No scheduled jobs.", "error": "", "exit_code": 0}
        lines = []
        for j in jobs:
            status = "✅" if j["enabled"] else "⏸"
            stype = j.get("schedule_type", "interval")
            task_label = "Message" if stype == "once" else "Task"
            err = f"\n   ⚠️ last error: {j['last_error'][:120]}" if j.get("last_error") else ""
            lines.append(
                f"{status} {j['tag']} ({j['schedule']})\n"
                f"   {task_label}: {j['task']}\n"
                f"   Last run: {j['last_run'] or 'never'}{err}"
            )
        return {"success": True, "output": "\n".join(lines), "error": "", "exit_code": 0}

    if action == "add":
        if not tag:
            return {"success": False, "output": "", "error": "tag is required for add", "exit_code": -1}
        result = scheduler.add_job(
            tag=tag,
            schedule_type=str(args.get("schedule_type", args.get("schedule", "cron"))),
            task=str(args.get("task", "")),
            notify=bool(args.get("notify", True)),
            hours=int(args["hours"]) if args.get("hours") is not None else None,
            minutes=int(args["minutes"]) if args.get("minutes") is not None else None,
            time_str=str(args.get("time", "")) or None,
            run_at=str(args.get("run_at", "")) or None,
            cron=str(args.get("cron", "")) or None,
            model=str(args["model"]) if args.get("model") else None,
            fallback_models=args.get("fallback_models"),
            preserve_context=bool(args.get("preserve_context", False)),
            max_iterations=int(args["max_iterations"]) if args.get("max_iterations") is not None else None,
        )
        if result["success"]:
            return {"success": True, "output": f"Job '{tag}' added.", "error": "", "exit_code": 0}
        return {"success": False, "output": "", "error": result["error"], "exit_code": -1}

    if action == "remove":
        ok = scheduler.remove_job(tag)
        if ok:
            return {"success": True, "output": f"Job '{tag}' removed.", "error": "", "exit_code": 0}
        return {"success": False, "output": "", "error": f"Job '{tag}' not found.", "exit_code": -1}

    if action == "pause":
        ok = scheduler.pause_job(tag)
        if ok:
            return {"success": True, "output": f"Job '{tag}' paused.", "error": "", "exit_code": 0}
        return {"success": False, "output": "", "error": f"Job '{tag}' not found.", "exit_code": -1}

    if action == "resume":
        ok = scheduler.resume_job(tag)
        if ok:
            return {"success": True, "output": f"Job '{tag}' resumed.", "error": "", "exit_code": 0}
        return {"success": False, "output": "", "error": f"Job '{tag}' not found.", "exit_code": -1}

    if action == "run_now":
        result = scheduler.run_now(tag)
        if result["success"]:
            return {"success": True, "output": f"Job '{tag}' triggered.", "error": "", "exit_code": 0}
        return {"success": False, "output": "", "error": result["error"], "exit_code": -1}

    return {"success": False, "output": "", "error": f"Unknown action '{action}'. Use: list, add, remove, pause, resume, run_now", "exit_code": -1}
