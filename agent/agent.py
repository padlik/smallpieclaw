import json
import os
import re
from typing import Dict, Optional, Tuple, List

import requests
import config

config_instance = config.config

from tool_index import index
from tool_registry import registry
from tool_execution import execute_tool
from tool_creator import create_tool

ACTION_RE = re.compile(r'^\s*{\s*"action"\s*:', re.IGNORECASE)

class LLMError(Exception):
    pass

def _llm_chat(messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
    base = config_instance.LLM_BASE_URL.rstrip("/")
    api_key = config_instance.LLM_API_KEY
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": config_instance.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 400
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise LLMError("Malformed LLM response")

def _extract_json(text: str) -> Dict:
    text = text.strip()
    m = ACTION_RE.search(text)
    if m:
        start = m.start()
        try:
            return json.loads(text[start:])
        except Exception:
            pass
    # fallback: try full
    try:
        return json.loads(text)
    except Exception:
        # last resort: try to find first {...}
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
            snippet = text[brace_start:brace_end+1]
            return json.loads(snippet)
        raise

def _build_system_prompt() -> str:
    mem_path = "memory.json"
    memory = {}
    if os.path.exists(mem_path):
        try:
            memory = json.loads(open(mem_path, "r", encoding="utf-8").read())
        except Exception:
            memory = {}
    prompt = f"""You are a lightweight remote-reasoning agent that orchestrates local tools on a Raspberry Pi.
Rules:
- You only choose and request tool executions via the provided JSON actions.
- Never reveal internal planning or chain-of-thought. Respond ONLY with the specified JSON.
- Keep outputs concise. If results are unclear, ask a clarifying question.
- Use finish when the user goal is satisfied or you cannot proceed.
Memory (injected):
{json.dumps(memory, indent=2)}
Allowed tools are provided dynamically. Each tool returns stdout/stderr and a return code.
"""
    return prompt

def _plan_action(query: str, tools_meta: List[Dict]) -> Dict:
    tools_text = "\n".join([f"- {t['name']}: {t['description']}" for t in tools_meta])
    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": f"User goal or question:\n{query}\n\nAvailable tools:\n{tools_text}\n\nRespond with a JSON object containing one of:\n{{\"action\":\"tool_name\",\"args\":{...}}}\n{{\"action\":\"create_tool\",\"name\":\"...\",\"language\":\"bash|python\",\"code\":\"...\",\"description\":\"...\"}}\n{{\"action\":\"finish\",\"result\":\"...\"}}\nDo not include any other text."}
    ]
    resp = _llm_chat(messages)
    return _extract_json(resp)

def _summarize_results(results: List[Dict]) -> str:
    lines = []
    for r in results:
        name = r.get("name", r.get("tool", "?"))
        rc = r.get("returncode")
        out = r.get("stdout", "")
        err = r.get("stderr", "")
        lines.append(f"[{name}] rc={rc}\n{out[:2000]}")
        if err:
            lines.append(f"[{name}] stderr:\n{err[:1000]}")
    return "\n".join(lines)

class Agent:
    def __init__(self):
        pass

    def discover_tools(self, query: str) -> List[Dict]:
        results = index.search(query, top_k=3)
        # Map to registry entries
        picked = []
        for r in results:
            meta = registry.get_tool(r["name"])
            if meta:
                picked.append(meta)
        # Fallback if no index built yet
        if not picked:
            tools = registry.list_tools()
            picked = tools[:3]
        return picked

    def run_goal(self, query: str, chat_id: Optional[int] = None, steps_override: Optional[int] = None) -> str:
        step_limit = steps_override or config_instance.MAX_STEPS
        observations: List[Dict] = []
        messages = [
            {"role": "system", "content": _build_system_prompt()}
        ]

        for step in range(step_limit):
            tools_meta = self.discover_tools(query)
            tools_text = "\n".join([f"- {t['name']}: {t['description']}" for t in tools_meta])

            user_content = f"Goal: {query}\nTools:\n{tools_text}\nObservations so far:\n{json.dumps(observations, indent=2)}\nRespond with a JSON object only."
            messages.append({"role": "user", "content": user_content})
            resp = _llm_chat(messages)
            try:
                action = _extract_json(resp)
            except Exception as e:
                return f"Failed to parse LLM action JSON: {e}\nLLM said:\n{resp}"

            act = action.get("action")
            if act == "finish":
                return action.get("result", "Task completed.")
            elif act == "create_tool":
                try:
                    meta = create_tool(
                        name=action["name"],
                        language=action["language"],
                        code=action["code"],
                        description=action.get("description", "")
                    )
                    # Update registry and index
                    registry.scan()
                    index.upsert_tool({"name": meta["name"], "description": meta["description"]})
                    observations.append({"type": "tool_created", "tool": meta["name"]})
                    # Execute immediately if requested
                    res = execute_tool(meta["name"], {})
                    observations.append({"type": "tool_result", "tool": meta["name"], "result": res})
                except Exception as e:
                    observations.append({"type": "error", "message": f"create_tool failed: {e}"})
                continue

            elif act and act not in ("tool_name", "finish", "create_tool"):
                observations.append({"type": "error", "message": f"Unknown action: {act}"})
                continue

            # act == "tool_name" or tool_name directly provided
            tool_name = act if act and act not in ("tool_name",) else action.get("tool") or (action.get("args", {}).get("tool"))
            # If action JSON provides direct "tool" field
            if not tool_name:
                # The JSON may be {"action":"tool_name", "args":{"tool":"..."}} or {"action":"check_disk"}
                if "tool" in action:
                    tool_name = action["tool"]
                else:
                    # fallback: action key equals a tool name
                    cand = [k for k in action.keys() if k not in ("action","args")]
                    if cand:
                        tool_name = cand[0]

            if not tool_name:
                observations.append({"type": "error", "message": "No tool specified in action."})
                continue

            # Execute tool safely
            args = action.get("args", {})
            try:
                res = execute_tool(tool_name, args)
                observations.append({"type": "tool_result", "tool": tool_name, "result": res})
            except Exception as e:
                observations.append({"type": "error", "message": f"Tool execution failed: {e}"})

        # Step limit reached
        summary = _summarize_results([r["result"] for r in observations if r.get("type") == "tool_result"])
        return f"Reached step limit ({step_limit}). Summary:\n{summary}"

