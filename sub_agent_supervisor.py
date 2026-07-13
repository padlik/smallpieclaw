"""
sub_agent_supervisor.py — background lifecycle supervision for sub-agents.

The supervisor is the first-class owner of the background execution envelope
around spawned and scheduled sub-agents. It performs synchronous admission
(capacity check, runner construction, ``SubAgentRecord`` creation/registration,
``agent_id`` minting) and then runs the sub-agent on a thread pool, signalling
completion, persisting context, delivering notifications, invoking scheduler
callbacks, and cleaning up.

The model-facing ``spawn_agent``/``BuiltinExecutor._exec_spawn_agent`` path
remains a thin compatibility shim: it validates tool arguments, shapes the task,
builds the context payload, and delegates accepted runs to ``submit``. Scheduler
and other internal supervision controls flow through :class:`SupervisionOptions`
(a per-submission value), never through the model-facing argument dictionary.
"""

from __future__ import annotations

import html as _html_mod
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from sub_agent_registry import SOURCE_ON_DEMAND

logger = logging.getLogger(__name__)


@dataclass
class SupervisionOptions:
    """Per-submission internal supervision controls.

    These are runtime supervision controls (set by the scheduler or other
    internal launch paths), not model-facing tool arguments. They are passed
    per submission so concurrent scheduled jobs cannot overwrite each other's
    callbacks via shared mutable state.
    """

    job_tag: Optional[str] = None                       # scheduler job tag
    finish_cb: Optional[Callable[[str], None]] = None   # called with finish tag on terminal
    result_log_cb: Optional[Callable[..., None]] = None  # scheduler execution-log recorder
    notify: bool = True                                 # deliver Telegram notification
    expandable: bool = True                             # wrap result in expandable blockquote
    source: str = SOURCE_ON_DEMAND                      # registry source category (internal only)


@dataclass
class SubmissionRequest:
    """Everything the supervisor needs to admit and run one sub-agent.

    Built by the model-facing shim (or an internal launch path) after all
    model-facing validation, task shaping, and context-payload construction has
    completed.
    """

    task: str                       # augmented task string passed to runner.run
    response_format: str            # "text" | "json" | "file"
    label: str                      # job_tag or context_key or "on-demand"
    context_key: Optional[str]      # persistence key, or None
    factory: Callable               # sub_agent_factory
    factory_kwargs: dict            # kwargs forwarded to the factory
    data_dir: str                   # for context persistence
    notify_html_fn: Optional[Callable[[str], None]] = None  # HTML notify path
    save_context: Optional[Callable[[str, object, str], None]] = None  # context persister


