## ADDED Requirements

### Requirement: Shell Guard mode behavior
Shell Guard SHALL support `transparent`, `classify`, and `active` modes for operator-attended interactive depth-0 shell tool calls. Scheduled or otherwise unattended shell calls SHALL be excluded from Shell Guard enforcement, prompting, classify telemetry, and policy restrictions in v0.1 regardless of caller depth.

#### Scenario: Unconfigured Shell Guard defaults to transparent behavior
- **GIVEN** Shell Guard is not configured or enabled
- **AND** an interactive depth-0 shell command is requested
- **WHEN** the shell tool preflight runs
- **THEN** behavior MUST be identical to transparent mode
- **AND** existing shell safety and execution behavior MUST remain unchanged

#### Scenario: Transparent mode preserves existing shell behavior
- **GIVEN** Shell Guard is configured in `transparent` mode
- **AND** an interactive depth-0 shell command is requested
- **WHEN** the shell tool preflight runs
- **THEN** Shell Guard MUST NOT parse, classify, prompt, block, or log Shell Guard metadata for the command
- **AND** the existing shell safety and execution behavior MUST remain unchanged

#### Scenario: Classify mode observes without enforcing new decisions
- **GIVEN** Shell Guard is configured in `classify` mode
- **AND** an interactive depth-0 shell command is requested
- **WHEN** Shell Guard evaluates the command
- **THEN** Shell Guard MUST record a best-effort `decision_if_active`
- **AND** Shell Guard MUST NOT prompt, block, mutate policy, or enforce its own decision
- **AND** the existing dangerous-command confirmation behavior MUST continue to gate execution as it did before Shell Guard

#### Scenario: Active mode enforces Shell Guard decisions
- **GIVEN** Shell Guard is configured in `active` mode
- **AND** an interactive depth-0 shell command is requested
- **WHEN** Shell Guard evaluates the command
- **THEN** Shell Guard MUST decide `allow`, `ask`, or `deny`
- **AND** `allow` MUST execute through the existing shell backend
- **AND** `ask` MUST use Shell Guard-specific confirmation behavior
- **AND** `deny` MUST return a failure without executing the command

#### Scenario: Headless sub-agent shell behavior remains unchanged
- **GIVEN** a sub-agent or headless caller requests a shell command with `caller_depth >= 1`
- **WHEN** shell preflight runs
- **THEN** Shell Guard active/classify behavior MUST NOT apply in v0.1
- **AND** the existing regex-gated dangerous-shell deny behavior MUST remain available

#### Scenario: Scheduled job shell behavior remains unchanged
- **GIVEN** a scheduled or otherwise unattended run requests a shell command
- **AND** the run is top-level with `caller_depth = 0`
- **WHEN** shell preflight runs
- **THEN** Shell Guard active/classify behavior MUST NOT apply in v0.1
- **AND** Shell Guard MUST NOT prompt, enforce policy, or record classify telemetry for that command
- **AND** existing scheduled shell behavior MUST remain unchanged

#### Scenario: Interactive detection is not based only on caller depth
- **GIVEN** a shell command is requested with `caller_depth = 0`
- **WHEN** Shell Guard determines whether active/classify behavior applies
- **THEN** it MUST also use a typed run-origin and operator-attended signal
- **AND** it MUST distinguish interactive operator-attended runs from scheduled or unattended runs

#### Scenario: Missing run-origin defaults to unattended
- **GIVEN** a shell command is requested
- **AND** run origin or operator-attended status is unset, unknown, or ambiguous
- **WHEN** Shell Guard determines whether active/classify behavior applies
- **THEN** Shell Guard MUST treat the run as non-interactive and unattended
- **AND** Shell Guard active/classify behavior MUST NOT apply
- **AND** existing shell behavior MUST be preserved

#### Scenario: Run-origin signal gates Shell Guard applicability
- **GIVEN** a shell command is requested
- **WHEN** shell preflight evaluates Shell Guard applicability
- **THEN** Shell Guard active/classify behavior MAY apply only when run origin is interactive, operator-attended is true, and caller depth is zero
- **AND** Shell Guard active/classify behavior MUST NOT apply for scheduled, sub-agent, or otherwise unattended origins

#### Scenario: Scheduled bypass happens before Shell Guard work
- **GIVEN** a scheduled or otherwise unattended run requests a shell command
- **WHEN** shell preflight runs
- **THEN** Shell Guard MUST bypass the command before parsing, policy matching, LLM classification, metadata emission, or remote inspection
- **AND** existing behavior MUST be preserved

### Requirement: Shell Guard validation pipeline
Shell Guard SHALL use an Aegish-inspired validation pipeline that enriches an LLM decision-tree classifier with parsed shell structure without executing commands during classification.

