# schedule-tool-surface Specification

## Purpose

Define the model-facing contract of the `schedule` built-in tool: the action set (`list`, `show`, `add`, `remove`, `pause`, `resume`, `modify`, `run_now`), the parameter surface per action, sub-agent depth-guard rules, tool output semantics, and `run_now` supervised-execution semantics.

## Requirements

### Requirement: schedule tool action set

The `schedule` built-in tool SHALL support the actions `list`, `show`, `add`, `remove`, `pause`, `resume`, `modify`, and `run_now`. The read-only actions (`list`, `show`) SHALL be available at every caller depth. The mutating actions (`add`, `remove`, `pause`, `resume`, `modify`, `run_now`) SHALL be blocked at `caller_depth >= 1` (sub-agents) with a non-recoverable error naming the blocked action and the allowed read-only actions.

Feature: schedule tool surface
Rule: Read actions are always readable; write actions are main-agent-only.

#### Scenario: Full action set accepted

- **GIVEN** the main agent (caller_depth 0) calls `schedule` with each of the eight actions and valid arguments
- **WHEN** each call executes
- **THEN** every action is dispatched to its handler and none returns `Unknown action`

#### Scenario: Sub-agent read access

- **GIVEN** a sub-agent (caller_depth 1) calls `schedule` with `action = "list"` or `action = "show"`
- **WHEN** the call executes
- **THEN** the call succeeds and returns the same output the main agent would receive

#### Scenario: Sub-agent mutation blocked

- **GIVEN** a sub-agent (caller_depth 1) calls `schedule` with `action = "modify"` (or `add`, `remove`, `pause`, `resume`, `run_now`)
- **WHEN** the call executes
- **THEN** the call returns `success = false` with a non-recoverable error stating sub-agents cannot modify scheduled jobs
- **AND** the error lists `list` and `show` as the actions available to sub-agents

### Requirement: modify action partial-patch semantics

The `modify` action SHALL update only the fields provided in the call; all omitted job fields SHALL retain their current values. The `modify` action SHALL NOT reset `created_at`, `last_run`, run history, or `source`. The job `tag` SHALL be immutable under `modify` (rename requires `remove` + `add`). The `modify` action SHALL accept the fields `task`, `cron`, `schedule_type`, `run_at`, `notify`, `model`, `preserve_context`, `context_max_messages`, and `overlap_policy`. The `modify` action SHALL reject calls that provide no updatable field, and calls that provide `enabled`, with actionable errors. Schedule changes (`cron`, `schedule_type`, `run_at`) SHALL be validated through the same schedule-resolution logic as `add` and re-registered via the standard re-registration primitives. Legacy interval fields (`hours`, `minutes`, `time`) SHALL NOT be accepted by `modify`.

#### Scenario: Modify task preserves all other job fields

- **GIVEN** a job `report` exists with `model = "kimi-k2.5:cloud"`, `preserve_context = true`, `context_max_messages = 80`, `overlap_policy = "parallel"`, `notify = false`, `cron = "0 */6 * * *"`, and a recorded `last_run`
- **WHEN** the main agent calls `schedule` with `action = "modify"`, `tag = "report"`, and `task = "new task text"`
- **THEN** the job's `task` equals `"new task text"`
- **AND** `model`, `preserve_context`, `context_max_messages`, `overlap_policy`, `notify`, `cron`, `created_at`, and `last_run` are unchanged

#### Scenario: Modify cron re-registers the schedule

- **GIVEN** a job `report` exists with `cron = "0 */6 * * *"` and enabled registration at that cadence
- **WHEN** the main agent calls `schedule` with `action = "modify"`, `tag = "report"`, and `cron = "0 2 * * *"`
- **THEN** the job's cron expression equals `"0 2 * * *"` and the underlying scheduler registration is re-registered at the new cadence

#### Scenario: Modify invalid cron rejected

- **GIVEN** a job `report` exists with `cron = "0 */6 * * *"`
- **WHEN** the main agent calls `schedule` with `action = "modify"`, `tag = "report"`, and `cron = "not a cron"`
- **THEN** the call returns `success = false` with the validation error and the job's cron expression is unchanged

#### Scenario: Modify empty patch rejected

- **GIVEN** a job `report` exists
- **WHEN** the main agent calls `schedule` with `action = "modify"` and `tag = "report"` and no updatable field
- **THEN** the call returns `success = false` with an error naming the updatable fields

#### Scenario: Modify enabled rejected

- **GIVEN** a job `report` exists
- **WHEN** the main agent calls `schedule` with `action = "modify"`, `tag = "report"`, and `enabled = false`
- **THEN** the call returns `success = false` with an error directing the caller to use `pause` or `resume`

#### Scenario: Modify unknown tag

- **WHEN** the main agent calls `schedule` with `action = "modify"` and `tag = "nonexistent"`
- **THEN** the call returns `success = false` with an error listing the known job tags

#### Scenario: Modify preserves once-job semantics

- **GIVEN** a once-job `reminder` exists with `schedule_type = "once"` and `run_at = "18:30"`
- **WHEN** the main agent calls `schedule` with `action = "modify"`, `tag = "reminder"`, and `task = "updated reminder text"`
- **THEN** the job remains a once-job with `run_at = "18:30"` and the new task text

#### Scenario: Modify hidden parameters

- **GIVEN** a job `report` exists
- **WHEN** the main agent calls `schedule` with `action = "modify"`, `tag = "report"`, `context_max_messages = 20`, and `overlap_policy = "parallel"`
- **THEN** the job's `context_max_messages` equals 20 and `overlap_policy` equals `"parallel"`

