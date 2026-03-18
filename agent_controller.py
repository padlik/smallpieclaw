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

import json
import logging
import re
import threading
from typing import Any, Callable, Optional

from llm_client import LLMClient
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
  shell      — execute any shell command on the host system
  file_read  — read a file from the filesystem
  file_write — write content to a file on the filesystem

AVAILABLE TOOLS:
{tools}

RESPONSE FORMAT — you must ALWAYS respond with a single valid JSON object.
No markdown, no prose, just JSON.

Possible actions:

1. Execute a tool (built-in or registered):
   {{"action": "tool", "tool": "<tool_name>", "args": {{}}}}

2. Create a new tool only when built-ins and registered tools cannot fulfil the request:
   {{"action": "create_tool", "name": "<snake_case_name>", "language": "bash", "code": "<script>", "description": "<one line>"}}

3. Finish and return an answer to the user:
   {{"action": "finish", "result": "<your answer>"}}

Rules:
- Always try shell / file_read / file_write before creating a new tool.
- When creating a tool, write only safe, minimal shell or Python code.
- Never include dangerous commands (rm -rf /, sudo, eval, reverse shells, etc.).
- If a tool fails, try a different approach or explain the issue.
- Always end with a "finish" action.
""".strip()

# Marker prefix used to request user confirmation through the progress callback
_CONFIRM_PREFIX = "__CONFIRM__"


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

        # Confirmation state: token -> threading.Event and result holder
        self._confirm_events: dict[str, threading.Event] = {}
        self._confirm_results: dict[str, bool] = {}

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

        # Auto-select model based on goal hint
        if hasattr(self.llm, "select_model_by_hint"):
            switched = self.llm.select_model_by_hint(user_goal)
            if switched:
                logger.info("Auto-selected model '%s' based on goal hint", switched)

        # Start working memory task tracking
        if self.working:
            self.working.start_task(user_goal)

        # 1. Find relevant tools
        relevant_tools = self.tool_index.search(user_goal, top_k=self.top_tools)
        tools_text = self._format_tools(relevant_tools)
        memory_text = self.memory.as_prompt_text()
        short_term_text = self.short_term.as_prompt_text() if self.short_term else "No recent conversation."
        past_results_text = self.results.as_prompt_text(user_goal, top_k=2) if self.results else "No past results."

        system = _SYSTEM_PROMPT.format(
            memory=memory_text,
            short_term=short_term_text,
            past_results=past_results_text,
            tools=tools_text,
        )
        messages: list[dict] = [{"role": "user", "content": user_goal}]

        self.memory.record_event(f"User request: {user_goal[:100]}")

        # 2. ReAct loop
        for step in range(1, self.max_iterations + 1):
            logger.info("Agent step %d/%d", step, self.max_iterations)
            _progress(f"⚙️ Thinking… (step {step})")

            # Context compaction check
            messages = self._maybe_compact(messages, system)

            # LLM call
            try:
                raw = self.llm.chat(messages, system=system)
            except Exception as exc:
                return f"❌ LLM error: {exc}"

            # Parse JSON
            action_obj = self._parse_json(raw)
            if action_obj is None:
                logger.warning("LLM returned non-JSON: %s", raw[:200])
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": 'Please respond with a valid JSON object only (no markdown, no prose).'
                })
                continue

            messages.append({"role": "assistant", "content": raw})
            action = action_obj.get("action", "")

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
                        for s in self.working._steps
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
                        confirmed = event.wait(timeout=300)
                        result_confirmed = self._confirm_results.pop(token, False) if confirmed else False
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
                tool_result = self._format_tool_result(tool_name, outcome)
                logger.info("Tool '%s' result: success=%s", tool_name, outcome["success"])
                _progress(self._fmt_tool_result_progress(tool_name, args, outcome))
                messages.append({"role": "user", "content": tool_result})

            elif action == "create_tool":
                tool_name = action_obj.get("name", "unnamed_tool")
                language = action_obj.get("language", "bash")
                code = action_obj.get("code", "")
                description = action_obj.get("description", "")
                _progress(f"🛠️ Creating new tool: <code>{tool_name}</code>")
                result = self.creator.create(tool_name, language, code, description)
                if self.working:
                    self.working.add_step("create_tool", {"name": tool_name, "success": result["success"]})
                if result["success"]:
                    feedback = (
                        f"Tool '{result['name']}' was created successfully at {result['path']}. "
                        "You can now use it with the 'tool' action."
                    )
                    _progress(f"🔧 Tool Created: <code>{result['name']}</code>\n✅ Success\n📝 {description}")
                else:
                    feedback = f"Tool creation failed: {result['error']}"
                    _progress(f"🔧 Tool Creation Failed: <code>{tool_name}</code>\n❌ {result['error']}")
                logger.info("Tool creation '%s': %s", tool_name, result)
                messages.append({"role": "user", "content": feedback})

            else:
                logger.warning("Unknown action '%s' from LLM", action)
                messages.append({
                    "role": "user",
                    "content": f'Unknown action "{action}". Use "tool", "create_tool", or "finish".',
                })

        # Max iterations reached
        self.memory.record_event("Agent hit max iterations")
        return "⚠️ Agent reached maximum steps without a final answer. Please rephrase your request."

    def resume(self, token: str, confirmed: bool) -> None:
        """Called by TelegramInterface when user responds to a confirmation prompt."""
        logger.info("resume() called: token=%s confirmed=%s event_found=%s",
                    token[:8], confirmed, token in self._confirm_events)
        self._confirm_results[token] = confirmed
        event = self._confirm_events.get(token)
        if event:
            event.set()
        else:
            logger.warning("resume(): no event found for token=%s (already resolved or timed out?)", token[:8])

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
                    for s in self.working._steps
                    if s["action"] == "tool"
                ]
                self.results.add_result(
                    goal=self.working._goal,
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
    def _parse_json(text: str) -> Optional[dict]:
        """Extract and parse the first JSON object found in the text."""
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass
        return None

