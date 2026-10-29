"""Sub-agent built-in tools: spawn_agent and get_agent_result.

Handler module: ``AgentTools`` holds a back-reference to the ``BuiltinExecutor``
façade (``owner``) and reads late-bound collaborators (``_sub_agent_factory``,
``_working``, ``_results``, ``_memory``, ``_graph_memory``, ``_notify_html_fn``,
``_supervisor``, ``_max_subagents``, ``_subagent_result_timeout``, ``_data_dir``)
through it at call time — they are wired onto the executor after construction and
must never be snapshotted.

Accepted runs are delegated to ``SubAgentSupervisor.submit`` (ADR-0005): the
supervisor owns registration, background execution, result signalling, context
persistence, notification, and cleanup. The ``agent_runtime`` /
``sub_agent_registry`` / ``prompt_loader`` imports are kept function-local to
avoid import cycles; context persistence is resolved through the
``context_io`` module at call time so a ``builtin_tools.context_io._save_context``
patch intercepts it. The ``builtin_executor`` import is under ``TYPE_CHECKING`` only.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Optional

from outcome_utils import with_execution
from sub_agent_supervisor import (
    ORIGIN_ON_DEMAND,
    ORIGIN_SCHEDULED,
    ExecutionOwnership,
    SubmissionRequest,
    SupervisionOptions,
)

from builtin_tools import context_io

if TYPE_CHECKING:
    from builtin_executor import BuiltinExecutor

from sub_agent_registry import SOURCE_SCHEDULED, get_registry as _get_agent_registry

logger = logging.getLogger(__name__)


def _turn_id_from_caller_tag(caller_tag: str) -> Optional[str]:
    """Extract the immutable request trace used as the operator-turn identity.

    Accepted legacy fallback: direct non-loop callers reuse the trace as a
    legacy owner identity; the ReAct path always injects an explicit per-turn
    owner (_operator_turn_id), which takes precedence at every call site.
    """
    candidate = caller_tag.rsplit(" ", 1)[-1] if caller_tag else ""
    return candidate if candidate.startswith("r-") else None


def _inaccessible_child_result(tool_name: str, agent_ids: str | list[str]) -> dict:
    """Return the indistinguishable unknown/non-owned child error contract."""
    if isinstance(agent_ids, str):
        supplied = f"id '{agent_ids}'"
    else:
        supplied = "ids " + ", ".join(f"'{agent_id}'" for agent_id in agent_ids)
    return with_execution({
        "success": False,
        "output": "",
        "error": (
            f"{tool_name}: no child accessible in this turn with {supplied}."
        ),
        "exit_code": -1,
        "status": "not_found",
        "error_type": "file_not_found",
        "recoverable": False,
        "suggestion": "Use an agent_id spawned by this operator turn.",
    }, "refused")


_OBSOLETE_SPAWN_KEYS = ("max_tokens", "temperature", "top_p")


def _validate_spawn_args(
    owner: BuiltinExecutor,
    args: dict,
    caller_depth: int,
    options: SupervisionOptions,
) -> dict | tuple[str, str, Optional[str], Optional[str], str]:
    """Validate model-facing ``spawn_agent`` arguments.

    Handles task aliases, depth guard, factory availability, concurrency cap,
    ``response_format`` normalization/task augmentation, and ``context_key``
    syntax validation.

    Returns either an error dict compatible with tool output, or a tuple of
    ``(task, response_format, model, context_key, label)``.
    """
    from sub_agent_registry import get_registry as get_agent_registry

    task = args.get("task", "").strip()
    # Accept common LLM aliases for the 'task' parameter
    if not task:
        for _alias in ("prompt", "goal", "description"):
            _v = args.get(_alias, "").strip()
            if _v:
                logger.warning(
                    "spawn_agent: received '%s' instead of 'task' — treating as task (fix your prompt)", _alias
                )
                task = _v
                break
    if not task:
        return with_execution({
            "success": False,
            "output": "",
            "error": "spawn_agent: 'task' is required.",
            "exit_code": -1,
            "error_type": "fundamentally_wrong_approach",
            "recoverable": False,
            "suggestion": "Provide a clear task string describing what the sub-agent should do.",
        }, "refused")

    # Depth guard — prevent recursive sub-agent spawning (hard error, not a silent no-op)
    if caller_depth >= 1:
        return with_execution({
            "success": False,
            "output": "",
            "error": "spawn_agent cannot be called from within a sub-agent (max nesting depth: 1).",
            "exit_code": -1,
            "error_type": "fundamentally_wrong_approach",
            "recoverable": False,
            "suggestion": "Do not spawn sub-agents from within a sub-agent; perform the work directly.",
        }, "refused")

    if owner._sub_agent_factory is None:
        return with_execution({
            "success": False,
            "output": "",
            "error": "spawn_agent: sub_agent_factory not configured.",
            "exit_code": -1,
            "error_type": "impossible_with_current_tools",
            "recoverable": False,
            "suggestion": "The agent runtime is missing sub-agent support; this cannot be recovered in-flight.",
        }, "refused")

    # Concurrency cap — preserve current managed-record semantics.
    # Scheduled launches still use source="on-demand" until the later
    # running-agent visibility/cap policy change decides otherwise.
    current_managed = get_agent_registry().count_managed()
    if current_managed >= owner._max_subagents:
        return with_execution({
            "success": False,
            "output": "",
            "error": (
                f"spawn_agent: max_subagents cap reached ({current_managed}/{owner._max_subagents}). "
                "Wait for a managed sub-agent to finish or cancel one with /agents cancel managed."
            ),
            "exit_code": -1,
            "error_type": "tool_timeout",
            "recoverable": True,
            "suggestion": "Wait for an existing sub-agent to finish, then retry.",
        }, "refused")

    obsolete = [key for key in _OBSOLETE_SPAWN_KEYS if key in args]
    if obsolete:
        return with_execution({
            "success": False,
            "output": "",
            "error": (
                "spawn_agent: unsupported argument(s): "
                + ", ".join(obsolete)
                + ". Remove them; sub-agents use the configured model settings."
            ),
            "exit_code": -1,
            "error_type": "validation_error",
            "recoverable": False,
            "suggestion": "Remove max_tokens, temperature and top_p; use the 'model' argument to pick a different model.",
        }, "refused")

    model = args.get("model")
    # Scheduler-built options are trusted: the raw saved selector (any type,
    # untrimmed) is forwarded verbatim for exactly one root admission. The
    # strict shape guard stays on for every model-facing source.
    if (
        model is not None
        and options.source != SOURCE_SCHEDULED
        and (not isinstance(model, str) or not model.strip())
    ):
        return with_execution({
            "success": False,
            "output": "",
            "error": "spawn_agent: 'model' must be a non-empty string or omitted.",
            "exit_code": -1,
            "error_type": "validation_error",
            "recoverable": False,
            "suggestion": "Pass a canonical provider-id/upstream-id from /models, or omit model to inherit the active main model.",
        }, "refused")

    # response_format — how the sub-agent should return its result
    response_format = args.get("response_format", "text").lower()
    if response_format not in ("text", "json", "file"):
        response_format = "text"
    if response_format == "json":
        task = task + "\n\nReturn your entire answer as a single valid JSON object. Do not include any prose or markdown fences."
    elif response_format == "file":
        task = task + "\n\nWrite your output to a file and return only the absolute file path as your answer."

    context_key = args.get("context_key") or None
    if context_key and options.source == SOURCE_SCHEDULED:
        # The scheduler's truthy context_key is only a preserve_context marker.
        # The persisted key is derived from the job identity in an origin-scoped
        # namespace; no model-facing value ever reaches it.
        context_key = context_io.scheduled_context_key(options.job_tag or "")
    elif context_key:
        try:
            context_key = context_io._validate_context_key(str(context_key))
        except ValueError as exc:
            return with_execution({
                "success": False,
                "output": "",
                "error": f"spawn_agent: invalid context_key: {exc}",
                "exit_code": -1,
                "error_type": "permission_denied",
                "recoverable": False,
                "suggestion": "Use a context_key with only letters, digits, underscore, dash, or dot.",
            }, "refused")

    label = options.job_tag or context_key or "on-demand"
    return task, response_format, model, context_key, label


def _build_context_payload(task: str, args: dict, owner: BuiltinExecutor) -> dict:
    """Build the parent context payload for a sub-agent spawn.

    Parses an explicit ``context_payload`` argument, falling back to an
    automatic summary from available memory sources when none is supplied.
    """
    context_payload = args.get("context_payload")
    if isinstance(context_payload, str):
        try:
            context_payload = json.loads(context_payload)
        except Exception:  # noqa: BLE001
            context_payload = {"parent_note": context_payload}
    if context_payload is None:
        # Implicit context: build an automatic summary from available sources.
        from prompt_loader import build_spawn_context_summary
        context_payload = build_spawn_context_summary(
            user_goal=task,
            working=owner._working,
            memory=owner._memory,
            results=owner._results,
            graph_memory=owner._graph_memory,
        )
    if not isinstance(context_payload, dict):
        context_payload = {"parent_note": str(context_payload)}
    return context_payload


class AgentTools:
    """spawn_agent / get_agent_result handlers, delegating to the supervisor."""

    def __init__(self, owner: BuiltinExecutor) -> None:
        self._owner = owner

    def _exec_spawn_agent(self, args: dict, caller_depth: int = 0, caller_tag: str = "",
                          trace_id: str = "", options: Optional[SupervisionOptions] = None) -> dict:
        """
        Model-facing compatibility shim for the ``spawn_agent`` tool.

        Validates tool arguments (task aliases, depth guard, response_format,
        context_key syntax), builds the parent context payload, and delegates
        accepted runs to ``SubAgentSupervisor.submit`` which owns the background
        lifecycle. Returns immediately with an ``agent_id`` (or a friendly
        rejection dict).

        ``options`` carries internal supervision controls (scheduler job tag,
        finish/result-log callbacks, notify/expandable flags). These are NOT
        model-facing tool arguments and must never be read from ``args``; the
        scheduler passes them here as a per-submission ``SupervisionOptions``.

        caller_depth is the depth of the AgentController that invoked this tool.
        Sub-agents (depth ≥ 1) are not allowed to spawn further sub-agents.
        """
        options = options or SupervisionOptions()
        self._owner._wire_grant_scope_expiry(options)
        explicit_prompt_id = args.pop("_operator_prompt_id", None)
        explicit_turn_id = args.pop("_operator_turn_id", None)
        if options.source == SOURCE_SCHEDULED:
            options.prompt_id = None
        elif explicit_prompt_id is not None:
            options.prompt_id = str(explicit_prompt_id)
        # Otherwise only an explicit ``options.prompt_id`` is used; the prompt
        # is never inferred from mutable ambient executor fields.

        validation = _validate_spawn_args(self._owner, args, caller_depth, options)
        if isinstance(validation, dict):
            return validation
        task, response_format, model, context_key, label = validation

        context_payload = (
            {}
            if options.source == SOURCE_SCHEDULED
            else _build_context_payload(task, args, self._owner)
        )

        # Internal supervision controls (job tag, callbacks, notify/expandable)
        # arrive via `options`, never through the model-facing `args` dict.

        # Construction profile travels through the internal factory channel only
        # (never through the model-facing ``args`` dict). Scheduled launches carry
        # source="scheduled" via SupervisionOptions and construct under the
        # SCHEDULED_AGENT profile; model-facing spawns use ON_DEMAND_SUBAGENT.
        from agent_runtime import RuntimeProfile
        runtime_profile = (
            RuntimeProfile.SCHEDULED_AGENT
            if options.source == SOURCE_SCHEDULED
            else RuntimeProfile.ON_DEMAND_SUBAGENT
        )
        ownership = ExecutionOwnership(
            origin=(
                ORIGIN_SCHEDULED
                if options.source == SOURCE_SCHEDULED
                else ORIGIN_ON_DEMAND
            ),
            # A direct submission with no turn identity is legacy/unowned: it
            # creates no turn obligation and stays readable by unowned callers.
            # Accepted legacy fallback: direct non-loop callers reuse the trace
            # as a legacy owner identity; the ReAct path always injects an
            # explicit per-turn owner (_operator_turn_id).
            owner_turn_id=(
                None
                if options.source == SOURCE_SCHEDULED
                or not (explicit_turn_id or trace_id)
                else str(explicit_turn_id or trace_id)
            ),
            prompt_id=options.prompt_id,
        )

        factory_kwargs = dict(
            model=model,
            context_key=context_key,
            label=label,
            notify_fn=None,   # factory sets this from main notify_fn
            trace_id=trace_id or None,
            context_payload=context_payload,
            runtime_profile=runtime_profile,
        )
        request = SubmissionRequest(
            task=task,
            response_format=response_format,
            label=label,
            context_key=context_key,
            factory=self._owner._sub_agent_factory,
            factory_kwargs=factory_kwargs,
            data_dir=self._owner._data_dir,
            notify_html_fn=self._owner._notify_html_fn,
            save_context=context_io._save_context,
            ownership=ownership,
            prompt_registry=getattr(self._owner, "_prompt_registry", None),
        )
        # Accepted run — the supervisor owns registration, background execution,
        # result signalling, context persistence, notification, scheduler
        # callbacks, and cleanup.
        result = self._owner._supervisor.submit(request, options)

        # Only an admitted run (an agent was registered) is an executed spawn;
        # a rejected runner construction never started anything.
        admitted = bool(result.get("success")) and bool(result.get("agent_id"))
        return with_execution(result, "executed" if admitted else "refused")

    def _exec_wait_for_any_agent(self, args: dict, caller_tag: str = "") -> dict:
        """Wait for the first of a set of sub-agents to finish and return its result.

        Implements the council pattern: call repeatedly with the remaining agent IDs
        to collect results in completion order. A 200ms poll loop checks each
        candidate's completion event and terminal status. The timeout does not
        cancel any sub-agents.
        """
        get_agent_registry = _get_agent_registry

        agent_ids = args.get("agent_ids", [])
        if not isinstance(agent_ids, list) or not agent_ids:
            return with_execution({
                "success": False,
                "output": "",
                "error": "wait_for_any_agent: 'agent_ids' must be a non-empty list.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide a list of sub-agent IDs returned by spawn_agent.",
            }, "refused")

        timeout = args.get("timeout", self._owner._subagent_result_timeout)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = self._owner._subagent_result_timeout

        records = []
        owner_turn_id = args.pop(
            "_operator_turn_id", None
        ) or _turn_id_from_caller_tag(caller_tag)
        inaccessible = []
        for raw_id in agent_ids:
            aid = str(raw_id).strip()
            record = self._owner._supervisor.get_owned_record(owner_turn_id, aid)
            if record is None:
                record = get_agent_registry().get(aid)
            if record is None:
                record = get_agent_registry().get_completed(aid)
            if record is None or not self._owner._supervisor.turn_can_access_result(
                record, owner_turn_id
            ):
                inaccessible.append(aid)
                continue
            records.append(record)
        if inaccessible:
            return _inaccessible_child_result("wait_for_any_agent", inaccessible)

        for record in records:
            if self._owner._supervisor.result_was_discarded(
                owner_turn_id, record.agent_id
            ):
                return self._discarded_result("wait_for_any_agent", record.agent_id)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for record in records:
                if record._result_event.is_set() and record.status in ("done", "failed", "cancelled"):
                    result = {
                        "success": record.status == "done",
                        "output": record.result or "",
                        "error": record.result if record.status == "failed" else "",
                        "exit_code": 0 if record.status == "done" else -1,
                        "status": record.status,
                        "agent_id": record.agent_id,
                        "result": record.result,
                        "result_type": record.result_type,
                    }
                    self._owner._supervisor.mark_result_retrieved(
                        owner_turn_id, record.agent_id
                    )
                    return with_execution(result, "executed")
            time.sleep(0.2)

        return with_execution({
            "success": False,
            "output": f"wait_for_any_agent: timed out after {timeout}s.",
            "error": "",
            "exit_code": 0,
            "status": "timeout",
            "agent_ids": [r.agent_id for r in records],
        }, "executed")

    def _exec_cancel_agent(self, args: dict, caller_tag: str = "") -> dict:
        """Cancel a specific sub-agent or all managed sub-agents.

        Not confirmation-gated: the LLM can cancel its own workers directly.
        The operator retains `/agents cancel` and `/stop` as overrides.
        """
        get_agent_registry = _get_agent_registry

        agent_id = args.get("agent_id", "").strip()
        if not agent_id:
            return with_execution({
                "success": False,
                "output": "",
                "error": "cancel_agent: 'agent_id' is required (or 'managed'/'all' to cancel all).",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide a sub-agent id, or use 'managed'/'all' to cancel all managed agents.",
            }, "refused")

        owner_turn_id = args.pop(
            "_operator_turn_id", None
        ) or _turn_id_from_caller_tag(caller_tag)
        if agent_id in ("managed", "all"):
            n = self._owner._supervisor.cancel_all_owned_children(owner_turn_id)
            return with_execution({
                "success": True,
                "output": f"Cancelled {n} turn-owned sub-agent(s).",
                "error": "",
                "exit_code": 0,
            }, "executed")

        record = self._owner._supervisor.get_owned_record(owner_turn_id, agent_id)
        if record is None:
            record = get_agent_registry().get(agent_id)
        if record is None:
            record = get_agent_registry().get_completed(agent_id)
        if record is None:
            return _inaccessible_child_result("cancel_agent", agent_id)
        if not self._owner._supervisor.turn_can_access_result(record, owner_turn_id):
            return _inaccessible_child_result("cancel_agent", agent_id)
        cancelled = self._owner._supervisor.cancel_owned_child(
            owner_turn_id, agent_id
        )
        if cancelled is None:
            if record.status == "running" and not record.is_cancelled:
                record.cancel()
        return with_execution({
            "success": True,
            "output": f"Cancelled sub-agent '{agent_id}'.",
            "error": "",
            "exit_code": 0,
        }, "executed")

    def _exec_get_agent_result(self, args: dict, caller_tag: str = "") -> dict:
        """
        Wait for a sub-agent to finish and return its result.

        Blocks until the agent's _result_event is set or timeout expires.
        """
        get_agent_registry = _get_agent_registry

        agent_id = args.get("agent_id", "").strip()
        if not agent_id:
            return with_execution({
                "success": False,
                "output": "",
                "error": "get_agent_result: 'agent_id' is required.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide the agent_id returned by spawn_agent.",
            }, "refused")

        timeout = args.get("timeout", self._owner._subagent_result_timeout)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = self._owner._subagent_result_timeout

        owner_turn_id = args.pop(
            "_operator_turn_id", None
        ) or _turn_id_from_caller_tag(caller_tag)
        registry = get_agent_registry()
        record = self._owner._supervisor.get_owned_record(owner_turn_id, agent_id)
        if record is None:
            record = registry.get(agent_id)
        if record is None:
            record = registry.get_completed(agent_id)
        if record is None or not self._owner._supervisor.turn_can_access_result(
            record, owner_turn_id
        ):
            return _inaccessible_child_result("get_agent_result", agent_id)
        if self._owner._supervisor.result_was_discarded(
            owner_turn_id, agent_id
        ):
            return self._discarded_result("get_agent_result", agent_id)

        # If already finished (event already set), return immediately
        finished = record._result_event.wait(timeout=timeout)
        if not finished:
            # Auto-cancel the sub-agent unless caller explicitly opted out.
            # This prevents orphaned sub-agents from wasting tokens and sending
            # irrelevant Telegram notifications after the caller has moved on.
            cancel_on_timeout = args.get("cancel_on_timeout", True)
            if cancel_on_timeout and not record._cancel_event.is_set():
                record._timeout_cancelled = True
                cancelled = self._owner._supervisor.cancel_owned_child(
                    owner_turn_id, agent_id
                )
                if cancelled is None:
                    record.cancel()
                logger.info(
                    "get_agent_result: timed out after %ds — auto-cancelled agent '%s'",
                    timeout, agent_id,
                )
            return with_execution({
                "success": False,
                "output": f"get_agent_result: timed out after {timeout}s waiting for agent '{agent_id}'.",
                "error": "",
                "exit_code": 0,
                "status": "timeout",
                "agent_id": agent_id,
                "error_type": "tool_timeout",
                "recoverable": True,
                "suggestion": "Wait for the sub-agent to finish and call get_agent_result again.",
            }, "executed")

        error_type = ""
        recoverable = False
        suggestion = ""
        if record.status == "failed":
            error_type = "wrong_model_for_task"
            recoverable = False
            suggestion = "Consider using a different model or breaking the task into smaller steps."
        result = {
            "success": record.status == "done",
            "output": record.result or "",
            "error": record.result if record.status == "failed" else "",
            "exit_code": 0 if record.status == "done" else -1,
            "status": record.status,
            "result_type": record.result_type,
            "result": record.result,
            "agent_id": agent_id,
            "error_type": error_type,
            "recoverable": recoverable,
            "suggestion": suggestion,
        }
        self._owner._supervisor.mark_result_retrieved(owner_turn_id, agent_id)
        return with_execution(result, "executed")

    @staticmethod
    def _discarded_result(tool_name: str, agent_id: str) -> dict:
        """Return an actionable terminal response without discarded result text."""
        return with_execution({
            "success": False,
            "output": (
                f"{tool_name}: child '{agent_id}' was cancelled and its unread "
                "result was discarded."
            ),
            "error": "",
            "exit_code": -1,
            "status": "cancelled",
            "agent_id": agent_id,
            "result": None,
            "error_type": "",
            "recoverable": False,
            "suggestion": "Spawn a new child if this work is still needed.",
        }, "executed")
