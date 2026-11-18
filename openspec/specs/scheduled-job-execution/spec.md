# scheduled-job-execution Specification

## Purpose
Defines bounded, isolated scheduled job execution: each run owns its context, handles approvals noninteractively, keeps complete retained records, and presents results quietly in Telegram.

## Requirements
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

### Requirement: Scheduler configuration never blocks bot startup

Scheduler construction, load, start, and registration SHALL NOT raise into or block bot startup, including malformed scheduler files, numeric or local-setting casts, job shapes, state or storage failures, and registration failures. Whole-scheduler failures SHALL be contained to the scheduler, logged, and shown to the operator while commands, Telegram, and model work stay ready.

#### Scenario: Unreadable or unparseable scheduler file at cold start
- **GIVEN** the scheduler file exists but cannot be read or parsed
- **WHEN** the bot starts
- **THEN** startup completes with no executable job map and commands remain ready
- **AND** the file bytes are preserved and mutations return the file error until the file parses

#### Scenario: Missing scheduler file at cold start
- **GIVEN** no scheduler file exists
- **WHEN** the bot starts
- **THEN** the scheduler has no jobs and no error
- **AND** the first successful mutation may create the file

#### Scenario: File failure is operator-visible
- **GIVEN** the scheduler load result is unreadable or unparseable
- **WHEN** startup completes and the operator lists or shows jobs
- **THEN** an actionable notice naming the file path, what failed, and what to do was sent once through the existing notify path
- **AND** the same notice stays visible in list and show responses without including file content

#### Scenario: Failed reload keeps the last good scheduler state
- **GIVEN** a valid map, raw document, hash, and registrations are active
- **WHEN** a reload finds the file unreadable, unparseable, or unstageable
- **THEN** the last good map, document, hash, and registrations remain untouched
- **AND** the caller receives an actionable failure and nothing is retried automatically

#### Scenario: Numeric cast and local setting errors are contained
- **GIVEN** a scheduler-local setting or a job numeric field is not castable
- **WHEN** the scheduler is constructed or loads the job
- **THEN** the failure is contained to the scheduler or that job and logged
- **AND** the bot starts and healthy jobs still load

#### Scenario: State, storage, and registration failures are contained
- **GIVEN** state loading, storage access, or a job registration raises
- **WHEN** the scheduler starts or reloads
- **THEN** the failure is logged and visible without aborting startup
- **AND** an unaffected job keeps its existing registration

### Requirement: Job configuration errors are contained to the offending job

A parseable job with a malformed or obsolete field SHALL keep its raw values and fail only itself: a bad or obsolete time or scheduling field leaves it visibly unscheduled with a config error; any other such field shows a config error and records a launch failure on firing. Obsolete fields SHALL NOT be converted, derived into a cron, or ignored, and their errors SHALL name the field and direct to `cron`, `once`, or key removal. Add and modify SHALL reject obsolete fields before `_on_attempt`.

#### Scenario: Bad scheduling field leaves a job unscheduled
- **GIVEN** a parseable job has an invalid schedule type, cron, `run_at`, or non-boolean `enabled`
- **WHEN** the scheduler loads it
- **THEN** raw fields are retained and a `config_error` is visible in list and show
- **AND** the job never fires and `run_now` returns the existing failure result with that error

#### Scenario: Obsolete legacy schedule is unscheduled with guidance
- **GIVEN** a job uses `schedule_type = "daily"` with `time`, or `schedule_type = "interval"` with `hours` or `minutes`
- **WHEN** the scheduler loads it
- **THEN** no cron is derived and the job is visibly unscheduled with a `config_error` directing the operator to rewrite it as `cron` or `once`
- **AND** the file is not rewritten

#### Scenario: Obsolete per-job fallback fails only that job
- **GIVEN** a scheduled job contains `fallback_models`
- **WHEN** the scheduler loads it and the job fires
- **THEN** list and show expose a `config_error` directing removal of `fallback_models`
- **AND** the firing is a recorded launch failure and no model fallback or retry on another model occurs

#### Scenario: Obsolete fields are refused on mutation
- **GIVEN** an add or modify supplies `schedule_type = "interval"`, `hours`, `minutes`, `time`, or `fallback_models`
- **WHEN** the mutation is validated
- **THEN** it is refused before `_on_attempt` with actionable guidance and no change

