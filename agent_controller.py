"""
agent_controller.py
-------------------
Core ReAct-style agent loop.

Workflow for each user request:
  1. Semantic search for relevant tools
  2. Send goal + tools + memory context to LLM
  3. Parse LLM JSON response
  4. Dispatch action: tool | plan | finish
  5. Feed result back to LLM and repeat (max N iterations)
"""

from __future__ import annotations

import logging
import json
import math
import os
import threading
import time
import uuid
from typing import Callable, Optional

from agent_logging import bind_run_context
from agent_runtime import AgentRuntime, ControllerDeps
from config_schema import AgentConfig, ExecutorPaths
from confirmation import ConfirmationManager
from context_monitor import ContextMonitor
from conversation_io import _load_or_create_conversation_id, _save_conversation
from llm_client import LLMClient
from model_selection_guard import ConversationSelectionGuard, SelectionBudgetIdentity
from memory_store import MemoryStore, extract_tools_used, save_task_outcome
from prompt_loader import build_system_prompt as _build_system_prompt
from prompt_builder import estimate_tokens as _estimate_tokens
from react_loop import react_loop
from tool_index import ToolIndex
from trace_context import child_trace_id

logger = logging.getLogger(__name__)


def _is_checkpoint_primitive(value: object) -> bool:
    """Check that nested message data is JSON-safe and finite."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_checkpoint_primitive(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_checkpoint_primitive(item) for key, item in value.items())
    return False


def _valid_checkpoint_content(content: object, role: str) -> bool:
    """Validate supported text and multimodal content-block forms."""
    if isinstance(content, str):
        return True
    if not isinstance(content, list) or not content:
        return False
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            return False
        block_type = block["type"]
        if block_type in {"text", "input_text"}:
            if not isinstance(block.get("text"), str):
                return False
        elif block_type == "image_url":
            if role != "user":
                return False
            image_url = block.get("image_url")
            if not isinstance(image_url, dict) or not isinstance(image_url.get("url"), str):
                return False
        elif block_type in {"image", "input_image"}:
            if role != "user" or not _is_checkpoint_primitive(block):
                return False
            if block_type == "image":
                source = block.get("source")
                if (
                    not isinstance(source, dict)
                    or source.get("type") != "base64"
                    or not isinstance(source.get("media_type"), str)
                    or not isinstance(source.get("data"), str)
                ):
                    return False
            elif not isinstance(block.get("image_url"), (str, dict)):
                return False
        else:
            return False
        if not _is_checkpoint_primitive(block):
            return False
    return True


def _valid_checkpoint_messages(messages: object, goal_idx: int, user_goal: str) -> bool:
    """Validate message records without modifying legitimate stored history."""
    if not isinstance(messages, list) or not messages:
        return False
    pending_tool_calls: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or not _is_checkpoint_primitive(message):
            return False
        role = message.get("role")
        if not isinstance(role, str) or role not in {"system", "developer", "user", "assistant", "tool"}:
            return False
        if "images" in message and (
            role != "user"
            or
            not isinstance(message["images"], list)
            or not all(isinstance(path, str) for path in message["images"])
        ):
            return False
        calls = message.get("tool_calls")
        if calls is not None:
            if role != "assistant" or not isinstance(calls, list) or not calls:
                return False
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
                    return False
                if call.get("type") not in (None, "function"):
                    return False
                function = call.get("function")
                if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
                    return False
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        parsed = json.loads(arguments)
                    except (json.JSONDecodeError, TypeError):
                        return False
                    if not isinstance(parsed, (dict, list)):
                        return False
                elif not isinstance(arguments, (dict, list)):
                    return False
                if call["id"] in pending_tool_calls:
                    return False
                pending_tool_calls.add(call["id"])
        if role == "tool":
            if not isinstance(message.get("tool_call_id"), str) or not message["tool_call_id"]:
                return False
            if message["tool_call_id"] not in pending_tool_calls:
                return False
            pending_tool_calls.remove(message["tool_call_id"])
            if not isinstance(message.get("content"), str):
                return False
        elif "content" not in message:
            if not (role == "assistant" and calls):
                return False
        elif message["content"] is None:
            if not (role == "assistant" and calls):
                return False
        elif not _valid_checkpoint_content(message["content"], role):
            return False

    if type(goal_idx) is not int or not 0 <= goal_idx < len(messages):
        return False
    goal_message = messages[goal_idx]
    return (
        isinstance(goal_message, dict)
        and goal_message.get("role") == "user"
        and goal_message.get("content") == user_goal
        and not pending_tool_calls
    )


# ---------------------------------------------------------------------------
# Cancel-event registry — per-run private events for a shared-owner controller
# ---------------------------------------------------------------------------

class CancelEventRegistry:
    """Mints a private cancel_event per run and fans out cancel_all() to whoever
    is currently registered.

    A single MAIN AgentController instance is shared across every user's
    request (main.py constructs one; telegram_interface.py's per-user locks only
    serialize a *single* user's own messages, so two different users' runs can
    execute concurrently against the same controller). Handing every run the
    *same* threading.Event and clearing it at loop start doesn't work under that
    concurrency: whichever run starts next silently clears a cancellation a
    still-in-flight sibling run is relying on, and — the reverse failure mode —
    a stop meant for one run can end up killing every *new* run at step 0 for as
    long as any sibling stays active, since nothing ever gets a chance to see the
    event settle back to "not cancelled".

    Giving every run its own private event removes the sharing entirely: no
    run's clear() can affect another run's event, and no run can inherit a
    cancellation intended for someone else. cancel_all() fans the stop signal
    out to exactly the runs active *right now*, matching the operator-facing
    "stop everything currently running" semantics without leaking into runs
    that start afterward.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: set[threading.Event] = set()

    def new_run_event(self) -> threading.Event:
        """Create and register a fresh, unset event for a new run."""
        event = threading.Event()
        with self._lock:
            self._events.add(event)
        return event

    def release(self, event: threading.Event) -> None:
        """Unregister *event* once its run has finished."""
        with self._lock:
            self._events.discard(event)

    def cancel_all(self) -> None:
        """Set every event currently registered (i.e. every run in flight now)."""
        with self._lock:
            events = list(self._events)
        for event in events:
            event.set()