### Requirement: show action full metadata

The `show` action SHALL return the complete metadata of a single job: `tag`, `task`, `schedule` (prose description), `cron` (raw expression when present), `schedule_type`, `run_at` (for once-jobs), `next_run`, `last_run`, `last_error`, `notify`, `model`, `preserve_context`, `context_max_messages`, `overlap_policy`, `source`, `enabled`, `is_running`, and `created_at`. The `show` action SHALL return an error listing known tags when the tag does not exist. The `list` action output SHALL remain compact and SHALL NOT be extended.

#### Scenario: Show returns full metadata

- **GIVEN** a job `report` exists with `model = "kimi-k2.5:cloud"`, `preserve_context = true`, `context_max_messages = 80`, `overlap_policy = "parallel"`, `cron = "0 */6 * * *"`, and `source = "dynamic"`
- **WHEN** the main agent calls `schedule` with `action = "show"` and `tag = "report"`
- **THEN** the output includes the raw cron expression `"0 */6 * * *"`, `model`, `preserve_context`, `context_max_messages = 80`, `overlap_policy = "parallel"`, `source = "dynamic"`, `next_run`, `enabled`, `is_running`, and `created_at`

#### Scenario: Show unknown tag

- **WHEN** the main agent calls `schedule` with `action = "show"` and `tag = "nonexistent"`
- **THEN** the call returns `success = false` with an error listing the known job tags

### Requirement: run_now manual override of paused jobs

The `run_now` action SHALL execute the job through the standard scheduled-run machinery even when the job is paused (`enabled = false`). The output SHALL note that the job remains paused. Scheduled (cron-fired) executions SHALL continue to skip paused jobs.

Feature: schedule tool surface
Rule: `run_now` is the manual-test path; the paused state gates only automatic firing.

#### Scenario: run_now on paused job executes

- **GIVEN** a paused job `report` (`enabled = false`) with a valid task
- **WHEN** the main agent calls `schedule` with `action = "run_now"` and `tag = "report"`
- **THEN** the job is dispatched for execution through the scheduled-run machinery
- **AND** the tool output states the job was triggered and notes the job remains paused

#### Scenario: Paused job still skipped by scheduler firing

- **GIVEN** a paused job `report` (`enabled = false`) whose cron cadence fires
- **WHEN** the scheduler processes the firing
- **THEN** the job is not executed

### Requirement: run_now output semantics

The `run_now` action SHALL return immediately and its output SHALL state that the job executes as a supervised scheduled-run sub-agent through the standard scheduled-run machinery, that the result is delivered per the job's `notify` setting, that `show` is the follow-up for outcome, and — for once-jobs — that the job is removed after the manual run (matching scheduled once-job behavior). The output SHALL note that the trigger is subject to the job's overlap policy (a `skip`-policy job with a run in flight will skip the trigger — visible via `show`/`is_running`). The output SHALL NOT imply the result returns synchronously in the tool response.

#### Scenario: run_now output states supervised sub-agent execution

- **GIVEN** an enabled cron job `report`
- **WHEN** the main agent calls `schedule` with `action = "run_now"` and `tag = "report"`
- **THEN** the tool returns `success = true` immediately
- **AND** the output states the job runs through the scheduled-run machinery as a supervised sub-agent
- **AND** the output directs follow-up to `schedule show` per the job's `notify` setting

#### Scenario: run_now on once-job notes removal

- **GIVEN** an enabled once-job `reminder` (`schedule_type = "once"`)
- **WHEN** the main agent calls `schedule` with `action = "run_now"` and `tag = "reminder"`
- **THEN** the tool returns `success = true`
- **AND** the output notes the job will be removed after this run

#### Scenario: run_now on skip-policy job with in-flight run notes overlap subject

- **GIVEN** a job `report` with `overlap_policy = "skip"` and a run in progress (`is_running = true`)
- **WHEN** the main agent calls `schedule` with `action = "run_now"` and `tag = "report"`
- **THEN** the tool returns `success = true` with output noting the trigger is subject to the job's overlap policy
- **AND** the presence of an in-flight run at trigger time governs whether the manual trigger executes (visible via `show` / `is_running`)

#### Scenario: run_now unknown tag

- **WHEN** the main agent calls `schedule` with `action = "run_now"` and `tag = "nonexistent"`
- **THEN** the call returns `success = false` with an error listing the known job tags

### Requirement: add action parameter surface

The `add` action SHALL accept `context_max_messages` (int, default 50) and `overlap_policy` (`skip` or `parallel`, default `skip`) in addition to the existing parameters. The tool schema and descriptor SHALL document all accepted parameters for `add` and `modify`. Calls with `overlap_policy` values outside `skip`/`parallel` SHALL be rejected.

#### Scenario: add with overlap policy parallel

- **WHEN** the main agent calls `schedule` with `action = "add"`, `tag = "longjob"`, `task = "heavy work"`, `cron = "0 */2 * * *"`, and `overlap_policy = "parallel"`
- **THEN** the job is created with `overlap_policy = "parallel"`

#### Scenario: add rejects invalid overlap policy

- **WHEN** the main agent calls `schedule` with `action = "add"`, `tag = "longjob"`, `task = "heavy work"`, `cron = "0 */2 * * *"`, and `overlap_policy = "sometimes"`
- **THEN** the call returns `success = false` with a validation error naming the valid values