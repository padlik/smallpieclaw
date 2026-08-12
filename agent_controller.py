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
import os
import threading
import time
import uuid
from typing import Callable, Optional

from agent_logging import bind_run_context
from agent_runtime import AgentRuntime, ControllerDeps
from confirmation import ConfirmationManager
from conversation_io import _load_or_create_conversation_id, _save_conversation
from llm_client import LLMClient
from memory_store import MemoryStore, extract_tools_used, save_task_outcome
from prompt_loader import build_system_prompt as _build_system_prompt
from prompt_builder import estimate_tokens as _estimate_tokens
from react_loop import react_loop
from tool_index import ToolIndex
from trace_context import child_trace_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent Controller
# ---------------------------------------------------------------------------

class AgentController:
    """
    Orchestrates the ReAct loop between the user, the LLM, and the tool system.
    """

    def __init__(
        self,
        llm: LLMClient,
        tool_index: ToolIndex,
        memory: MemoryStore,
        max_iterations: int = 8,
        top_tools: int = 3,
        ctx_max_tokens: int = 90_000,
        short_term=None,       # Optional[ShortTermMemory]
        working=None,          # Optional[WorkingMemory]
        results=None,          # Optional[ResultsMemory]
        builtin_executor=None, # Optional[BuiltinExecutor]
        skill_registry=None,   # Optional[SkillRegistry]
        mcp_manager=None,      # Optional[MCPManager]
        tmp_dir: str = "/tmp/agent",
        downloads_dir: str = "downloads",
        workspace_dir: str = "~/Documents",
        log_file: str = "agent.log",
        log_backup_count: int = 30,
        cancel_event: Optional[threading.Event] = None,
        depth: int = 0,
        label: str = "main",   # identifies this agent in log lines
        on_step=None,          # Optional[Callable[[int], None]] — called after each step
        on_tool_trace=None,    # Optional[Callable[[ToolTrace], None]] — called after each tool call
        job_history_fn=None,   # Optional[Callable[[], str]] — returns job execution history for prompt
        trace_id=None,         # Optional[str] — propagate a parent run's trace; None => fresh per run
        creativity_mode: str = "default",
        plan_max_iterations: int = 50,
        inactivity_warn_minutes: int = 15,
    ):
        self.llm = llm
        self.tool_index = tool_index
        self.memory = memory
        self.max_iterations = max_iterations
        self.top_tools = top_tools
        self.ctx_max_tokens = ctx_max_tokens
        self.short_term = short_term
        self.working = working
        self.results = results
        self.builtin_executor = builtin_executor
        self.skill_registry = skill_registry
        self.mcp_manager = mcp_manager
        self.tmp_dir = tmp_dir
        self.downloads_dir = downloads_dir
        self.workspace_dir = workspace_dir
        self.log_file = log_file
        self.log_backup_count = log_backup_count
        self._cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self._owns_cancel_event = cancel_event is None
        self.label = label
        self._on_step = on_step
        self._on_tool_trace = on_tool_trace
        self._job_history_fn = job_history_fn
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
        # Creativity / inactivity settings passed through to react_loop
        self.creativity_mode = creativity_mode
        self.plan_max_iterations = plan_max_iterations
        self.inactivity_warn_minutes = inactivity_warn_minutes

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
            job_history_fn=self._job_history_fn,
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
            trusted_zone_checker=(
                self.builtin_executor.trusted_zone_checker
                if self.builtin_executor is not None
                else None
            ),
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
    ) -> str:
        """
        Process a user goal and return the final answer string.
        Optionally calls progress_callback(msg) for intermediate updates.
        Pass images=["/path/to/file.jpg", ...] to include images in the first
        user message for vision-capable models.

        The primary model index is always restored on exit so transient
        fallbacks during one run never permanently demote subsequent requests.

        ``trace_id`` may be supplied by the caller (e.g. TelegramInterface) to
        correlate the run with an externally managed prompt record. When omitted,
        a fresh child trace ID is minted.

        ``prompt_id`` is the operator-facing "Prompt #N"; it is propagated into
        the log context and onto the shared BuiltinExecutor for sub-agent
        tracking.
        """
        # Save primary model index — mirrors the same pattern in SubAgentRunner.run().
        # chat_with_fallback() intentionally leaves _active_idx on the working model so
        # the rest of a run re-uses it; but we must restore it afterward so the *next*
        # interactive request always starts on the configured primary model.
        _primary_idx = self.llm._active_idx

        # Request-scoped trace ID: reuse a propagated parent trace when present,
        # otherwise mint a fresh one so each interactive run is correlatable.
        run_trace_id = trace_id if trace_id else child_trace_id(self._trace_id)
        _prev_trace = getattr(self.llm, "_trace_id", "")
        if hasattr(self.llm, "set_trace_id"):
            self.llm.set_trace_id(run_trace_id)

        # Bind run identity into structlog context so every log line carries
        # trace/agent/prompt_id.
        bind_run_context(trace=run_trace_id, agent=self.label, prompt_id=str(prompt_id) if prompt_id is not None else "")

        # Per-prompt shared approval set: sub-agents use the same set object as the
        # main agent for one-prompt approve-all semantics.
        if self.builtin_executor is not None and self._depth == 0:
            self.builtin_executor._prompt_approval_set = self._confirmation.auto_approve_tools
            self.builtin_executor._current_prompt_id = prompt_id
            # Reset the default tracker so stale grants from a prior run (or
            # from a Telegram callback that fell back to the default) don't
            # accumulate across runs.
            self.builtin_executor._default_grant_tracker.reset()
        # ReactContext assembly is owned by the runtime (ADR-0007). This frontend
        # keeps only the per-run concerns: trace minting (above), model
        # _active_idx save/restore (below), and progress/image passthrough.
        ctx = AgentRuntime.build_react_context(self, run_trace_id)
        try:
            if self.builtin_executor is not None:
                from builtin_tools.access_control import GrantTracker  # noqa: PLC0415
                with self.builtin_executor.use_grant_tracker(GrantTracker()):
                    return react_loop(ctx, user_goal, progress_callback, images)
            return react_loop(ctx, user_goal, progress_callback, images)
        finally:
            self.llm._active_idx = _primary_idx
            if hasattr(self.llm, "set_trace_id"):
                self.llm.set_trace_id(_prev_trace)
            if self.builtin_executor is not None and self._depth == 0:
                self.builtin_executor._prompt_approval_set = None
                self.builtin_executor._current_prompt_id = None
            self._confirmation.clear_auto_approve()


    def cancel(self) -> None:
        """Cancel the currently-running task. Safe to call from any thread."""
        self._cancel_event.set()
        logger.info("Cancel requested by operator label=%s", self.label)

    def resume(self, token: str, confirmed: bool) -> None:
        """Called by TelegramInterface when user responds to a file_write/shell confirmation."""
        self._confirmation.signal_confirmation(token, confirmed)

    def resume_extend(self, token: str, response: str) -> None:
        """Called by TelegramInterface when user responds to a max-steps extension prompt.

        response: "yes" | "no" | "unlimited"
        """
        self._confirmation.signal_extension(token, response)

    def resume_approve_all(self, token: str, tool_name: str) -> None:
        """Called by TelegramInterface when user presses 'Approve all {tool_name}'.

        Confirms the current pending operation AND registers tool_name for
        automatic approval for the rest of this task.
        """
        self._confirmation.signal_approve_all(token, tool_name)

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
        self._confirmation.clear_auto_approve()
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
        max_iterations: int = 8,
        top_tools: int = 3,
        ctx_max_tokens: int = 90_000,
        tmp_dir: str = "/tmp/agent",
        downloads_dir: str = "downloads",
        workspace_dir: str = "~/Documents",
        usage_registry=None,          # TokenUsageRegistry
        depth: int = 1,
        fallback_models: list[str] | None = None,  # None = inherit from parent config
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
        # fallback_models=None means inherit from sub_config (which inherited from parent config)
        self._llm = LLMClient(sub_config, usage_registry=usage_registry,
                              cancel_event=self._cancel_event,
                              fallback_models=fallback_models,
                              caller_tag=self.agent_id)

        # Own blank memory (working context for this task)
        self._short_term = short_term if short_term is not None else ShortTermMemory()
        self._working = WorkingMemory()

        # Note: strategy_memory is intentionally not forwarded to sub-agents (short-lived runs).
        # Build the isolated AgentController — headless (no confirm callbacks)
        self._agent = AgentController(
            llm=self._llm,
            tool_index=tool_index,
            memory=base_memory,
            max_iterations=max_iterations,
            top_tools=top_tools,
            ctx_max_tokens=ctx_max_tokens,
            short_term=self._short_term,
            working=self._working,
            results=results,
            builtin_executor=builtin_executor,
            skill_registry=skill_registry,
            mcp_manager=mcp_manager,
            tmp_dir=tmp_dir,
            downloads_dir=downloads_dir,
            workspace_dir=workspace_dir,
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
        # Save primary model index; restore after run() so next job starts fresh
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
