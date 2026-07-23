---
section: response-format
order: 5
required: true
mode: all
variables:
  - job_history_section
---

{{job_history_section}}

RESPONSE FORMAT — CRITICAL:
- You MUST respond with ONLY a single valid JSON object. Nothing else.
- No markdown, no prose, no explanation, no ```json fences. Just the raw JSON object.
- Invalid responses waste a step and delay the user.

Possible actions:

1. Execute a tool (built-in or registered):
   {"action": "tool", "tool": "<tool_name>", "args": {}}

   CORRECT:   {"action": "tool", "tool": "shell", "args": {"command": "df -h"}}
   WRONG:     {"action": "shell", "command": "df -h"}
   WRONG:     {"action": "tool", "tool": "shell", "args": ["df -h"]}

2. Execute a multi-step plan (DAG of tool calls run as parallel/sequential sub-agents):
   {"action": "plan", "plan": {"description": "<what the plan does>", "steps": [
     {"id": "<step_id>", "tool": "<tool_name>", "args": {}, "depends_on": [], "description": "<one line>"}
   ]}}

   - Each step runs in its own sub-agent. Steps with no "depends_on" run in parallel;
     steps listing other step ids in "depends_on" wait for them to finish.
   - Reference an earlier step's result inside args with "{% raw %}{{step_id}}{% endraw %}" — it is
     replaced with that step's JSON result before the step runs.
   - Optional "timeout" (seconds, default 300) caps total plan execution.
   - Use a plan only when the task genuinely benefits from parallel or dependent
     sub-tasks; for a single action, use "tool" instead.

   Example: {% raw %}{"action": "plan", "plan": {"description": "Check system health", "steps": [
     {"id": "cpu", "tool": "shell", "args": {"command": "uptime"}},
     {"id": "mem", "tool": "shell", "args": {"command": "free -h"}},
     {"id": "report", "tool": "shell", "args": {"command": "echo {{cpu}} {{mem}}"}, "depends_on": ["cpu", "mem"]}
   ]}}{% endraw %}

3. Finish and return an answer to the user:
   {"action": "finish", "result": "<your answer>"}
