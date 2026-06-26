---
section: task
order: 2
required: true
mode: all
variables:
  - parent_context
  - task
---

PARENT CONTEXT (injected by the parent agent):
{{parent_context}}

YOUR TASK:
{{task}}

If a skill is referenced, read its SKILL.md with file_read first, then follow the
instructions inside. Any tools or sub-commands described in a SKILL.md are not registered
tools — implement them with shell or file_read as needed.
