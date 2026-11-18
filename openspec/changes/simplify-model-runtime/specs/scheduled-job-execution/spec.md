# scheduled-job-execution Delta

## Purpose

Baseline: canonical `openspec/specs/scheduled-job-execution/spec.md` as already extended by the pending predecessor delta `openspec/changes/provider-model-catalog/specs/scheduled-job-execution/spec.md` (implementation baseline `a57ab26`), which adds every requirement name modified or removed below. This delta MUST be synchronized only after that predecessor delta. It makes explicit **scheduled run** selectors exact, removes warn-and-inherit selector fallback, the main-model gate for explicit selectors, legacy JSON job execution, and legacy schedule and fallback compatibility. The canonical context, approval, **job execution record**, history, and notice requirements and the predecessor's startup containment, raw selector storage, bounded evidence, lifecycle ordering, mutation freshness, publication latch, and state projection requirements are unchanged. `/stop` and cancellation behavior are unchanged.

## MODIFIED Requirements

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

## REMOVED Requirements

### Requirement: Scheduled jobs fail normally without a valid main model

**Reason**: Explicit scheduled selectors no longer require an active main selection; only jobs that inherit main need one.

**Migration**: Covered by `Scheduled selectors are inherited or exact, never substituted`. Jobs that must run while no main model is selected should save an explicit `model`.

### Requirement: Scheduled admission resolves the saved selector before pinning

**Reason**: Malformed, non-string, and unknown selectors no longer warn and inherit main; explicit selectors are used exactly and no longer require a main selection.

**Migration**: Replaced by `Scheduled selectors are inherited or exact, never substituted`. Fix or remove job `model` values that relied on inheriting main.

### Requirement: Legacy per-job fallback models are ignored at execution

**Reason**: Legacy per-job `fallback_models` becomes a contained, actionable job configuration error instead of being silently ignored.

**Migration**: Remove `fallback_models` from each job in `scheduler.toml`; see `Job configuration errors are contained to the offending job`.

### Requirement: Scheduled admission races fail the job without retry

**Reason**: Provider connections are fixed for the process lifetime and the catalog is advisory, so no fingerprint, generation, revision, or document identity is rechecked.

**Migration**: None required; selector warnings and records keep the bounded redacted representation defined by `Selector evidence is bounded and redacted`.

### Requirement: Legacy scheduled_jobs.json jobs remain runnable and read-only

**Reason**: The state-home JSON-only job representation (`scheduler_jobs.json`, named `scheduled_jobs.json` in the predecessor's prose) is no longer loaded or executed.

**Migration**: Manually rewrite each needed legacy job as a `cron` or `once` job in `scheduler.toml`, then reload. The legacy file is left on disk untouched; see `Legacy state-home job file is detected but never loaded`.

### Requirement: Removing a TOML job cannot resurrect a legacy job

**Reason**: Legacy JSON jobs are no longer loaded, so a removed TOML job can never be shadowed or resurrected by one.

**Migration**: None required; TOML job removal follows the ordinary mutation protocol.

## ADDED Requirements

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
