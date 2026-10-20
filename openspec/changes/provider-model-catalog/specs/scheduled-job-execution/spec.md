# scheduled-job-execution Specification

## Purpose

Isolate the scheduler from bot startup, persist the raw job document safely, keep legacy job files read-only, and resolve each job's model selector at execution. Existing context, approval, record, history, and notice requirements are unchanged.

## ADDED Requirements

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

A parseable job with malformed fields SHALL keep its raw values and fail only itself. A bad time or scheduling field SHALL leave the job visibly unscheduled with a visible config error. Any other bad field SHALL leave the job scheduled and produce a recorded launch failure when it fires. Field checks SHALL be type-strict with no coercion, and healthy sibling jobs SHALL load, register, and run normally.

#### Scenario: Bad scheduling field leaves a job unscheduled
- **GIVEN** a parseable job has an invalid schedule type, cron, `run_at`, legacy fields that cannot derive a cron, or non-boolean `enabled`
- **WHEN** the scheduler loads it
- **THEN** raw fields are retained and a `config_error` is visible in list and show
- **AND** the job never fires and `run_now` returns the existing failure result with that error

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

### Requirement: Scheduled admission resolves the saved selector before pinning

Scheduled model admission SHALL require a valid active main model, then classify the raw saved selector. Absent or `None` SHALL inherit main silently. A blank string, whitespace-only string, non-string value, or exact non-blank string absent from the catalog SHALL emit a warning and inherit main. An exact catalog entry, fresh or stale, SHALL be pinned. The raw selector and main selection SHALL NOT change.

#### Scenario: Absent or None selector inherits silently
- **GIVEN** valid main and a job whose `model` is absent
- **WHEN** the job is admitted
- **THEN** main is pinned and no warning is emitted

#### Scenario: Blank or whitespace selector warns and inherits
- **GIVEN** valid main and a saved model that is `""` or whitespace only
- **WHEN** the job is admitted
- **THEN** a bounded warning is emitted and main is pinned
- **AND** the stored raw value is unchanged

#### Scenario: Non-string selector warns without content
- **GIVEN** valid main and a saved model that is a boolean, integer, float, datetime, array, or table
- **WHEN** the job is admitted
- **THEN** a warning with only a classification label is emitted and main is pinned
- **AND** the value is not stringified or rewritten

#### Scenario: Unknown exact string warns and inherits
- **GIVEN** valid main and a saved exact non-blank string absent from the catalog, including case or padding variants
- **WHEN** the job is admitted
- **THEN** a bounded redacted warning is emitted and main is pinned
- **AND** no alias, trim, or suggested replacement is applied

#### Scenario: Known selector is pinned including stale entries
- **GIVEN** valid main and a saved exact string matching a fresh or stale catalog entry
- **WHEN** the job is admitted
- **THEN** that model is pinned with no warning
- **AND** staleness is not treated as unknown

#### Scenario: Fallback leaves main and the document unchanged
- **GIVEN** a malformed or unknown selector inherited main
- **WHEN** admission completes
- **THEN** main selection, settings, and revision are unchanged
- **AND** no scheduler file is rewritten

### Requirement: Scheduled jobs fail normally without a valid main model

Any scheduled job SHALL fail normally before any model call when no valid active main model exists, including a job with a valid explicit canonical selector. Valid main means an active selection present in the catalog. Ordinary model admission for on-demand, plan, and diagnostic runs SHALL keep its active-main requirement and strict selectors.

#### Scenario: No catalog or no active selection fails the job
- **GIVEN** the catalog is missing, empty, unselected, or the saved selection is unavailable
- **WHEN** a scheduled job with any model value is admitted
- **THEN** admission fails before any provider I/O
- **AND** the existing failed-launch record and alert are produced

#### Scenario: Valid explicit selector does not bypass the main gate
- **GIVEN** the catalog has a valid explicit model but main is invalid
- **WHEN** a job naming it fires
- **THEN** the job fails normally and the model is not pinned