#### Scenario: Other bad field fails at fire time
- **GIVEN** a scheduled job has a wrong-typed `notify`, `preserve_context`, `context_max_messages`, or `max_iterations`, or a non-string `task`
- **WHEN** the job fires
- **THEN** a normal failed launch is recorded with the config error and the launch-failure alert is sent regardless of `notify`
- **AND** no value was coerced by `int()` or `bool()`

#### Scenario: Non-string task is not an empty reminder
- **GIVEN** one job has a non-string `task` and another has a blank-string `task`
- **WHEN** both fire
- **THEN** only the blank-string job takes the empty-task reminder path with no run ID
- **AND** the non-string job fails as a launch failure

#### Scenario: One bad job leaves siblings running
- **GIVEN** one job is malformed and others are healthy
- **WHEN** the scheduler loads and fires jobs
- **THEN** healthy jobs load, register, and run normally
- **AND** the bad job is contained and visible

#### Scenario: Invalid overlap policy skips while the job is active
- **GIVEN** a job with an invalid `overlap_policy` is already running
- **WHEN** it fires again
- **THEN** the trigger is skipped without cancelling the active run
- **AND** no execution is inferred from the invalid value

#### Scenario: Invalid overlap policy fails an idle job as a launch failure
- **GIVEN** a job with an invalid `overlap_policy` is not running
- **WHEN** it fires
- **THEN** the owned run ID and active tracking are minted and `last_run` is persisted
- **AND** the config error is reported through the normal failed-launch record, alert regardless of `notify`, and tracking release

#### Scenario: Job field checks are type-strict
- **GIVEN** a job field holds a value of the wrong type
- **WHEN** the field is checked
- **THEN** it SHALL be treated as bad without coercion

#### Scenario: Obsolete field set is fixed
- **GIVEN** a job uses `schedule_type` `daily` or `interval`, `time`, `hours`, `minutes`, or `fallback_models`
- **WHEN** the job is loaded or add/modify is validated
- **THEN** the field SHALL be obsolete and its config error SHALL name it and direct the operator to `cron` or `once` scheduling or to remove the key
- **AND** it SHALL NOT be converted, derived into a cron, or ignored
- **AND** add and modify SHALL reject it before `_on_attempt` with the same guidance

### Requirement: Saved job model selectors stay raw until execution

A job's saved `model` selector SHALL be stored and retained exactly as supplied and classified only at execution. Absent or `None` SHALL mean silent inherit of the main model on add, load, and execution. Modify with absent or `None` SHALL leave the saved value unchanged, and no clear operation SHALL exist. Add and modify SHALL NOT check catalog availability, and load SHALL NOT warn about or flag any `model` value.

#### Scenario: Add without a model inherits
- **GIVEN** a job is added with the model omitted or `None`
- **WHEN** the job is persisted
- **THEN** the `model` key is omitted from the stored document
- **AND** no warning is emitted

#### Scenario: Modify without a model changes nothing
- **GIVEN** a job has a saved model
- **WHEN** it is modified with the model omitted or `None`
- **THEN** the saved selector is unchanged
- **AND** no operation clears a saved selector

#### Scenario: Blank and non-string selectors are stored raw
- **GIVEN** add or modify supplies `""`, whitespace, or a TOML-representable non-string value
- **WHEN** the mutation succeeds
- **THEN** the exact raw value is stored without trimming, normalizing, stringifying, or dropping
- **AND** no load-time warning or `config_error` is produced

#### Scenario: Exact strings are preserved without trimming
- **GIVEN** a model string has padding or different case from a catalog entry
- **WHEN** it is added, modified, saved, and reloaded
- **THEN** the exact string is retained unchanged
- **AND** only execution classifies it

### Requirement: Selector evidence is bounded and redacted

Wherever a saved selector is shown, logged, or recorded before a run model is pinned, the scheduler SHALL use one bounded, redacted representation: `inherit` for absent, a classification label for blank and non-string values, and a length-bounded, secret-redacted string otherwise. After admission, execution records and notices SHALL store the pinned canonical model ID.

#### Scenario: Non-string selector content is never shown
- **GIVEN** a job's saved model is a table, array, number, boolean, or datetime
- **WHEN** list, show, a warning, or an execution record refers to it
- **THEN** only its classification label appears
- **AND** the value is never stringified

