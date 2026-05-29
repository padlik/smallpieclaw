"""
agent_controller.py
-------------------
Core ReAct-style agent loop.

Workflow for each user request:
  1. Semantic search for relevant tools
  2. Send goal + tools + memory context to LLM
  3. Parse LLM JSON response
  4. Dispatch action: tool | create_tool | finish
  5. Feed result back to LLM and repeat (max N iterations)
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from confirmation import ConfirmationManager
from llm_client import LLMClient
from memory_store import MemoryStore
from prompt_builder import (
    build_system_prompt as _build_system_prompt,
    estimate_tokens as _estimate_tokens,
    format_log_section as _format_log_section_impl,
    format_models as _format_models_impl,
    format_skills as _format_skills_impl,
    format_tools as _format_tools_impl,
)
from react_loop import (
    ReactContext,
    extract_json_candidates,
    fmt_tool_call,
    fmt_tool_result_progress,
    format_tool_result,
    parse_json,
    react_loop,
)
from tool_creator import ToolCreator
from tool_executor import ToolExecutor
from tool_index import ToolIndex

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
        executor: ToolExecutor,
        creator: ToolCreator,
        memory: MemoryStore,
        max_iterations: int = 8,
        top_tools: int = 3,
        ctx_max_tokens: int = 90_000,
        short_term=None,       # Optional[ShortTermMemory]
        working=None,          # Optional[WorkingMemory]
        long_term=None,        # Optional[LongTermMemory]
        results=None,          # Optional[ResultsMemory]
        builtin_executor=None, # Optional[BuiltinExecutor]
        skill_registry=None,   # Optional[SkillRegistry]
        mcp_manager=None,      # Optional[MCPManager]
        tmp_dir: str = "/tmp/agent",
        downloads_dir: str = "downloads",
        log_file: str = "agent.log",
        log_backup_count: int = 30,
        cancel_event: Optional[threading.Event] = None,
        depth: int = 0,
        label: str = "main",   # identifies this agent in log lines
        on_step=None,          # Optional[Callable[[int], None]] — called after each step
    ):
        self.llm = llm
        self.tool_index = tool_index
        self.executor = executor
        self.creator = creator
        self.memory = memory
        self.max_iterations = max_iterations
        self.top_tools = top_tools
        self.ctx_max_tokens = ctx_max_tokens
        self.short_term = short_term
        self.working = working
        self.long_term = long_term
        self.results = results
        self.builtin_executor = builtin_executor
        self.skill_registry = skill_registry
        self.mcp_manager = mcp_manager
        self.tmp_dir = tmp_dir
        self.downloads_dir = downloads_dir
        self.log_file = log_file
        self.log_backup_count = log_backup_count
        self._cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.label = label
        self._log_prefix = f"[{label}] "
        self._on_step = on_step
        self._depth = depth  # 0 = main agent, 1 = sub-agent (spawn_agent blocked at depth ≥ 1)

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        user_goal: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        images: Optional[list[str]] = None,
    ) -> str:
        """
        Process a user goal and return the final answer string.
        Optionally calls progress_callback(msg) for intermediate updates.
        Pass images=["/path/to/file.jpg", ...] to include images in the first
        user message for vision-capable models.
        """
        ctx = ReactContext(
            llm=self.llm,
            tool_index=self.tool_index,
            executor=self.executor,
            creator=self.creator,
            memory=self.memory,
            builtin_executor=self.builtin_executor,
            mcp_manager=self.mcp_manager,
            skill_registry=self.skill_registry,
            max_iterations=self.max_iterations,
            top_tools=self.top_tools,
            ctx_max_tokens=self.ctx_max_tokens,
            tmp_dir=self.tmp_dir,
            downloads_dir=self.downloads_dir,
            log_file=self.log_file,
            log_backup_count=self.log_backup_count,
            depth=self._depth,
            label=self.label,
            short_term=self.short_term,
            working=self.working,
            results=self.results,
            cancel_event=self._cancel_event,
            on_step=self._on_step,
            confirmation=self._confirmation,
        )
        return react_loop(ctx, user_goal, progress_callback, images)


    def cancel(self) -> None:
        """Cancel the currently-running task. Safe to call from any thread."""
        self._cancel_event.set()
        logger.info("%sCancel requested by operator", self._log_prefix)

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

    def get_pending_tool_create(self, token: str) -> Optional[dict]:
        """Return pending tool-create data for display in Telegram UI."""
        return self._confirmation.get_pending_tool_create(token)

    def resume_tool_create(self, token: str, action: str) -> None:
        """Called by TelegramInterface with 'create', 'run', or 'cancel'."""
        self._confirmation.signal_tool_create(token, action)

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
            log_file=self.log_file,
            log_backup_count=self.log_backup_count,
            top_tools=self.top_tools,
            user_goal=user_goal,
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
                tools_used = [
                    s["details"].get("tool", "")
                    for s in self.working.steps
                    if s["action"] == "tool"
                ]
                self.results.add_result(
                    goal=self.working.goal,
                    summary=summary,
                    tools_used=list(filter(None, tools_used)),
                )
            msg = "✅ Task saved to results memory. Starting fresh context."
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
            logger.error("compress_context: LLM call failed: %s", exc)
            return f"❌ Compression failed: {exc}"

        self.short_term.clear()
        self.short_term.add("assistant", f"[Compressed context summary]\n{summary}")

        after_tokens = _estimate_tokens(summary)
        saved = max(0, before_tokens - after_tokens)
        pct = int(saved / before_tokens * 100) if before_tokens else 0
        logger.info(
            "compress_context: %d → ~%d tokens (%d%% reduction, %d messages → 1)",
            before_tokens, after_tokens, pct, len(messages),
        )
        return (
            f"✅ Context compressed.\n"
            f"  Messages: {len(messages)} → 1\n"
            f"  Tokens: ~{before_tokens:,} → ~{after_tokens:,} "
            f"(−{saved:,}, {pct}% smaller)"
        )

    # ------------------------------------------------------------------
    # Internals (delegates to react_loop module)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_tools(tools) -> str:
        return _format_tools_impl(tools)

    def _format_skills(self) -> str:
        """Return the AVAILABLE SKILLS prompt section, or empty string if no skills."""
        return _format_skills_impl(self.skill_registry)

    def _format_models(self) -> str:
        """Return the AVAILABLE MODELS prompt section listing all configured models."""
        return _format_models_impl(self.llm)

    def list_models(self) -> list[dict]:
        """Return all configured models as a list of dicts."""
        try:
            return list(self.llm._models)
        except AttributeError:
            return []

    def _format_log_section(self) -> str:
        """Build the AGENT LOG section for the system prompt."""
        return _format_log_section_impl(self.log_file, self.log_backup_count)

    @staticmethod
    def _fmt_tool_call(tool_name: str, args: dict) -> str:
        return fmt_tool_call(tool_name, args)

    @staticmethod
    def _fmt_tool_result_progress(tool_name: str, args: dict, outcome: dict) -> str:
        return fmt_tool_result_progress(tool_name, args, outcome)

    @staticmethod
    def _format_tool_result(tool_name: str, outcome: dict) -> str:
        return format_tool_result(tool_name, outcome)

    @staticmethod
    def _extract_json_candidates(text: str) -> list[str]:
        return extract_json_candidates(text)

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        return parse_json(text)



# ---------------------------------------------------------------------------
# SubAgentRunner
# ---------------------------------------------------------------------------

class SubAgentRunner:
    """
    An isolated agent instance for background task execution.

    Each runner has its own LLMClient, ShortTermMemory, and WorkingMemory.
    It shares ToolIndex, ToolExecutor, ToolCreator, BuiltinExecutor,
    SkillRegistry, LongTermMemory, and ResultsMemory with the main agent.

    Results are delivered via notify_fn (Telegram) and written to long_term memory.
    """

    def __init__(
        self,
        *,
        model_cfg: dict,              # single [[models]] entry dict
        config: dict,                 # full agent config (for LLMClient)
        tool_index,                   # ToolIndex (shared)
        executor,                     # ToolExecutor (shared)
        creator,                      # ToolCreator (shared)
        base_memory,                  # MemoryStore (shared long-term facts)
        builtin_executor,             # BuiltinExecutor (shared)
        skill_registry=None,          # SkillRegistry (shared)
        mcp_manager=None,             # MCPManager (shared, optional)
        long_term=None,               # LongTermMemory (shared)
        results=None,                 # ResultsMemory (shared)
        short_term=None,              # ShortTermMemory — pre-loaded context (optional)
        notify_fn=None,               # Callable[[str], None]
        context_key: str = None,      # for context persistence
        label: str = "on-demand",     # label for logging/display
        max_iterations: int = 8,
        top_tools: int = 3,
        ctx_max_tokens: int = 90_000,
        tmp_dir: str = "/tmp/agent",
        downloads_dir: str = "downloads",
        usage_registry=None,          # TokenUsageRegistry
        depth: int = 1,
        fallback_models: list[str] | None = None,  # None = inherit from parent config
        on_step=None,                 # Optional[Callable[[int], None]] — for iteration tracking
    ):
        import uuid
        from memory_store import ShortTermMemory, WorkingMemory

        self.agent_id = "sa-" + uuid.uuid4().hex[:6]
        self.label = label
        self.context_key = context_key
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

        self._cancel_event = threading.Event()

        # Own LLM instance with model override + shared token registry + cancellation
        # fallback_models=None means inherit from sub_config (which inherited from parent config)
        self._llm = LLMClient(sub_config, usage_registry=usage_registry,
                              cancel_event=self._cancel_event,
                              fallback_models=fallback_models,
                              caller_tag=self.agent_id)

        # Own blank memory (working context for this task)
        self._short_term = short_term if short_term is not None else ShortTermMemory()
        self._working = WorkingMemory()

        # Build the isolated AgentController — headless (no confirm callbacks)
        self._agent = AgentController(
            llm=self._llm,
            tool_index=tool_index,
            executor=executor,
            creator=creator,
            memory=base_memory,
            max_iterations=max_iterations,
            top_tools=top_tools,
            ctx_max_tokens=ctx_max_tokens,
            short_term=self._short_term,
            working=self._working,
            long_term=long_term,
            results=results,
            builtin_executor=builtin_executor,
            skill_registry=skill_registry,
            mcp_manager=mcp_manager,
            tmp_dir=tmp_dir,
            downloads_dir=downloads_dir,
            cancel_event=self._cancel_event,
            depth=depth,
            label=self.agent_id,
            on_step=on_step,
        )

        self._model_id = model_cfg.get("model", "unknown")

    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Signal cooperative cancellation. Takes effect between iterations."""
        self._cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self, task: str) -> str:
        """
        Run the sub-agent synchronously in the calling thread.
        Returns the final result string (or an error/cancellation message).
        Should be called from a background thread.
        """
        import time
        start = time.time()
        self._log.info(
            "Starting (model: %s, context_key: %s)",
            self._model_id,
            self.context_key or "none",
        )
        # Save primary model index; restore after run() so next job starts fresh
        _primary_idx = self._llm._active_idx
        try:
            result = self._agent.run(task)
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