#### Scenario: Ordinary explicit selectors remain strict
- **GIVEN** an on-demand, plan, or diagnostic request supplies a padded, aliased, blank, or unknown selector
- **WHEN** ordinary admission runs
- **THEN** it fails without inheriting main or applying scheduled classification

### Requirement: A pinned scheduled model never falls back

Selector fallback SHALL apply only before the effective scheduled model is chosen. Once the effective model is chosen, credential, capability, context, inference, and provider-outage errors SHALL fail the job without selecting, retrying, or falling back to another model.

#### Scenario: Credential or capability failure on a valid selector
- **GIVEN** a job names a valid catalog model whose credential is missing or capability is unsupported
- **WHEN** the job runs
- **THEN** it fails as a normal job failure
- **AND** main is not substituted

#### Scenario: Provider outage or oversized request does not retry elsewhere
- **GIVEN** a pinned scheduled run hits a provider outage, inference error, or exceeded context
- **WHEN** the error is handled
- **THEN** the run fails without a cross-model retry or whole-run replay
- **AND** the failure is recorded normally

### Requirement: Legacy per-job fallback models are ignored at execution

A legacy per-job `fallback_models` setting SHALL NOT affect scheduler load or bot startup and SHALL remain in the job document unrewritten. When that job executes, the scheduler SHALL emit a runtime warning, ignore the value, and never enable cross-model retry. No job converter SHALL exist.

#### Scenario: Load and startup ignore the setting
- **GIVEN** a job contains `fallback_models`
- **WHEN** the scheduler loads at startup or reload
- **THEN** no error or warning is raised and the key remains in the document

#### Scenario: Execution warns and ignores the setting
- **GIVEN** a job with `fallback_models` executes
- **WHEN** the run starts
- **THEN** a runtime warning is emitted and the value is not passed to the spawn factory
- **AND** no fallback or retry on another model occurs

### Requirement: Scheduled admission uses a trusted source and one root admission

The scheduler SHALL pass the raw saved selector through supervision with `SOURCE_SCHEDULED` options built only by scheduler code. That source SHALL exempt only the strict model-shape guard; all other spawn checks SHALL remain. The root factory SHALL call one scheduled model admission that snapshots, resolves credentials, and admits once, returning an immutable pin; every other ordinary profile SHALL use the unchanged ordinary admission.

#### Scenario: Model-facing spawn keeps its strict guard
- **GIVEN** a model-facing `spawn_agent` call supplies a blank or non-string `model`
- **WHEN** arguments are validated with default options
- **THEN** the shape guard rejects it before admission
- **AND** obsolete-argument rejection is unchanged

#### Scenario: Scheduled source exempts only the shape guard
- **GIVEN** a scheduler-built spawn with `SOURCE_SCHEDULED` and a raw selector
- **WHEN** arguments are validated
- **THEN** the shape guard does not reject the selector
- **AND** task, depth, capacity, obsolete-argument, context-key, refusal, and R6 checks still apply

#### Scenario: Selector is passed whenever the key is present
- **GIVEN** a job's raw `model` key is present and not `None`
- **WHEN** the scheduler builds spawn arguments
- **THEN** the raw object is passed through the existing `model` keyword with no new public argument, schema, or channel

#### Scenario: Exactly one admission per run
- **GIVEN** a scheduled run reaches the root factory
- **WHEN** the scheduled profile is constructed
- **THEN** one scheduled model admission performs snapshot, credential resolution, and admission and returns an immutable pin
- **AND** runtime construction does not re-admit or resolve credentials again

#### Scenario: Other profiles keep ordinary admission
- **GIVEN** a main, on-demand, plan, or diagnostic run
- **WHEN** the root factory admits it
- **THEN** ordinary admission is used unchanged

### Requirement: Scheduled admission races fail the job without retry

Scheduled model admission SHALL recheck, after credential resolution, the provider connection fingerprint, configuration generation, model record, main selection identity and revision, and catalog document identity. Any mismatch SHALL fail that run attempt with a typed normal job failure and SHALL NOT re-lookup, reclassify, or retry. Emitted warnings SHALL be redacted and bounded.

