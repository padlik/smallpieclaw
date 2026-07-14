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
from typing import TYPE_CHECKING, Optional

from sub_agent_supervisor import SubmissionRequest, SupervisionOptions

from builtin_tools import context_io

if TYPE_CHECKING:
    from builtin_executor import BuiltinExecutor

logger = logging.getLogger(__name__)


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
        from sub_agent_registry import get_registry as get_agent_registry

        options = options or SupervisionOptions()

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

        if self._owner._sub_agent_factory is None:
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
        if current_managed >= self._owner._max_subagents:
            return {
                "success": False,
                "output": "",
                "error": (
                    f"spawn_agent: max_subagents cap reached ({current_managed}/{self._owner._max_subagents}). "
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

        # context_payload — parent context shared with sub-agent
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
                working=self._owner._working,
                memory=self._owner._memory,
                results=self._owner._results,
                graph_memory=self._owner._graph_memory,
            )
        if not isinstance(context_payload, dict):
            context_payload = {"parent_note": str(context_payload)}

        fallback_models = args.get("fallback_models")  # None = inherit; [] = disable
        # Internal supervision controls (job tag, callbacks, notify/expandable)
        # arrive via `options`, never through the model-facing `args` dict.
        label = options.job_tag or context_key or "on-demand"

        # Build the sub-agent via factory
        max_iterations = args.get("max_iterations")  # None = use factory default (scheduled_max_iter)
        if max_iterations is not None:
            try:
                max_iterations = int(max_iterations)
                if max_iterations <= 0:
                    max_iterations = None  # treat 0/negative as "use default"
            except (ValueError, TypeError):
                max_iterations = None

        # Optional per-call LLM parameter overrides
        _raw_max_tokens = args.get("max_tokens")
        _raw_temperature = args.get("temperature")
        _raw_top_p = args.get("top_p")
        try:
            max_tokens_override = int(_raw_max_tokens) if _raw_max_tokens is not None else None
        except (ValueError, TypeError):
            max_tokens_override = None
        try:
            temperature_override = float(_raw_temperature) if _raw_temperature is not None else None
        except (ValueError, TypeError):
            temperature_override = None
        try:
            top_p_override = float(_raw_top_p) if _raw_top_p is not None else None
        except (ValueError, TypeError):
            top_p_override = None

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
            max_tokens=max_tokens_override,
            temperature=temperature_override,
            top_p=top_p_override,
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
        return self._owner._supervisor.submit(request, options)

    def _exec_get_agent_result(self, args: dict, caller_tag: str = "") -> dict:
        """
        Wait for a sub-agent to finish and return its result.

        Blocks until the agent's _result_event is set or timeout expires.
        """
        from sub_agent_registry import get_registry as get_agent_registry

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
