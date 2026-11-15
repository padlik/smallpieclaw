"""The ``schedule`` built-in tool (list/show/add/remove/pause/resume/modify/run_now).

Leaf module: reads exactly one collaborator (``scheduler``), passed in as a
plain parameter rather than via an owner back-reference — no state to own.

Actions:
  • list   — compact overview of all jobs (read-only, available at every depth)
  • show   — full metadata for a single job (read-only, available at every depth)
  • add    — create a new job (mutating, main-agent only)
  • remove — delete a job (mutating, main-agent only)
  • pause  — disable a job (mutating, main-agent only)
  • resume — re-enable a job (mutating, main-agent only)
  • modify — partial-patch an existing job (mutating, main-agent only)
  • run_now — trigger a job immediately through the standard scheduled-run
              machinery (mutating, main-agent only)

Sub-agents (caller_depth >= 1) may only use the read-only actions ``list`` and
``show``. All mutating actions are hard-blocked for sub-agents.
"""

from __future__ import annotations

from typing import Callable

from model_registry import bounded_selector_evidence
from outcome_utils import with_execution


# Valid overlap-policy values, enforced at the tool layer before reaching the
# Scheduler (which stays permissive for operator-authored scheduler.toml).
_OVERLAP_POLICIES = ("skip", "parallel")


def _coerce_bool(value: object, field: str, action: str) -> bool:
    """Parse a boolean-like value from LLM input.

    Real booleans pass through. Strings are interpreted case-insensitively:
    "true" / "1" / "yes" → True; "false" / "0" / "no" → False. Any other
    value raises ``ValueError`` with a user-facing message so the caller can
    return a validation error.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes"):
            return True
        if normalized in ("false", "0", "no"):
            return False
    raise ValueError(f"schedule {action}: {field} must be a boolean.")


def _coerce_context_max_messages(value: object, action: str) -> int:
    """Parse context_max_messages as a positive integer.

    Accepts an int directly or a string that parses as int. Rejects values
    below 1 and any non-integer input with ``ValueError`` and a user-facing
    message.
    """
    if isinstance(value, bool):
        raise ValueError(f"schedule {action}: context_max_messages must be an integer >= 1.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise ValueError(
                f"schedule {action}: context_max_messages must be an integer >= 1."
            ) from exc
    else:
        raise ValueError(f"schedule {action}: context_max_messages must be an integer >= 1.")
    if parsed < 1:
        raise ValueError(f"schedule {action}: context_max_messages must be an integer >= 1.")
    return parsed


def _blocks(scheduler: object, tag: str) -> bool:
    """Whether the job's config error leaves it unscheduled (unknown schedulers: assume it does)."""
    check = getattr(scheduler, "config_blocks_schedule", None)
    return bool(check(tag)) if callable(check) else True


def _file_notice(scheduler) -> str:
    """Return the scheduler's file-failure notice, or "" when there is none."""
    accessor = getattr(scheduler, "load_notice", None)
    notice = accessor() if callable(accessor) else None
    return notice if isinstance(notice, str) else ""


def _reload_required_refusal(scheduler) -> dict | None:
    """Return a refused result while the scheduler requires a reload, else None."""
    accessor = getattr(scheduler, "reload_required_notice", None)
    notice = accessor() if callable(accessor) else None
    if not isinstance(notice, str) or not notice:
        return None
    return with_execution({"success": False, "output": "", "error": notice, "exit_code": -1}, "refused")


_CHANGE_VERBS = {"remove": "removed", "pause": "paused", "resume": "resumed"}


def _change_core(scheduler) -> Callable[..., tuple[bool, str, bool]] | None:
    """Return the scheduler class's detailed remove/pause/resume core, or None.

    Looked up on the class, never the instance, so a mock collaborator's
    auto-created attribute is not mistaken for it; such collaborators keep
    the boolean ``remove_job``/``pause_job``/``resume_job`` path.
    """
    core = getattr(type(scheduler), "_change_job", None)
    return core if callable(core) else None