#### Scenario: Long or secret-bearing string is bounded and redacted
- **GIVEN** a saved model string is very long or contains a known secret
- **WHEN** it is shown or logged before pinning
- **THEN** the output is length-bounded and redacted
- **AND** the raw value in the document remains unchanged

#### Scenario: Records use the pinned ID after admission
- **GIVEN** a scheduled run was admitted with a pinned model
- **WHEN** its execution record or notice is written
- **THEN** it contains the pinned canonical model ID
- **AND** a launch that failed before the pin contains only the bounded representation

### Requirement: A pinned scheduled model never falls back

No scheduled run SHALL fall back to, substitute, or retry on another model at any stage. Selector errors, credential failures, adapter limitations, context rejections, inference errors, and provider outages SHALL fail that run as a normal job failure recorded in its **job execution record**, without affecting the daemon or other jobs.

#### Scenario: Credential or capability failure on a valid selector
- **GIVEN** a job names a configured connection whose credential cannot be resolved, or whose adapter cannot perform the request
- **WHEN** the job runs
- **THEN** it fails as a normal job failure
- **AND** main is not substituted

#### Scenario: Provider outage or oversized request does not retry elsewhere
- **GIVEN** a pinned scheduled run hits a provider outage, inference error, or exceeded context
- **WHEN** the error is handled
- **THEN** the run fails without a cross-model retry or whole-run replay
- **AND** the failure is recorded normally and other jobs keep running

### Requirement: Scheduled admission uses a trusted source and one root admission

The scheduler SHALL pass the raw saved selector through supervision with `SOURCE_SCHEDULED` options built only by scheduler code, exempting only the strict model-shape guard so scheduled selector classification applies. The root factory SHALL call one scheduled admission that resolves the selector against the fixed connection map, resolves credentials once, and returns an immutable pin; every other profile SHALL use ordinary direct admission.

#### Scenario: Model-facing spawn keeps its strict guard
- **GIVEN** a model-facing `spawn_agent` call supplies a blank or non-string `model`
- **WHEN** arguments are validated with default options
- **THEN** the shape guard rejects it before admission
- **AND** obsolete-argument rejection is unchanged

#### Scenario: Scheduled source exempts only the shape guard
- **GIVEN** a scheduler-built spawn with `SOURCE_SCHEDULED` and a raw selector
- **WHEN** arguments are validated
- **THEN** the shape guard does not reject the selector, and scheduled admission classifies it
- **AND** task, depth, capacity, obsolete-argument, context-key, and refusal checks still apply

#### Scenario: Selector is passed whenever the key is present
- **GIVEN** a job's raw `model` key is present and not `None`
- **WHEN** the scheduler builds spawn arguments
- **THEN** the raw object is passed through the existing `model` keyword with no new public argument, schema, or channel

#### Scenario: Exactly one admission per run
- **GIVEN** a scheduled run reaches the root factory
- **WHEN** the scheduled profile is constructed
- **THEN** one scheduled admission resolves the selector and credentials and returns an immutable pin
- **AND** runtime construction does not re-admit or resolve credentials again

#### Scenario: Other profiles keep ordinary admission
- **GIVEN** a main, on-demand, plan, or diagnostic run
- **WHEN** the root factory admits it
- **THEN** ordinary direct admission is used

#### Scenario: Scheduled admission performs no identity recheck
- **GIVEN** a scheduled admission is attempted
- **WHEN** admission resolves the selector and credentials
- **THEN** no connection fingerprint, configuration generation, selection revision, or document identity recheck SHALL be performed

### Requirement: Scheduled run lifecycle ordering is preserved around admission

For an agentic scheduled run the scheduler SHALL decide overlap, mint the run ID and active tracking, and persist `last_run` before spawn and model admission, and SHALL record the terminal result afterward. A launch or model-admission failure SHALL be recorded as a failed execution under that run ID, alert regardless of `notify`, and release tracking. No pre-spawn terminal row or trace SHALL be invented.

#### Scenario: Admission failure is a recorded launch failure
- **GIVEN** a run ID was minted and `last_run` persisted
- **WHEN** model admission fails
- **THEN** a failed execution record carries that run ID and the error
- **AND** the alert is sent even when `notify=false` and tracking is released in all cases

