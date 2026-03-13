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
from typing import Any, Callable, Optional

from llm_client import LLMClient
from memory_store import MemoryStore
from tool_creator import ToolCreator
from tool_executor import ToolExecutor
from tool_index import ToolIndex

logger = logging.getLogger(__name__)

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

AVAILABLE TOOLS:
{tools}

RESPONSE FORMAT — you must ALWAYS respond with a single valid JSON object.
No markdown, no prose, just JSON.

Possible actions:

1. Execute a tool:
   {{"action": "tool", "tool": "<tool_name>", "args": {{}}}}

2. Create a new tool when the capability is missing:
   {{"action": "create_tool", "name": "<snake_case_name>", "language": "bash", "code": "<script>", "description": "<one line>"}}

3. Finish and return an answer to the user:
   {{"action": "finish", "result": "<your answer>"}}

Rules:
- Prefer existing tools over creating new ones.
- When creating a tool, write only safe, minimal shell or Python code.
- Never include dangerous commands (rm -rf /, sudo, eval, reverse shells, etc.).
- If a tool fails, try a different approach or explain the issue.
- Always end with a "finish" action.
""".strip()


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
        short_term=None,   # Optional[ShortTermMemory]
        working=None,      # Optional[WorkingMemory]
        long_term=None,    # Optional[LongTermMemory]
        results=None,      # Optional[ResultsMemory]
    ):
        self.llm = llm
        self.tool_index = tool_index
        self.executor = executor
        self.creator = creator
        self.memory = memory
        self.max_iterations = max_iterations
        self.top_tools = top_tools
        self.short_term = short_term
        self.working = working
        self.long_term = long_term
        self.results = results

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
                # Update short-term memory
                if self.short_term:
                    self.short_term.add("user", user_goal)
                    self.short_term.add("assistant", result)
                # Save to results memory
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
                _progress(f"🔧 Running tool: `{tool_name}`")
                outcome = self.executor.execute(tool_name, args)
                if self.working:
                    self.working.add_step("tool", {"tool": tool_name, "args": args, "success": outcome["success"]})
                tool_result = self._format_tool_result(tool_name, outcome)
                logger.info("Tool '%s' result: success=%s", tool_name, outcome["success"])
                messages.append({"role": "user", "content": tool_result})

            elif action == "create_tool":
                tool_name = action_obj.get("name", "unnamed_tool")
                language = action_obj.get("language", "bash")
                code = action_obj.get("code", "")
                description = action_obj.get("description", "")
                _progress(f"🛠️ Creating new tool: `{tool_name}`")
                result = self.creator.create(tool_name, language, code, description)
                if self.working:
                    self.working.add_step("create_tool", {"name": tool_name, "success": result["success"]})
                if result["success"]:
                    feedback = (
                        f"Tool '{result['name']}' was created successfully at {result['path']}. "
                        "You can now use it with the 'tool' action."
                    )
                    _progress(f"🔧 *Tool Created:* `{result['name']}`\n✅ Status: Success\n📝 Description: {description}")
                else:
                    feedback = f"Tool creation failed: {result['error']}"
                    _progress(f"🔧 *Tool Creation Failed:* `{tool_name}`\n❌ Error: {result['error']}")
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
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _format_tools(tools) -> str:
        if not tools:
            return "No tools available."
        lines = [f"  {t.name}: {t.description}" for t in tools]
        return "\n".join(lines)

    @staticmethod
    def _format_tool_result(tool_name: str, outcome: dict) -> str:
        if outcome["success"]:
            output = outcome["output"] or "(no output)"
            return f"Tool '{tool_name}' succeeded:\n{output}"
        else:
            parts = [f"Tool '{tool_name}' failed (exit {outcome['exit_code']})."]
            if outcome["error"]:
                parts.append(f"stderr: {outcome['error']}")
            if outcome["output"]:
                parts.append(f"stdout: {outcome['output']}")
            return "\n".join(parts)

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        """Extract and parse the first JSON object found in the text."""
        text = text.strip()
        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Strip markdown code fences
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass
        # Find first {...} block
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass
        return None
