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

import base64
import json
import logging
import re
import secrets
import subprocess
import sys
import threading
from typing import Callable, Optional

from llm_client import LLMClient, LLMCancelledError
from memory_store import MemoryStore
from tool_creator import ToolCreator
from tool_executor import ToolExecutor
from tool_index import ToolIndex

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Conservative character-to-token estimate (4 chars ≈ 1 token)."""
    return len(text) // 4


def _estimate_messages_tokens(messages: list[dict], system: str = "") -> int:
    total = _estimate_tokens(system)
    for m in messages:
        total += _estimate_tokens(m.get("content", ""))
    return total

# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
You are a home-server management agent running on a Raspberry Pi.
You help the user control and query their home server via Telegram.

PERSISTENT MEMORY (facts about this system):
{memory}

RECENT CONVERSATION:
{short_term}

RELEVANT PAST RESULTS:
{past_results}

BUILT-IN TOOLS (always available — prefer these before creating new tools):
  shell        — execute any shell command on the host system
  file_read    — read a file from the filesystem
  file_write   — write content to a file on the filesystem
  schedule     — manage scheduled jobs and reminders (actions: list, add, remove, pause, resume, run_now)
  spawn_agent  — spawn an isolated sub-agent in the background for long-running or model-specific tasks
  memory_write — read/write the agent's persistent memory (actions: set, append, delete, get); value must be a native JSON value (object, array, number, string) — do NOT pre-serialize to a string

AVAILABLE TOOLS:
{tools}

{skills_section}{models_section}FILE STORAGE:
{file_storage}

AGENT LOG:
{log_section}

RESPONSE FORMAT — CRITICAL:
- You MUST respond with ONLY a single valid JSON object. Nothing else.
- No markdown, no prose, no explanation, no ```json fences. Just the raw JSON object.
- Invalid responses waste a step and delay the user.

Possible actions:

1. Execute a tool (built-in or registered):
   {{"action": "tool", "tool": "<tool_name>", "args": {{}}}}

   CORRECT:   {{"action": "tool", "tool": "shell", "args": {{"command": "df -h"}}}}
   WRONG:     {{"action": "shell", "command": "df -h"}}
   WRONG:     {{"action": "tool", "tool": "shell", "args": ["df -h"]}}

2. Propose creating a new tool (requires operator approval — see rules):
   {{"action": "create_tool", "name": "<snake_case_name>", "language": "python", "code": "<code>", "description": "<one line>"}}

3. Finish and return an answer to the user:
   {{"action": "finish", "result": "<your answer>"}}

Rules:
- Always try shell / file_read / file_write before proposing a new tool.
- If the user says "use skill <name>" or the task clearly matches a listed skill, read its SKILL.md first using file_read, then follow the instructions inside.
- SKILL.md files describe *how* to accomplish tasks using shell commands and other means. Any "tools" or sub-commands mentioned inside a SKILL.md are descriptions of functionality — they are NOT registered tools you can call. Do not call them with {{"action": "tool", ...}}. Use shell or file_read to implement the instructions described in the skill.
- When a SKILL.md references scripts, binaries, or files with relative paths (e.g. scripts/run.sh, ./process.py), resolve them against the skill's directory shown in AVAILABLE SKILLS. Use the absolute path directly or prefix the command with: cd <skill_dir> && <command>.
- Use the shell tool for one-off or task-specific scripts — do NOT create a tool for single-use tasks.
- Propose a new tool ONLY when it would be genuinely reusable across many different scenarios.
- Tools must follow the UNIX paradigm: one tool, one task. Keep tools compact and composable.
- Prefer Python for tools; use bash only for very simple one-liners.
- Never hardcode paths, usernames, or task-specific values in tools — use parameters.
- It is fine to propose multiple small tools instead of one large one.
- All tool creation requires operator confirmation — the operator will review your code before approving.
- Never include dangerous commands (rm -rf /, sudo, eval, reverse shells, etc.).
- If a tool fails, try a different approach or explain the issue.
- Always end with a "finish" action.
""".strip()

