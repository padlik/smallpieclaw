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
import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

from agent_logging import bind_run_context, clear_run_context
from model_registry import ModelRegistryError
from outcome_utils import is_runtime_failure_text
from sub_agent_registry import SOURCE_ON_DEMAND
from trace_context import child_trace_id

if TYPE_CHECKING:
    from agent_controller import SubAgentRunner
    from sub_agent_registry import SubAgentRecord

logger = logging.getLogger(__name__)


class SupervisorStoppingError(RuntimeError):
    """Raised when a submission reaches a supervisor that is shutting down."""


CANCELLED_SENTINEL = "[Cancelled]"
ORIGIN_ON_DEMAND = "on-demand"
ORIGIN_SCHEDULED = "scheduled"
# Upper bound on how long a turn waits for owned-child lifecycle cleanup. It
# covers a full headless confirmation wait (120s) plus cleanup margin, so only
# genuinely stuck, non-cooperative children are reported and released.
OWNED_CHILD_DRAIN_TIMEOUT_S = 180.0


_ADMISSION_FAILED_LABEL = (
    "spawn_agent: model admission failed; details are withheld (check the model selector and /models)"
)


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


def _result_is_failure(result: object) -> bool:
    """Classify a non-cancelled runner result: structured outcome, then prefix.

    An explicit structured outcome is only the *entire* stripped text parsed as
    one JSON object whose ``success`` is a real boolean; it alone decides. JSON
    embedded in prose or fences, malformed JSON and non-boolean flags are not
    structured outcomes and fall through to the reserved plain-string prefix.
    """
    if isinstance(result, str):
        try:
            parsed = json.loads(result.strip())
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("success"), bool):
            return not parsed["success"]
    return is_runtime_failure_text(result)


@dataclass
class _AcceptedEntry:
    """An accepted submission that has not yet completed terminal cleanup."""

    request: SubmissionRequest
    options: SupervisionOptions
    runner: SubAgentRunner
    record: SubAgentRecord
    future: Optional[Future] = None