#### Scenario: Shell structure enriches classification
- **GIVEN** an interactive shell command enters Shell Guard in `classify` or `active` mode
- **WHEN** Shell Guard prepares the command for decision
- **THEN** it MUST canonicalize the command where supported
- **AND** it MUST parse shell structure with a mature parser such as `bashlex`
- **AND** it MUST extract command units, pipelines or lists, redirects, substitutions, and annotations where available
- **AND** it MUST provide this structure to policy matching and LLM classification

#### Scenario: Classification does not execute command substitutions
- **GIVEN** a shell command contains command substitution
- **WHEN** Shell Guard classifies the command
- **THEN** Shell Guard MUST inspect or describe the substitution
- **AND** Shell Guard MUST NOT execute the substitution to determine the classification

#### Scenario: Parser failure falls back to unknown ask
- **GIVEN** Shell Guard cannot parse a shell command
- **WHEN** the command is evaluated in active mode
- **THEN** Shell Guard MUST classify the parse status as `unknown`
- **AND** the effective decision MUST be `ask`
- **AND** the prompt or metadata MUST preserve the raw command and parse error context

#### Scenario: LLM classifier maps Aegish-style actions to Shell Guard decisions
- **GIVEN** no local policy or hard deterministic decision is sufficient
- **WHEN** Shell Guard calls the LLM classifier
- **THEN** the classifier MUST return an Aegish-style action of `allow`, `warn`, or `block` with reason, risk, and confidence
- **AND** Shell Guard MUST map `allow` to `allow`, `warn` to `ask`, and `block` to `deny` unless a stricter local rule applies

### Requirement: Deterministic careless-operation floor
Shell Guard active mode SHALL preserve at least the current legacy dangerous-shell protection for destructive local command shapes and common careless-operation categories.

#### Scenario: Active mode is never weaker than legacy dangerous regex
- **GIVEN** Shell Guard is in active mode
- **AND** a command shape would have required confirmation under the legacy dangerous-shell pattern gate
- **WHEN** Shell Guard evaluates the command
- **THEN** the effective decision MUST be `ask` or `deny`
- **AND** an LLM `allow` result MUST NOT downgrade the command below confirmation

#### Scenario: Destructive local categories default to ask or deny
- **GIVEN** Shell Guard is in active mode
- **AND** a command matches a deterministic careless-operation category such as destructive recursive delete, raw device or disk write, filesystem format, critical-path clobber, broad permission or ownership change, history rewrite or forced push, or broad absolute-path mutation
- **WHEN** Shell Guard evaluates the command
- **THEN** the effective decision MUST be `ask` or `deny`
- **AND** a broad binary-global allow rule MUST NOT override the deterministic category

#### Scenario: Exact allow can approve a specific destructive-local shape
- **GIVEN** Shell Guard is in active mode
- **AND** a command matches a deterministic careless-operation category whose default decision is `ask`
- **AND** the command also matches an exact-command allow rule
- **WHEN** Shell Guard evaluates the command
- **THEN** the exact allow rule MAY satisfy the hard ask
- **AND** Shell Guard MUST still apply hard deny categories and compound-command aggregation before allowing execution

#### Scenario: Compound command aggregates most restrictive unit decision
- **GIVEN** Shell Guard evaluates a pipeline, list, or chained shell command with multiple command units
- **AND** the command units produce different decisions
- **WHEN** Shell Guard aggregates the unit decisions
- **THEN** the effective decision MUST be the most restrictive unit decision
- **AND** an allow rule on one unit MUST NOT allow the entire compound command

#### Scenario: Classify mode may use LLM without remote tools
- **GIVEN** Shell Guard is in classify mode
- **AND** no local policy or hard deterministic decision is sufficient for a non-remote command shape
- **WHEN** Shell Guard records `decision_if_active`
- **THEN** it MAY call the LLM classifier using local command structure and local read-only context only when classify-mode LLM use is enabled
- **AND** it MUST NOT use remote-script downloads or web/docs lookups by default
- **AND** metadata MUST indicate whether the LLM was used

#### Scenario: Classify mode LLM can be disabled
- **GIVEN** Shell Guard is in classify mode
- **AND** classify-mode LLM use is disabled by configuration
- **WHEN** no local policy or hard deterministic decision is sufficient
- **THEN** Shell Guard MUST NOT call the LLM classifier
- **AND** it MUST record a best-effort `decision_if_active` of `ask` for the unknown command shape
- **AND** metadata MUST record `llm.used = false`

#### Scenario: Classify mode LLM failure does not block execution
- **GIVEN** Shell Guard is in classify mode
- **AND** classify-mode LLM use is enabled
- **WHEN** the LLM classifier fails or times out
- **THEN** Shell Guard MUST continue the shell flow according to classify-mode non-enforcement rules
- **AND** it MUST record a best-effort `decision_if_active` of `ask`
- **AND** metadata MUST record the LLM error or timeout