# ---------------------------------------------------------------------------
# Agent Controller
# ---------------------------------------------------------------------------

class AgentController:
    """
    Orchestrates the ReAct loop between the user, the LLM, and the tool system.
    """

    def __init__(
        self,
        agent_cfg: AgentConfig,
        paths: ExecutorPaths,
        *,
        llm: LLMClient,
        tool_index: ToolIndex,
        memory: MemoryStore,
        short_term=None,       # Optional[ShortTermMemory]
        working=None,          # Optional[WorkingMemory]
        results=None,          # Optional[ResultsMemory]
        builtin_executor=None, # Optional[BuiltinExecutor]
        skill_registry=None,   # Optional[SkillRegistry]
        mcp_manager=None,      # Optional[MCPManager]
        cancel_event: Optional[threading.Event] = None,
        depth: int = 0,
        label: str = "main",   # identifies this agent in log lines
        on_step=None,          # Optional[Callable[[int], None]] — called after each step
        on_tool_trace=None,    # Optional[Callable[[ToolTrace], None]] — called after each tool call
        trace_id=None,         # Optional[str] — propagate a parent run's trace; None => fresh per run
        checkpoint_store=None,  # Optional[CheckpointStore]
        context_monitor: ContextMonitor | None = None,
        step_notify_interval: int = 0,
    ):
        self._agent_cfg = agent_cfg
        self._paths = paths
        self.llm = llm
        self.tool_index = tool_index
        self.memory = memory
        self.short_term = short_term
        self.working = working
        self.results = results
        self.builtin_executor = builtin_executor
        self.skill_registry = skill_registry
        self.mcp_manager = mcp_manager
        self._cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self._owns_cancel_event = cancel_event is None
        # When this controller owns its cancellation (no external event was
        # supplied — the MAIN agent's case), run() mints each run its own
        # private event via this registry instead of reusing self._cancel_event,
        # so concurrent runs on this one shared controller can never race on a
        # single event's clear()/set() state. self._cancel_event remains as the
        # fallback used only by direct build_react_context() calls that bypass
        # run() (e.g. characterization tests) — see CancelEventRegistry.
        self._cancel_registry = CancelEventRegistry() if self._owns_cancel_event else None
        self.label = label
        self._on_step = on_step
        self._on_tool_trace = on_tool_trace
        self._depth = depth  # 0 = main agent, 1 = sub-agent (spawn_agent blocked at depth ≥ 1)
        # Optional parent trace to propagate; when None each run() gets a fresh ID.
        self._trace_id = trace_id
        # Sub-agent context sharing (set by SubAgentRunner only for sub-agents)
        self._context_payload: dict | None = None
        self._prompt_variant: str | None = None
        # Strategy memory (optional — set by main.py after init when enabled)
        self.strategy_memory: Optional[object] = None  # Set post-construction by main.py
        # Graph memory (optional — set by main.py after init when enabled)
        self._graph_memory = None          # Optional[GraphMemoryStore]
        self._graph_memory_writer = None   # Optional[GraphMemoryWriter]
        self._graph_memory_max_entries = 10
        # Step milestone notification interval (main agent only; 0 disables)
        self.step_notify_interval = step_notify_interval
        # ------------------------------------------------------------------
        # Cross-thread synchronisation for operator confirmation prompts.
        #
        # Pattern: the ReAct loop (run() method) executes on a worker thread
        # (via run_in_executor in telegram_interface._run_agent_task). When
        # a dangerous tool call or step-limit extension needs user approval,
        # ConfirmationManager creates a threading.Event keyed by a unique
        # token, sends a progress callback to request UI buttons, then
        # blocks on event.wait(). The Telegram callback handler (on the
        # asyncio event loop thread) calls the signal_* methods, which set
        # the result and unblock the worker thread.
        # ------------------------------------------------------------------
        self._confirmation = ConfirmationManager()
        self.checkpoint_store = checkpoint_store
        self._checkpoint_enabled = True
        self._retry_timeout_seconds = 120
        # Shared instance: the same ContextMonitor must be passed to both
        # AgentController and BuiltinExecutor so that the context_profile tool
        # reads the same monitor the ReAct loop publishes to. See ADR-0022.
        self.context_monitor = context_monitor or ContextMonitor()
        self._selection_guard = ConversationSelectionGuard()
        if short_term is not None and short_term.get_messages():
            # Injected history has unknown provenance: protect it until a request fits.
            self._selection_guard.notify_history_restored(None)

    # ------------------------------------------------------------------
    # AgentConfig / ExecutorPaths accessors
    #
    # The constructor accepts typed config objects rather than a long list of
    # flat kwargs. These properties keep the internal code readable by reading
    # from the stored config bundles on demand.
    # ------------------------------------------------------------------

    @property
    def max_iterations(self) -> int:
        return self._agent_cfg.max_iterations

    @property
    def top_tools(self) -> int:
        return self._agent_cfg.top_tools

    @property
    def ctx_max_tokens(self) -> int:
        return self._agent_cfg.ctx_max_tokens

    @property
    def creativity_mode(self) -> str:
        return self._agent_cfg.creativity_mode

    @property
    def plan_max_iterations(self) -> int:
        return self._agent_cfg.plan_max_iterations

    @property
    def inactivity_warn_minutes(self) -> int:
        return self._agent_cfg.inactivity_warn_minutes

    @property
    def tmp_dir(self) -> str:
        return self._paths.tmp_dir

    @property
    def downloads_dir(self) -> str:
        return self._paths.downloads_dir

    @property
    def workspace_dir(self) -> str:
        return self._paths.workspace_dir

    @property
    def log_file(self) -> str:
        return self._paths.log_file

    @property
    def log_backup_count(self) -> int:
        return self._paths.log_backup_count

    @property
    def runtime_deps(self) -> ControllerDeps:
        """Runtime dependency bundle used by AgentRuntime.build_react_context.

        Returned on demand so post-init wiring (graph memory, strategy memory,
        registry-installed ``_on_step``, etc.) is captured at run start exactly as
        the legacy inline assembly did.  Keeping the field set in one place
        eliminates three-way desync when ``ReactContext``/``ControllerDeps``
        gain a new shared dependency.
        """
        return ControllerDeps(
            llm=self.llm,
            tool_index=self.tool_index,
            memory=self.memory,
            builtin_executor=self.builtin_executor,
            mcp_manager=self.mcp_manager,
            skill_registry=self.skill_registry,
            max_iterations=self.max_iterations,
            top_tools=self.top_tools,
            ctx_max_tokens=self.ctx_max_tokens,
            tmp_dir=self.tmp_dir,
            downloads_dir=self.downloads_dir,
            workspace_dir=self.workspace_dir,
            log_file=self.log_file,
            log_backup_count=self.log_backup_count,
            depth=self._depth,
            label=self.label,
            short_term=self.short_term,
            working=self.working,
            results=self.results,
            cancel_event=self._cancel_event,
            owns_cancel_event=self._owns_cancel_event,
            on_step=self._on_step,
            on_tool_trace=self._on_tool_trace,
            graph_memory=self._graph_memory,
            graph_memory_writer=self._graph_memory_writer,
            graph_memory_max_entries=self._graph_memory_max_entries,
            strategy_memory=self.strategy_memory,
            max_subagents=(
                getattr(self.builtin_executor, "_max_subagents", 6)
                if self.builtin_executor is not None
                else 6
            ),
            creativity_mode=self.creativity_mode,
            plan_max_iterations=self.plan_max_iterations,
            inactivity_warn_minutes=self.inactivity_warn_minutes,
            confirmation=self._confirmation,
            checkpoint_store=self.checkpoint_store,
            checkpoint_enabled=self._checkpoint_enabled,
            retry_timeout_seconds=self._retry_timeout_seconds,
            max_retries=self._agent_cfg.max_retries,
            retry_delay=self._agent_cfg.retry_delay,
            context_monitor=self.context_monitor,
            step_notify_interval=self.step_notify_interval,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        user_goal: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        images: Optional[list[str]] = None,
        *,
        prompt_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        resume_from: Optional[str] = None,
        milestone_notify_fn: Optional[Callable[[int], None]] = None,
        admitted_llm: Optional[LLMClient] = None,
    ) -> str:
        """
        Process a user goal and return the final answer string.
        Optionally calls progress_callback(msg) for intermediate updates.
        Pass images=["/path/to/file.jpg", ...] to include images in the first
        user message for vision-capable models.

        ``trace_id`` may be supplied by the caller (e.g. TelegramInterface) to
        correlate the run with an externally managed prompt record. When omitted,
        a fresh child trace ID is minted.

        ``prompt_id`` is the operator-facing "Prompt #N"; it is propagated into
        the log context and onto the shared BuiltinExecutor for sub-agent
        tracking.

        ``admitted_llm`` is the already-admitted client this run uses for resume
        validation, trace binding, context and cleanup; it never replaces
        ``self.llm`` or retargets runs already in flight.
        """
        # Request-scoped trace ID: reuse a propagated parent trace when present,
        # otherwise mint a fresh one so each interactive run is correlatable.
        run_trace_id = trace_id if trace_id else child_trace_id(self._trace_id)

        checkpoint_owner = None
        if self.checkpoint_store is not None and (self._checkpoint_enabled or resume_from is not None):
            owner_trace = resume_from if resume_from is not None else run_trace_id
            try:
                checkpoint_owner = self.checkpoint_store.acquire_owner(owner_trace)
            except (ValueError, RuntimeError):
                return "❌ Checkpoint ownership could not be acquired for this run."
            if checkpoint_owner is None:
                return "❌ A run for this checkpoint trace is already active. Try again after it finishes."

        try:
            return self._run_with_checkpoint_owner(
                user_goal,
                progress_callback,
                images,
                prompt_id=prompt_id,
                run_trace_id=run_trace_id,
                resume_from=resume_from,
                milestone_notify_fn=milestone_notify_fn,
                checkpoint_owner=checkpoint_owner,
                admitted_llm=admitted_llm,
            )
        finally:
            if checkpoint_owner is not None:
                checkpoint_owner.release()

    def _run_with_checkpoint_owner(
        self,
        user_goal: str,
        progress_callback: Optional[Callable[[str], None]],
        images: Optional[list[str]],
        *,
        prompt_id: Optional[str],
        run_trace_id: str,
        resume_from: Optional[str],
        milestone_notify_fn: Optional[Callable[[int], None]],
        checkpoint_owner: object,
        admitted_llm: Optional[LLMClient] = None,
    ) -> str:
        """Execute a run while ``run`` owns the checkpoint trace lease."""
        run_llm = admitted_llm if admitted_llm is not None else self.llm
        # Load a saved checkpoint when resuming after an LLM error.
        # This must happen BEFORE build_react_context so the checkpoint's
        # trace_id is used for the context, LLM trace binding, and log correlation.
        initial_state = None
        if resume_from is not None:
            checkpoint = (
                self.checkpoint_store.load(resume_from, owner=checkpoint_owner)
                if self.checkpoint_store else None
            )
            try:
                from call_attempt_state import CallAttemptState
                from react_loop import _LoopState
                if (
                    not isinstance(checkpoint, dict)
                    or type(checkpoint.get("format_version")) is not int
                    or checkpoint.get("format_version") != 2
                ):
                    raise ValueError("checkpoint is legacy, missing, or has an unsupported format")
                required = {
                    "format_version", "trace_id", "user_goal", "messages", "step",
                    "goal_idx", "max_steps", "json_fail_streak", "model_id",
                    "attempt_state", "error_info", "created_at",
                }
                if set(checkpoint) != required:
                    raise ValueError("checkpoint fields are incomplete or unknown")
                model_id = run_llm.llm_cfg.get("id")
                if not model_id or checkpoint["model_id"] != model_id:
                    raise ValueError("checkpoint model does not match this pinned run model")
                if not isinstance(checkpoint["error_info"], dict):
                    raise ValueError("checkpoint history or error information is malformed")
                if set(checkpoint["error_info"]) != {"type", "message", "retryable", "detail"}:
                    raise ValueError("checkpoint error information is incomplete or unknown")
                if (
                    not isinstance(checkpoint["trace_id"], str)
                    or not isinstance(checkpoint["user_goal"], str)
                    or not isinstance(checkpoint["model_id"], str)
                    or not isinstance(checkpoint["created_at"], str)
                ):
                    raise ValueError("checkpoint identity or timestamp is malformed")
                if checkpoint["trace_id"] != resume_from:
                    raise ValueError("checkpoint trace does not match its requested identity")
                error_info = checkpoint["error_info"]
                if (
                    not isinstance(error_info["type"], str)
                    or error_info["type"] not in {"timeout", "connection", "rate_limit", "empty", "context", "permanent", "unknown"}
                    or not isinstance(error_info["message"], str)
                    or not isinstance(error_info["retryable"], bool)
                    or not isinstance(error_info["detail"], str)
                ):
                    raise ValueError("checkpoint error information is malformed")
                for key in ("step", "goal_idx", "max_steps", "json_fail_streak"):
                    if type(checkpoint[key]) is not int or checkpoint[key] < 0:
                        raise ValueError(f"checkpoint {key} is malformed")
                if checkpoint["max_steps"] == 0 or not _valid_checkpoint_messages(
                    checkpoint["messages"], checkpoint["goal_idx"], checkpoint["user_goal"]
                ):
                    raise ValueError("checkpoint history or goal position is malformed")
                attempt_state = CallAttemptState.from_dict(checkpoint["attempt_state"])
                if attempt_state.outbound_attempts and not attempt_state.manual_retry_authorized:
                    attempt_state.authorize_manual_retry()
                initial_state = _LoopState(
                    messages=checkpoint["messages"], goal_idx=checkpoint["goal_idx"],
                    max_steps=checkpoint["max_steps"], step=checkpoint["step"],
                    json_fail_streak=checkpoint["json_fail_streak"],
                    attempt_state=attempt_state,
                )
                run_trace_id = checkpoint["trace_id"]
                user_goal = checkpoint["user_goal"]
            except (ValueError, TypeError, KeyError) as exc:
                return f"❌ Cannot resume checkpoint: {exc}. Start a new task instead."

        try:
            _prev_trace = getattr(run_llm, "_trace_id", "")
            if hasattr(run_llm, "set_trace_id"):
                run_llm.set_trace_id(run_trace_id)

        # Bind run identity into structlog context so every log line carries
        # trace/agent/prompt_id.
            bind_run_context(trace=run_trace_id, agent=self.label, prompt_id=str(prompt_id) if prompt_id is not None else "")

            identity_fn = getattr(run_llm, "selection_identity", None)
            ticket = (
                self._selection_guard.begin(identity_fn())
                if callable(identity_fn) else None
            )
            guarded = ticket is not None and ticket.guarded

            def install_run_scope() -> None:
                if self.builtin_executor is not None and self._depth == 0:
                    self.builtin_executor._coordinator = self._confirmation
                    self.builtin_executor._current_prompt_id = prompt_id
                    self._confirmation.grant_ledger.clear_prompt_scope(None)

            if not guarded:
                install_run_scope()
        # ReactContext assembly is owned by the runtime (ADR-0007). This frontend
        # keeps only per-run concerns: trace minting (above), checkpoint
        # restoration, and progress/image passthrough. The LLM client is pinned.
        #
        # Cancellation: when this controller owns its cancel_event (no external
        # event was supplied), mint a run-private event via the registry instead
        # of reusing self._cancel_event across concurrent runs — see
        # CancelEventRegistry for the race this avoids. Controllers with an
        # externally-supplied event (sub-agents forwarding a parent's stop
        # signal) pass cancel_event=None here and keep using that shared event
        # unchanged, since that sharing is intentional cascade-cancel behavior.
            run_cancel_event = (
                self._cancel_registry.new_run_event()
                if self._cancel_registry is not None
                else None
            )
            ctx = AgentRuntime.build_react_context(self, run_trace_id, cancel_event=run_cancel_event)
            if run_llm is not self.llm:
                ctx.llm = run_llm
            if ticket is not None:
                ctx.on_accepted = lambda: self._selection_guard.complete(ticket)
            if guarded:
                ctx.guarded_first_request = True
                ctx.activated = False
                ctx.on_activate = install_run_scope
            ctx.checkpoint_owner = checkpoint_owner
            ctx.prompt_id = prompt_id
            ctx.milestone_notify_fn = milestone_notify_fn
        except Exception:
            raise

        loop_kwargs: dict = {}
        if initial_state is not None:
            loop_kwargs["initial_state"] = initial_state
        try:
            return react_loop(ctx, user_goal, progress_callback, images, **loop_kwargs)
        finally:
            if hasattr(run_llm, "set_trace_id"):
                run_llm.set_trace_id(_prev_trace)
            if self.builtin_executor is not None and self._depth == 0 and ctx.activated:
                # Idempotent fallback only: react_loop's finally is the canonical
                # turn drain (with progress). Repeating it here is a no-op once
                # the children are released and covers a loop that exited before
                # reaching its own drain. Ordering is load-bearing: the
                # coordinator, prompt id and staged tokens are cleared only
                # after the drain, so confirmation callbacks stay live during it.
                try:
                    drain_errors = (
                        self.builtin_executor._supervisor.drain_owned_children(
                            ctx.turn_id, cancel=True
                        )
                    )
                    for drain_error in drain_errors or []:
                        logger.error(
                            "sub-agent lifecycle cleanup failed: %s", drain_error
                        )
                except Exception:  # noqa: BLE001
                    logger.error("sub-agent turn drain failed", exc_info=True)
                finally:
                    self.builtin_executor._coordinator = None
                    self.builtin_executor._current_prompt_id = None
                    # Run-scoped cleanup: abandoned staged confirmation tokens from this
                    # run must not leak into the next interactive run.
                    self.builtin_executor._pending_confirmations.reset()
            if self._cancel_registry is not None and run_cancel_event is not None:
                self._cancel_registry.release(run_cancel_event)


    def notify_manual_selection(self, identity: SelectionBudgetIdentity) -> None:
        """Record a persisted manual model selection for the conversation guard."""
        self._selection_guard.notify_manual_selection(identity)

    def notify_history_restored(
        self, selection: Optional[SelectionBudgetIdentity] = None
    ) -> None:
        """Record that conversation history was restored from an earlier selection."""
        history = self.short_term.get_messages() if self.short_term is not None else []
        self._selection_guard.notify_history_restored(selection, empty=not history)

    @property
    def selection_guard_armed(self) -> bool:
        """Whether the next first request is previewed before any run effect."""
        return self._selection_guard.armed

    def cancel(self) -> None:
        """Cancel the currently-running task(s). Safe to call from any thread.

        When this controller owns its cancellation, fans the stop signal out to
        every run currently in flight (see CancelEventRegistry) rather than
        setting one shared event — so the stop reaches everything running now
        without also poisoning runs that start afterward.
        """
        if self._cancel_registry is not None:
            self._cancel_registry.cancel_all()
        else:
            self._cancel_event.set()
        logger.info("Cancel requested by operator label=%s", self.label)

    def resume(self, token: str, confirmed: bool) -> None:
        """Called by TelegramInterface when user responds to a file_write/shell confirmation."""
        self._confirmation.signal_confirmation(token, confirmed)

    def resume_llm_error(self, token: str, response: str) -> None:
        """Called by TelegramInterface when user responds to an LLM error retry prompt.

        response: "retry" | "cancel"
        """
        self._confirmation.signal_retry(token, response)

    def build_system_prompt(self, user_goal: str = "(context snapshot)") -> tuple[str, int]:
        """Build the full system prompt as it would be sent to the LLM.

        Returns (prompt_text, estimated_tokens).
        """
        return _build_system_prompt(
            tool_index=self.tool_index,
            memory=self.memory,
            results=self.results,
            skill_registry=self.skill_registry,
            llm=self.llm,
            tmp_dir=self.tmp_dir,
            downloads_dir=self.downloads_dir,
            workspace_dir=self.workspace_dir,
            log_file=self.log_file,
            log_backup_count=self.log_backup_count,
            top_tools=self.top_tools,
            user_goal=user_goal,
            parent_context_section="",
        )

    def reset_task(self, save: bool = True) -> str:
        """Save (optionally) and clear the current working + short-term context."""
        msg = "✅ Context cleared."
        if save and self.working and self.working.has_content():
            working_text = self.working.to_summary_text()
            try:
                summary = self.llm.chat(
                    [{"role": "user", "content": f"Summarize this task concisely in 2-3 sentences:\n\n{working_text}"}]
                )
            except Exception:
                summary = working_text[:300]
            if self.results:
                tools_used = list(filter(None, extract_tools_used(self.working.steps)))
                save_task_outcome(
                    results=self.results,
                    graph_memory_writer=self._graph_memory_writer,
                    goal=self.working.goal,
                    summary=summary,
                    tools_used=tools_used,
                )
            msg = "✅ Task saved to results memory. Starting fresh context."
        # Conversation persistence: save the current conversation and rotate to a
        # new conversation id so the next task starts with a clean context file.
        old_id = ""
        if self.builtin_executor:
            old_id = self.builtin_executor.conversation_id
        state_dir = getattr(self, "_conversation_state_dir", "")
        if save and old_id and state_dir:
            try:
                conv_path = os.path.join(state_dir, "conversations", old_id + ".json")
                if self.short_term:
                    _save_conversation(conv_path, self.short_term)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to save conversation on reset", exc_info=True)
        if state_dir and self.builtin_executor:
            try:
                new_id = _load_or_create_conversation_id(state_dir, force_new=True)
                self.builtin_executor.conversation_id = new_id
            except Exception:  # noqa: BLE001
                logger.warning("Failed to rotate conversation_id", exc_info=True)
        if self.working:
            self.working.clear()
        if self.short_term:
            self.short_term.clear()
        # /reset clears both prompt and session grants (session lifetime boundary).
        self._confirmation.grant_ledger.clear_all()
        self._selection_guard.reset()
        return msg

    def compress_context(self) -> str:
        """Summarise the short-term conversation history in place.

        Uses the LLM to condense all current messages into a single compact
        summary entry so the next agent run starts with a smaller context.
        Unlike reset_task(), working memory is not cleared and nothing is
        discarded — only the short_term ring buffer is replaced.

        Returns a human-readable status string with token estimates.
        """
        if not self.short_term:
            return "ℹ️ No short-term memory available."

        messages = self.short_term.get_messages()
        if len(messages) < 2:
            return "ℹ️ Context is already minimal — nothing to compress."

        def _msg_text(m: dict) -> str:
            """Extract plain text from a message, handling multimodal list content."""
            content = m.get("content", "")
            if isinstance(content, list):
                return " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            return str(content)

        before_tokens = _estimate_tokens(
            "\n".join(f"[{m['role']}]: {_msg_text(m)}" for m in messages)
        )

        history_text = "\n".join(
            f"[{m['role']}]: {_msg_text(m)[:600]}" for m in messages
        )
        try:
            summary = self.llm.chat([{
                "role": "user",
                "content": (
                    "Summarize this conversation history as concise bullet points. "
                    "Preserve key facts, decisions, tool names, outcomes, and any "
                    "unresolved items. Omit pleasantries and repetition:\n\n"
                    + history_text
                ),
            }])
        except Exception as exc:
            # Deterministic fallback: the LLM summarization call failed, but a user
            # invoking /compress (often because the context is over budget) still
            # needs relief. Replace the buffer with a bounded head+tail slice of the
            # history so the next run starts smaller, matching the robustness of
            # context_manager.maybe_compact()'s _fallback_summary path.
            from context_manager import _truncate_head_tail
            logger.error(
                "compress_context: LLM call failed, applying deterministic fallback: %s",
                exc,
            )
            fallback = _truncate_head_tail(history_text, 3000, 1000)
            self.short_term.clear()
            self.short_term.add(
                "assistant",
                f"[Compressed context summary — deterministic fallback]\n{fallback}",
            )
            return self._compression_report(
                before_tokens,
                _estimate_tokens(fallback),
                len(messages),
                fallback=True,
                reason=str(exc),
            )

        self.short_term.clear()
        self.short_term.add("assistant", f"[Compressed context summary]\n{summary}")

        return self._compression_report(
            before_tokens,
            _estimate_tokens(summary),
            len(messages),
            fallback=False,
        )

    def _compression_report(
        self,
        before: int,
        after: int,
        n_messages: int,
        *,
        fallback: bool,
        reason: str = "",
    ) -> str:
        """Build and log a human-readable compression report, then return it."""
        saved = max(0, before - after)
        pct = int(saved / before * 100) if before else 0
        logger.info(
            "compress_context%s: %d → ~%d tokens (%d%% reduction, %d messages → 1)",
            " (fallback)" if fallback else "",
            before, after, pct, n_messages,
        )
        if fallback:
            return (
                f"⚠️ LLM compression unavailable — applied deterministic truncation.\n"
                f"  Messages: {n_messages} → 1\n"
                f"  Tokens: ~{before:,} → ~{after:,} "
                f"(−{saved:,}, {pct}% smaller)\n"
                f"  Reason: {reason}"
            )
        return (
            f"✅ Context compressed.\n"
            f"  Messages: {n_messages} → 1\n"
            f"  Tokens: ~{before:,} → ~{after:,} "
            f"(−{saved:,}, {pct}% smaller)"
        )

    def list_models(self) -> list[dict]:
        """Return all configured models as a list of dicts."""
        try:
            return list(self.llm._models)
        except AttributeError:
            return []

