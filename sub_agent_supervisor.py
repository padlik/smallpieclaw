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
import inspect
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from agent_logging import bind_run_context, clear_run_context
from sub_agent_registry import SOURCE_ON_DEMAND
from trace_context import child_trace_id

if TYPE_CHECKING:
    from agent_controller import SubAgentRunner
    from sub_agent_registry import SubAgentRecord

logger = logging.getLogger(__name__)


CANCELLED_SENTINEL = "[Cancelled]"


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
    prompt_id: Optional[str] = None                     # parent prompt id for log correlation


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
        record.prompt_id = options.prompt_id

        # Log spawn params for observability
        logger.info(
            "spawn_agent: id=%s label=%s model=%s task=%s",
            runner.agent_id, request.label, runner.model_id, request.task[:100],
        )

        self._pool.submit(lambda: self._run_and_notify(request, options, runner, record))

        return {
            "success": True,
            "output": (
                f"Sub-agent spawned (id: {runner.agent_id}, model: {runner.model_id}, "
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

    def _run_and_notify(
        self,
        request: SubmissionRequest,
        options: SupervisionOptions,
        runner: SubAgentRunner,
        record: SubAgentRecord,
    ) -> None:
        """Run the sub-agent to completion and deliver its result.

        Executes on a pool thread. Preserves the terminal-path invariants:
        context is saved before ``record.signal_result`` is set, stale success
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
        context_save_attempted = False

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
            nonlocal context_save_attempted
            if not context_key or context_save_attempted or save_context is None:
                return
            context_save_attempted = True
            try:
                save_context(context_key, runner.short_term, data_dir)
            except Exception as save_exc:  # noqa: BLE001
                logger.warning(
                    "spawn_agent: [%s] context save failed for %s: %s",
                    label, context_key, save_exc,
                )

        try:
            # Bind the parent's prompt_id into the pool thread's log context so
            # every sub-agent log line correlates with the originating prompt.
            parent_trace = runner.trace_id
            run_trace_id = child_trace_id(parent_trace) if parent_trace else ""
            bind_run_context(
                trace=run_trace_id,
                agent=runner.agent_id,
                prompt_id=str(options.prompt_id) if options.prompt_id is not None else "",
            )
            # Real SubAgentRunner.run() accepts a prompt_id kwarg; test fakes may
            # not, so fall back to the legacy signature. Use inspect.signature
            # rather than a bare TypeError catch so real TypeErrors inside the
            # runner do not silently restart execution.
            run_signature = inspect.signature(runner.run)
            if "prompt_id" in run_signature.parameters:
                result = runner.run(task, prompt_id=options.prompt_id)
            else:
                result = runner.run(task)
            # P2 consolidation: sub-agent results are NOT auto-persisted into
            # semantic/graph memory. Auto-writing arbitrary sub-agent output
            # risks prompt poisoning. If a result should be remembered, the
            # operator/main agent must explicitly (and with confirmation) call
            # memory_graph_store.
            if result == CANCELLED_SENTINEL:
                elapsed = int(time.time() - record.started_at)
                self._finalize_terminal(
                    record=record,
                    status="cancelled",
                    result=CANCELLED_SENTINEL,
                    runner=runner,
                    label=label,
                    task=task,
                    elapsed=elapsed,
                    model=runner.model_id,
                    result_log_cb=result_log_cb,
                    notify=notify_result,
                    notify_cb=lambda: self._send_cancelled_text(
                        runner, record, label
                    ),
                    save_context_cb=_save_context_before_completion,
                    result_log_result=CANCELLED_SENTINEL,
                    result_log_success=False,
                    log_level="info",
                    log_msg=("spawn_agent: [%s] cancelled | id=%s"),
                    log_args=(label, runner.agent_id),
                    icon_verb=("🛑", "cancelled"),
                )
            else:
                elapsed = int(time.time() - record.started_at)
                self._finalize_terminal(
                    record=record,
                    status="done",
                    result=result,
                    runner=runner,
                    label=label,
                    task=task,
                    elapsed=elapsed,
                    model=runner.model_id,
                    result_log_cb=result_log_cb,
                    notify=notify_result,
                    notify_cb=lambda: _send_result_html(
                        self._result_header(
                            "✅", "finished", runner, label, task, elapsed
                        ),
                        result,
                    ),
                    save_context_cb=_save_context_before_completion,
                    result_log_result=result,
                    result_log_success=True,
                    log_level="info",
                    log_msg=(
                        "spawn_agent: [%s] done | id=%s model=%s elapsed=%ds"
                    ),
                    log_args=(label, runner.agent_id, runner.model_id, elapsed),
                    icon_verb=("✅", "finished"),
                )
        except Exception as exc:  # noqa: BLE001
            error_text = f"Error: {exc}"
            elapsed = int(time.time() - record.started_at)
            self._finalize_terminal(
                record=record,
                status="failed",
                result=str(exc),
                runner=runner,
                label=label,
                task=task,
                elapsed=elapsed,
                model=runner.model_id,
                result_log_cb=result_log_cb,
                notify=notify_result,
                notify_cb=lambda: _send_result_html(
                    self._result_header(
                        "❌", "failed", runner, label, task, elapsed
                    ),
                    error_text,
                ),
                save_context_cb=_save_context_before_completion,
                result_log_result=error_text,
                result_log_success=False,
                log_level="error",
                log_msg=(
                    "spawn_agent: [%s] failed | id=%s model=%s elapsed=%ds | %s"
                ),
                log_args=(label, runner.agent_id, runner.model_id, elapsed, exc),
                icon_verb=("❌", "failed"),
                exc_info=True,
            )
        finally:
            # Persist conversation context (if requested) regardless of
            # success/cancellation/failure so a crash mid-task does not lose
            # the sub-agent's short-term memory.
            _save_context_before_completion()
            deregister_run(runner.agent_id)
            runner.close()
            clear_run_context()
            if options.finish_cb:
                options.finish_cb(finish_tag)

    def _finalize_terminal(
        self,
        *,
        record: SubAgentRecord,
        status: str,
        result: str,
        runner: SubAgentRunner,
        label: str,
        task: str,
        elapsed: int,
        model: str,
        result_log_cb: Optional[Callable[..., None]],
        notify: bool,
        notify_cb: Callable[[], None],
        save_context_cb: Callable[[], None],
        result_log_result: str,
        result_log_success: bool,
        log_level: str,
        log_msg: str,
        log_args: tuple,
        icon_verb: tuple[str, str],
        exc_info: bool = False,
    ) -> None:
        """Handle the common terminal sequence for a sub-agent run.

        Sets the record status/result, persists context, signals completion,
        logs the outcome, invokes the scheduler result callback safely, and
        delivers any branch-specific notification.
        """
        record.status = status
        record.result = result
        save_context_cb()
        record.signal_result()
        log_fn = logger.error if log_level == "error" else logger.info
        if exc_info:
            log_fn(log_msg, *log_args, exc_info=True)
        else:
            log_fn(log_msg, *log_args)
        self._safe_result_log(
            result_log_cb,
            tag=label,
            task=task,
            result=result_log_result,
            success=result_log_success,
            elapsed_s=elapsed,
            model=model,
        )
        if not notify:
            return
        if status == "cancelled" and record.timeout_cancelled:
            return
        try:
            notify_cb()
        except Exception as notify_exc:  # noqa: BLE001
            logger.warning(
                "spawn_agent: [%s] notify failed (%s): %s",
                label, icon_verb[1], notify_exc,
            )

    def _safe_result_log(
        self,
        cb: Optional[Callable[..., None]],
        *,
        tag: Optional[str],
        task: str,
        result: str,
        success: bool,
        elapsed_s: int,
        model: str,
    ) -> None:
        """Invoke the scheduler result callback, swallowing exceptions."""
        if cb is None:
            return
        try:
            cb(
                tag=tag,
                task=task,
                result=result,
                success=success,
                elapsed_s=elapsed_s,
                model=model,
            )
        except Exception as log_exc:  # noqa: BLE001
            logger.warning(
                "spawn_agent: [%s] result_log_cb failed (%s): %s",
                tag, "success" if success else "terminal", log_exc,
            )

    def _result_header(
        self,
        icon: str,
        verb: str,
        runner: SubAgentRunner,
        label: str,
        task: str,
        elapsed: int,
    ) -> str:
        """Build the common HTML header for a sub-agent result notification."""
        return (
            f"{icon} <b>Sub-agent</b> <code>{_html_mod.escape(runner.agent_id)}</code>"
            f" {verb} ({elapsed}s)\n"
            f"<b>Job:</b> {_html_mod.escape(label)}"
            f" | <b>Model:</b> <code>{_html_mod.escape(runner.model_id)}</code>\n"
            f"<b>Task:</b> {_html_mod.escape(task[:120])}"
        )

    def _send_cancelled_text(
        self,
        runner: SubAgentRunner,
        record: SubAgentRecord,
        label: str,
    ) -> None:
        """Send the plain-text notification for a cancelled sub-agent run."""
        runner.notify_fn(
            f"🛑 Sub-agent {runner.agent_id} cancelled\n"
            f"Job: **{label}**\n"
            f"Completed {record.iteration}/{record.max_iterations} iterations before stop."
        )

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
