# sub-agent-context Specification

## Purpose
Define transient parent context passed to spawned sub-agents.

## Requirements

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

When the operator spawns an on-demand sub-agent without an explicit `context_payload`, the parent agent SHALL automatically generate a summary of the current user goal, last 2 tool results, and relevant memory entries as the child's default transient `context_payload`. Scheduler-owned launches MUST NOT use this automatic parent summary or read the operator's current working memory, KV memory, or result recall; they receive only their job task and optional saved job conversation.

#### Scenario: Automatic context for sub-agent
- **GIVEN** an operator turn has checked CPU while investigating system health
- **WHEN** it calls `spawn_agent` without `context_payload`
- **THEN** the child receives a brief parent summary such as `User asked to check system health. Already checked CPU (load: 0.45). Next: check memory.`

#### Scenario: Scheduled launch has no operator parent summary
- **GIVEN** the operator is partway through a different task
- **WHEN** a scheduled job starts without a context payload
- **THEN** the job receives no automatic operator-goal, recent-tool-result, or operator-memory summary
- **AND** it uses its own saved conversation only when `preserve_context` is enabled

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

### Requirement: Context payload and supervision channel separation

Model-facing on-demand sub-agent context payloads SHALL remain separate from internal supervision controls used for scheduling, notification, result logging, and cleanup. Scheduled runs MUST use their job-owned input rather than deriving a parent payload from the operator's ambient state or from internal controls.

#### Scenario: Model-provided context remains model-facing
- **GIVEN** the model calls `spawn_agent` with a valid `context_payload`
- **WHEN** an on-demand child is spawned
- **THEN** the payload is treated as transient parent context for the child's prompt
- **AND** scheduler/internal supervision controls are not inferred from that payload

#### Scenario: Internal supervision controls are not context payload
- **GIVEN** a scheduled job launches a supervised run with job metadata and callbacks
- **WHEN** the job constructs its prompt context
- **THEN** scheduler/internal control values are not injected into a `PARENT CONTEXT` section
- **AND** the on-demand context payload size, auto-summary, prompt injection, and persistence-exclusion rules remain unchanged for on-demand children
