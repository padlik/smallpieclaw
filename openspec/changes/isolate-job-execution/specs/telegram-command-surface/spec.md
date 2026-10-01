## ADDED Requirements

### Requirement: Read-only scheduled job history and result commands

The authenticated Telegram `/jobs` command SHALL preserve its existing job-management surface and add read-only `/jobs history <tag>` and `/jobs result <execution-id>` views. These commands MUST read retained **job execution records** directly and MUST NOT invoke the operator agent, create an operator prompt, or read or modify operator memory. History SHALL show retained run IDs, statuses, and timestamps, including records for removed once-jobs. Legacy records without execution IDs SHALL be identified as legacy/partial and SHALL NOT be assigned fabricated IDs or represented as complete. Result lookup SHALL present one complete retained outcome; when it exceeds Telegram's message limit, it SHALL be delivered as a complete UTF-8 text document. Unknown or expired execution IDs SHALL report that the result is unavailable. Missing arguments SHALL return usage. For history, a tag with no active definition and no retained runs is unknown; an active job with no retained runs has empty history; a removed once-job with retained records remains known until those records expire. The commands are read-only and available only to the authenticated operator.

#### Scenario: History lists retained runs directly
- **GIVEN** an authenticated operator requests `/jobs history <tag>` for a tag with retained executions
- **WHEN** the command is handled
- **THEN** the response lists each retained execution ID, status, and timestamp
- **AND** no operator agent turn or memory access occurs

#### Scenario: Removed once-job remains visible in history
- **GIVEN** a once-job definition has been removed but its execution records remain retained
- **WHEN** the operator requests `/jobs history <tag>`
- **THEN** its retained run IDs, statuses, and timestamps are listed

#### Scenario: Legacy history is not fabricated as complete
- **GIVEN** a retained legacy record has no execution ID and contains truncated output
- **WHEN** it appears in `/jobs history <tag>`
- **THEN** it is identified as legacy/partial with its mapped status and timestamp
- **AND** no execution ID or missing output is fabricated

#### Scenario: Result command returns complete retained outcome
- **GIVEN** an authenticated operator requests `/jobs result <execution-id>` for a retained record
- **WHEN** the complete result fits within Telegram's message limit
- **THEN** the command returns the full result without truncation
- **AND** it does not invoke the operator agent or alter context

#### Scenario: Large result is delivered as a document
- **GIVEN** an authenticated operator requests a retained result larger than Telegram's message limit
- **WHEN** the command presents the result
- **THEN** it delivers the complete result as a UTF-8 text document
- **AND** it does not truncate the result or invoke the operator agent

#### Scenario: Unknown or expired result is unavailable
- **GIVEN** an authenticated operator requests `/jobs result <execution-id>` for an unknown or expired ID
- **WHEN** the command performs the lookup
- **THEN** it reports that the result is unavailable
- **AND** it does not access operator memory or create a prompt

#### Scenario: Unknown tag history is reported
- **GIVEN** an authenticated operator requests history for a tag with no active definition and no retained runs
- **WHEN** the command looks up the tag
- **THEN** the command reports the tag as unknown

#### Scenario: Empty history is reported for an active job
- **GIVEN** an authenticated operator requests history for a tag with an active definition but no retained runs
- **WHEN** the command looks up the tag
- **THEN** the command reports empty history

#### Scenario: Missing arguments show command usage
- **GIVEN** an authenticated operator invokes `/jobs history` or `/jobs result` without its required argument
- **WHEN** the command is handled
- **THEN** it returns the corresponding usage text without invoking the operator agent

#### Scenario: Existing management commands remain available
- **GIVEN** an authenticated operator uses existing `/jobs`, `/agents`, or `/stop` commands
- **WHEN** the new history/result subcommands are added
- **THEN** the existing command and cancellation contracts remain unchanged

#### Scenario: Help discovers the history and result views
- **GIVEN** an authenticated operator requests `/help` or views `/jobs` usage
- **WHEN** command help is presented
- **THEN** it documents `/jobs history <tag>` and `/jobs result <execution-id>` as read-only views
- **AND** it preserves the existing `/jobs` management command guidance

#### Scenario: Unauthenticated user cannot read job records
- **GIVEN** a Telegram user is not authenticated as the authorised operator
- **WHEN** that user requests `/jobs history <tag>` or `/jobs result <execution-id>`
- **THEN** access is denied under the existing authentication policy
- **AND** no retained job information is disclosed
