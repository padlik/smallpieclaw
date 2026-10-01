## MODIFIED Requirements

### Requirement: Scheduled job failure classification and notification

The system SHALL inspect scheduled job sub-agent result strings for error patterns. A failure is detected when a result starts with `❌` or matches a known error type string; the system SHALL classify the error type and preserve it with the next scheduled run time in the outcome. No auto-retry SHALL be performed—the next cron run is the natural retry. For a **scheduled run** with `notify=true`, success and failure run-result notices SHALL be deferred until the **operator turn** is idle and typing has stopped, then sent without notification sound. A failure notice SHALL include `⚠️ Scheduled job failed: {tag}`, the error type, and the next scheduled run time. With `notify=false`, both success and failure run-result notices MUST be suppressed while outcomes remain recorded. Launch-failure alerts remain direct and independent of `notify`.

#### Scenario: Scheduled job timeout failure is classified and notified
- **GIVEN** a scheduled job returns a result string starting with `❌ LLM error: TimeoutException` and has `notify=true`
- **WHEN** the scheduler processes the failure while an operator turn is active
- **THEN** the failure is classified as a timeout error
- **AND** the failure outcome retains the error type and next scheduled run time
- **AND** a soundless `⚠️ Scheduled job failed: {tag}` notice including the error type and next run time is sent only after the operator turn is idle and typing has stopped

#### Scenario: Scheduled job success is notified normally
- **GIVEN** a scheduled job returns a successful result and has `notify=true`
- **WHEN** the scheduler processes the result
- **THEN** no error classification is applied
- **AND** its run-result notice waits until the operator turn is idle and typing has stopped
- **AND** the notice is sent without notification sound

#### Scenario: Muted scheduled failure has no run-result notice
- **GIVEN** a scheduled job with `notify=false` fails
- **WHEN** the scheduler records the classified failure
- **THEN** the error type and next scheduled run time remain in the recorded outcome
- **AND** no success or failure run-result notice is sent

#### Scenario: Muted scheduled success has no run-result notice
- **GIVEN** a scheduled job with `notify=false` succeeds
- **WHEN** the scheduler records the outcome
- **THEN** the outcome is retained
- **AND** no run-result notice is sent

#### Scenario: Scheduled job failure is recorded in execution log
- **GIVEN** a scheduled job fails with an LLM error
- **WHEN** the failure is recorded in the execution log
- **THEN** the log entry includes the error type classification
- **AND** the log entry includes the failure timestamp
- **AND** when `notify=true`, its failure notice is deferred until the operator turn is idle and sent without notification sound

#### Scenario: Launch-failure alert ignores notify setting
- **GIVEN** a scheduled job cannot launch and has `notify=false`
- **WHEN** the scheduler handles the launch failure
- **THEN** the existing direct launch-failure alert is sent independently of the run-result notification setting