class SubAgentSupervisor:
    """Owns the background lifecycle of spawned and scheduled sub-agents."""

    def __init__(self, max_subagents: int = 6):
        self._pool = ThreadPoolExecutor(
            max_workers=max_subagents, thread_name_prefix="sub-agent"
        )
        self._owned_lock = threading.Lock()
        self._owned_children: dict[str, _OwnedChildState] = {}
        self._active_turn_ids: set[str] = set()
        # Guards only accepted-entry tracking and the stopping flag; never
        # nested with ``_owned_lock`` or registry locks. Reentrant only so a
        # synchronous (inline) pool double can retire its entry during submit.
        self._submit_lock = threading.RLock()
        self._entries: dict[str, _AcceptedEntry] = {}
        self._stopping = False

    def register_active_turn(self, turn_id: str) -> None:
        """Mark *turn_id* as an operator turn whose confirmation channel is live."""
        with self._owned_lock:
            self._active_turn_ids.add(turn_id)

    def unregister_active_turn(self, turn_id: str) -> None:
        """Remove only *turn_id* from the active set; unknown ids are a no-op."""
        with self._owned_lock:
            self._active_turn_ids.discard(turn_id)

    def child_may_use_turn_grants(self, agent_id: str) -> bool:
        """Return whether *agent_id* may consult its owning turn's session grants.

        A child unknown to the supervisor is not turn-owned, so the legacy
        ledger path stays unchanged for it. A known child that is cancelled or
        drain-expired is always denied, whether or not any turn is active.
        Otherwise only a child whose immutable owner is one of the currently
        active operator turns may; every other known child is denied.
        """
        with self._owned_lock:
            state = self._owned_children.get(agent_id)
            if state is None:
                return True
            if state.drain_expired or state.record.is_cancelled:
                return False
            return state.record.ownership.owner_turn_id in self._active_turn_ids

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
            # An inactive Main refuses implicit requests until the operator selects one.
            no_main = isinstance(exc, ModelRegistryError) and exc.code == "no_active_selection"
            bad_selector = isinstance(exc, ModelRegistryError) and exc.code in {
                "malformed_selector", "unknown_connection",
            }
            # Only the trusted registry error carries fixed, safe text; any other factory ValueError may hold provider/secret text, so it
            # is replaced by a fixed label and never forwarded or stringified.
            trusted = isinstance(exc, ModelRegistryError)
            return {
                "success": False,
                "output": "",
                "error": f"spawn_agent: {exc}" if trusted else _ADMISSION_FAILED_LABEL,
                "exit_code": -1,
                "error_type": "wrong_model_for_task",
                "recoverable": False,
                "suggestion": (
                    "Use /models to select a main model, then retry this request."
                    if no_main
                    else (
                        "Use '<connection>/<model>' where <connection> is a configured provider connection."
                        if bad_selector
                        else "Choose a model that exists in the configured model list."
                    )
                ),
            }

        # Create + wire (cancel_event/LLM client/on-step) + register the record
        # through the shared helper so spawn and plan/diagnostic paths stay in
        # sync. ``options.source`` distinguishes on-demand from scheduled runs;
        # both count against the global capacity guard.
        # From here the supervisor owns the constructed runner: any failure
        # before the task is accepted rolls back by runner identity and closes it.
        try:
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

            entry = _AcceptedEntry(request, options, runner, record)
            with self._submit_lock:
                if self._stopping:
                    raise SupervisorStoppingError("sub-agent supervisor is shutting down")
                self._entries[runner.agent_id] = entry
                try:
                    entry.future = self._pool.submit(
                        lambda: self._run_and_notify(request, options, runner, record)
                    )
                except BaseException:
                    self._entries.pop(runner.agent_id, None)
                    raise
        except BaseException:
            # Pre-acceptance failure (including cancellation): independently
            # attempt every rollback stage once; cleanup faults are logged and
            # never replace the primary exception, which is re-raised.
            self._rollback_rejected(request, runner)
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

    def _rollback_rejected(self, request: SubmissionRequest, runner: SubAgentRunner) -> None:
        """Release everything a rejected submission may have installed.

        Locates state by ``runner.agent_id`` so it works even when registration
        inserted a record and then raised. Each stage runs independently.
        """
        from sub_agent_registry import deregister_run

        agent_id = runner.agent_id
        prompt_id = request.ownership.prompt_id
        stages: list[tuple[str, Callable[[], object]]] = [
            ("owned_children", lambda: self._discard_owned(agent_id)),
        ]
        if (
            request.ownership.origin == ORIGIN_ON_DEMAND
            and prompt_id is not None
            and request.prompt_registry is not None
        ):
            registry = request.prompt_registry
            stages.append(
                ("prompt_association", lambda: registry.remove_sub_agent(prompt_id, agent_id))
            )
        stages.append(("deregister_run", lambda: deregister_run(agent_id)))
        stages.append(("runner.close", runner.close))
        for stage_name, stage in stages:
            try:
                stage()
            except BaseException:  # noqa: BLE001
                # Every stage must run and the caller's primary exception must
                # survive, even if a stage is itself cancelled.
                logger.warning(
                    "spawn_agent: rollback stage %s failed for %s",
                    stage_name, agent_id, exc_info=True,
                )

    def _discard_owned(self, agent_id: str) -> None:
        """Drop the turn-owned child state for *agent_id*, if any."""
        with self._owned_lock:
            self._owned_children.pop(agent_id, None)

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
        task = request.task
        label = request.label
        job_tag = options.job_tag
        finish_tag = job_tag or label
        result_log_cb = options.result_log_cb
        notify_result = (
            options.notify and request.ownership.origin == ORIGIN_SCHEDULED
        )
        completion_cb = options.completion_cb
        _save_context_before_completion = self._context_saver(request, runner)

        primary: Optional[BaseException] = None
        terminal_started = False
        try:
            try:
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
                    terminal_started = True
                    if result == CANCELLED_SENTINEL or record.is_cancelled:
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
                    elif _result_is_failure(result):
                        # Failure, not success. Cancellation was handled above; then an
                        # explicit structured outcome decides, and only an unstructured
                        # string falls back to the reserved runtime terminal-failure prefix.
                        elapsed = int(time.time() - record.started_at)
                        self._finalize_terminal(
                            record=record,
                            status="failed",
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
                            result_log_success=False,
                            log_level="error",
                            log_msg=(
                                "spawn_agent: [%s] failed | id=%s model=%s elapsed=%ds"
                            ),
                            log_args=(label, runner.agent_id, runner.model_id, elapsed),
                            icon_verb=("❌", "failed"),
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
                    if terminal_started:
                        # A terminal-stage fault is never re-finalized as an
                        # execution failure; it propagates after cleanup.
                        try:
                            # Fixed label, agent id and exception class only: raw
                            # text/traceback may carry secrets or provider output.
                            logger.error(
                                "spawn_agent: terminal stage fault | id=%s error_class=%s",
                                runner.agent_id, type(exc).__name__,
                            )
                        except BaseException:  # noqa: BLE001
                            # Best-effort: a diagnostic failure (even cancellation)
                            # must never replace the propagating primary fault.
                            pass
                        raise
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
            except BaseException as primary_exc:  # noqa: BLE001
                primary = primary_exc
                raise
        finally:
            # Persist conversation context (if requested) regardless of
            # success/cancellation/failure so a crash mid-task does not lose
            # the sub-agent's short-term memory.
            self._terminal_cleanup(
                options, runner, record, finish_tag,
                _save_context_before_completion, clear_context=True,
                primary=primary,
            )

    @staticmethod
    def _context_saver(
        request: SubmissionRequest, runner: SubAgentRunner
    ) -> Callable[[], None]:
        """Return an at-most-once, failure-tolerant context persistence stage."""
        attempted = False

        def save() -> None:
            """Persist context before exposing completion to result callers."""
            nonlocal attempted
            if not request.context_key or attempted or request.save_context is None:
                return
            attempted = True
            try:
                request.save_context(
                    request.context_key, runner.short_term, request.data_dir
                )
            except Exception as save_exc:  # noqa: BLE001
                logger.warning(
                    "spawn_agent: [%s] context save failed for %s: %s",
                    request.label, request.context_key, save_exc,
                )

        return save

    def _terminal_cleanup(
        self,
        options: SupervisionOptions,
        runner: SubAgentRunner,
        record: SubAgentRecord,
        finish_tag: str,
        save_context_cb: Callable[[], None],
        *,
        clear_context: bool,
        primary: Optional[BaseException] = None,
    ) -> None:
        """Independently attempt each terminal cleanup stage exactly once.

        *primary* is the exception propagating out of the caller's own
        operation, if any; cleanup cancellation never replaces it.
        """
        from sub_agent_registry import deregister_run

        cleanup_stages: list[tuple[str, Callable[[], object]]] = [
            ("save_context", save_context_cb),
            ("deregister_run", lambda: deregister_run(runner.agent_id)),
            ("runner.close", runner.close),
        ]
        if clear_context:
            cleanup_stages.append(("clear_run_context", clear_run_context))
        if options.grant_scope_cb:
            cleanup_stages.append(
                ("grant_scope_cb", lambda: options.grant_scope_cb(runner.agent_id))
            )
        if options.finish_cb:
            cleanup_stages.append(("finish_cb", lambda: options.finish_cb(finish_tag)))

        cleanup_errors: list[str] = []
        deferred: Optional[BaseException] = None
        try:
            for stage_name, cleanup_stage in cleanup_stages:
                try:
                    cleanup_stage()
                except BaseException as cleanup_exc:  # noqa: BLE001
                    error = f"{stage_name} failed for {runner.agent_id}: {cleanup_exc!r}"
                    cleanup_errors.append(error)
                    logger.warning("spawn_agent: %s", error, exc_info=True)
                    if not isinstance(cleanup_exc, Exception) and deferred is None:
                        # Cancellation propagates only after every stage ran.
                        deferred = cleanup_exc
        finally:
            try:
                self._signal_owned_cleanup(record, cleanup_errors)
            except BaseException as signal_exc:  # noqa: BLE001
                logger.warning(
                    "spawn_agent: owned cleanup signal failed for %s",
                    runner.agent_id, exc_info=True,
                )
                if not isinstance(signal_exc, Exception) and deferred is None:
                    deferred = signal_exc
            finally:
                self._retire_entry(runner.agent_id)
        if deferred is not None and primary is None:
            # Cleanup cancellation propagates only with no primary in flight.
            raise deferred

    def _retire_entry(self, agent_id: str) -> None:
        """Forget an accepted entry once its outermost cleanup has finished."""
        with self._submit_lock:
            self._entries.pop(agent_id, None)

    def _finalize_cancelled_entry(self, entry: "_AcceptedEntry") -> None:
        """Run cancellation-only terminal effects for work that never started.

        Runs on the shutdown caller, outside every lock, without worker-only
        context binding so the caller's thread-local context is untouched.
        """
        request, options = entry.request, entry.options
        runner, record = entry.runner, entry.record
        save_context_cb = self._context_saver(request, runner)
        primary: Optional[BaseException] = None
        try:
            self._finalize_terminal(
                record=record,
                status="cancelled",
                result=CANCELLED_SENTINEL,
                runner=runner,
                label=request.label,
                task=request.task,
                elapsed=int(time.time() - record.started_at),
                model=runner.model_id,
                result_log_cb=options.result_log_cb,
                notify=options.notify and request.ownership.origin == ORIGIN_SCHEDULED,
                completion_cb=options.completion_cb,
                save_context_cb=save_context_cb,
                result_log_result=CANCELLED_SENTINEL,
                result_log_success=False,
                log_level="info",
                log_msg="spawn_agent: [%s] cancelled before start | id=%s",
                log_args=(request.label, runner.agent_id),
                icon_verb=("🛑", "cancelled"),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "spawn_agent: terminal stage failed for %s", runner.agent_id,
                exc_info=True,
            )
        except BaseException as primary_exc:  # noqa: BLE001
            primary = primary_exc
            raise
        finally:
            self._terminal_cleanup(
                options, runner, record, options.job_tag or request.label,
                save_context_cb, clear_context=False, primary=primary,
            )

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
        deferred: Optional[BaseException] = None

        def defer(exc: BaseException) -> None:
            """Keep the first cancellation, else the first ordinary fault."""
            nonlocal deferred
            if deferred is None or (
                isinstance(deferred, Exception) and not isinstance(exc, Exception)
            ):
                deferred = exc

        try:
            save_context_cb()
        except BaseException as save_exc:  # noqa: BLE001
            # A fault must not skip later attempts; re-raise afterwards.
            defer(save_exc)
        try:
            try:
                record.signal_result()
            except BaseException as signal_exc:  # noqa: BLE001
                # A signal fault must not skip the remaining attempts.
                defer(signal_exc)
            try:
                log_fn = logger.error if log_level == "error" else logger.info
                if exc_info:
                    log_fn(log_msg, *log_args, exc_info=True)
                else:
                    log_fn(log_msg, *log_args)
            except BaseException as lifecycle_exc:  # noqa: BLE001
                defer(lifecycle_exc)
            try:
                self._safe_result_log(
                    result_log_cb,
                    tag=label,
                    task=task,
                    result=result_log_result,
                    success=result_log_success,
                    elapsed_s=elapsed,
                    model=model,
                )
            except BaseException as log_exc:  # noqa: BLE001
                # Cancellation must not skip the completion handoff.
                defer(log_exc)
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
            except BaseException as notify_cancel:  # noqa: BLE001
                defer(notify_cancel)
        finally:
            if deferred is not None:
                raise deferred


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

        with self._submit_lock:
            self._stopping = True
            accepted = list(self._entries.values())
        for entry in accepted:
            entry.record.cancel()
        registry = get_registry()
        active = registry.list_active()
        for record in active:
            record.cancel()
        # Queue-first: cancel never-started Futures and finalize them BEFORE the
        # cooperative drain of started work, so queued cleanup is not hostage to
        # the drain. Started workers keep ownership of their own clients.
        self._pool.shutdown(wait=False, cancel_futures=True)
        with self._submit_lock:
            cancelled = [
                entry for entry in self._entries.values()
                if entry.future is not None and entry.future.cancelled()
            ]
            for entry in cancelled:
                self._entries.pop(entry.runner.agent_id, None)
        deferred: Optional[BaseException] = None

        def defer(exc: BaseException) -> None:
            """Keep the first cancellation, else the first ordinary fault."""
            nonlocal deferred
            if deferred is None or (
                isinstance(deferred, Exception) and not isinstance(exc, Exception)
            ):
                deferred = exc

        for entry in cancelled:
            try:
                self._finalize_cancelled_entry(entry)
            except BaseException as exc:  # noqa: BLE001
                # One entry's failure or cancellation must not abandon the
                # remaining claimed entries or the drain; re-raise afterwards.
                logger.warning(
                    "Shutdown: terminal cleanup failed for %s",
                    entry.runner.agent_id, exc_info=True,
                )
                if not isinstance(exc, Exception):
                    defer(exc)
        try:
            if active:
                logger.info("Shutdown: cancelling %d active sub-agent(s)…", len(active))
                deadline = time.monotonic() + graceful_timeout
                while time.monotonic() < deadline:
                    if not any(r.status == "running" for r in registry.list_active()):
                        break
                    time.sleep(0.25)
        except BaseException as drain_exc:  # noqa: BLE001
            defer(drain_exc)
        if deferred is not None:
            raise deferred
        logger.debug("Sub-agent pool shut down.")
