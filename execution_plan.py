"""
execution_plan.py
-----------------
Core execution planning module for orchestrated multi-agent execution.

Provides data structures, validation, topological scheduling, and a batched
executor that runs each plan step inside an isolated sub-agent. The module is
pure execution logic — it has no UI or Telegram dependencies.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from error_registry import ErrorTypeRegistry
from exceptions import AgentError
from react_loop import ReactContext, parse_json

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_\-]+)\}\}")
_STEP_MAX_ITERATIONS = 5
_CANCELLATION_GRACE_SECONDS = 5.0
# How often (seconds) to wake up and re-check cancel_event while waiting for
# running futures.  Smaller = faster parent-cancel response; larger = less
# scheduling overhead.  0.2 s is imperceptible in practice.
_CANCEL_POLL_INTERVAL = 0.2


class PlanValidationError(AgentError):
    """Raised when an execution plan is structurally invalid."""


@dataclass
class PlanStep:
    """A single step in an execution plan.

    Attributes:
        id: Unique step identifier.
        tool: Name of the tool to execute.
        args: Tool arguments; string values may contain ``{{step_id}}``
            placeholders that will be replaced with completed step results.
        depends_on: Step IDs that must complete successfully before this step runs.
        description: Human-readable description of the step.
    """

    id: str
    tool: str
    args: dict
    depends_on: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class ExecutionPlan:
    """A validated, executable plan consisting of dependent steps.

    Attributes:
        description: Human-readable plan description.
        steps: Ordered list of plan steps. The order is arbitrary for execution;
            the executor topologically sorts steps into parallel batches.
        timeout: Maximum total execution time in seconds.
    """

    description: str
    steps: list[PlanStep]
    timeout: int = 300


def validate_plan(plan: ExecutionPlan) -> None:
    """Validate an execution plan.

    Checks for duplicate step IDs, missing dependency references, and circular
    dependencies using Kahn's algorithm. Raises :class:`PlanValidationError` on
    any problem.

    Args:
        plan: The plan to validate.

    Raises:
        PlanValidationError: If the plan is structurally invalid.
    """
    if not plan.steps:
        raise PlanValidationError("Plan has no steps.")

    ids = [step.id for step in plan.steps]
    if len(ids) != len(set(ids)):
        seen: set[str] = set()
        for step_id in ids:
            if step_id in seen:
                raise PlanValidationError(f"Duplicate step id: '{step_id}'.")
            seen.add(step_id)

    id_set = set(ids)
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in id_set:
                raise PlanValidationError(
                    f"Step '{step.id}' depends on unknown step '{dep}'."
                )

    # Kahn's algorithm to detect cycles and ensure sortability.
    in_degree: dict[str, int] = {step.id: 0 for step in plan.steps}
    dependents: dict[str, list[str]] = {step.id: [] for step in plan.steps}
    for step in plan.steps:
        for dep in step.depends_on:
            dependents[dep].append(step.id)
            in_degree[step.id] += 1

    queue = [sid for sid, deg in in_degree.items() if deg == 0]
    processed = 0
    while queue:
        current = queue.pop(0)
        processed += 1
        for dependent in dependents[current]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if processed != len(plan.steps):
        raise PlanValidationError("Plan contains a circular dependency.")


def topological_sort(plan: ExecutionPlan) -> list[list[PlanStep]]:
    """Sort a plan into parallel batches using Kahn's algorithm.

    Each batch contains steps whose dependencies have all been satisfied in
    previous batches and can therefore run concurrently.

    Args:
        plan: A validated execution plan.

    Returns:
        A list of batches, where each batch is a list of :class:`PlanStep`
        instances.

    Raises:
        PlanValidationError: If the plan cannot be sorted (e.g., a cycle).
    """
    validate_plan(plan)

    steps_by_id = {step.id: step for step in plan.steps}
    in_degree: dict[str, int] = {step.id: len(step.depends_on) for step in plan.steps}
    dependents: dict[str, list[str]] = {step.id: [] for step in plan.steps}
    for step in plan.steps:
        for dep in step.depends_on:
            dependents[dep].append(step.id)

    batches: list[list[PlanStep]] = []
    current = [sid for sid, deg in in_degree.items() if deg == 0]

    while current:
        batch = [steps_by_id[sid] for sid in current]
        batches.append(batch)
        next_batch: list[str] = []
        for sid in current:
            for dependent in dependents[sid]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    next_batch.append(dependent)
        current = next_batch

    total_sorted = sum(len(batch) for batch in batches)
    if total_sorted != len(plan.steps):
        raise PlanValidationError("Topological sort failed: circular dependency.")

    return batches


def substitute_results(step: PlanStep, results: dict[str, dict]) -> PlanStep:
    """Replace ``{{step_id}}`` placeholders in a step's arguments with results.

    Only placeholders referring to completed steps (present in *results*) are
    substituted. Each completed result is JSON-serialized before insertion.
    Returns a new :class:`PlanStep` without mutating the original.

    Args:
        step: The step whose arguments should be updated.
        results: Mapping from step ID to the completed result dict.

    Returns:
        A new PlanStep with substituted arguments.
    """
    def _substitute(value: object) -> object:
        if isinstance(value, str):
            def _replace(match: re.Match) -> str:
                step_id = match.group(1)
                if step_id in results:
                    return json.dumps(results[step_id], ensure_ascii=False)
                return match.group(0)
            return _PLACEHOLDER_RE.sub(_replace, value)
        if isinstance(value, dict):
            return {k: _substitute(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_substitute(v) for v in value]
        return value

    new_args = _substitute(copy.deepcopy(step.args))
    if not isinstance(new_args, dict):
        new_args = {}

    return PlanStep(
        id=step.id,
        tool=step.tool,
        args=new_args,
        depends_on=list(step.depends_on),
        description=step.description,
    )


def _build_step_task(step: PlanStep) -> tuple[str, str]:
    """Build the task prompt and response format for a sub-agent step.

    Args:
        step: The plan step to translate into a sub-agent task.

    Returns:
        A tuple of (task_text, response_format).
    """
    response_format = "json"
    task = (
        f"Execute plan step '{step.id}'"
        f"{f' — {step.description}' if step.description else ''}.\n"
        f"Tool: {step.tool}\n"
        f"Arguments: {json.dumps(step.args, ensure_ascii=False)}\n\n"
        f"Run the tool exactly once with the arguments above, then finish. "
        f"Return ONLY a single valid JSON object with these keys: "
        f"success (bool), output (str), error (str), exit_code (int). "
        f"If the tool fails, also include error_type (str), recoverable (bool), "
        f"and suggestion (str) copied from the tool's failure report when present. "
        f"Do not add prose or markdown fences."
    )
    return task, response_format


def _standardize_sub_agent_result(result: str, response_format: str) -> dict:
    """Convert a sub-agent's final text into a standard outcome dict.

    Args:
        result: The raw text returned by the sub-agent.
        response_format: Expected format ('text', 'json', or 'file').

    Returns:
        A dict with at least ``success``, ``output``, ``error``, and
        ``exit_code`` keys.
    """
    if result == "[Cancelled]":
        return {
            "success": False,
            "output": "",
            "error": "Sub-agent was cancelled.",
            "exit_code": -1,
        }

    parsed = parse_json(result)
    if isinstance(parsed, dict):
        if "success" in parsed:
            outcome = dict(parsed)
            outcome.setdefault("output", "")
            outcome.setdefault("error", "")
            outcome.setdefault("exit_code", 0 if outcome.get("success") else -1)
            return outcome
        if response_format == "json":
            return {
                "success": True,
                "output": json.dumps(parsed, ensure_ascii=False),
                "error": "",
                "exit_code": 0,
                "result": parsed,
            }

    return {
        "success": True,
        "output": result,
        "error": "",
        "exit_code": 0,
    }


def _format_plan_result_message(plan: ExecutionPlan, result: dict) -> str:
    """Build a concise user message summarising a plan execution result."""
    success = result.get("success", False)
    status = "succeeded" if success else "failed"
    lines = [
        f"Execution plan '{plan.description}' {status}.",
        "Step results:",
    ]
    results_by_step = result.get("results", {})
    for step in plan.steps:
        outcome = results_by_step.get(step.id, {})
        icon = "✅" if outcome.get("success") else "❌"
        lines.append(f"  {icon} {step.id}: {step.tool} — {outcome.get('error') or 'done'}")
    for error in result.get("errors", []):
        lines.append(f"  ⚠️ {error}")
    return "\n".join(lines)


class PlanExecutor:
    """Execute an :class:`ExecutionPlan` by running each step in a sub-agent.

    Steps are grouped into topologically sorted batches and executed in parallel
    within each batch, up to *max_concurrent* sub-agents at once. The executor
    does not count plan steps against the parent agent's iteration budget.

    Args:
        max_concurrent: Maximum number of sub-agents to run in parallel.
        sub_agent_factory: Optional callable used to create a :class:`SubAgentRunner`
            for each step. If not provided, the executor attempts to use the
            built-in executor available on the :class:`ReactContext`.
    """

    def __init__(
        self,
        max_concurrent: int = 6,
        sub_agent_factory: Optional[Callable] = None,
    ):
        self.max_concurrent = max(1, max_concurrent)
        self._sub_agent_factory = sub_agent_factory
        self._error_registry = ErrorTypeRegistry()
        self._active_runners: dict[str, Any] = {}  # step_id -> runner

    def _get_factory(self, ctx: ReactContext) -> Optional[Callable]:
        """Return the best available sub-agent factory for this context."""
        if self._sub_agent_factory is not None:
            return self._sub_agent_factory
        builtin = getattr(ctx, "builtin_executor", None)
        if builtin is not None:
            return getattr(builtin, "_sub_agent_factory", None)
        return None

    def _create_runner(
        self,
        step: PlanStep,
        ctx: ReactContext,
        cancel_event: threading.Event,
        factory: Callable,
    ) -> tuple[Optional[Any], Optional[dict]]:
        """Create a sub-agent runner for *step*.

        Returns a tuple ``(runner, error_outcome)``. On success *error_outcome* is
        ``None``; on failure *runner* is ``None`` and *error_outcome* is a
        standard failure dict.
        """
        label = f"plan-{step.id}"
        try:
            payload = None
            working = getattr(ctx, "working", None)
            if working is not None:
                payload_text = getattr(working, "to_summary_text", lambda: "")()
                if payload_text:
                    payload = {"parent_working_summary": str(payload_text)[:800]}
            runner = factory(
                model=None,
                context_key=None,
                label=label,
                notify_fn=lambda _msg: None,
                fallback_models=None,
                max_iterations=_STEP_MAX_ITERATIONS,
                trace_id=ctx.trace_id,
                cancel_event=cancel_event,
                context_payload=payload,
                prompt_variant="sub-agent",
            )
            return runner, None
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to create sub-agent for step '%s'", step.id)
            error = {
                "success": False,
                "output": "",
                "error": f"Failed to create sub-agent: {exc}",
                "exit_code": -1,
            }
            return None, error

    @staticmethod
    def _execute_runner(runner: Any, task: str, response_format: str) -> dict:
        """Run a prepared sub-agent and standardise its result.

        Args:
            runner: Sub-agent runner instance with a ``run(task)`` method.
            task: The task text to pass to the runner.
            response_format: Expected response format (used to wrap JSON results).

        Returns:
            Standard outcome dict for the step.
        """
        try:
            result_text = runner.run(task)
            return _standardize_sub_agent_result(result_text, response_format)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sub-agent failed")
            return {
                "success": False,
                "output": "",
                "error": f"Sub-agent execution failed: {exc}",
                "exit_code": -1,
            }

    def _dependencies_satisfied(self, step: PlanStep, results: dict[str, dict]) -> bool:
        """Return True if all of *step*'s dependencies succeeded."""
        for dep in step.depends_on:
            outcome = results.get(dep)
            if outcome is None or not outcome.get("success", False):
                return False
        return True

    def _skip_step(self, step: PlanStep, reason: str) -> dict:
        """Return a failure outcome for a skipped step."""
        return {
            "success": False,
            "output": "",
            "error": f"Skipped: {reason}",
            "exit_code": -1,
            "error_type": "",
            "recoverable": False,
            "suggestion": "",
        }

    def _diagnose_step_failure(
        self,
        step: PlanStep,
        outcome: dict,
        ctx: ReactContext,
        factory: Callable,
        cancel_event: threading.Event,
    ) -> str:
        """Spawn a diagnostic sub-agent to analyse a failed step.

        Returns the diagnostic text, which should be fed back as a user message
        so the parent can re-plan.
        """
        error_type = outcome.get("error_type", "")
        error = outcome.get("error", "")
        tool = step.tool
        task = (
            f"Analyze why plan step '{step.id}' using tool '{tool}' failed with "
            f"error_type '{error_type}': {error}. Suggest an alternative approach."
        )
        try:
            runner = factory(
                model=None,
                context_key=None,
                label=f"diagnose-{step.id}",
                notify_fn=lambda _msg: None,
                fallback_models=None,
                max_iterations=_STEP_MAX_ITERATIONS,
                trace_id=ctx.trace_id,
                cancel_event=cancel_event,
            )
            result = runner.run(task)
            try:
                runner.close()
            except Exception:  # noqa: BLE001
                pass
            return str(result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Diagnostic sub-agent failed for step '%s'", step.id)
            return f"Diagnostic analysis unavailable: {exc}"

    def _execute_step_with_recovery(
        self,
        step: PlanStep,
        ctx: ReactContext,
        factory: Callable,
        cancel_event: threading.Event,
    ) -> dict:
        """Run a single plan step, retrying transient failures and diagnosing others.

        Returns the final outcome dict. The dict includes a ``retry_count`` key
        when retries were attempted.
        """
        error_registry = self._error_registry
        task, response_format = _build_step_task(step)
        retry_count = 0
        last_outcome: dict | None = None

        while True:
            if cancel_event.is_set():
                return self._skip_step(step, "cancelled before execution")
            runner, creation_error = self._create_runner(
                step, ctx, cancel_event, factory,
            )
            if runner is None:
                err = creation_error.get("error", "Sub-agent creation failed.") if creation_error else "Sub-agent creation failed."
                return {
                    "success": False,
                    "output": "",
                    "error": err,
                    "exit_code": -1,
                    "error_type": creation_error.get("error_type", "") if creation_error else "",
                    "recoverable": False,
                    "suggestion": creation_error.get("suggestion", "") if creation_error else "",
                }
            # Re-check after potentially-blocking runner creation. Cancellation
            # may have arrived while the factory was running; discard the runner.
            if cancel_event.is_set():
                try:
                    runner.cancel()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    runner.close()
                except Exception:  # noqa: BLE001
                    pass
                return self._skip_step(step, "cancelled during runner creation")
            self._active_runners[step.id] = runner
            outcome = self._execute_runner(runner, task, response_format)
            self._active_runners.pop(step.id, None)
            try:
                runner.close()
            except Exception:  # noqa: BLE001
                pass

            if outcome.get("success", False):
                if retry_count > 0:
                    outcome["retry_count"] = retry_count
                return outcome

            last_outcome = outcome
            error_type = outcome.get("error_type", "")
            recoverable = outcome.get("recoverable", False)
            info = error_registry.get(error_type)

            if not recoverable or info is None or retry_count >= info.max_retries:
                break

            retry_count += 1
            backoff = info.backoff_base * (2 ** (retry_count - 1))
            logger.info(
                "Step '%s' failed with recoverable error '%s'; retry %d/%d after %.1fs",
                step.id, error_type, retry_count, info.max_retries, backoff,
            )
            # Cancellable backoff: wake immediately if the plan is cancelled or
            # times out so we do not spawn another runner after cleanup begins.
            if cancel_event.wait(backoff):
                last_outcome["retry_count"] = retry_count
                return last_outcome

        # Retries exhausted or error is not recoverable — run diagnostics only
        # when a known Phase 3 error_type is present. Legacy failures without an
        # error_type skip diagnostics so existing tests that count runner calls
        # remain stable.
        if last_outcome is not None and last_outcome.get("error_type") not in ("", None):
            diagnosis = self._diagnose_step_failure(
                step, last_outcome, ctx, factory, cancel_event,
            )
            last_outcome["diagnosis"] = diagnosis
            last_outcome["retry_count"] = retry_count
        elif last_outcome is not None:
            last_outcome["retry_count"] = retry_count
        return last_outcome or {
            "success": False,
            "output": "",
            "error": "Step execution failed for an unknown reason.",
            "exit_code": -1,
            "error_type": "",
            "recoverable": False,
            "suggestion": "",
        }

    def execute(
        self,
        plan: ExecutionPlan,
        ctx: ReactContext,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Execute an :class:`ExecutionPlan` and return a result summary.

        Args:
            plan: The plan to execute.
            ctx: Parent :class:`ReactContext` used for dependency injection and
                trace propagation.
            progress_cb: Optional callback invoked with progress messages.

        Returns:
            A dict with keys ``success`` (bool), ``results`` (dict[str, dict]),
            and ``errors`` (list[str]). If the parent context has short-term
            memory, a summary message is also appended to it.
        """
        factory = self._get_factory(ctx)
        if factory is None:
            return {
                "success": False,
                "results": {},
                "errors": ["No sub-agent factory available."],
            }

        validate_plan(plan)
        batches = topological_sort(plan)
        results: dict[str, dict] = {}
        errors: list[str] = []
        cancel_event = threading.Event()
        start = time.monotonic()
        deadline = start + max(0, plan.timeout)

        # Bridge parent-agent cancellation into the plan's cancel_event so that
        # cancelling the main agent also stops plan execution and its sub-agents.
        parent_cancel = getattr(ctx, "cancel_event", None)
        # Track whether the cancellation came from the parent agent (vs. plan
        # timeout) so results and errors are classified correctly everywhere,
        # including for steps that were never submitted.
        cancelled_by_parent = False

        # Synchronous check: honour cancellation that was already set before
        # execute() was called.  The bridge thread only handles cancellations
        # that arrive *after* this point.
        if parent_cancel is not None and parent_cancel.is_set():
            cancel_event.set()
            cancelled_by_parent = True

        _bridge_stop = threading.Event()

        def _bridge_parent_cancel() -> None:
            """Set plan cancel_event when the parent agent is cancelled."""
            nonlocal cancelled_by_parent
            while not _bridge_stop.wait(0.1):
                if parent_cancel is not None and parent_cancel.is_set():
                    cancel_event.set()
                    cancelled_by_parent = True
                    break

        bridge_thread: Optional[threading.Thread] = None
        if parent_cancel is not None:
            bridge_thread = threading.Thread(
                target=_bridge_parent_cancel,
                daemon=True,
                name="plan-cancel-bridge",
            )
            bridge_thread.start()

        def _progress(msg: str) -> None:
            if progress_cb:
                try:
                    progress_cb(msg)
                except Exception:  # noqa: BLE001
                    pass
            logger.debug("Plan '%s': %s", plan.description, msg)

        _progress(f"Starting plan '{plan.description}' ({len(plan.steps)} steps in {len(batches)} batches)")

        for batch_index, batch in enumerate(batches, start=1):
            if cancel_event.is_set():
                break
            if time.monotonic() >= deadline:
                cancel_event.set()
                errors.append(f"Plan timeout exceeded ({plan.timeout}s).")
                break

            # Substitute completed results and skip steps with failed dependencies.
            ready_steps: list[PlanStep] = []
            for step in batch:
                if not self._dependencies_satisfied(step, results):
                    failed_dep = next(
                        dep for dep in step.depends_on
                        if not results.get(dep, {}).get("success", False)
                    )
                    results[step.id] = self._skip_step(
                        step, f"dependency '{failed_dep}' did not succeed"
                    )
                    errors.append(f"Step '{step.id}' skipped: dependency '{failed_dep}' failed.")
                    continue
                ready_steps.append(substitute_results(step, results))

            if not ready_steps:
                continue

            step_ids = ", ".join(s.id for s in ready_steps)
            _progress(f"Batch {batch_index}/{len(batches)}: {step_ids}")

            futures: dict[Future, str] = {}
            # Use a non-daemon pool so cancellation can finish without blocking
            # interpreter shutdown. The pool is shut down explicitly after the
            # batch to avoid leaking threads when runners ignore cancellation.
            pool = ThreadPoolExecutor(
                max_workers=min(self.max_concurrent, len(ready_steps)),
                thread_name_prefix="plan-step",
            )
            try:
                for step in ready_steps:
                    if cancel_event.is_set() or time.monotonic() >= deadline:
                        cancel_event.set()
                        break
                    future = pool.submit(
                        self._execute_step_with_recovery,
                        step, ctx, factory, cancel_event,
                    )
                    futures[future] = step.id

                done_futures: set[Future] = set()
                pending_futures: set[Future] = set(futures.keys())
                # Poll with short intervals so parent-cancel (set by the bridge
                # thread) wakes the executor promptly rather than waiting for
                # the full plan deadline to expire.
                while pending_futures and not cancel_event.is_set():
                    time_left = deadline - time.monotonic()
                    if time_left <= 0:
                        cancel_event.set()
                        errors.append(f"Plan timeout exceeded ({plan.timeout}s).")
                        break
                    done_slice, pending_futures = wait(
                        pending_futures,
                        timeout=min(_CANCEL_POLL_INTERVAL, time_left),
                    )
                    done_futures |= done_slice

                if pending_futures:
                    cancel_event.set()
                    # Request cooperative cancellation on still-running runners,
                    # then allow a brief grace period to take effect.
                    for sid in list(futures.values()):
                        runner = self._active_runners.get(sid)
                        if runner is not None and hasattr(runner, "cancel"):
                            try:
                                runner.cancel()
                            except Exception:  # noqa: BLE001
                                pass
                    done2, pending_futures = wait(
                        pending_futures, timeout=_CANCELLATION_GRACE_SECONDS,
                    )

                    for future in done2:
                        sid = futures[future]
                        self._active_runners.pop(sid, None)
                        if cancelled_by_parent:
                            results[sid] = {
                                "success": False,
                                "output": "",
                                "error": "Cancelled by parent agent (completed during grace period).",
                                "exit_code": -1,
                                "error_type": "",
                                "recoverable": False,
                                "suggestion": "",
                            }
                            errors.append(f"Step '{sid}' cancelled by parent agent.")
                        else:
                            # Completed during grace — deadline was already exceeded.
                            results[sid] = {
                                "success": False,
                                "output": "",
                                "error": "Plan timeout exceeded (completed during cancellation grace period).",
                                "exit_code": -1,
                                "error_type": "tool_timeout",
                                "recoverable": True,
                                "suggestion": "Retry the plan with a longer timeout.",
                            }
                            errors.append(
                                f"Step '{sid}' timed out (deadline exceeded; completed during grace period)."
                            )

                    for future in pending_futures:
                        sid = futures[future]
                        self._active_runners.pop(sid, None)
                        if cancelled_by_parent:
                            results[sid] = {
                                "success": False,
                                "output": "",
                                "error": (
                                    "Cancelled by parent agent; the step may still be "
                                    "running in the background."
                                ),
                                "exit_code": -1,
                                "error_type": "",
                                "recoverable": False,
                                "suggestion": "",
                            }
                            errors.append(
                                f"Step '{sid}' cancelled by parent agent "
                                f"(may still be running in the background)."
                            )
                        else:
                            # Cancellation is cooperative and cannot interrupt a tool
                            # that is already mid-execution (e.g. a running shell
                            # subprocess). Report this honestly rather than claiming
                            # the work was stopped.
                            results[sid] = {
                                "success": False,
                                "output": "",
                                "error": (
                                    "Plan timeout exceeded; cancellation was requested "
                                    "but the step may still be running in the background."
                                ),
                                "exit_code": -1,
                                "error_type": "tool_timeout",
                                "recoverable": True,
                                "suggestion": "Retry the plan with a longer timeout.",
                            }
                            errors.append(
                                f"Step '{sid}' timed out: cancellation requested "
                                f"(may still be running in the background)."
                            )

                for future in done_futures:
                    sid = futures[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Step '%s' future raised", sid)
                        outcome = {
                            "success": False,
                            "output": "",
                            "error": f"Execution error: {exc}",
                            "exit_code": -1,
                            "error_type": "",
                            "recoverable": False,
                            "suggestion": "",
                        }
                    results[sid] = outcome
                    diagnosis = outcome.get("diagnosis")
                    retry_count = outcome.get("retry_count", 0)
                    if diagnosis:
                        errors.append(f"Step '{sid}' diagnosis: {diagnosis}")
                    if not outcome.get("success", False):
                        err_msg = outcome.get("error", "unknown error")
                        if retry_count:
                            err_msg = f"{err_msg} (retried {retry_count} time(s))"
                        errors.append(f"Step '{sid}' failed: {err_msg}")
                    else:
                        if retry_count:
                            _progress(f"Step '{sid}' completed successfully after {retry_count} retry(s)")
                        else:
                            _progress(f"Step '{sid}' completed successfully")
            finally:
                # Cancel any runners that did not complete and shut down the
                # pool without waiting indefinitely. Pending futures that
                # ignore cancellation will be forcibly discarded.
                for sid, runner in list(self._active_runners.items()):
                    if hasattr(runner, "cancel"):
                        try:
                            runner.cancel()
                        except Exception:  # noqa: BLE001
                            pass
                self._active_runners.clear()
                for future in list(futures.keys()):
                    if not future.done():
                        future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)

            if cancel_event.is_set():
                break

        # Mark any remaining unexecuted steps as skipped/cancelled.
        for step in plan.steps:
            if step.id not in results:
                if cancelled_by_parent:
                    results[step.id] = {
                        "success": False,
                        "output": "",
                        "error": "Cancelled by parent agent before this step could be executed.",
                        "exit_code": -1,
                        "error_type": "",
                        "recoverable": False,
                        "suggestion": "",
                    }
                    errors.append(
                        f"Step '{step.id}' cancelled: parent agent cancelled before execution."
                    )
                else:
                    results[step.id] = self._skip_step(
                        step, "execution terminated before this step"
                    )

        success = (
            not errors
            and not cancel_event.is_set()
            and all(r.get("success", False) for r in results.values())
            and len(results) == len(plan.steps)
        )
        result = {"success": success, "results": results, "errors": errors}

        if ctx.short_term is not None:
            try:
                add_fn = getattr(ctx.short_term, "add", None)
                if callable(add_fn):
                    add_fn("user", _format_plan_result_message(plan, result))
            except Exception:  # noqa: BLE001
                logger.warning("Failed to append plan result to short-term memory")

        _progress(
            f"Plan '{plan.description}' finished: success={success}, "
            f"errors={len(errors)}, elapsed={time.monotonic() - start:.1f}s"
        )

        # Stop the parent-cancellation bridge (no-op if it already stopped).
        _bridge_stop.set()
        if bridge_thread is not None:
            bridge_thread.join(timeout=0.5)

        return result