#### Scenario: Refresh lands inside the credential window
- **GIVEN** a catalog refresh changes the provider, model record, or document identity while credentials resolve
- **WHEN** the recheck runs
- **THEN** the run fails with a typed failure and the normal failed-launch record and alert
- **AND** nothing is re-looked-up, reclassified, or retried

#### Scenario: Main selection changes inside the credential window
- **GIVEN** main selection identity or revision changes before the recheck
- **WHEN** the recheck runs
- **THEN** the attempt fails with a typed failure and the next fire reclassifies against new state

#### Scenario: Same-revision document replacement is detected
- **GIVEN** a refresh whose persistence failed replaced the document at the same revision
- **WHEN** the recheck compares document identity
- **THEN** the mismatch fails the attempt

#### Scenario: Warnings never carry raw secrets
- **GIVEN** a malformed or unknown selector warning is emitted before credential resolution
- **WHEN** it is logged
- **THEN** it uses the bounded redacted representation
- **AND** no raw secret or non-string content appears

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

The scheduler SHALL write its file only for explicit operator mutations, each applying a patch of only the requested fields to a deep copy of the parsed raw document and re-emitting every type `tomli` returns. Parsed-value equality SHALL be NaN-aware and type-strict; comments and layout SHALL NOT be promised. `None` SHALL be represented by key omission, and load SHALL NOT rewrite the file.

#### Scenario: Untouched values survive a mutation
- **GIVEN** a document with strings containing control characters, booleans, integers, floats including `inf` and `nan`, datetimes, arrays, inline tables, and quoted keys
- **WHEN** one job is modified
- **THEN** the written document reparses equal for every untouched value, with booleans not equal to integers

#### Scenario: Comments and layout are not promised
- **GIVEN** the file has comments and custom layout
- **WHEN** a mutation commits
- **THEN** only parsed-value equality is guaranteed

#### Scenario: Load and cron derivation never write
- **GIVEN** a legacy `daily` or `interval` schedule
- **WHEN** the scheduler loads
- **THEN** the cron is derived in memory only and the file is not written

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

Reload SHALL publish only the candidate parsed from its own input bytes. Under the commit locks it SHALL recheck the TOML candidate hash and legacy JSON fingerprint. A freshness-race mismatch SHALL fail without retry, preserving the last-good map and registrations, reporting `reload again`, and not toggling `RELOAD_REQUIRED`. A successful reload SHALL atomically publish document, hash, runtime view, and registrations and clear a set latch.

#### Scenario: Operator edit is loaded
- **GIVEN** the operator edited the file after the last stored hash
- **WHEN** reload parses and rechecks the same bytes
- **THEN** the new document is published with its hash and registrations
- **AND** the locked recheck compares the parsed bytes' own hashes, and success clears the `RELOAD_REQUIRED` latch

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
- **GIVEN** a reload of the current scheduler file and legacy JSON
- **WHEN** the complete candidate is prepared outside the locks
- **THEN** the file bytes are read, hashed, and parsed once into the candidate input hash
- **AND** the legacy JSON fingerprint comes from its own parsed legacy bytes, never the previously stored hash

#### Scenario: A failed reload race preserves the latch
- **GIVEN** a reload whose locked recheck finds a freshness mismatch, with the `RELOAD_REQUIRED` latch initially clear or already set
- **WHEN** the failure is reported
- **THEN** last-good map, document, hash, and registrations are preserved and the caller is told to reload again
- **AND** a clear latch stays clear, a set latch stays set, and no model or run retry happens

### Requirement: Mutation refusals precede the attempt hook only when knowable

A mutation SHALL be refused before `_on_attempt` when failure is already known: invalid input, known unparseable or unreadable scheduler file, the `RELOAD_REQUIRED` latch, a legacy refusal, or an unresolvable legacy shadow check. A failure discoverable only afterward SHALL be an executed failure under existing `_on_attempt` semantics and SHALL NOT be reclassified as refused.