#### Scenario: Classify mode LLM calls are bounded
- **GIVEN** Shell Guard is in classify mode
- **AND** classify-mode LLM use is enabled
- **WHEN** Shell Guard calls the LLM classifier
- **THEN** the call MUST use a configured timeout
- **AND** metadata SHOULD record classifier duration when available

#### Scenario: Classify mode may cache repeated command shapes
- **GIVEN** Shell Guard is in classify mode
- **AND** the same normalized command shape has already been classified during the current run
- **WHEN** Shell Guard records another observation for that shape
- **THEN** it MAY reuse the prior local classification result instead of issuing another LLM call
- **AND** metadata MUST indicate whether the decision came from cache when caching is used

### Requirement: Shell Guard advisor is read-only and non-authoritative
Shell Guard SHALL treat LLM advisor/classifier output as advisory classification data, not as authority to execute, approve, or mutate policy.

#### Scenario: Advisor cannot approve its own recommendation
- **GIVEN** the LLM classifier returns an `allow`, `warn`, or `block` action
- **WHEN** Shell Guard maps the classifier output to a decision
- **THEN** the classifier output MUST NOT count as user approval
- **AND** any `ask` decision MUST still require an explicit Shell Guard user decision in active mode

#### Scenario: Advisor cannot mutate policy
- **GIVEN** the LLM classifier recommends a policy rule
- **WHEN** Shell Guard handles the recommendation
- **THEN** Shell Guard MUST NOT persist the rule solely because the LLM recommended it
- **AND** policy mutation MUST require an explicit user action or CLI apply action

#### Scenario: Advisor cannot execute shell commands
- **GIVEN** Shell Guard is preparing advisor context
- **WHEN** the advisor uses supporting context
- **THEN** it MUST NOT execute shell commands or command substitutions
- **AND** it MUST NOT write files as part of classification

### Requirement: Remote URL commands require user decision without guard-side fetching
Shell Guard SHALL treat commands that access remote URLs, including remote-download-to-interpreter commands, as user-decision points in active mode without attempting deep network protection or guard-side remote fetching in v0.1.

#### Scenario: Active mode asks before remote download and execute
- **GIVEN** a shell command intends to download a remote script and pipe or pass it to an interpreter
- **AND** Shell Guard is in active mode
- **WHEN** Shell Guard evaluates the command
- **THEN** the effective decision MUST be `ask` unless a stricter deny rule applies
- **AND** the Telegram prompt MUST show the URL or remote destination context available from the command
- **AND** the prompt MUST explain that executing the command may contact the remote destination and run downloaded content
- **AND** Shell Guard MUST NOT fetch or execute the remote content before the user decides

#### Scenario: Active mode asks before remote network utility use
- **GIVEN** a shell command uses a network-capable tool such as `curl`, `wget`, `nc`, or a similar remote URL/socket command
- **AND** Shell Guard is in active mode
- **WHEN** no existing policy rule allows or denies the command
- **THEN** the effective decision MUST be `ask`
- **AND** Shell Guard MAY include LLM advice about the visible destination or command intent
- **AND** the user MUST make the execution decision

#### Scenario: Classify mode never performs remote fetches
- **GIVEN** a shell command intends to download a remote script and pipe or pass it to an interpreter
- **AND** Shell Guard is in classify mode
- **WHEN** Shell Guard records its observation
- **THEN** it MUST NOT download the remote script
- **AND** it MUST record that active mode would require remote inspection

### Requirement: Active ask fails closed without approval channel
Shell Guard SHALL deny active-mode `ask` decisions when no Shell Guard approval channel is available.

#### Scenario: Ask without approval channel is denied
- **GIVEN** Shell Guard is in active mode
- **AND** Shell Guard's effective decision is `ask`
- **AND** no Shell Guard approval channel is available
- **WHEN** Shell Guard returns the shell preflight result
- **THEN** the command MUST NOT execute
- **AND** the result MUST be a denial with a clear error explaining approval is unavailable
- **AND** Shell Guard MUST write a metadata or normal log event for the fail-closed denial

#### Scenario: Approval channel failure is unavailable
- **GIVEN** Shell Guard is in active mode
- **AND** Shell Guard's effective decision is `ask`
- **WHEN** Telegram is not configured, the bot is unreachable, prompt sending fails, or the approval request times out
- **THEN** Shell Guard MUST treat the approval channel as unavailable
- **AND** the command MUST be denied fail-closed with a clear error and metadata or normal log event

## MODIFIED Requirements

## REMOVED Requirements
