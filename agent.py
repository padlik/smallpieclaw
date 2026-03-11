"""
agent.py — ReAct-style agent controller.

Loop: goal → reason → choose tool → execute → observe → repeat → finish
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Callable

import config
import llm_client
from memory import Memory
from tool_creator import ToolCreationError, ToolCreator
from tool_executor import ExecutionError, ToolExecutor
from tool_index import ToolIndex
from tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── system prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an autonomous system agent running on a Raspberry Pi.
You have access to a set of tools (shell and Python scripts) that you can call to gather information or perform actions.

RESPONSE FORMAT — you MUST reply with a single JSON object, nothing else:

To call a tool:
{"action": "tool_name", "args": {"ENV_VAR": "value"}, "thought": "why you chose this tool"}

To create a new tool (if no suitable tool exists):
{"action": "create_tool", "name": "snake_case_name", "language": "bash" or "python", "code": "full script body including # description: comment", "thought": "why"}

To finish:
{"action": "finish", "answer": "your final answer to the user", "thought": "summary"}

Rules:
- Always prefer existing tools over creating new ones.
- Generated tool scripts MUST start with a comment: # description: <one line>
- Keep answers concise and factual.
- Never make up tool results.
"""


@dataclass
class AgentResult:
    answer: str
    steps: int
    tool_calls: list[str] = field(default_factory=list)
    created_tools: list[str] = field(default_factory=list)
    error: str | None = None


class Agent:
    def __init__(
        self,
        registry: ToolRegistry,
        index: ToolIndex,
        executor: ToolExecutor,
        creator: ToolCreator,
        memory: Memory,
    ):
        self._registry = registry
        self._index = index
        self._executor = executor
        self._creator = creator
        self._memory = memory

    # ── public entry point ────────────────────────────────────────────────────

    def run(self, goal: str, status_cb: Callable[[str], None] | None = None) -> AgentResult:
        """
        Run the ReAct loop for a user goal.
        status_cb: optional callback to stream intermediate status messages.
        """
        logger.info("Agent goal: %s", goal)
        result = AgentResult(answer="", steps=0)
        messages = self._build_initial_messages(goal)

        for step in range(1, config.MAX_STEPS + 1):
            result.steps = step
            logger.debug("Step %d", step)

            try:
                action_obj = llm_client.chat_json(messages)
            except Exception as e:
                logger.error("LLM error at step %d: %s", step, e)
                result.error = str(e)
                result.answer = f"I encountered an LLM error: {e}"
                break

            action = action_obj.get("action", "finish")
            thought = action_obj.get("thought", "")
            if thought and status_cb:
                status_cb(f"💭 {thought}")

            # ── finish ─────────────────────────────────────────────────────────
            if action == "finish":
                result.answer = action_obj.get("answer", "[No answer provided]")
                break

            # ── create_tool ────────────────────────────────────────────────────
            if action == "create_tool":
                obs = self._handle_create_tool(action_obj, result, status_cb)

            # ── tool call ──────────────────────────────────────────────────────
            else:
                obs = self._handle_tool_call(action, action_obj.get("args", {}), result, status_cb)

            # Append observation to conversation and continue
            messages.append({"role": "assistant", "content": str(action_obj)})
            messages.append({"role": "user", "content": f"Observation: {obs}"})

        else:
            # Exhausted max steps
            result.answer = (
                "I reached the maximum number of reasoning steps without finishing. "
                "Here is the last observation:\n" + (messages[-1]["content"] if messages else "")
            )

        return result

    # ── step handlers ─────────────────────────────────────────────────────────

    def _handle_tool_call(
        self,
        tool_name: str,
        args: dict,
        result: AgentResult,
        status_cb: Callable | None,
    ) -> str:
        if status_cb:
            status_cb(f"🔧 Running tool: `{tool_name}`")
        result.tool_calls.append(tool_name)
        try:
            obs = self._executor.run(tool_name, args)
        except ExecutionError as e:
            obs = f"[Tool error] {e}"
        logger.debug("Tool '%s' output: %s", tool_name, obs[:200])
        return obs

    def _handle_create_tool(
        self,
        action_obj: dict,
        result: AgentResult,
        status_cb: Callable | None,
    ) -> str:
        name = action_obj.get("name", "unnamed_tool")
        language = action_obj.get("language", "bash")
        code = action_obj.get("code", "")
        if status_cb:
            status_cb(f"🛠️ Creating new tool: `{name}` ({language})")
        try:
            tool = self._creator.create(name, language, code)
            result.created_tools.append(tool.name)
            obs = f"Tool '{tool.name}' created successfully. Running it now …"
            # Auto-execute the freshly created tool
            try:
                output = self._executor.run(tool.name)
                obs += f"\n{output}"
                result.tool_calls.append(tool.name)
            except ExecutionError as e:
                obs += f"\n[Execution error] {e}"
        except ToolCreationError as e:
            obs = f"[Tool creation failed] {e}"
        return obs

    # ── message construction ──────────────────────────────────────────────────

    def _build_initial_messages(self, goal: str) -> list[dict]:
        relevant_tools = self._index.search(goal, top_k=3)

        tools_text = ""
        if relevant_tools:
            tools_text = "\n\nAvailable tools (most relevant to your goal):\n"
            for t in relevant_tools:
                tools_text += f"  - {t.name}: {t.description}\n"
        else:
            tools_text = "\n\nNo pre-built tools seem relevant. You may create a new tool."

        memory_text = self._memory.to_prompt_snippet()
        if memory_text:
            memory_text = f"\n\n{memory_text}"

        user_content = f"Goal: {goal}{tools_text}{memory_text}"

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
