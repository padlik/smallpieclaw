## ADDED Requirements

### Requirement: Scheduled execution owns its context and memory boundary

Each cron, once, and `run_now` **scheduled run** SHALL use **job-owned context** only: its job task and, when `preserve_context` is enabled, that job's saved conversation. It MUST NOT receive the operator's working memory, KV or graph memory, result recall, or history from other jobs. Scheduled output/history MUST NOT be automatically inserted into the operator's prompt or result recall. This requirement does not restrict shared files or default-available log introspection tools.

#### Scenario: Scheduled job runs while operator is active
- **GIVEN** an operator turn is running and a scheduled job is due
- **WHEN** the job starts and reads its prompt context
- **THEN** it sees only its job task and its own saved conversation when enabled
- **AND** it does not receive the operator's live tool steps, memory, recall, or other job history

#### Scenario: Scheduler-triggered and manual executions use the same boundary
- **GIVEN** a job exists with `preserve_context` enabled
- **WHEN** it runs from cron, once, or `run_now`
- **THEN** each execution uses the job-owned context boundary
- **AND** `run_now` remains asynchronous and subject to its existing overlap policy

#### Scenario: Scheduled memory tools cannot reach operator memory
- **GIVEN** a scheduled run is active
- **WHEN** it invokes `memory_write` with get, set, append, or delete, or invokes `memory_graph_search` or `memory_graph_store`
- **THEN** the tool returns `scheduled_memory_unavailable` without reading or writing operator memory
- **AND** no new persistent job KV or graph store is created

#### Scenario: Scheduled output does not enter operator recall
- **GIVEN** a scheduled run completes while the operator is working
- **WHEN** the operator's next prompt is built
- **THEN** neither the job result nor a scheduled-job-history summary is automatically included
- **AND** the result remains available only through job-owned history and authorised presentation

### Requirement: Scheduled approval attempts fail without operator interaction

Scheduled executions SHALL retain the existing default path/tool boundary and depth-1 shell restriction. After normal path classification, any operation that requires operator approval MUST fail promptly with an actionable noninteractive-denial result, before consulting the operator grant ledger, staging confirmation tokens, or sending a confirmation message. This policy applies even when an operator turn and session grants are active, including confirmation paths outside ordinary file tools.

#### Scenario: Default-allowed file operation succeeds without a prompt
- **GIVEN** a scheduled run attempts a file operation within an allowed default boundary
- **WHEN** the path policy permits the operation without confirmation
- **THEN** the operation follows existing tool behavior without requesting a grant

#### Scenario: Outside-boundary request is denied while operator is active
- **GIVEN** an operator turn is active with a session-lifetime file grant
- **WHEN** a scheduled run attempts an otherwise confirmable operation outside default-allowed boundaries
- **THEN** the job receives an actionable noninteractive denial without using the grant
- **AND** no Telegram approval prompt or pending confirmation token is created

#### Scenario: Other confirmation route cannot prompt
- **GIVEN** a scheduled run attempts a vision, MCP, or sensitive built-in operation that would require operator confirmation
- **WHEN** the confirmation boundary is reached
- **THEN** the attempt is denied without prompting or borrowing the interactive coordinator

#### Scenario: Prohibited path and shell rules remain unchanged
- **GIVEN** a scheduled run attempts a prohibited path or depth-1 shell command
- **WHEN** its existing security boundary classifies the operation
- **THEN** the existing prohibited-path or depth-1 shell denial applies

#### Scenario: No scheduled executor fails closed
- **GIVEN** an agentic scheduled job is due but a scheduled-profile executor is unavailable
- **WHEN** the scheduler would otherwise invoke its direct-agent fallback
- **THEN** it does not execute the singleton operator agent
- **AND** it reports a launch failure through the existing launch-failure alert policy

### Requirement: Scheduled runs have distinct complete execution records

Each admitted agentic **scheduled run** SHALL have a distinct `run-<uuid4 hex>` execution ID, minted at admission after overlap and empty-task checks and unrelated to its agent ID or operator prompt ID. Its retained **job execution record** SHALL contain complete untruncated returned text, terminal status, timestamp, job tag, and applicable failure classification/next-run metadata. Skipped triggers and empty-task reminders SHALL NOT create agentic execution records.

#### Scenario: Parallel executions have distinct result IDs
- **GIVEN** two parallel runs of the same job are admitted
- **WHEN** they complete in either order
- **THEN** each has its own complete, independently retrievable record
- **AND** the job remains `is_running=true` until both runs finish
- **AND** preserved-context updates retain the existing parallel last-writer-wins risk

#### Scenario: Failed launch is recorded but a skipped trigger is not
- **GIVEN** a scheduled job is eligible to fire
- **WHEN** its agentic launch fails after admission
- **THEN** a failed execution record has the admitted run ID and the launch error
- **BUT** an overlap-policy skip receives neither a run ID nor an execution record
- **AND** the failed launch is reported through the direct launch-failure alert path independently of `notify`

### Requirement: Job execution history is bounded and expiry-aware on read

