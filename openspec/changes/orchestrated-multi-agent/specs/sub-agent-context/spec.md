## ADDED Requirements

### Requirement: Parent context payload
The `spawn_agent` tool SHALL accept a new optional `context_payload` parameter (object). This payload is injected into the sub-agent's system prompt as a `PARENT CONTEXT` section.

#### Scenario: Parent shares conversation summary
- **WHEN** the parent agent spawns a sub-agent with `context_payload: {"conversation_summary": "User asked about disk space", "relevant_memory": ["disk_warning: 90% full"]}`
- **THEN** the sub-agent's system prompt includes a `PARENT CONTEXT` section with this information

### Requirement: Context payload size limits
The `context_payload` SHALL be truncated to a configurable maximum size (default 2000 chars) to prevent token overflow. Truncation preserves key names and truncates values.

#### Scenario: Large payload truncated
- **WHEN** a `context_payload` exceeds 2000 characters
- **THEN** values are truncated with `...` ellipsis, preserving the key names and structure

### Requirement: Automatic context summarization
When spawning a sub-agent without an explicit `context_payload`, the parent agent SHALL automatically generate a summary of: the current user goal, last 2 tool results, and any relevant memory entries. This summary is injected as the default `context_payload`.

#### Scenario: Automatic context for sub-agent
- **WHEN** spawn_agent is called without `context_payload`
- **THEN** the system generates a brief summary: `User asked to check system health. Already checked CPU (load: 0.45). Next: check memory.`

### Requirement: Sub-agent prompt variant
When a sub-agent is spawned as part of a plan execution, it SHALL receive the `prompts/sub-agent/` prompt variant (simpler, focused on task execution) rather than the full `prompts/system/` prompt.

#### Scenario: Plan sub-agent gets focused prompt
- **WHEN** step 3 of a plan spawns a sub-agent
- **THEN** the sub-agent loads `prompts/sub-agent/01-identity.md`, `prompts/sub-agent/02-task.md`, etc., not the full system prompt

### Requirement: Context payload excluded from sub-agent persistence
The `context_payload` SHALL NOT be saved to the sub-agent's `context_key` persistence file. It is transient, per-invocation context.

#### Scenario: Context does not leak across invocations
- **WHEN** a sub-agent with `context_key="health-check"` is spawned twice with different `context_payload`
- **THEN** the persisted context at `data/job_contexts/health-check.json` does not contain either payload