#### Scenario: No invented pre-spawn record
- **GIVEN** an overlap skip or empty-task reminder
- **WHEN** it is handled
- **THEN** no run ID, terminal row, or trace is created
- **AND** cancellation, callbacks, and ownership keep their existing behavior

#### Scenario: Cancellation around admission
- **GIVEN** a run is cancelled before or during admission
- **WHEN** it terminates
- **THEN** tracking is released and the record follows existing cancellation semantics

### Requirement: Scheduler mutations edit the parsed document by explicit patch

The scheduler SHALL write its file only for explicit operator mutations, each applying a patch of only the requested fields to a deep copy of the parsed raw document and re-emitting every type `tomli` returns. Parsed-value equality SHALL be NaN-aware and type-strict; comments and layout SHALL NOT be promised. `None` SHALL be represented by key omission, and load SHALL NOT rewrite the file, including when it contains obsolete fields.

#### Scenario: Untouched values survive a mutation
- **GIVEN** a document with strings containing control characters, booleans, integers, floats including `inf` and `nan`, datetimes, arrays, inline tables, and quoted keys
- **WHEN** one job is modified
- **THEN** the written document reparses equal for every untouched value, with booleans not equal to integers

#### Scenario: Comments and layout are not promised
- **GIVEN** the file has comments and custom layout
- **WHEN** a mutation commits
- **THEN** only parsed-value equality is guaranteed

#### Scenario: Load and cron derivation never write
- **GIVEN** a scheduler file containing an obsolete `daily` or `interval` schedule or `fallback_models`
- **WHEN** the scheduler loads or reloads
- **THEN** no cron is derived, the file is not written, and the obsolete values remain byte-for-byte

#### Scenario: Unrepresentable mutation is refused before the attempt hook
- **GIVEN** a mutation value is an arbitrary object, `None` inside a container, or a non-string table key
- **WHEN** validation runs
- **THEN** it is refused before `_on_attempt` with no change

### Requirement: Mutations prepare, verify freshness, and publish as one unit

Mutations SHALL revalidate their source document and generation under commit locks, discard and re-prepare stale candidates, then check disk freshness against the stored hash before an atomic write. Publication SHALL include the matching document, hash, runtime view, and registrations. Locks SHALL follow `_running_lock` then `_save_lock`, with `_history_lock` outermost only where already required.

#### Scenario: Registration preparation fails before write
- **GIVEN** a registration cannot be prepared
- **WHEN** the mutation prepares
- **THEN** it fails contained before anything is written with all state intact

#### Scenario: Candidate and registrations are prepared before the write
- **GIVEN** an explicit operator mutation preparing its commit
- **WHEN** the candidate is built before the commit locks
- **THEN** it is encoded, reparsed, and verified, with triggers and registrations prepared but unpublished
- **AND** nothing is published or written during preparation

#### Scenario: Own write is not an external edit
- **GIVEN** a mutation just committed
- **WHEN** a second mutation runs
- **THEN** the stored hash matches the disk and it is not treated as external
- **AND** mutations compare the disk with the stored hash, while a reload compares its own candidate input hash

#### Scenario: External edit blocks the write
- **GIVEN** the disk hash differs from the stored hash, or a missing file now exists
- **WHEN** a mutation reaches the locked check
- **THEN** it fails as an executed failure without overwriting the file
- **AND** a file recorded as missing that is still missing counts as fresh, not divergent
- **AND** an unreadable or unparseable file is never overwritten

#### Scenario: Concurrent mutations do not lose updates
- **GIVEN** two independent job mutations whose candidates were prepared from the same committed document revision
- **WHEN** the first commits under the locks and the second reaches its locked recheck
- **THEN** the second discards its stale candidate, re-prepares it from the current document, and writes after the first
- **AND** both job changes persist and no committed mutation is lost
- **AND** re-preparation is an internal candidate re-derivation that does not repeat `_on_attempt`, retry a model or tool run, or emit any external effect

#### Scenario: Write failure is explicit
- **GIVEN** the atomic write raises `OSError`
- **WHEN** the mutation runs
- **THEN** the failure is reported and prior state remains
- **AND** existing backup-and-prune behavior is retained

#### Scenario: Locks have one order
- **GIVEN** concurrent runs, reloads, history writes, and state saves
- **WHEN** they acquire the three locks
- **THEN** none takes them in another order and no deadlock occurs
- **AND** the atomic write and the one-unit publication of document, hash, runtime view, and complete registrations are performed with the held locks