**Job execution record** history SHALL keep at most `execution_log_max_per_job` entries per job tag (default 10), each for at most `execution_log_max_age_hours` (default 48). Reads MUST enforce these limits even without another write, so an expired or over-limit record is unavailable even if the file has not rotated.

#### Scenario: Old truncated history is not represented as complete
- **GIVEN** a retained pre-change record has a boolean `success` and no run ID
- **WHEN** history is displayed
- **THEN** `success=true` appears as legacy/partial done and `success=false` as legacy/partial failed
- **AND** no run ID or missing result text is fabricated

#### Scenario: Read-time expiry and tag reuse
- **GIVEN** a once-job is removed after execution and a new job later reuses its tag
- **WHEN** the operator reads retained history for that tag
- **THEN** retained runs of both definitions are distinguished by execution IDs and timestamps
- **AND** a result past `execution_log_max_age_hours` (default 48) or outside `execution_log_max_per_job` (default 10) entries per tag is unavailable even if the file has not rotated since

#### Scenario: History write fails
- **GIVEN** an execution has finished but its full outcome cannot be persisted
- **WHEN** the record write fails
- **THEN** the persistence failure is always logged and its dedicated `last_error` marker overwrites any previous job `last_error` value
- **AND** the actual run outcome, including any real execution failure, remains in logs and, only when `notify=true`, the run-result notice
- **AND** a removed once-job's persistence-failure marker remains available through retained scheduler state
- **AND** no response claims full Details is available
- **AND** a later successful history write clears `last_error` only if it still contains the persistence-failure marker; otherwise it preserves the current value

### Requirement: Scheduled result notices are deferred and silent

When a job has `notify=true`, terminal run-result notices SHALL be queued until the **operator turn** response has been sent and typing has stopped, then delivered silently through Telegram. Successful, failed, and explicitly cancelled results SHALL be handled. A scheduler-origin timeout cancellation classified as such SHALL retain the existing stale-notification suppression; this does not define or add a scheduler timeout path.

#### Scenario: Result arrives during an operator turn
- **GIVEN** a scheduled run completes while the operator is typing a response
- **WHEN** its `notify=true` result becomes available
- **THEN** no result notice interrupts that active turn
- **AND** a soundless notice is sent after the final response and typing stop

#### Scenario: Operator stops a scheduled run
- **GIVEN** a scheduled run has `notify=true`
- **WHEN** `/stop` cancels it while an operator turn is active
- **THEN** `/stop` still confirms cancellation immediately
- **AND** the scheduled cancellation result notice waits until that turn is idle and is sent without sound

### Requirement: Notice suppression and direct alerts preserve their existing scopes

`notify=false` SHALL suppress all terminal run-result notices but SHALL NOT suppress recording. Launch-failure alerts, empty-task reminders, and long-running-agent warnings SHALL keep their existing direct delivery behavior. Notice delivery is best-effort; history is authoritative.

#### Scenario: Muted job still retains failed result
- **GIVEN** a job has `notify=false`
- **WHEN** a run fails
- **THEN** the classified failure is retained in job history
- **AND** no run-result notification is sent

#### Scenario: Non-result alerts retain existing behavior
- **GIVEN** a job cannot launch, an empty-task reminder is due, or a long-running-agent warning is raised
- **WHEN** the scheduler reports that event
- **THEN** its existing direct alert/reminder path is used rather than the run-result idle queue

#### Scenario: Restart loses queued notice but not retained outcome
- **GIVEN** a result is recorded and its notice is queued while an operator turn is active
- **WHEN** the bot restarts before sending the notice
- **THEN** the retained result remains available through `/jobs history` and `/jobs result`
- **AND** no exactly-once Telegram delivery is claimed

### Requirement: Scheduled result notices are expandable or compact with Details

The Telegram notice SHALL show a short summary and an HTML expandable quotation containing the result when its rendered text fits the 4,096-character message limit. Otherwise it SHALL show a compact notice with an authorised Details control resolving the retained execution ID.

#### Scenario: Short result expands in place
- **GIVEN** a retained result and summary fit in one Telegram message after entity parsing
- **WHEN** the queued notice is sent
- **THEN** the summary is visible outside an expandable blockquote containing the result
- **AND** the message is sent without notification sound

#### Scenario: Persisting the result fails
- **GIVEN** a run has `notify=true` but full-result storage failed
- **WHEN** the quiet notice is delivered
- **THEN** it states that the result was not retained and Details are unavailable

### Requirement: Full result delivery is complete and read-only

A full result requested through Details or `/jobs result` SHALL be sent as text when it fits the Telegram message limit, or as a complete UTF-8 document when it does not. Neither action SHALL invoke the operator agent or add to its context. Unknown or expired execution IDs MUST give an unavailable response.

#### Scenario: Long result is delivered completely on request
- **GIVEN** a result exceeds Telegram's rendered message limit
- **WHEN** its notice is sent and an authorised operator requests Details
- **THEN** a compact notice links to that execution ID
- **AND** the full retained result is delivered as a UTF-8 document without truncation or an agent turn

#### Scenario: Details has expired
- **GIVEN** a notice points to a result no longer retained
- **WHEN** the operator requests Details
- **THEN** the operator sees that the result is unavailable
- **AND** no operator memory or prompt is accessed
