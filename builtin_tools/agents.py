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

from sub_agent_supervisor import SubmissionRequest, SupervisionOptions

from builtin_tools import context_io

if TYPE_CHECKING:
    from builtin_executor import BuiltinExecutor

from sub_agent_registry import SOURCE_ON_DEMAND, get_registry as _get_agent_registry

logger = logging.getLogger(__name__)


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
        return {
            "success": False,
            "output": "",
            "error": "spawn_agent: 'task' is required.",
            "exit_code": -1,
            "error_type": "fundamentally_wrong_approach",
            "recoverable": False,
            "suggestion": "Provide a clear task string describing what the sub-agent should do.",
        }

    # Depth guard — prevent recursive sub-agent spawning (hard error, not a silent no-op)
    if caller_depth >= 1:
        return {
            "success": False,
            "output": "",
            "error": "spawn_agent cannot be called from within a sub-agent (max nesting depth: 1).",
            "exit_code": -1,
            "error_type": "fundamentally_wrong_approach",
            "recoverable": False,
            "suggestion": "Do not spawn sub-agents from within a sub-agent; perform the work directly.",
        }

    if owner._sub_agent_factory is None:
        return {
            "success": False,
            "output": "",
            "error": "spawn_agent: sub_agent_factory not configured.",
            "exit_code": -1,
            "error_type": "impossible_with_current_tools",
            "recoverable": False,
            "suggestion": "The agent runtime is missing sub-agent support; this cannot be recovered in-flight.",
        }

    # Concurrency cap — preserve current managed-record semantics.
    # Scheduled launches still use source="on-demand" until the later
    # running-agent visibility/cap policy change decides otherwise.
    current_managed = get_agent_registry().count_managed()
    if current_managed >= owner._max_subagents:
        return {
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
        }

    # response_format — how the sub-agent should return its result
    response_format = args.get("response_format", "text").lower()
    if response_format not in ("text", "json", "file"):
        response_format = "text"
    if response_format == "json":
        task = task + "\n\nReturn your entire answer as a single valid JSON object. Do not include any prose or markdown fences."
    elif response_format == "file":
        task = task + "\n\nWrite your output to a file and return only the absolute file path as your answer."

    model = args.get("model") or None
    context_key = args.get("context_key") or None
    if context_key:
        try:
            context_key = context_io._validate_context_key(str(context_key))
        except ValueError as exc:
            return {
                "success": False,
                "output": "",
                "error": f"spawn_agent: invalid context_key: {exc}",
                "exit_code": -1,
                "error_type": "permission_denied",
                "recoverable": False,
                "suggestion": "Use a context_key with only letters, digits, underscore, dash, or dot.",
            }

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