### Requirement: Failed publication after commit requires reload

If publication cannot complete after `os.replace` has committed bytes, the scheduler SHALL set an in-memory `RELOAD_REQUIRED` latch and SHALL refuse every further mutation before `_on_attempt` until a reload succeeds. The committed file SHALL stay authoritative, and the scheduler SHALL NOT publish the new hash while keeping the old document.

#### Scenario: Incomplete publication latches reload
- **GIVEN** the document, hash, and view cannot be installed as one unit after the write
- **WHEN** the failure occurs
- **THEN** the original attempt is an executed failure stating the file may already contain the change
- **AND** later mutations are refused before `_on_attempt` until reload

#### Scenario: Partial registration publication has no mixed set
- **GIVEN** registrations are partially published
- **WHEN** the failure is detected
- **THEN** `RELOAD_REQUIRED` is set and the next successful reload completes the replacement set
- **AND** active registrations are not left mixed

### Requirement: Reload publishes only the candidate it parsed

Reload SHALL publish only the candidate parsed from its own input bytes. Under the commit locks it SHALL recheck the TOML candidate hash. A freshness-race mismatch SHALL fail without retry, preserving the last-good map and registrations, reporting `reload again`, and not toggling `RELOAD_REQUIRED`. A successful reload SHALL atomically publish document, hash, runtime view, and registrations and clear a set latch.

#### Scenario: Operator edit is loaded
- **GIVEN** the operator edited the file after the last stored hash
- **WHEN** reload parses and rechecks the same bytes
- **THEN** the new document is published with its hash and registrations
- **AND** the locked recheck compares the parsed bytes' own hash, and success clears the `RELOAD_REQUIRED` latch

#### Scenario: Concurrent mutation between parse and publish
- **GIVEN** a mutation commits after the reload parsed
- **WHEN** the reload rechecks
- **THEN** it fails with a reload-again instruction and last-good state is untouched
- **AND** the mutation is not overwritten and nothing is retried, re-parsed, or republished automatically

#### Scenario: No half-built state is observable
- **GIVEN** a reload publishes
- **WHEN** another thread reads jobs
- **THEN** it sees either the old or the complete new map and registrations

#### Scenario: Own input bytes define the candidate
- **GIVEN** a reload of the current scheduler file
- **WHEN** the complete candidate is prepared outside the locks
- **THEN** the file bytes are read, hashed, and parsed once into the candidate input hash
- **AND** the previously stored hash is not used as the candidate hash

#### Scenario: A failed reload race preserves the latch
- **GIVEN** a reload whose locked recheck finds a freshness mismatch, with the `RELOAD_REQUIRED` latch initially clear or already set
- **WHEN** the failure is reported
- **THEN** last-good map, document, hash, and registrations are preserved and the caller is told to reload again
- **AND** a clear latch stays clear, a set latch stays set, and no model or run retry happens

### Requirement: Mutation refusals precede the attempt hook only when knowable

A mutation SHALL be refused before `_on_attempt` when failure is already known: invalid or obsolete input, a known unparseable or unreadable scheduler file, or the `RELOAD_REQUIRED` latch. A failure discoverable only afterward SHALL be an executed failure under existing `_on_attempt` semantics and SHALL NOT be reclassified as refused.

#### Scenario: Known-bad file is refused
- **GIVEN** the scheduler file is known unparseable
- **WHEN** a mutation is requested
- **THEN** it is refused before `_on_attempt` with the file error and no change

#### Scenario: Late failures are executed failures
- **GIVEN** an external-edit hash mismatch, candidate roundtrip failure, or write failure after `_on_attempt`
- **WHEN** the mutation fails
- **THEN** it is an executed failure with no change

### Requirement: Scheduler state projection excludes raw selectors

`scheduler_state.json` SHALL keep its format, and its `jobs` section SHALL be written from a projection of runtime-safe fields only. Raw TOML selectors and other unvalidated values SHALL NOT be copied into it, and non-JSON values SHALL NOT be masked by string conversion. Serialization errors SHALL be contained as a state-storage failure.

#### Scenario: Raw selector is absent from state
- **GIVEN** a job has a non-string or secret-bearing `model`
- **WHEN** scheduler state is saved
- **THEN** the state file contains only scalar schedule fields, `last_run`, `last_error`, and `schedule_description`