def exec_schedule(scheduler, args: dict, caller_depth: int = 0) -> dict:
    """Run the ``schedule`` built-in tool's ``action`` against *scheduler*.

    ``args["action"]`` selects the operation (default ``"list"``):
    ``list`` (enumerate jobs), ``show`` (full metadata for ``tag``),
    ``add`` (requires ``tag``; see ``Scheduler.add_job`` for the remaining
    fields), ``remove``/``pause``/``resume``/``modify``/``run_now`` (each
    requires ``tag``). Returns the standard built-in result dict; an
    unrecognized action or a missing ``scheduler`` both return
    ``success=False`` with a descriptive ``error``.

    ``caller_depth`` is the depth of the invoking AgentController (0 = main
    agent, 1 = sub-agent). The read-only actions ``list`` and ``show`` are
    available at every depth; all mutating actions (``add``/``remove``/
    ``pause``/``resume``/``modify``/``run_now``) are blocked at
    ``caller_depth >= 1`` so sub-agents cannot create, delete, or alter
    scheduled jobs that persist beyond the sub-agent's lifetime.
    """
    if not scheduler:
        return with_execution({"success": False, "output": "", "error": "Scheduler not available.", "exit_code": -1}, "refused")
    action = str(args.get("action", "list")).lower()
    tag = str(args.get("tag", "")).strip()

    # Depth guard — sub-agents may only inspect scheduled jobs. The read-only
    # ``list`` and ``show`` actions are allowed; all other actions are
    # hard-blocked with a non-recoverable error.
    if caller_depth >= 1 and action not in ("list", "show"):
        return with_execution({
            "success": False,
            "output": "",
            "error": (
                f"schedule: sub-agents cannot modify scheduled jobs "
                f"(action '{action}' blocked at depth {caller_depth}). "
                f"Only 'list' and 'show' are available to sub-agents."
            ),
            "exit_code": -1,
            "error_type": "fundamentally_wrong_approach",
            "recoverable": False,
            "suggestion": (
                "Ask the parent agent to modify scheduled jobs; sub-agents may "
                "only list or show them."
            ),
        }, "refused")

    if action == "list":
        notice = _file_notice(scheduler)
        jobs = scheduler.list_jobs()
        if not jobs:
            output = "No scheduled jobs."
            if notice:
                output = f"⚠️ {notice}\n{output}"
            return with_execution({"success": True, "output": output, "error": "", "exit_code": 0}, "executed")
        lines = [f"⚠️ {notice}"] if notice else []
        for j in jobs:
            status = "✅" if j["enabled"] else "⏸"
            stype = j.get("schedule_type", "interval")
            task_label = "Message" if stype == "once" else "Task"
            err = f"\n   ⚠️ last error: {j['last_error'][:120]}" if j.get("last_error") else ""
            if j.get("config_error"):
                note = "not scheduled" if _blocks(scheduler, j["tag"]) else "launches fail until fixed"
                err += f"\n   ⚠️ config_error ({note}): {j['config_error']}"
            lines.append(
                f"{status} {j['tag']} ({j['schedule']})\n"
                f"   {task_label}: {j['task']}\n"
                f"   Last run: {j['last_run'] or 'never'}{err}"
            )
        return with_execution({"success": True, "output": "\n".join(lines), "error": "", "exit_code": 0}, "executed")

    if action == "show":
        if not tag:
            return with_execution({"success": False, "output": "", "error": "tag is required for show", "exit_code": -1}, "refused")
        notice = _file_notice(scheduler)
        job = scheduler.show_job(tag)
        if not job:
            known = ", ".join(entry["tag"] for entry in scheduler.list_jobs()) or "none"
            error = f"Job '{tag}' not found. Known jobs: {known}"
            if notice:
                error = f"{error}\n{notice}"
            return with_execution({"success": False, "output": "", "error": error, "exit_code": -1}, "executed")
        lines = [f"⚠️ {notice}"] if notice else []
        lines += [
            f"tag: {job['tag']}",
            f"task: {job['task']}",
            f"schedule: {job['schedule']}",
        ]
        if job.get("cron"):
            lines.append(f"cron: {job['cron']}")
        if job.get("run_at"):
            lines.append(f"run_at: {job['run_at']}")
        lines.extend([
            f"schedule_type: {job['schedule_type']}",
            f"next_run: {job['next_run'] or 'not scheduled'}",
            f"enabled: {job['enabled']}",
            f"is_running: {job['is_running']}",
            f"source: {job['source']}",
            f"notify: {job['notify']}",
            "model: " + bounded_selector_evidence(
                job.get("model"), redact=getattr(scheduler, "selector_redactor", None),
            ),
            f"preserve_context: {job['preserve_context']}",
            f"context_max_messages: {job['context_max_messages']}",
            f"overlap_policy: {job['overlap_policy']}",
            f"last_run: {job['last_run'] or 'never'}",
            f"last_error: {job['last_error'] or 'none'}",
            f"created_at: {job['created_at'] or 'unknown'}",
        ])
        if job.get("config_error"):
            note = "not scheduled; fix the job, then reload" if _blocks(scheduler, job["tag"]) else "scheduled, but every launch fails until fixed; then reload"
            lines.append(f"config_error: {job['config_error']} ({note})")
        return with_execution({"success": True, "output": "\n".join(lines), "error": "", "exit_code": 0}, "executed")

    if action in ("add", "modify"):
        # Obsolete scheduling/fallback input is known-bad: refuse by PRESENCE before any coercion,
        # validation of other fields or the attempt hook, changing nothing.
        from scheduler import obsolete_mutation_error  # lazy: scheduler imports this package

        obsolete = obsolete_mutation_error(args)
        if obsolete is not None:
            return with_execution({
                "success": False, "output": "", "error": obsolete.replace("schedule:", f"schedule {action}:", 1),
                "exit_code": -1, "error_type": "validation_error", "recoverable": True,
                "suggestion": "Use cron (or schedule_type 'once' with run_at); remove obsolete fields.",
            }, "refused")

    if action == "add":
        if not tag:
            return with_execution({"success": False, "output": "", "error": "tag is required for add", "exit_code": -1}, "refused")
        overlap_policy = str(args.get("overlap_policy", "skip")).lower()
        if args.get("overlap_policy") is not None and overlap_policy not in _OVERLAP_POLICIES:
            return with_execution({
                "success": False,
                "output": "",
                "error": (
                    f"schedule add: overlap_policy must be one of: "
                    f"{', '.join(_OVERLAP_POLICIES)}."
                ),
                "exit_code": -1,
                "error_type": "validation_error",
                "recoverable": True,
                "suggestion": "Set overlap_policy to 'skip' or 'parallel'.",
            }, "refused")
        if args.get("context_max_messages") is not None:
            try:
                context_max_messages = _coerce_context_max_messages(args["context_max_messages"], "add")
            except ValueError as exc:
                return with_execution({
                    "success": False,
                    "output": "",
                    "error": str(exc),
                    "exit_code": -1,
                    "error_type": "validation_error",
                    "recoverable": True,
                    "suggestion": "Provide context_max_messages as an integer >= 1, e.g. 50.",
                }, "refused")
        else:
            context_max_messages = 50
        notify = True
        if args.get("notify") is not None:
            try:
                notify = _coerce_bool(args["notify"], "notify", "add")
            except ValueError as exc:
                return with_execution({
                    "success": False,
                    "output": "",
                    "error": str(exc),
                    "exit_code": -1,
                    "error_type": "validation_error",
                    "recoverable": True,
                    "suggestion": "Provide notify as a boolean (true/false).",
                }, "refused")
        preserve_context = False
        if args.get("preserve_context") is not None:
            try:
                preserve_context = _coerce_bool(args["preserve_context"], "preserve_context", "add")
            except ValueError as exc:
                return with_execution({
                    "success": False,
                    "output": "",
                    "error": str(exc),
                    "exit_code": -1,
                    "error_type": "validation_error",
                    "recoverable": True,
                    "suggestion": "Provide preserve_context as a boolean (true/false).",
                }, "refused")
        attempted = False

        def _mark_attempt() -> None:
            nonlocal attempted
            attempted = True

        result = scheduler.add_job(
            tag=tag,
            schedule_type=str(args.get("schedule_type", args.get("schedule", "cron"))),
            task=str(args.get("task", "")),
            notify=notify,
            run_at=str(args.get("run_at", "")) or None,
            cron=str(args.get("cron", "")) or None,
            model=args.get("model"),
            preserve_context=preserve_context,
            context_max_messages=context_max_messages,
            overlap_policy=overlap_policy,
            _on_attempt=_mark_attempt,
        )
        disposition = "executed" if attempted else "refused"
        if result["success"]:
            return with_execution({"success": True, "output": f"Job '{tag}' added.", "error": "", "exit_code": 0}, disposition)
        return with_execution({"success": False, "output": "", "error": result["error"], "exit_code": -1}, disposition)

    if action in ("remove", "pause", "resume"):
        # Known before any attempt: a latched scheduler changes nothing.
        refusal = _reload_required_refusal(scheduler)
        if refusal is not None:
            return refusal
        core = _change_core(scheduler)
        if core is not None:
            # The core reports whether the change was attempted: pre-known
            # refusals (bad file, legacy read-only, shadow check) are refused.
            ok, error, attempted = core(scheduler, action, tag)
            disposition = "executed" if attempted else "refused"
            if ok:
                return with_execution({"success": True, "output": f"Job '{tag}' {_CHANGE_VERBS[action]}.", "error": "", "exit_code": 0}, disposition)
            return with_execution({"success": False, "output": "", "error": error or f"Job '{tag}' not found.", "exit_code": -1}, disposition)

    if action == "remove":
        disposition = "executed" if tag else "refused"
        ok = scheduler.remove_job(tag)
        if ok:
            return with_execution({"success": True, "output": f"Job '{tag}' removed.", "error": "", "exit_code": 0}, disposition)
        return with_execution({"success": False, "output": "", "error": _file_notice(scheduler) or f"Job '{tag}' not found.", "exit_code": -1}, disposition)

    if action == "pause":
        disposition = "executed" if tag else "refused"
        ok = scheduler.pause_job(tag)
        if ok:
            return with_execution({"success": True, "output": f"Job '{tag}' paused.", "error": "", "exit_code": 0}, disposition)
        return with_execution({"success": False, "output": "", "error": _file_notice(scheduler) or f"Job '{tag}' not found.", "exit_code": -1}, disposition)

    if action == "resume":
        disposition = "executed" if tag else "refused"
        ok = scheduler.resume_job(tag)
        if ok:
            return with_execution({"success": True, "output": f"Job '{tag}' resumed.", "error": "", "exit_code": 0}, disposition)
        return with_execution({"success": False, "output": "", "error": _file_notice(scheduler) or f"Job '{tag}' not found.", "exit_code": -1}, disposition)

    if action == "modify":
        if not tag:
            return with_execution({"success": False, "output": "", "error": "tag is required for modify", "exit_code": -1}, "refused")
        updates: dict = {}
        for field in (
            "task",
            "cron",
            "schedule_type",
            "run_at",
            "notify",
            "model",
            "preserve_context",
            "context_max_messages",
            "overlap_policy",
        ):
            if field in args and args[field] is not None:
                updates[field] = args[field]
        # Forward 'enabled' so Scheduler.modify_job returns the explicit
        # pause/resume-directed error instead of silently dropping it.
        if args.get("enabled") is not None:
            updates["enabled"] = args["enabled"]
        if "context_max_messages" in updates:
            try:
                updates["context_max_messages"] = _coerce_context_max_messages(updates["context_max_messages"], "modify")
            except ValueError as exc:
                return with_execution({
                    "success": False,
                    "output": "",
                    "error": str(exc),
                    "exit_code": -1,
                    "error_type": "validation_error",
                    "recoverable": True,
                    "suggestion": "Provide context_max_messages as an integer >= 1, e.g. 50.",
                }, "refused")
        if "overlap_policy" in updates:
            updates["overlap_policy"] = str(updates["overlap_policy"]).lower()
            if updates["overlap_policy"] not in _OVERLAP_POLICIES:
                return with_execution({
                    "success": False,
                    "output": "",
                    "error": (
                        f"schedule modify: overlap_policy must be one of: "
                        f"{', '.join(_OVERLAP_POLICIES)}."
                    ),
                    "exit_code": -1,
                    "error_type": "validation_error",
                    "recoverable": True,
                    "suggestion": "Set overlap_policy to 'skip' or 'parallel'.",
                }, "refused")
        if "notify" in updates:
            try:
                updates["notify"] = _coerce_bool(updates["notify"], "notify", "modify")
            except ValueError as exc:
                return with_execution({
                    "success": False,
                    "output": "",
                    "error": str(exc),
                    "exit_code": -1,
                    "error_type": "validation_error",
                    "recoverable": True,
                    "suggestion": "Provide notify as a boolean (true/false).",
                }, "refused")
        if "preserve_context" in updates:
            try:
                updates["preserve_context"] = _coerce_bool(updates["preserve_context"], "preserve_context", "modify")
            except ValueError as exc:
                return with_execution({
                    "success": False,
                    "output": "",
                    "error": str(exc),
                    "exit_code": -1,
                    "error_type": "validation_error",
                    "recoverable": True,
                    "suggestion": "Provide preserve_context as a boolean (true/false).",
                }, "refused")
        attempted = False

        def _mark_attempt() -> None:
            nonlocal attempted
            attempted = True

        result = scheduler.modify_job(tag, updates, _on_attempt=_mark_attempt)
        disposition = "executed" if attempted else "refused"
        if result["success"]:
            return with_execution({"success": True, "output": f"Job '{tag}' modified.", "error": "", "exit_code": 0}, disposition)
        return with_execution({
            "success": False,
            "output": "",
            "error": result["error"],
            "exit_code": -1,
            "error_type": "validation_error",
            "recoverable": True,
            "suggestion": "Check the error message and retry with valid, updatable fields.",
        }, disposition)

    if action == "run_now":
        if not tag:
            return with_execution({"success": False, "output": "", "error": "tag is required for run_now", "exit_code": -1}, "refused")
        # Snapshot before triggering: a fast once-job can complete and
        # self-remove before the result-building read-back runs.
        job = scheduler.show_job(tag)
        result = scheduler.run_now(tag)
        if not result["success"]:
            return with_execution({"success": False, "output": "", "error": result["error"], "exit_code": -1}, "executed")

        enabled = bool(job.get("enabled", True)) if job else True
        schedule_type = job.get("schedule_type", "cron") if job else "cron"
        overlap_policy = job.get("overlap_policy", "skip") if job else "skip"

        notes = [
            f"Job '{tag}' triggered to run now through the standard scheduled-run "
            "machinery as a supervised scheduled-run sub-agent.",
            "The result is delivered per the job's notify setting; it does not return here.",
            f"This trigger is subject to the job's overlap_policy ({overlap_policy}); "
            "if a run is already in progress and the policy is 'skip', the trigger is skipped.",
            f"Follow the outcome with 'schedule show {tag}'.",
        ]
        if not enabled:
            notes.append("The job remains paused (enabled=false); scheduled firings will still skip it.")
        if schedule_type == "once":
            notes.append("This is a once job: it will be removed after the run completes.")
        return with_execution({"success": True, "output": "\n".join(notes), "error": "", "exit_code": 0}, "executed")

    return with_execution({
        "success": False,
        "output": "",
        "error": (
            f"Unknown action '{action}'. "
            "Use: list, show, add, remove, pause, resume, modify, run_now"
        ),
        "exit_code": -1,
    }, "refused")