class SubAgentSupervisor:
    """Owns the background lifecycle of spawned and scheduled sub-agents."""

    def __init__(self, max_subagents: int = 6):
        self._pool = ThreadPoolExecutor(
            max_workers=max_subagents, thread_name_prefix="sub-agent"
        )

    # ------------------------------------------------------------------
    # Admission + submission
    # ------------------------------------------------------------------

    def submit(self, request: SubmissionRequest, options: SupervisionOptions) -> dict:
        """Synchronously admit a sub-agent run and background its execution.

        Returns a success dict (with ``agent_id``) when the run is admitted and
        the background task submitted, or a recoverable/friendly rejection dict
        matching the model-facing ``spawn_agent`` error shape when runner
        construction is rejected (invalid model). The caller (shim/scheduler) is
        responsible for the shared pre-submit capacity check, so only accepted
        runs reach ``submit``. No background task and no ``finish_cb`` fire on
        synchronous admission failure.
        """
        from sub_agent_registry import register_run

        try:
            runner = request.factory(**request.factory_kwargs)
        except ValueError as exc:
            return {
                "success": False,
                "output": "",
                "error": f"spawn_agent: {exc}",
                "exit_code": -1,
                "error_type": "wrong_model_for_task",
                "recoverable": False,
                "suggestion": "Choose a model that exists in the configured model list.",
            }

        # Create + wire (cancel_event/LLM client/on-step) + register the record
        # through the shared helper so spawn and plan/diagnostic paths stay in
        # sync. ``options.source`` distinguishes on-demand from scheduled runs;
        # both count against the global capacity guard.
        record = register_run(
            runner,
            source=options.source,
            label=request.label,
            task_preview=request.task,
            result_type=request.response_format,
        )

        # Log spawn params for observability
        fallback_models = request.factory_kwargs.get("fallback_models")
        _fb_log = str(fallback_models) if fallback_models is not None else "inherited"
        logger.info(
            "spawn_agent: id=%s label=%s model=%s fallback=%s task=%s",
            runner.agent_id, request.label, runner._model_id, _fb_log, request.task[:100],
        )

        self._pool.submit(lambda: self._run_and_notify(request, options, runner, record))

        return {
            "success": True,
            "output": (
                f"Sub-agent spawned (id: {runner.agent_id}, model: {runner._model_id}, "
                f"response_format: {request.response_format}). "
                f"Call get_agent_result(\"{runner.agent_id}\") to retrieve the result when needed."
            ),
            "error": "",
            "exit_code": 0,
            "agent_id": runner.agent_id,
            "response_format": request.response_format,
        }

    # ------------------------------------------------------------------
    # Background lifecycle
    # ------------------------------------------------------------------

    def _run_and_notify(self, request: SubmissionRequest, options: SupervisionOptions,
                        runner, record) -> None:
        """Run the sub-agent to completion and deliver its result.

        Executes on a pool thread. Preserves the terminal-path invariants:
        context is saved before ``record._result_event`` is set, stale success
        notifications are suppressed after a get_agent_result timeout cancel,
        and ``finish_cb`` fires exactly once after unregister/close on every
        background terminal path.
        """
        from sub_agent_registry import deregister_run

        task = request.task
        label = request.label
        context_key = request.context_key
        data_dir = request.data_dir
        save_context = request.save_context
        job_tag = options.job_tag
        finish_tag = job_tag or label
        result_log_cb = options.result_log_cb
        notify_result = options.notify
        expandable = options.expandable
        notify_html = request.notify_html_fn
        context_save_attempted = {"done": False}

        def _send_result_html(header_html: str, body: str) -> None:
            """Send header + body, optionally wrapped in an expandable blockquote."""
            escaped = _html_mod.escape(body)
            if expandable:
                msg = f"{header_html}\n<blockquote expandable>{escaped}</blockquote>"
            else:
                msg = f"{header_html}\n\n{escaped}"
            if notify_html:
                notify_html(msg)
            else:
                runner.notify_fn(msg)

        def _save_context_before_completion() -> None:
            """Persist context before exposing completion to get_agent_result callers."""
            if not context_key or context_save_attempted["done"] or save_context is None:
                return
            context_save_attempted["done"] = True
            try:
                save_context(context_key, runner._short_term, data_dir)
            except Exception as save_exc:  # noqa: BLE001
                logger.warning(
                    "spawn_agent: [%s] context save failed for %s: %s",
                    label, context_key, save_exc,
                )

        try:
            result = runner.run(task)
            # P2 consolidation: sub-agent results are NOT auto-persisted into
            # semantic/graph memory. Auto-writing arbitrary sub-agent output
            # risks prompt poisoning. If a result should be remembered, the
            # operator/main agent must explicitly (and with confirmation) call
            # memory_graph_store.
            if result == "[Cancelled]":
                record.status = "cancelled"
                record.result = "[Cancelled]"
                _save_context_before_completion()
                record._result_event.set()
                elapsed = int(time.time() - record.started_at)
                logger.info("spawn_agent: [%s] cancelled | id=%s", label, runner.agent_id)
                if result_log_cb:
                    try:
                        result_log_cb(
                            tag=job_tag,
                            task=task,
                            result="[Cancelled]",
                            success=False,
                            elapsed_s=elapsed,
                            model=runner._model_id,
                        )
                    except Exception as log_exc:  # noqa: BLE001
                        logger.warning("spawn_agent: [%s] result_log_cb failed (cancelled): %s", label, log_exc)
                # Suppress notification for agents cancelled due to get_agent_result
                # timeout — the caller already received a timeout response.
                if notify_result and not record._timeout_cancelled:
                    try:
                        runner.notify_fn(
                            f"🛑 Sub-agent {runner.agent_id} cancelled\n"
                            f"Job: **{label}**\n"
                            f"Completed {record.iteration}/{record.max_iterations} iterations before stop."
                        )
                    except Exception as notify_exc:  # noqa: BLE001
                        logger.warning("spawn_agent: [%s] notify failed (cancelled): %s", label, notify_exc)
            else:
                record.status = "done"
                record.result = result
                _save_context_before_completion()
                record._result_event.set()
                elapsed = int(time.time() - record.started_at)
                logger.info(
                    "spawn_agent: [%s] done | id=%s model=%s elapsed=%ds",
                    label, runner.agent_id, runner._model_id, elapsed,
                )
                if result_log_cb:
                    try:
                        result_log_cb(
                            tag=job_tag,
                            task=task,
                            result=result,
                            success=True,
                            elapsed_s=elapsed,
                            model=runner._model_id,
                        )
                    except Exception as log_exc:  # noqa: BLE001
                        logger.warning("spawn_agent: [%s] result_log_cb failed: %s", label, log_exc)
                if notify_result:
                    header_html = (
                        f"✅ <b>Sub-agent</b> <code>{_html_mod.escape(runner.agent_id)}</code>"
                        f" finished ({elapsed}s)\n"
                        f"<b>Job:</b> {_html_mod.escape(label)}"
                        f" | <b>Model:</b> <code>{_html_mod.escape(runner._model_id)}</code>\n"
                        f"<b>Task:</b> {_html_mod.escape(task[:120])}"
                    )
                    try:
                        _send_result_html(header_html, result)
                    except Exception as notify_exc:  # noqa: BLE001
                        logger.warning("spawn_agent: [%s] notify failed (success): %s", label, notify_exc)
        except Exception as exc:  # noqa: BLE001
            record.status = "failed"
            record.result = str(exc)
            _save_context_before_completion()
            record._result_event.set()
            elapsed = int(time.time() - record.started_at)
            logger.error(
                "spawn_agent: [%s] failed | id=%s model=%s elapsed=%ds | %s",
                label, runner.agent_id, runner._model_id, elapsed, exc, exc_info=True,
            )
            if result_log_cb:
                try:
                    result_log_cb(
                        tag=job_tag,
                        task=task,
                        result=f"Error: {exc}",
                        success=False,
                        elapsed_s=elapsed,
                        model=runner._model_id,
                    )
                except Exception as log_exc:  # noqa: BLE001
                    logger.warning("spawn_agent: [%s] result_log_cb failed (error): %s", label, log_exc)
            if notify_result:
                header_html = (
                    f"❌ <b>Sub-agent</b> <code>{_html_mod.escape(runner.agent_id)}</code>"
                    f" failed ({elapsed}s)\n"
                    f"<b>Job:</b> {_html_mod.escape(label)}"
                    f" | <b>Model:</b> <code>{_html_mod.escape(runner._model_id)}</code>\n"
                    f"<b>Task:</b> {_html_mod.escape(task[:120])}"
                )
                try:
                    _send_result_html(header_html, f"Error: {exc}")
                except Exception as notify_exc:  # noqa: BLE001
                    logger.warning("spawn_agent: [%s] notify failed (error): %s", label, notify_exc)
        finally:
            # Persist conversation context (if requested) regardless of
            # success/cancellation/failure so a crash mid-task does not lose
            # the sub-agent's short-term memory.
            _save_context_before_completion()
            deregister_run(runner.agent_id)
            runner.close()
            if options.finish_cb:
                options.finish_cb(finish_tag)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self, graceful_timeout: float = 10.0) -> None:
        """Cancel active sub-agents, drain briefly, then shut down the pool.

        Signals all active sub-agents to cancel, waits up to graceful_timeout
        seconds for them to finish, then forces shutdown of any stragglers.
        """
        from sub_agent_registry import get_registry

        registry = get_registry()
        active = registry.list_active()
        if active:
            logger.info("Shutdown: cancelling %d active sub-agent(s)…", len(active))
            for record in active:
                record.cancel()
            deadline = time.monotonic() + graceful_timeout
            while time.monotonic() < deadline:
                if not any(r.status == "running" for r in registry.list_active()):
                    break
                time.sleep(0.25)
        self._pool.shutdown(wait=False, cancel_futures=True)
        logger.debug("Sub-agent pool shut down.")