def _coerce_overrides(args: dict) -> dict[str, Optional[int | float]]:
    """Coerce optional LLM parameter overrides from model-facing args.

    ``max_tokens`` is converted to ``int``; ``temperature`` and ``top_p`` to
    ``float``. Unparsable or missing values become ``None``.
    """
    overrides: dict[str, Optional[int | float]] = {}
    for key, converter in (
        ("max_tokens", int),
        ("temperature", float),
        ("top_p", float),
    ):
        raw = args.get(key)
        if raw is None:
            overrides[key] = None
            continue
        try:
            overrides[key] = converter(raw)
        except (ValueError, TypeError):
            overrides[key] = None
    return overrides


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
        # Propagate the active prompt id (if any) onto the supervision options
        # so the supervisor can bind it into the sub-agent's log context.
        if options.prompt_id is None:
            options.prompt_id = getattr(self._owner, "_current_prompt_id", None)

        validation = _validate_spawn_args(self._owner, args, caller_depth, options)
        if isinstance(validation, dict):
            return validation
        task, response_format, model, context_key, label = validation

        context_payload = _build_context_payload(task, args, self._owner)
        overrides = _coerce_overrides(args)

        fallback_models = args.get("fallback_models")  # None = inherit; [] = disable
        # Internal supervision controls (job tag, callbacks, notify/expandable)
        # arrive via `options`, never through the model-facing `args` dict.

        # Build the sub-agent via factory
        max_iterations = args.get("max_iterations")  # None = use factory default (scheduled_max_iter)
        if max_iterations is not None:
            try:
                max_iterations = int(max_iterations)
                if max_iterations <= 0:
                    max_iterations = None  # treat 0/negative as "use default"
            except (ValueError, TypeError):
                max_iterations = None

        # Construction profile travels through the internal factory channel only
        # (never through the model-facing ``args`` dict). Scheduled launches carry
        # source="scheduled" via SupervisionOptions and construct under the
        # SCHEDULED_AGENT profile; model-facing spawns use ON_DEMAND_SUBAGENT.
        from agent_runtime import RuntimeProfile
        from sub_agent_registry import SOURCE_SCHEDULED
        runtime_profile = (
            RuntimeProfile.SCHEDULED_AGENT
            if options.source == SOURCE_SCHEDULED
            else RuntimeProfile.ON_DEMAND_SUBAGENT
        )

        factory_kwargs = dict(
            model=model,
            context_key=context_key,
            label=label,
            notify_fn=None,   # factory sets this from main notify_fn
            fallback_models=fallback_models,
            max_iterations=max_iterations,
            max_tokens=overrides["max_tokens"],
            temperature=overrides["temperature"],
            top_p=overrides["top_p"],
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
        )
        # Accepted run — the supervisor owns registration, background execution,
        # result signalling, context persistence, notification, scheduler
        # callbacks, and cleanup.
        result = self._owner._supervisor.submit(request, options)

        # Record the spawned sub-agent against the active prompt, if any.
        if result.get("success") and "agent_id" in result:
            registry = getattr(self._owner, "_prompt_registry", None)
            prompt_id = getattr(self._owner, "_current_prompt_id", None)
            if registry is not None and prompt_id is not None:
                registry.add_sub_agent(prompt_id, result["agent_id"])

        return result

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
            return {
                "success": False,
                "output": "",
                "error": "wait_for_any_agent: 'agent_ids' must be a non-empty list.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide a list of sub-agent IDs returned by spawn_agent.",
            }

        timeout = args.get("timeout", self._owner._subagent_result_timeout)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = self._owner._subagent_result_timeout

        records = []
        for raw_id in agent_ids:
            aid = str(raw_id).strip()
            record = get_agent_registry().get(aid)
            if record is None:
                record = get_agent_registry().get_completed(aid)
            if record is None:
                return {
                    "success": False,
                    "output": "",
                    "error": f"wait_for_any_agent: no active sub-agent with id '{aid}'.",
                    "exit_code": -1,
                    "status": "not_found",
                    "error_type": "file_not_found",
                    "recoverable": False,
                    "suggestion": "Check /agents or recent notifications for valid IDs.",
                }
            records.append(record)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for record in records:
                if record._result_event.is_set() and record.status in ("done", "failed", "cancelled"):
                    return {
                        "success": record.status == "done",
                        "output": record.result or "",
                        "error": record.result if record.status == "failed" else "",
                        "exit_code": 0 if record.status == "done" else -1,
                        "status": record.status,
                        "agent_id": record.agent_id,
                        "result": record.result,
                        "result_type": record.result_type,
                    }
            time.sleep(0.2)

        return {
            "success": False,
            "output": f"wait_for_any_agent: timed out after {timeout}s.",
            "error": "",
            "exit_code": 0,
            "status": "timeout",
            "agent_ids": [r.agent_id for r in records],
        }

    def _exec_cancel_agent(self, args: dict, caller_tag: str = "") -> dict:
        """Cancel a specific sub-agent or all managed sub-agents.

        Not confirmation-gated: the LLM can cancel its own workers directly.
        The operator retains `/agents cancel` and `/stop` as overrides.
        """
        get_agent_registry = _get_agent_registry

        agent_id = args.get("agent_id", "").strip()
        if not agent_id:
            return {
                "success": False,
                "output": "",
                "error": "cancel_agent: 'agent_id' is required (or 'managed'/'all' to cancel all).",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide a sub-agent id, or use 'managed'/'all' to cancel all managed agents.",
            }

        if agent_id in ("managed", "all"):
            # The model-facing cancel_agent tool cancels only sub-agents the LLM
            # itself spawned (on-demand). Scheduled jobs are operator-owned and
            # must be cancelled via /agents or /jobs commands, not by a sub-agent.
            targets = [
                r for r in get_agent_registry().list_active()
                if r.source == SOURCE_ON_DEMAND
            ]
            for r in targets:
                r.cancel()
            n = len(targets)
            return {
                "success": True,
                "output": f"Cancelled {n} managed sub-agent(s).",
                "error": "",
                "exit_code": 0,
            }

        record = get_agent_registry().get(agent_id)
        if record is None:
            return {
                "success": False,
                "output": "",
                "error": f"cancel_agent: no active sub-agent with id '{agent_id}'.",
                "exit_code": -1,
                "error_type": "file_not_found",
                "recoverable": False,
                "suggestion": "The agent may have already finished; check /agents or recent notifications.",
            }
        if record.source != SOURCE_ON_DEMAND:
            return {
                "success": False,
                "output": "",
                "error": (
                    f"cancel_agent: sub-agent '{agent_id}' is a '{record.source}' agent "
                    "and cannot be cancelled by the LLM. Use /agents cancel or /stop."
                ),
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Only on-demand sub-agents can be cancelled via cancel_agent.",
            }
        record.cancel()
        return {
            "success": True,
            "output": f"Cancelled sub-agent '{agent_id}'.",
            "error": "",
            "exit_code": 0,
        }

    def _exec_get_agent_result(self, args: dict, caller_tag: str = "") -> dict:
        """
        Wait for a sub-agent to finish and return its result.

        Blocks until the agent's _result_event is set or timeout expires.
        """
        get_agent_registry = _get_agent_registry

        agent_id = args.get("agent_id", "").strip()
        if not agent_id:
            return {
                "success": False,
                "output": "",
                "error": "get_agent_result: 'agent_id' is required.",
                "exit_code": -1,
                "error_type": "fundamentally_wrong_approach",
                "recoverable": False,
                "suggestion": "Provide the agent_id returned by spawn_agent.",
            }

        timeout = args.get("timeout", self._owner._subagent_result_timeout)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = self._owner._subagent_result_timeout

        record = get_agent_registry().get(agent_id)
        if record is None:
            return {
                "success": False,
                "output": "",
                "error": f"get_agent_result: no active sub-agent with id '{agent_id}'.",
                "exit_code": -1,
                "status": "not_found",
                "error_type": "file_not_found",
                "recoverable": False,
                "suggestion": "The agent may have already finished; check /agents or recent notifications.",
            }

        # If already finished (event already set), return immediately
        finished = record._result_event.wait(timeout=timeout)
        if not finished:
            # Auto-cancel the sub-agent unless caller explicitly opted out.
            # This prevents orphaned sub-agents from wasting tokens and sending
            # irrelevant Telegram notifications after the caller has moved on.
            cancel_on_timeout = args.get("cancel_on_timeout", True)
            if cancel_on_timeout and not record._cancel_event.is_set():
                record._timeout_cancelled = True
                record.cancel()
                logger.info(
                    "get_agent_result: timed out after %ds — auto-cancelled agent '%s'",
                    timeout, agent_id,
                )
            return {
                "success": False,
                "output": f"get_agent_result: timed out after {timeout}s waiting for agent '{agent_id}'.",
                "error": "",
                "exit_code": 0,
                "status": "timeout",
                "agent_id": agent_id,
                "error_type": "tool_timeout",
                "recoverable": True,
                "suggestion": "Wait for the sub-agent to finish and call get_agent_result again.",
            }

        error_type = ""
        recoverable = False
        suggestion = ""
        if record.status == "failed":
            error_type = "wrong_model_for_task"
            recoverable = False
            suggestion = "Consider using a different model or breaking the task into smaller steps."
        return {
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
