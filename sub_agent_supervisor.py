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

import inspect
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

from agent_logging import bind_run_context, clear_run_context
from sub_agent_registry import SOURCE_ON_DEMAND
from trace_context import child_trace_id

if TYPE_CHECKING:
    from agent_controller import SubAgentRunner
    from sub_agent_registry import SubAgentRecord

logger = logging.getLogger(__name__)


CANCELLED_SENTINEL = "[Cancelled]"
ORIGIN_ON_DEMAND = "on-demand"
ORIGIN_SCHEDULED = "scheduled"
# Upper bound on how long a turn waits for owned-child lifecycle cleanup. It
# covers a full headless confirmation wait (120s) plus cleanup margin, so only
# genuinely stuck, non-cooperative children are reported and released.
OWNED_CHILD_DRAIN_TIMEOUT_S = 180.0


@dataclass(frozen=True)
class ExecutionOwnership:
    """Immutable execution origin and optional owning operator-turn identity."""

    origin: str
    owner_turn_id: Optional[str] = None
    prompt_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.origin not in (ORIGIN_ON_DEMAND, ORIGIN_SCHEDULED):
            raise ValueError(f"Unsupported execution origin: {self.origin}")
        if self.origin == ORIGIN_SCHEDULED and self.owner_turn_id is not None:
            raise ValueError("Scheduled execution cannot have an operator-turn owner")
        if self.origin == ORIGIN_SCHEDULED and self.prompt_id is not None:
            raise ValueError("Scheduled execution cannot have an operator prompt")


@dataclass
class _OwnedChildState:
    """Turn-owned child state retained through result accounting and cleanup."""

    record: SubAgentRecord
    cleanup_event: threading.Event = field(default_factory=threading.Event)
    result_retrieved: bool = False
    result_discarded: bool = False
    cleanup_errors: list[str] = field(default_factory=list)
    drain_expired: bool = False  # turn released before this child's cleanup completed


@dataclass(frozen=True)
class RunCompletion:
    """Terminal outcome of one supervised run, handed to the presentation owner.

    The supervisor signals completion; deciding when and how to show it is the
    owner's job (ADR-0005), so this carries data only.
    """

    tag: str            # job tag / label of the run
    status: str         # "done" | "failed" | "cancelled"
    result: str         # complete result text ("[Cancelled]" when cancelled)
    elapsed_s: int      # wall-clock seconds
    model: str          # model id the run used
    agent_id: str       # sub-agent id (diagnostic only; not an execution ID)


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
    completion_cb: Optional[Callable[["RunCompletion"], None]] = None  # owner-side terminal handoff
    notify: bool = True                                 # deliver Telegram notification
    source: str = SOURCE_ON_DEMAND                      # registry source category (internal only)
    prompt_id: Optional[str] = None                     # parent prompt id for log correlation
    grant_scope_cb: Optional[Callable[[str], None]] = None  # called with agent_id on terminal so the depth-0 grant ledger can expire that sub-agent's prompt grants


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
    ownership: ExecutionOwnership = field(
        default_factory=lambda: ExecutionOwnership(ORIGIN_ON_DEMAND)
    )
    prompt_registry: Optional[object] = None


