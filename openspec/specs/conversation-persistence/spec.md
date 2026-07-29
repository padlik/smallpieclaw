### Requirement: Main agent chat history persists across process restarts

The main agent's `short_term` (chat history) MUST be serialized to `~/.local/state/<agent>/conversations/<conversation_id>.json` on process shutdown and on `/reset` (save variant). The save MUST be atomic — a crash during save MUST NOT produce a partial or corrupted file. On startup, if a `conversation_id` file exists at `~/.local/state/<agent>/conversation_id` and a corresponding `conversations/<id>.json` exists, the agent MUST load it into `ShortTermMemory` and pass it to `AgentController(short_term=loaded)`. If either file is missing or corrupted, the agent starts with a fresh `ShortTermMemory`. Working memory (`WorkingMemory`) is NEVER persisted — restart always starts a fresh task.

Feature: conversation-persistence
Rule: Only `short_term` (chat history) is persisted. `working` (task state) is always fresh on restart.

#### Scenario: Chat history survives a clean restart
- **GIVEN** the agent has been running with a conversation containing several turns
- **WHEN** the agent process shuts down cleanly (SIGTERM, SIGINT, or normal exit) and restarts
- **THEN** the restarted agent loads the saved `short_term` from `conversations/<conversation_id>.json`
- **AND** the conversation history is available in the first `run()` call

#### Scenario: Working memory is not persisted
- **GIVEN** the agent is in the middle of a multi-step task (working memory has a goal and steps)
- **WHEN** the agent process restarts
- **THEN** `working` memory is empty (`WorkingMemory()` with no goal, no steps)
- **AND** the agent starts fresh on whatever the user says next

#### Scenario: Corrupted conversation file starts fresh
- **GIVEN** the `conversations/<conversation_id>.json` file is corrupted (invalid JSON)
- **WHEN** the agent starts up
- **THEN** a warning is logged
- **AND** the agent starts with a fresh `ShortTermMemory`
- **AND** no crash occurs

#### Scenario: Missing conversation_id starts a new conversation
- **GIVEN** the `conversation_id` file does not exist (first startup or deleted)
- **WHEN** the agent starts up
- **THEN** a new `conversation_id` is generated (12 hex chars)
- **AND** it is persisted to the `conversation_id` file
- **AND** the agent starts with a fresh `ShortTermMemory`

### Requirement: conversation_id is generated, persisted, and rotated on /reset

A `conversation_id` (12-char hex string from `uuid4().hex[:12]`) MUST be persisted to `~/.local/state/<agent>/conversation_id` (a plain text file). On first startup, a new id is generated and written. On normal restart, the existing id is read and used. On `/reset` (both save and discard variants), a new id is generated and written, and `builtin_executor.conversation_id` is updated. Sub-agents inherit the main conversation's `conversation_id` — they do not get a separate id.

Feature: conversation-persistence

#### Scenario: /reset save rotates the conversation_id
- **GIVEN** the agent has an active conversation with id `abc123def456`
- **WHEN** the user runs `/reset` (save variant)
- **THEN** the current `short_term` is saved to `conversations/abc123def456.json`
- **AND** a new `conversation_id` is generated and written to the `conversation_id` file
- **AND** `builtin_executor.conversation_id` is updated to the new id
- **AND** `short_term` and `working` are cleared

#### Scenario: /reset discard rotates without saving
- **GIVEN** the agent has an active conversation with id `abc123def456`
- **WHEN** the user runs `/reset discard`
- **THEN** the current `short_term` is NOT saved to `conversations/abc123def456.json`
- **AND** a new `conversation_id` is generated and written to the `conversation_id` file
- **AND** `builtin_executor.conversation_id` is updated to the new id
- **AND** `short_term` and `working` are cleared

#### Scenario: Sub-agents share the main conversation's id
- **GIVEN** the main agent has `conversation_id` set to `abc123def456`
- **WHEN** a sub-agent is spawned
- **THEN** the sub-agent's shell calls use the same `conversation_id` for `session_logs` path computation
- **AND** the sub-agent does not generate its own `conversation_id`

### Requirement: Hard-crash gap is accepted

If the agent process is killed by SIGKILL, OOM, or a power loss, the `finally:` block MUST NOT run and the unsaved `short_term` tail is lost. This gap is accepted and matches the existing sub-agent precedent (sub-agents save context only on completion, not periodically).

Feature: conversation-persistence
Rule: No periodic checkpoint is implemented in this change. The gap is documented, not closed.

#### Scenario: SIGKILL loses unsaved conversation tail
- **GIVEN** the agent has been running with new conversation turns since the last save
- **WHEN** the agent process is killed by SIGKILL (or OOM, or power loss)
- **THEN** the `finally:` block does not run
- **AND** the unsaved conversation turns are lost
- **AND** on next startup, the agent loads the last successfully saved `short_term` (if any)