#### Scenario: Known-bad file is refused
- **GIVEN** the scheduler file is known unparseable
- **WHEN** a mutation is requested
- **THEN** it is refused before `_on_attempt` with the file error and no change

#### Scenario: Late failures are executed failures
- **GIVEN** an external-edit hash mismatch, candidate roundtrip failure, write failure, or fingerprint race after `_on_attempt`
- **WHEN** the mutation fails
- **THEN** it is an executed failure with no change

### Requirement: Legacy scheduled_jobs.json jobs remain runnable and read-only

Legacy `scheduled_jobs.json` jobs SHALL remain runnable from a non-destructive read; the file SHALL NOT be deleted, overwritten, renamed, adopted, or converted. A TOML job SHALL win on a duplicate tag. A JSON-only job SHALL show a visible `legacy, read-only` badge, and modify, remove, pause, and resume SHALL be refused before `_on_attempt` with the file path and manual cleanup and reload steps. `run_now` SHALL use normal admission.

#### Scenario: JSON-only job runs and is badged
- **GIVEN** a job exists only in the legacy file
- **WHEN** the scheduler loads and lists jobs
- **THEN** the job fires and its list and show output carries `legacy, read-only`

#### Scenario: TOML wins a duplicate tag
- **GIVEN** the same tag is in TOML and the legacy file
- **WHEN** the scheduler loads
- **THEN** the TOML job runs and no legacy duplicate runs

#### Scenario: Mutating a JSON-only job is refused
- **GIVEN** a JSON-only job
- **WHEN** modify, remove, pause, or resume is requested
- **THEN** it is refused before `_on_attempt` with the file path and manual edit and reload steps
- **AND** no false success is reported and no file content is shown

#### Scenario: run_now of a legacy job uses normal admission
- **GIVEN** a JSON-only job
- **WHEN** `run_now` is requested
- **THEN** it follows the normal scheduled admission and lifecycle

#### Scenario: Malformed legacy file is contained
- **GIVEN** the legacy file is unreadable or malformed at start or reload
- **WHEN** the scheduler loads
- **THEN** an operator notice is raised and TOML jobs continue
- **AND** on reload the last-good legacy jobs are retained

#### Scenario: Manual cleanup then reload removes the job
- **GIVEN** the operator deletes a job from the legacy file
- **WHEN** reload succeeds
- **THEN** the job is gone, with no adoption, tombstone, or auto-conversion

### Requirement: Removing a TOML job cannot resurrect a legacy job

Removing a TOML job SHALL be refused before `_on_attempt` when the current legacy file on disk contains that tag, or is unreadable or malformed, since resurrection cannot be ruled out. The preflight SHALL read the legacy file itself, not the cached map; a missing file SHALL allow removal. A legacy fingerprint change between preflight and commit SHALL fail the removal as an executed failure with no change.

#### Scenario: Shadow tag added after the last reload
- **GIVEN** the operator added the tag to the legacy file without reloading
- **WHEN** the TOML job is removed
- **THEN** the removal is refused with the file path and cleanup steps

#### Scenario: Missing legacy file allows removal
- **GIVEN** no legacy file exists
- **WHEN** the TOML job is removed
- **THEN** the removal proceeds normally

#### Scenario: Unreadable legacy file refuses removal
- **GIVEN** the legacy file is unreadable or malformed
- **WHEN** a TOML removal is requested
- **THEN** it is refused before `_on_attempt` with an actionable message

#### Scenario: Fingerprint race fails the removal
- **GIVEN** the legacy file changed between preflight and commit
- **WHEN** the commit compares fingerprints
- **THEN** it is an executed failure with no change

#### Scenario: Shadowing TOML job can still be modified
- **GIVEN** a TOML job shadows a legacy tag
- **WHEN** it is modified, paused, or resumed
- **THEN** the mutation proceeds and the TOML job still wins

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