class SubAgentSupervisor:
    """Owns the background lifecycle of spawned and scheduled sub-agents."""

    def __init__(self, max_subagents: int = 6):
        self._pool = ThreadPoolExecutor(
            max_workers=max_subagents, thread_name_prefix="sub-agent"
        )
        self._owned_lock = threading.Lock()
        self._owned_children: dict[str, _OwnedChildState] = {}

    @staticmethod
    def turn_can_access_result(record: SubAgentRecord, owner_turn_id: Optional[str]) -> bool:
        """Return whether ``record`` exposes its result to the calling turn."""
        ownership = getattr(record, "ownership", None)
        if ownership is None:
            return record.source == SOURCE_ON_DEMAND and owner_turn_id is None
        if ownership.origin != ORIGIN_ON_DEMAND:
            return False
        if ownership.owner_turn_id is None:
            # Legacy/unowned on-demand run: readable only by an unowned caller.
            return owner_turn_id is None
        return ownership.owner_turn_id == owner_turn_id

    def unaccounted_children(self, owner_turn_id: str) -> list[tuple[str, str]]:
        """Return this turn's running or completed-but-unretrieved children."""
        with self._owned_lock:
            return [
                (agent_id, state.record.status)
                for agent_id, state in self._owned_children.items()
                if state.record.ownership.owner_turn_id == owner_turn_id
                and not state.result_retrieved
                and not state.result_discarded
                and not state.record.is_cancelled
            ]

    def draining_children(self, owner_turn_id: str) -> list[tuple[str, str]]:
        """Return owned children whose lifecycle cleanup has not completed."""
        with self._owned_lock:
            return [
                (agent_id, state.record.status)
                for agent_id, state in self._owned_children.items()
                if state.record.ownership.owner_turn_id == owner_turn_id
                and not state.cleanup_event.is_set()
            ]

    def get_owned_record(
        self, owner_turn_id: Optional[str], agent_id: str
    ) -> Optional[SubAgentRecord]:
        """Return a retained child record only to its immutable owning turn."""
        if owner_turn_id is None:
            return None
        with self._owned_lock:
            state = self._owned_children.get(agent_id)
            if state is None:
                return None
            if state.record.ownership.owner_turn_id != owner_turn_id:
                return None
            return state.record

    def result_was_discarded(self, owner_turn_id: str, agent_id: str) -> bool:
        """Return whether explicit cancellation discarded an unread result."""
        with self._owned_lock:
            state = self._owned_children.get(agent_id)
            return bool(
                state is not None
                and state.record.ownership.owner_turn_id == owner_turn_id
                and state.result_discarded
            )

    def cancel_owned_child(self, owner_turn_id: str, agent_id: str) -> Optional[bool]:
        """Cancel or discard one owned child, returning None when inaccessible."""
        with self._owned_lock:
            state = self._owned_children.get(agent_id)
            if (
                state is None
                or state.record.ownership.owner_turn_id != owner_turn_id
            ):
                return None
            if state.result_retrieved or state.result_discarded:
                return False
            state.result_discarded = True
            record = state.record
        if record.status == "running" and not record.is_cancelled:
            record.cancel()
        return True

    def cancel_all_owned_children(self, owner_turn_id: str) -> int:
        """Cancel or discard every unaccounted child owned by one turn."""
        with self._owned_lock:
            agent_ids = [
                agent_id
                for agent_id, state in self._owned_children.items()
                if state.record.ownership.owner_turn_id == owner_turn_id
            ]
        return sum(
            self.cancel_owned_child(owner_turn_id, agent_id) is True
            for agent_id in agent_ids
        )

    def mark_result_retrieved(self, owner_turn_id: str, agent_id: str) -> None:
        """Account for one terminal child result consumed by its owning turn."""
        with self._owned_lock:
            state = self._owned_children.get(agent_id)
            if state is None:
                return
            if state.record.ownership.owner_turn_id != owner_turn_id:
                return
            state.result_retrieved = True

    def drain_owned_children(
        self,
        owner_turn_id: str,
        *,
        cancel: bool,
        timeout: float = OWNED_CHILD_DRAIN_TIMEOUT_S,
    ) -> list[str]:
        """Cancel when requested, await cleanup, and return cleanup errors.

        The wait is bounded by *timeout* seconds overall. A child whose
        lifecycle cleanup has not completed by then is reported as an error
        naming it (never as a clean finish) and stays retained, still
        draining, until its own cleanup completes; it is not waited for again.
        """
        with self._owned_lock:
            states = [
                (agent_id, state)
                for agent_id, state in self._owned_children.items()
                if state.record.ownership.owner_turn_id == owner_turn_id
                and not state.drain_expired
            ]
        if cancel:
            for _agent_id, state in states:
                if state.record.status == "running":
                    state.record.cancel()
        deadline = time.monotonic() + timeout
        stuck: list[str] = []
        for agent_id, state in states:
            remaining = max(0.0, deadline - time.monotonic())
            if not state.cleanup_event.wait(timeout=remaining):
                stuck.append(agent_id)
        with self._owned_lock:
            for agent_id, state in states:
                if agent_id not in stuck:
                    self._owned_children.pop(agent_id, None)
                    continue
                if state.cleanup_event.is_set():
                    # Cleanup completed after the deadline but before release.
                    self._owned_children.pop(agent_id, None)
                    stuck.remove(agent_id)
                    continue
                state.drain_expired = True
            errors = [
                error
                for agent_id, state in states
                if agent_id not in stuck
                for error in state.cleanup_errors
            ]
        for agent_id in stuck:
            error = (
                f"lifecycle cleanup for {agent_id} did not complete within "
                f"{timeout:g}s; the child is still draining"
            )
            logger.error("spawn_agent: %s", error)
            errors.append(error)
        return errors

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

        factory_kwargs = request.factory_kwargs
        if request.ownership.origin == ORIGIN_SCHEDULED:
            factory_kwargs = {**factory_kwargs, "context_payload": {}}
        try:
            runner = request.factory(**factory_kwargs)
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
        record.prompt_id = request.ownership.prompt_id
        record.ownership = request.ownership
        if request.ownership.owner_turn_id is not None:
            with self._owned_lock:
                self._owned_children[runner.agent_id] = _OwnedChildState(record)
        try:
            if (
                request.ownership.origin == ORIGIN_ON_DEMAND
                and request.ownership.prompt_id is not None
                and request.prompt_registry is not None
            ):
                request.prompt_registry.associate_sub_agent(
                    request.ownership.prompt_id,
                    runner.agent_id,
                )

            # Log spawn params for observability
            logger.info(
                "spawn_agent: id=%s label=%s model=%s task=%s",
                runner.agent_id, request.label, runner.model_id, request.task[:100],
            )

            self._pool.submit(
                lambda: self._run_and_notify(request, options, runner, record)
            )
        except Exception:
            # Admission failed after registration: roll back every piece of
            # per-run state so no registry capacity slot, owned-child record,
            # or prompt association is stranded by the failed submission.
            from sub_agent_registry import deregister_run

            with self._owned_lock:
                self._owned_children.pop(runner.agent_id, None)
            if (
                request.ownership.origin == ORIGIN_ON_DEMAND
                and request.ownership.prompt_id is not None
                and request.prompt_registry is not None
            ):
                try:
                    request.prompt_registry.remove_sub_agent(
                        request.ownership.prompt_id,
                        runner.agent_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "spawn_agent: rollback of prompt association failed for %s",
                        runner.agent_id,
                        exc_info=True,
                    )
            deregister_run(runner.agent_id)
            raise

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
        notify_result = (
            options.notify and request.ownership.origin == ORIGIN_SCHEDULED
        )
        completion_cb = options.completion_cb
        context_save_attempted = False

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
                prompt_id=(
                    str(request.ownership.prompt_id)
                    if request.ownership.prompt_id is not None
                    else ""
                ),
            )
            # Real SubAgentRunner.run() accepts a prompt_id kwarg; test fakes may
            # not, so fall back to the legacy signature. Use inspect.signature
            # rather than a bare TypeError catch so real TypeErrors inside the
            # runner do not silently restart execution.
            run_signature = inspect.signature(runner.run)
            if "prompt_id" in run_signature.parameters:
                result = runner.run(task, prompt_id=request.ownership.prompt_id)
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
                    completion_cb=completion_cb,
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
                    completion_cb=completion_cb,
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
                completion_cb=completion_cb,
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
            cleanup_stages: list[tuple[str, Callable[[], object]]] = [
                ("save_context", _save_context_before_completion),
                ("deregister_run", lambda: deregister_run(runner.agent_id)),
                ("runner.close", runner.close),
                ("clear_run_context", clear_run_context),
            ]
            if options.grant_scope_cb:
                cleanup_stages.append(
                    (
                        "grant_scope_cb",
                        lambda: options.grant_scope_cb(runner.agent_id),
                    )
                )
            if options.finish_cb:
                cleanup_stages.append(("finish_cb", lambda: options.finish_cb(finish_tag)))

            cleanup_errors: list[str] = []
            try:
                for stage_name, cleanup_stage in cleanup_stages:
                    try:
                        cleanup_stage()
                    except Exception as cleanup_exc:  # noqa: BLE001
                        error = f"{stage_name} failed for {runner.agent_id}: {cleanup_exc}"
                        cleanup_errors.append(error)
                        logger.warning("spawn_agent: %s", error, exc_info=True)
            finally:
                self._signal_owned_cleanup(record, cleanup_errors)

    def _signal_owned_cleanup(
        self, record: SubAgentRecord, cleanup_errors: Optional[list[str]] = None
    ) -> None:
        """Publish lifecycle cleanup after runner close and terminal callbacks."""
        agent_id = getattr(record, "agent_id", None)
        if agent_id is None:
            return
        with self._owned_lock:
            state = self._owned_children.get(agent_id)
            if state is None:
                return
            if cleanup_errors:
                state.cleanup_errors.extend(cleanup_errors)
            state.cleanup_event.set()
            if state.drain_expired:
                # The owning turn already reported this child as stuck and was
                # released; finish its cleanup contract here.
                self._owned_children.pop(agent_id, None)
                logger.warning(
                    "spawn_agent: late lifecycle cleanup completed for %s%s",
                    agent_id,
                    f" with errors: {state.cleanup_errors}" if state.cleanup_errors else "",
                )

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
        completion_cb: Optional[Callable[[RunCompletion], None]],
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
        hands the terminal outcome to the presentation owner. The supervisor
        never sends result notices itself.
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
        if not notify or completion_cb is None:
            return
        if status == "cancelled" and record.timeout_cancelled:
            return
        try:
            completion_cb(
                RunCompletion(
                    tag=label, status=status, result=result_log_result,
                    elapsed_s=elapsed, model=model, agent_id=runner.agent_id,
                )
            )
        except Exception as notify_exc:  # noqa: BLE001
            logger.warning(
                "spawn_agent: [%s] completion handoff failed (%s): %s",
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