# ---------------------------------------------------------------------------
# SubAgentRunner
# ---------------------------------------------------------------------------

class SubAgentRunner:
    """
    An isolated agent instance for background task execution.

    Each runner has its own LLMClient, ShortTermMemory, and WorkingMemory.
    It shares ToolIndex, BuiltinExecutor, SkillRegistry, and ResultsMemory
    with the main agent.

    Results are delivered via notify_fn (Telegram). Sub-agent output is NOT
    auto-persisted into semantic memory (P2 consolidation); to remember a fact,
    the operator/main agent must explicitly call memory_graph_store (confirmed).
    """

    def __init__(
        self,
        *,
        model_cfg: dict,              # single [[models]] entry dict
        config: dict,                 # full agent config (for LLMClient)
        agent_cfg: AgentConfig,        # typed agent config (max iterations, top tools, etc.)
        paths: ExecutorPaths,          # runtime filesystem paths bundle
        tool_index,                   # ToolIndex (shared)
        base_memory,                  # MemoryStore (shared long-term facts)
        builtin_executor,             # BuiltinExecutor (shared)
        skill_registry=None,          # SkillRegistry (shared)
        mcp_manager=None,             # MCPManager (shared, optional)
        results=None,                 # ResultsMemory (shared)
        short_term=None,              # ShortTermMemory — pre-loaded context (optional)
        notify_fn=None,               # Callable[[str], None]
        context_key: Optional[str] = None,  # for context persistence
        label: str = "on-demand",     # label for logging/display
        usage_registry=None,          # TokenUsageRegistry
        depth: int = 1,
        on_step=None,                 # Optional[Callable[[int], None]] — for iteration tracking
        on_tool_trace=None,           # Optional[Callable[[ToolTrace], None]] — for trace collection
        cancel_event=None,            # Optional[threading.Event] — shared cancel signal (e.g. forwarded from a parent agent stop)
        trace_id=None,                # Optional[str] — parent run trace to share; None => fresh per run
        context_payload: dict | None = None,  # Optional parent context dict to inject in system prompt
        prompt_variant: str | None = None,    # None => default system prompts; "sub-agent" => sub-agent prompts
    ):
        from memory_store import ShortTermMemory, WorkingMemory

        self.agent_id = "sa-" + uuid.uuid4().hex[:6]
        self.label = label
        self.context_key = context_key
        self.context_payload = context_payload
        self.prompt_variant = prompt_variant
        self.notify_fn = notify_fn or (lambda msg: None)
        self._log = logging.getLogger("agent").getChild(self.agent_id)

        # Build a sub-config that uses the overridden model as default
        sub_config = dict(config)
        # Place the chosen model first so it becomes default
        other_models = [m for m in config.get("models", []) if m.get("model") != model_cfg.get("model")]
        sub_config["models"] = [model_cfg] + other_models
        agent_section = dict(config.get("agent", {}))
        agent_section["default_model"] = model_cfg.get("model", "")
        sub_config["agent"] = agent_section

        self._cancel_event = cancel_event if cancel_event is not None else threading.Event()

        # Own LLM instance with model override + shared token registry + cancellation
        self._llm = LLMClient(sub_config, usage_registry=usage_registry,
                              cancel_event=self._cancel_event,
                              caller_tag=self.agent_id)

        # Own blank memory (working context for this task)
        self._short_term = short_term if short_term is not None else ShortTermMemory()
        self._working = WorkingMemory()

        # Note: strategy_memory is intentionally not forwarded to sub-agents (short-lived runs).
        # Build the isolated AgentController — headless (no confirm callbacks)
        self._agent = AgentController(
            agent_cfg=agent_cfg,
            paths=paths,
            llm=self._llm,
            tool_index=tool_index,
            memory=base_memory,
            short_term=self._short_term,
            working=self._working,
            results=results,
            builtin_executor=builtin_executor,
            skill_registry=skill_registry,
            mcp_manager=mcp_manager,
            cancel_event=self._cancel_event,
            depth=depth,
            label=self.agent_id,
            on_step=on_step,
            on_tool_trace=on_tool_trace,
            trace_id=trace_id,
        )
        # Propagate sub-agent context sharing settings into the inner controller
        # so react_loop can inject the parent context and select the right prompt
        # variant when building the system prompt.
        self._agent._context_payload = context_payload  # type: ignore[attr-defined]
        self._agent._prompt_variant = prompt_variant    # type: ignore[attr-defined]

        self._model_id = model_cfg.get("model", "unknown")

    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        """The model identifier used by this runner."""
        return self._model_id

    @property
    def short_term(self):
        """The runner's short-term memory/context object."""
        return self._short_term

    @property
    def trace_id(self) -> Optional[str]:
        """Parent run trace id, or None."""
        return getattr(self._agent, "_trace_id", None)

    def cancel(self) -> None:
        """Signal cooperative cancellation. Takes effect between iterations."""
        self._cancel_event.set()

    def close(self) -> None:
        """Release the sub-agent's LLM HTTP resources. Idempotent; safe to call once after run()."""
        try:
            self._llm.close()
        except Exception:
            pass

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self, task: str, prompt_id: Optional[str] = None) -> str:
        """
        Run the sub-agent synchronously in the calling thread.
        Returns the final result string (or an error/cancellation message).
        Should be called from a background thread.
        """
        start = time.time()
        self._log.info(
            "Starting (model: %s, context_key: %s)",
            self._model_id,
            self.context_key or "none",
        )
        # Save primary model index; restore after run() so next job starts fresh.
        # chat_with_fallback() reverts vision switches in its own finally, but
        # this belt-and-suspenders restore guarantees the next scheduled job
        # always starts on the configured primary model.
        _primary_idx = self._llm._active_idx
        try:
            result = self._agent.run(task, prompt_id=prompt_id)
            elapsed = time.time() - start
            self._log.info("Done in %.1fs | model: %s", elapsed, self._model_id)
            return result
        except Exception as exc:
            elapsed = time.time() - start
            self._log.error(
                "Failed after %.1fs: %s", elapsed, exc, exc_info=True
            )
            raise
        finally:
            self._llm._active_idx = _primary_idx  # reset to primary for next job
