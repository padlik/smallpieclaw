---
section: response-format
order: 4
required: true
mode: all
variables: []
---

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

2. Finish and return an answer to the parent agent:
   {"action": "finish", "result": "<your answer>"}

Rules:
- Always end with a "finish" action.
- Keep output concise and in the exact format requested by the parent task.