# Marker prefixes used to send interactive requests through the progress callback
_CONFIRM_PREFIX = "__CONFIRM__"
_EXTEND_PREFIX = "__EXTEND__"
_TOOL_CREATE_PREFIX = "__TOOL_CREATE__"


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
        tmp_dir: str = "/tmp/agent",
        downloads_dir: str = "downloads",
        log_file: str = "agent.log",
        log_backup_count: int = 30,
        cancel_event: Optional[threading.Event] = None,
        depth: int = 0,
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
        self.tmp_dir = tmp_dir
        self.downloads_dir = downloads_dir
        self.log_file = log_file
        self.log_backup_count = log_backup_count
        self._cancel_event = cancel_event
        self._depth = depth

        # Confirmation state: token -> threading.Event and result holder
        self._confirm_events: dict[str, threading.Event] = {}
        self._confirm_results: dict[str, bool] = {}

        # Max-steps extension state
        self._extend_events: dict[str, threading.Event] = {}
        self._extend_results: dict[str, bool] = {}

        # Tool-creation confirmation state
        self._tool_create_events: dict[str, threading.Event] = {}
        self._tool_create_results: dict[str, str] = {}   # "create" | "run" | "cancel"
        self._tool_create_pending: dict[str, dict] = {}  # token → {name, language, code, description}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        user_goal: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Process a user goal and return the final answer string.
        Optionally calls progress_callback(msg) for intermediate updates.
        """
        def _progress(msg: str):
            if progress_callback:
                progress_callback(msg)
            logger.debug("Agent progress: %s", msg)

        # Start working memory task tracking
        if self.working:
            self.working.start_task(user_goal)

        # 1. Find relevant tools
        relevant_tools = self.tool_index.search(user_goal, top_k=self.top_tools)
        tools_text = self._format_tools(relevant_tools)
        memory_text = self.memory.as_prompt_text()
        short_term_text = self.short_term.as_prompt_text() if self.short_term else "No recent conversation."
        past_results_text = self.results.as_prompt_text(user_goal, top_k=2) if self.results else "No past results."
        skills_section = self._format_skills()
        models_section = self._format_models()
        file_storage = (
            f"- Temporary files (downloads, intermediate outputs, anything only needed for this task):\n"
            f"    {self.tmp_dir}  ← cleaned by OS on reboot\n"
            f"- Permanent downloads (files the user wants to keep):\n"
            f"    {self.downloads_dir}\n"
            f"- Use tmp for QR codes, generated images, fetched configs, etc.\n"
            f"- Use downloads for files the operator explicitly wants to keep.\n"
            f"- Never write files to the agent script directory."
        )
        log_section = self._format_log_section()

        system = _SYSTEM_PROMPT.format(
            memory=memory_text,
            short_term=short_term_text,
            past_results=past_results_text,
            tools=tools_text,
            skills_section=skills_section,
            models_section=models_section,
            file_storage=file_storage,
            log_section=log_section,
        )
        messages: list[dict] = [{"role": "user", "content": user_goal}]

        self.memory.record_event(f"User request: {user_goal[:100]}")

        # 2. ReAct loop — supports dynamic extension when max steps are reached
        max_steps = self.max_iterations
        step = 0

        while True:  # outer loop: allows step-count extension by user
            while step < max_steps:
                # Cooperative cancellation check (sub-agents)
                if self._cancel_event and self._cancel_event.is_set():
                    logger.warning("Agent cancelled at step %d/%d", step, max_steps)
                    return "[Cancelled]"

                step += 1
                logger.info("Agent step %d/%d", step, max_steps)
                _progress(f"⚙️ Thinking… (step {step})")

                # Context compaction check
                messages = self._maybe_compact(messages, system)

                # LLM call — with in-place retry on empty response (network glitch)
                _MAX_EMPTY_RETRIES = 2
                raw = ""
                for _attempt in range(1 + _MAX_EMPTY_RETRIES):
                    try:
                        raw = self.llm.chat(messages, system=system, progress_cb=_progress)
                    except LLMCancelledError:
                        logger.info("Agent LLM call cancelled at step %d/%d", step, max_steps)
                        return "[Cancelled]"
                    except Exception as exc:
                        err = f"❌ LLM error: {type(exc).__name__}: {exc}"
                        _progress(err)
                        return err
                    if raw.strip():
                        break
                    if _attempt < _MAX_EMPTY_RETRIES:
                        logger.warning(
                            "LLM returned empty response (step %d/%d), retrying in-place (%d/%d)…",
                            step, max_steps, _attempt + 1, _MAX_EMPTY_RETRIES,
                        )
                        _progress(f"⏳ Empty LLM response, retrying ({_attempt + 1}/{_MAX_EMPTY_RETRIES})…")

                # Parse JSON
                action_obj = self._parse_json(raw)
                if action_obj is None:
                    logger.warning(
                        "LLM returned non-JSON (step %d/%d, ~%d chars):\n--- BEGIN ---\n%s\n--- END ---",
                        step, max_steps, len(raw), raw[:1000],
                    )
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({
                        "role": "user",
                        "content": (
                            'ERROR: Your response was not valid JSON. '
                            'You MUST respond with ONLY a raw JSON object — no markdown, '
                            'no prose, no ```json fences. Example: '
                            '{"action": "tool", "tool": "shell", "args": {"command": "df -h"}}'
                        )
                    })
                    continue

                messages.append({"role": "assistant", "content": raw})
                action = action_obj.get("action", "")

                # Normalize shorthand: {"action": "shell"} → {"action": "tool", "tool": "shell"}
                _BUILTIN_NAMES = {"shell", "file_read", "file_write", "schedule", "spawn_agent", "memory_write"}
                if action in _BUILTIN_NAMES:
                    logger.warning("LLM used shorthand action '%s' — normalizing to tool call", action)
                    action_obj = {"action": "tool", "tool": action, "args": {k: v for k, v in action_obj.items() if k != "action"}}
                    action = "tool"

                # ---- Dispatch ----

                if action == "finish":
                    result = action_obj.get("result", "Done.")
                    self.memory.record_event(f"Agent finished: {result[:80]}")
                    if self.short_term:
                        self.short_term.add("user", user_goal)
                        self.short_term.add("assistant", result)
                    if self.results and self.working and self.working.has_content():
                        tools_used = [
                            s["details"].get("tool", "")
                            for s in self.working.steps
                            if s["action"] == "tool"
                        ]
                        self.results.add_result(
                            goal=user_goal,
                            summary=result[:500],
                            tools_used=tools_used,
                        )
                    if self.working:
                        self.working.clear()
                    return result

                elif action == "tool":
                    tool_name = action_obj.get("tool", "")
                    args = action_obj.get("args", {})
                    # Normalize: LLM sometimes emits args as a list instead of a dict
                    if isinstance(args, list):
                        args = {str(i): v for i, v in enumerate(args)}
                    _progress(f"🔧 Running tool: <code>{tool_name}</code>\n{self._fmt_tool_call(tool_name, args)}")

                    # Built-in tools take priority
                    if self.builtin_executor and self.builtin_executor.is_builtin(tool_name):
                        outcome = self.builtin_executor.execute(tool_name, args)

                        if outcome.get("requires_confirmation"):
                            token = outcome["token"]
                            description = outcome.get("description", tool_name)
                            # Set up blocking event
                            event = threading.Event()
                            self._confirm_events[token] = event
                            self._confirm_results[token] = False
                            # Signal the UI to show confirmation buttons
                            _progress(f"{_CONFIRM_PREFIX}:{token}:{description}")
                            # Block until user responds (timeout 5 min)
                            event.wait(timeout=300)
                            # Always read the actual result — handles the race where
                            # resume() fires between timeout-return and this pop.
                            result_confirmed = self._confirm_results.pop(token, False)
                            self._confirm_events.pop(token, None)

                            if result_confirmed:
                                outcome = self.builtin_executor.confirm(token)
                                _progress(f"✅ Confirmed — executing <code>{tool_name}</code>\n{self._fmt_tool_call(tool_name, args)}")
                            else:
                                self.builtin_executor.cancel(token)
                                outcome = {"success": False, "output": "", "error": "Cancelled by user.", "exit_code": -1}
                                _progress("❌ Cancelled by user.")
                    else:
                        outcome = self.executor.execute(tool_name, args)

                    if self.working:
                        self.working.add_step("tool", {"tool": tool_name, "args": args, "success": outcome["success"]})

                    # If the tool produced a file to send, trigger async upload via progress prefix
                    if outcome.get("send_file"):
                        path_b64 = base64.b64encode(outcome["send_file"].encode()).decode()
                        caption_b64 = base64.b64encode(outcome.get("caption", "").encode()).decode()
                        _progress(f"__FILE__:{path_b64}:{caption_b64}")

                    tool_result = self._format_tool_result(tool_name, outcome)
                    if outcome["success"]:
                        logger.info("Tool '%s' result: success=True", tool_name)
                    else:
                        logger.warning(
                            "Tool '%s' result: success=False | error=%s | args=%s",
                            tool_name,
                            outcome.get("error", ""),
                            {k: str(v)[:120] for k, v in args.items()},
                        )
                    _progress(self._fmt_tool_result_progress(tool_name, args, outcome))
                    messages.append({"role": "user", "content": tool_result})

                elif action == "create_tool":
                    tool_name = action_obj.get("name", "unnamed_tool")
                    language = action_obj.get("language", "python")
                    code = action_obj.get("code", "")
                    description = action_obj.get("description", "")

                    # All tool creation requires operator confirmation
                    token = secrets.token_hex(4)
                    self._tool_create_pending[token] = {
                        "name": tool_name,
                        "language": language,
                        "code": code,
                        "description": description,
                    }
                    tc_event = threading.Event()
                    self._tool_create_events[token] = tc_event
                    self._tool_create_results[token] = "cancel"
                    _progress(f"{_TOOL_CREATE_PREFIX}:{token}")
                    # Block up to 5 min
                    tc_event.wait(timeout=300)
                    tc_event = self._tool_create_events.pop(token, None)
                    tc_action = self._tool_create_results.pop(token, "cancel")
                    self._tool_create_pending.pop(token, None)

                    if tc_action == "create":
                        result = self.creator.create(tool_name, language, code, description)
                        if self.working:
                            self.working.add_step("create_tool", {"name": tool_name, "success": result["success"]})
                        if result["success"]:
                            feedback = (
                                f"Tool '{result['name']}' was created successfully at {result['path']}. "
                                "You can now use it with the 'tool' action."
                            )
                            _progress(f"🛠️ Tool Created: <code>{result['name']}</code>\n✅ {description}")
                        else:
                            feedback = f"Tool creation failed: {result['error']}"
                            _progress(f"🛠️ Tool Creation Failed: <code>{tool_name}</code>\n❌ {result['error']}")
                        logger.info("Tool creation '%s': %s", tool_name, result)

                    elif tc_action == "run":
                        _progress(f"⚡ Running <code>{tool_name}</code> as one-off script…")
                        try:
                            if language == "python":
                                proc = subprocess.run(
                                    [sys.executable, "-c", code],
                                    capture_output=True, text=True, timeout=30
                                )
                            else:
                                proc = subprocess.run(
                                    ["bash", "-c", code],
                                    capture_output=True, text=True, timeout=30
                                )
                            output = (proc.stdout or "") + (proc.stderr or "")
                            output = output[:2000]
                            feedback = f"Script executed (exit {proc.returncode}):\n{output}" if output else f"Script executed (exit {proc.returncode}), no output."
                            _progress(f"⚡ Script result (exit {proc.returncode}):\n<blockquote>{output[:400]}</blockquote>" if output else "⚡ Script ran, no output.")
                        except Exception as exc:
                            feedback = f"Script execution failed: {exc}"
                            _progress(f"❌ Script failed: {exc}")

                    else:  # cancel
                        feedback = "Tool creation was cancelled by operator. Try a different approach or use shell."
                        _progress("❌ Tool creation cancelled by operator.")

                    messages.append({"role": "user", "content": feedback})

                else:
                    logger.warning("Unknown action '%s' from LLM", action)
                    messages.append({
                        "role": "user",
                        "content": f'Unknown action "{action}". Use "tool", "create_tool", or "finish".',
                    })

            # Inner while exited — max steps reached. Ask user to extend.
            ext_token = secrets.token_hex(4)
            ext_event = threading.Event()
            self._extend_events[ext_token] = ext_event
            self._extend_results[ext_token] = False
            _progress(f"{_EXTEND_PREFIX}:{ext_token}:{max_steps}")
            ext_event.wait(timeout=120)  # 2-min window to respond
            self._extend_events.pop(ext_token, None)
            should_extend = self._extend_results.pop(ext_token, False)

            if should_extend:
                max_steps += 10
                logger.info("Agent steps extended to %d by user", max_steps)
                _progress(f"⏩ Extended — continuing to step {max_steps}…")
                continue  # back to outer while → re-enters inner while

            # User declined or timed out
            break

        # Max iterations reached and user declined to extend
        self.memory.record_event("Agent hit max iterations")
        return "⚠️ Agent reached maximum steps. Operation cancelled."

    def resume(self, token: str, confirmed: bool) -> None:
        """Called by TelegramInterface when user responds to a file_write/shell confirmation."""
        logger.info("resume() called: token=%s confirmed=%s event_found=%s",
                    token[:8], confirmed, token in self._confirm_events)
        self._confirm_results[token] = confirmed
        event = self._confirm_events.get(token)
        if event:
            event.set()
        else:
            logger.warning("resume(): no event found for token=%s (already resolved or timed out?)", token[:8])

    def resume_extend(self, token: str, confirmed: bool) -> None:
        """Called by TelegramInterface when user responds to a max-steps extension prompt."""
        logger.info("resume_extend(): token=%s confirmed=%s", token[:8], confirmed)
        self._extend_results[token] = confirmed
        event = self._extend_events.get(token)
        if event:
            event.set()
        else:
            logger.warning("resume_extend(): no event for token=%s", token[:8])

    def get_pending_tool_create(self, token: str) -> Optional[dict]:
        """Return pending tool-create data for display in Telegram UI."""
        return self._tool_create_pending.get(token)

    def resume_tool_create(self, token: str, action: str) -> None:
        """Called by TelegramInterface with 'create', 'run', or 'cancel'."""
        logger.info("resume_tool_create(): token=%s action=%s", token[:8], action)
        self._tool_create_results[token] = action
        event = self._tool_create_events.get(token)
        if event:
            event.set()
        else:
            logger.warning("resume_tool_create(): no event for token=%s", token[:8])

    def build_system_prompt(self, user_goal: str = "(context snapshot)") -> tuple[str, int]:
        """Build the full system prompt as it would be sent to the LLM.

        Returns (prompt_text, estimated_tokens).
        """
        relevant_tools = self.tool_index.search(user_goal, top_k=self.top_tools)
        tools_text = self._format_tools(relevant_tools)
        memory_text = self.memory.as_prompt_text()
        short_term_text = self.short_term.as_prompt_text() if self.short_term else "No recent conversation."
        past_results_text = self.results.as_prompt_text(user_goal, top_k=2) if self.results else "No past results."
        skills_section = self._format_skills()
        models_section = self._format_models()
        file_storage = (
            f"- Temporary files:\n    {self.tmp_dir}\n"
            f"- Permanent downloads:\n    {self.downloads_dir}"
        )
        prompt = _SYSTEM_PROMPT.format(
            memory=memory_text,
            short_term=short_term_text,
            past_results=past_results_text,
            tools=tools_text,
            skills_section=skills_section,
            models_section=models_section,
            file_storage=file_storage,
            log_section=self._format_log_section(),
        )
        return prompt, _estimate_tokens(prompt)

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
        return msg

    # ------------------------------------------------------------------
    # Context compaction
    # ------------------------------------------------------------------

    def _maybe_compact(self, messages: list[dict], system: str) -> list[dict]:
        """
        If estimated context tokens exceed 85% of ctx_max_tokens, summarise
        the middle portion of the conversation to reduce size.
        """
        total = _estimate_messages_tokens(messages, system)
        threshold = int(self.ctx_max_tokens * 0.85)
        if total <= threshold:
            return messages
        if len(messages) <= 3:
            return messages  # too short to compact meaningfully

        logger.warning(
            "Context size ~%d tokens exceeds threshold %d — compacting…",
            total, threshold,
        )
        # Keep first message (user goal) and last 2 messages; summarise the middle
        first = messages[:1]
        middle = messages[1:-2]
        last = messages[-2:]

        middle_text = "\n".join(
            f"[{m['role']}]: {m['content'][:500]}" for m in middle
        )
        try:
            summary = self.llm.chat([{
                "role": "user",
                "content": (
                    "Summarize these intermediate agent steps concisely in bullet points "
                    "(preserve tool names, key outputs, and decisions):\n\n" + middle_text
                ),
            }])
        except Exception as exc:
            logger.error("Compaction LLM call failed: %s", exc)
            return messages  # fall back to uncompacted

        compacted = first + [
            {"role": "user", "content": f"[Compacted context — earlier steps summary]\n{summary}"}
        ] + last
        new_total = _estimate_messages_tokens(compacted, system)
        logger.info("Compacted context: %d → ~%d tokens", total, new_total)
        return compacted

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _format_tools(tools) -> str:
        if not tools:
            return "No additional tools registered."
        lines = [f"  {t.name}: {t.description}" for t in tools]
        return "\n".join(lines)

    def _format_skills(self) -> str:
        """Return the AVAILABLE SKILLS prompt section, or empty string if no skills."""
        if not self.skill_registry:
            return ""
        skills = self.skill_registry.all()
        if not skills:
            return ""
        lines = ["AVAILABLE SKILLS (read SKILL.md via file_read to activate a skill):"]
        for s in skills:
            lines.append(f"  {s.name}")
            lines.append(f"    SKILL.md: {s.skill_md_path}")
            lines.append(f"    Skill dir: {s.path}/")
            lines.append(f"    {s.description}")
        lines.append("")
        return "\n".join(lines)

    def _format_models(self) -> str:
        """Return the AVAILABLE MODELS prompt section listing all configured models."""
        try:
            models = self.llm._models
        except AttributeError:
            return ""
        if not models:
            return ""
        active_model = self.llm.llm_cfg.get("model", "")
        lines = ["AVAILABLE MODELS (use the 'model' parameter in spawn_agent to select):"]
        for m in models:
            name = m.get("name", "")
            model_id = m.get("model", "")
            hint = m.get("hint", "")
            marker = " ← active" if model_id == active_model else ""
            hint_str = f"  [{hint}]" if hint else ""
            display = f"  {model_id}"
            if name:
                display += f" ({name})"
            display += hint_str + marker
            lines.append(display)
        lines.append("")
        return "\n".join(lines) + "\n"

    def list_models(self) -> list[dict]:
        """Return all configured models as a list of dicts."""
        try:
            return list(self.llm._models)
        except AttributeError:
            return []

    def _format_log_section(self) -> str:
        """Build the AGENT LOG section for the system prompt."""
        import os
        log_abs = os.path.abspath(self.log_file)
        lines = [
            f"- Active log (always current session): {log_abs}",
            "- Rotation: nightly at 00:00 local time. Rotated files use numbered suffixes:",
            f"    {log_abs}    ← today (active)",
            f"    {log_abs}.1  ← yesterday",
            f"    {log_abs}.2  ← 2 days ago  … up to .{self.log_backup_count}",
            f"- To read recent log entries:  file_read(path='{log_abs}', offset=-10000)",
            f"- To read yesterday's log:     file_read(path='{log_abs}.1')",
            "- Always use file_read with a negative offset (e.g. -20000) to read the tail of large logs.",
            "- Do NOT use 'tail' or 'journalctl' for agent logs — use file_read on the paths above.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _fmt_tool_call(tool_name: str, args: dict) -> str:
        """Format a tool call as a compact, readable string for progress display."""
        if tool_name == "shell":
            cmd = args.get("command", "")
            return f"<blockquote>$ {cmd}</blockquote>"
        if tool_name == "file_read":
            return f"<blockquote>read: {args.get('path', '?')}</blockquote>"
        if tool_name == "file_write":
            path = args.get("path", "?")
            size = len(args.get("content", ""))
            return f"<blockquote>write: {path} ({size} bytes)</blockquote>"
        # Generic: show args as compact JSON, truncated
        import json as _json
        try:
            arg_str = _json.dumps(args, ensure_ascii=False)
        except Exception:
            arg_str = str(args)
        if len(arg_str) > 200:
            arg_str = arg_str[:197] + "…"
        return f"<blockquote>{arg_str}</blockquote>" if arg_str and arg_str != "{}" else ""

    @staticmethod
    def _fmt_tool_result_progress(tool_name: str, args: dict, outcome: dict) -> str:
        """Format a tool result as a short progress update."""
        call = AgentController._fmt_tool_call(tool_name, args)
        if outcome["success"]:
            out = (outcome.get("output") or "").strip()
            if out:
                lines = out.splitlines()
                preview = "\n".join(lines[:8])
                if len(lines) > 8 or len(preview) > 400:
                    preview = preview[:400] + "\n…"
                return f"🔧 <b>{tool_name}</b> ✅\n{call}\n<blockquote>{preview}</blockquote>"
            return f"🔧 <b>{tool_name}</b> ✅\n{call}\n<i>(no output)</i>"
        else:
            err = (outcome.get("error") or outcome.get("output") or "failed").strip()
            if len(err) > 300:
                err = err[:297] + "…"
            return f"🔧 <b>{tool_name}</b> ❌\n{call}\n<blockquote>{err}</blockquote>"

    @staticmethod
    def _format_tool_result(tool_name: str, outcome: dict) -> str:
        if outcome["success"]:
            output = outcome["output"] or "(no output)"
            return f"Tool '{tool_name}' succeeded:\n{output}"
        else:
            parts = [f"Tool '{tool_name}' failed (exit {outcome.get('exit_code', '?')})."]
            if outcome.get("error"):
                parts.append(f"stderr: {outcome['error']}")
            if outcome.get("output"):
                parts.append(f"stdout: {outcome['output']}")
            return "\n".join(parts)

    @staticmethod
    def _extract_json_candidates(text: str) -> list[str]:
        """
        Brace-counting extractor: returns all balanced {…} substrings found in text.
        Handles multiple JSON objects in a single response and prose-wrapped objects.
        """
        candidates = []
        depth = 0
        start = -1
        in_string = False
        escape_next = False
        for i, ch in enumerate(text):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start != -1:
                    candidates.append(text[start:i + 1])
                    start = -1
        return candidates

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        """Extract and parse the first valid JSON action object found in the text."""
        text = text.strip()
        if not text:
            return None

        # 1. Try the whole text as-is
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        # 2. Strip markdown code fences then try again
        fence_match = re.search(r"```(?:json)?\s*(\{.*?})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                obj = json.loads(fence_match.group(1))
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass

        # 3. Brace-counting extractor — handles multiple objects and prose wrappers.
        #    Prefer the first candidate that has an "action" key; fall back to first parseable dict.
        candidates = AgentController._extract_json_candidates(text)
        first_valid: Optional[dict] = None
        for candidate in candidates:
            try:
                obj = json.loads(candidate)
                if not isinstance(obj, dict):
                    continue
                if first_valid is None:
                    first_valid = obj
                if "action" in obj:
                    return obj
            except json.JSONDecodeError:
                continue
        if first_valid is not None:
            return first_valid

        return None



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
    ):
        import uuid
        from memory_store import ShortTermMemory, WorkingMemory

        self.agent_id = "sa-" + uuid.uuid4().hex[:6]
        self.label = label
        self.context_key = context_key
        self.notify_fn = notify_fn or (lambda msg: None)
        self._log = logging.getLogger("agent").getChild(f"sub.{label}")

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
        self._llm = LLMClient(sub_config, usage_registry=usage_registry,
                              cancel_event=self._cancel_event)

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
            tmp_dir=tmp_dir,
            downloads_dir=downloads_dir,
            cancel_event=self._cancel_event,
            depth=depth,
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
