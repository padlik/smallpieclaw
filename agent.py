import json
from typing import List, Dict, Optional
from llm_client import LLMClient
from tool_registry import ToolRegistry
from tool_index import ToolIndex
from tool_executor import ToolExecutor
from tool_creator import ToolCreator
from memory import Memory

class Agent:
    def __init__(self):
        self.llm = LLMClient()
        self.memory = Memory()
        self.registry = ToolRegistry()
        self.index = ToolIndex(self.registry)
        self.executor = ToolExecutor(self.registry)
        self.creator = ToolCreator(self.registry, self.executor)
        
        # Ensure index is built
        if not self.index.index:
            self.index.rebuild_index()
    
    def process_goal(self, goal: str) -> str:
        """Main ReAct loop"""
        step = 0
        context = []
        final_response = ""
        
        # Find relevant tools
        relevant_tools = self.index.search(goal, top_k=3)
        tools_context = self._format_tools(relevant_tools)
        
        system_prompt = f"""You are a lightweight AI agent running on Raspberry Pi.
You have access to tools. Use this format:
{{
    "action": "tool_name" or "create_tool" or "finish",
    "args": {{}},
    "thought": "your reasoning"
}}

Available tools:
{tools_context}

System Memory:
{self.memory.get_context()}

Rules:
- Use tools to gather information
- If a tool doesn't exist and you need it, use create_tool
- Max steps: {Config.MAX_AGENT_STEPS}
- Be concise due to limited context"""

        while step < Config.MAX_AGENT_STEPS:
            step += 1
            
            user_prompt = f"Goal: {goal}\nStep: {step}\nPrevious context: {json.dumps(context)}\nWhat do you do next?"
            
            try:
                response = self.llm.chat(system_prompt, user_prompt, json_mode=True)
                action_data = json.loads(response)
                
                action = action_data.get('action', 'finish')
                args = action_data.get('args', {})
                thought = action_data.get('thought', 'No thought provided')
                
                context.append({
                    "step": step,
                    "thought": thought,
                    "action": action
                })
                
                if action == 'finish':
                    final_response = args.get('message', 'Task completed')
                    break
                
                elif action == 'create_tool':
                    # Create new tool
                    name = args.get('name')
                    language = args.get('language', 'bash')
                    code = args.get('code', '')
                    description = args.get('description', 'Generated tool')
                    
                    success, msg = self.creator.create_tool(name, language, code, description)
                    
                    # Update index with new tool
                    self.index.rebuild_index()
                    
                    context.append({
                        "step": step,
                        "tool_result": msg,
                        "success": success
                    })
                    
                    if success:
                        # Execute the newly created tool immediately
                        result = self.executor.execute(name)
                        context.append({
                            "step": step,
                            "tool_execution": result
                        })
                
                elif action in self.registry.tools:
                    # Execute existing tool
                    result = self.executor.execute(action, args)
                    context.append({
                        "step": step,
                        "tool_execution": result
                    })
                    
                    if step == Config.MAX_AGENT_STEPS - 1:
                        # Force finish on last step
                        final_prompt = f"Goal: {goal}\nContext: {json.dumps(context)}\nProvide final response:"
                        final_resp = self.llm.chat(system_prompt, final_prompt, json_mode=True)
                        try:
                            final_data = json.loads(final_resp)
                            final_response = final_data.get('args', {}).get('message', final_resp)
                        except:
                            final_response = final_resp
                        break
                else:
                    context.append({
                        "step": step,
                        "error": f"Unknown action: {action}"
                    })
                    
            except Exception as e:
                final_response = f"Error in agent loop: {str(e)}"
                break
        
        # Update memory with task completion
        self.memory.set(f"last_task_{hash(goal) % 10000}", {
            "goal": goal,
            "steps": step,
            "completed": True
        })
        
        return final_response if final_response else "Task completed without response"
    
    def _format_tools(self, tool_names: List[str]) -> str:
        lines = []
        for name in tool_names:
            tool = self.registry.get_tool(name)
            if tool:
                lines.append(f"- {name}: {tool['description']}")
        return "\n".join(lines)
    
    def get_status(self) -> str:
        return f"""Agent Status:
Memory usage: Low
Tools loaded: {len(self.registry.tools)}
Index size: {len(self.index.index)}
Last memory update: {self.memory.get('last_backup_date', 'Never')}"""