#### Scenario: Serialization error is contained
- **GIVEN** state serialization raises
- **WHEN** the state is saved
- **THEN** it is recorded as a state-storage failure and the scheduler keeps running

### Requirement: Scheduled selectors are inherited or exact, never substituted

Each **scheduled run** SHALL classify its raw saved selector once. An absent or `None` selector SHALL inherit the active main selection, failing the run normally when none exists. Any other value SHALL be used exactly, needing neither main nor cache membership; a blank, non-string, malformed, or unknown-connection value SHALL fail that run before provider I/O and SHALL NOT substitute main. No selector, selection, or preference SHALL change.

#### Scenario: Absent or None selector inherits silently
- **GIVEN** an active main selection and a job whose `model` is absent
- **WHEN** the job is admitted
- **THEN** main is pinned and no warning is emitted

#### Scenario: Inheriting job fails without a main selection
- **GIVEN** no active main selection exists and a job has no `model`
- **WHEN** the job fires
- **THEN** it fails before any provider I/O with the normal failed-launch record and alert naming the missing selection

#### Scenario: Explicit selector runs without a main selection
- **GIVEN** no active main selection exists and the cache is empty
- **AND** a job saves `model = "openai-prod/gpt-x"` with connection `openai-prod` configured
- **WHEN** the job fires
- **THEN** the run is pinned to `openai-prod/gpt-x` and executes

#### Scenario: Blank or non-string selector fails the run
- **GIVEN** a job's saved model is `""`, whitespace only, or a boolean, number, datetime, array, or table
- **WHEN** the job fires
- **THEN** that run fails normally with a bounded classification label in its record and alert
- **AND** main is not pinned and the stored raw value is unchanged

#### Scenario: Malformed or unknown-connection selector fails the run
- **GIVEN** a job saves `gpt-x`, `openai-prod/`, ` openai-prod/gpt-x`, or `staging/gpt-x` with no `staging` connection
- **WHEN** the job fires
- **THEN** that run fails normally before provider I/O with bounded redacted evidence
- **AND** no trim, alias, case folding, or main substitution is applied

#### Scenario: Unknown upstream is attempted exactly
- **GIVEN** a job saves `openai-prod/not-a-model` on configured connection `openai-prod`
- **WHEN** the job fires and the provider rejects the upstream ID
- **THEN** the run fails as a normal job failure with its record and alert
- **AND** no other model is tried

#### Scenario: Failure leaves main and the document unchanged
- **GIVEN** a scheduled selector failed admission
- **WHEN** the failure is recorded
- **THEN** main selection and preference are unchanged
- **AND** no scheduler file is rewritten

### Requirement: Legacy state-home job file is detected but never loaded

The scheduler's only contact with the state-home legacy job file `scheduler_jobs.json` (resolved as `scheduler_jobs`) SHALL be an existence check at load and reload. When it exists, one actionable notice SHALL name its path and direct manual conversion into `scheduler.toml` `cron` or `once` jobs, and SHALL stay visible in job listing. The file SHALL NOT be read, executed, converted, written, renamed, or deleted, and SHALL NOT block TOML jobs, mutations, or removals.

#### Scenario: Legacy file exists
- **GIVEN** `scheduler_jobs.json` exists in the state home with any content
- **WHEN** the scheduler starts
- **THEN** one notice names its path with manual conversion steps
- **AND** none of its jobs is listed, scheduled, or run, and its bytes are unchanged

#### Scenario: Legacy file does not block TOML work
- **GIVEN** `scheduler_jobs.json` exists and contains a tag that `scheduler.toml` also defines
- **WHEN** the TOML job is modified or removed
- **THEN** the mutation proceeds under the ordinary protocol without a legacy refusal

#### Scenario: Legacy file is absent
- **GIVEN** no `scheduler_jobs.json` exists
- **WHEN** the scheduler starts
- **THEN** no legacy notice is sent

#### Scenario: XDG storage-layout migration is unchanged
- **GIVEN** the unrelated XDG storage-layout migration (`migrate.py`) copied a pre-XDG job file into the state home
- **WHEN** the scheduler starts
- **THEN** the copied file is handled only by this existence check and is never loaded as jobs
- **AND** `migrate.py` behavior is unchanged
