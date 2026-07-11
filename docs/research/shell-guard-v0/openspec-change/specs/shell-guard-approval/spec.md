## ADDED Requirements

### Requirement: Shell Guard ask confirmation is command-specific
Shell Guard SHALL present active-mode `ask` decisions as command-specific Telegram confirmations. Broad tool-scoped approve-all behavior SHALL NOT be available.

#### Scenario: Shell Guard ask shows compact decision context
- **GIVEN** Shell Guard decides `ask` for an interactive depth-0 shell command
- **WHEN** the Telegram confirmation prompt is sent
- **THEN** the prompt MUST show the command, risk level, confidence where available, short advisor reason, strongest signals, and a hint to deny if unsure
- **AND** the prompt MUST provide `Allow this time`, `Deny this time`, rule-creation actions, and `Details`

#### Scenario: Remote command disclosure appears in prompt
- **GIVEN** Shell Guard active-mode advisor context identifies a command that may contact a remote URL or socket destination
- **WHEN** the Telegram confirmation prompt is sent
- **THEN** the prompt MUST disclose that executing the command may contact the remote destination
- **AND** if the command downloads content into an interpreter, the prompt MUST disclose that the command may run downloaded content

#### Scenario: No approve-all state can auto-confirm Shell Guard ask
- **GIVEN** Shell Guard decides `ask` for a shell command
- **WHEN** the ReAct loop handles the confirmation result
- **THEN** the Shell Guard prompt MUST still require a Shell Guard-specific user decision
- **AND** no tool-scoped approve-all state MUST be available to auto-confirm the command

#### Scenario: Shell Guard prompt does not show approve-all
- **GIVEN** Shell Guard decides `ask` for an interactive depth-0 shell command
- **WHEN** the Telegram confirmation prompt is sent
- **THEN** the prompt MUST NOT include an approve-all button
- **AND** the prompt MUST offer one-time decisions and explicit rule creation instead

#### Scenario: Deny never enters confirmation
- **GIVEN** Shell Guard decides `deny` for a shell command
- **WHEN** the result is returned to the ReAct loop
- **THEN** the command MUST NOT execute
- **AND** no approve-all or confirmation path MUST be able to override the denial

### Requirement: Shell Guard details preserve the pending decision
Shell Guard SHALL provide a details flow that explains the decision without consuming or regenerating the pending command.

#### Scenario: Details sends a separate message
- **GIVEN** a Shell Guard confirmation prompt is pending
- **WHEN** the user selects `Details`
- **THEN** Telegram MUST send a separate details message
- **AND** the original pending command token or state MUST remain valid
- **AND** the details message MUST include parsed command units, matched rules, risk signals, advisor notes, safer alternatives where available, and possible rule scopes

#### Scenario: Back to decision sends a fresh compact prompt
- **GIVEN** a Shell Guard details message is displayed
- **WHEN** the user selects `Back to decision`
- **THEN** Telegram MUST send a fresh compact decision prompt for the same pending command
- **AND** the command MUST NOT be reclassified or duplicated solely because details were viewed

### Requirement: Telegram actions match the effective Shell Guard decision
Telegram SHALL render only actions that are valid for the effective Shell Guard decision after policy precedence and semantic safety rules are applied.

#### Scenario: Hard deny does not offer allow actions
- **GIVEN** Shell Guard's effective decision is `deny`
- **WHEN** Telegram displays or updates Shell Guard information for that command
- **THEN** Telegram MUST NOT offer `Allow this time`
- **AND** Telegram MUST NOT offer create-allow-rule actions that would override the denial
- **AND** Telegram MAY offer details explaining the denial

#### Scenario: Ask offers only scoped decision actions
- **GIVEN** Shell Guard's effective decision is `ask`
- **WHEN** Telegram renders the Shell Guard confirmation prompt
- **THEN** Telegram MUST offer one-time allow and deny actions
- **AND** any persistent rule actions MUST be limited to scopes allowed by the effective risk and policy model

### Requirement: Telegram rule creation remains explicit
Shell Guard SHALL let users create persistent rules from a pending prompt without forcing rule creation for one-time decisions.

#### Scenario: Allow rule creation separates policy change from execution
- **GIVEN** a Shell Guard prompt is pending
- **WHEN** the user chooses to create an allow rule
- **THEN** the user MUST choose a rule scope before the rule is saved
- **AND** after the rule is saved, the user MUST be asked whether to run the current command now

#### Scenario: Ask rule creation re-shows the decision
- **GIVEN** a Shell Guard prompt is pending
- **WHEN** the user creates an ask rule
- **THEN** Shell Guard MUST save the ask rule
- **AND** Telegram MUST show a rule-created message
- **AND** Telegram MUST re-show the current decision prompt

#### Scenario: Deny rule creation denies the current command
- **GIVEN** a Shell Guard prompt is pending
- **WHEN** the user creates a deny rule
- **THEN** Shell Guard MUST save the deny rule
- **AND** the current command MUST be denied

### Requirement: Generic confirmation prompts require per-operation decisions
Generic confirmation prompts SHALL require an explicit decision for each pending operation unless an eligible non-shell tool has an active prompt/run-scoped approval lease. Broad tool-scoped approve-all behavior SHALL NOT be available.

#### Scenario: Generic confirmation prompt omits broad approve-all
- **GIVEN** a non-Shell-Guard built-in operation requires generic confirmation
- **WHEN** Telegram renders the confirmation prompt
- **THEN** the prompt MUST include a confirm action for the current operation
- **AND** the prompt MUST include a cancel action for the current operation
- **AND** the prompt MUST NOT include a broad approve-all action that survives beyond the current prompt/run

#### Scenario: Generic confirmation callback has no approve-all branch
- **GIVEN** a generic confirmation is pending
- **WHEN** Telegram handles confirmation callbacks
- **THEN** callbacks MUST support confirming or cancelling the pending operation
- **AND** callbacks MUST NOT register future automatic approval for all operations of the same tool

### Requirement: Prompt-scoped approval leases are bounded
The system SHALL support prompt/run-scoped approval leases only for explicitly eligible non-shell tools, and leases SHALL expire when the current prompt/run ends.

#### Scenario: Eligible tool can be approved for current prompt
- **GIVEN** a generic confirmation is pending for an eligible non-shell tool such as `file_patch`
- **WHEN** Telegram renders the confirmation prompt
- **THEN** it MAY offer an action to approve that tool for the current prompt/run
- **AND** the prompt MUST state that the approval expires when the current prompt/run finishes

#### Scenario: Prompt-scoped lease expires at run end
- **GIVEN** a prompt-scoped approval lease exists for an eligible tool
- **WHEN** the current `AgentController.run()` finishes or is reset
- **THEN** the lease MUST be cleared
- **AND** future prompts/runs MUST require confirmation again

#### Scenario: Ineligible tools do not offer prompt-scoped leases
- **GIVEN** a confirmation is pending for `shell`, `secret_get`, `memory_graph_store`, or a Shell Guard `ask` decision
- **WHEN** Telegram renders the prompt
- **THEN** it MUST NOT offer a prompt-scoped approval lease
- **AND** the operation MUST require an explicit one-time decision or Shell Guard policy/rule behavior

#### Scenario: Prompt-scoped lease is tied to run identity
- **GIVEN** a prompt-scoped approval lease exists
- **WHEN** a different run, scheduled run, sub-agent run, or later prompt attempts the same tool
- **THEN** the lease MUST NOT apply

## MODIFIED Requirements

## REMOVED Requirements
