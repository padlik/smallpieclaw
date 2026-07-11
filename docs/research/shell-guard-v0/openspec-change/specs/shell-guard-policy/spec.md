## ADDED Requirements

### Requirement: Shell Guard policy is stored separately from main config
Shell Guard SHALL store persistent allow, ask, and deny rules in a separate TOML policy file.

#### Scenario: Policy file has defaults and rules
- **GIVEN** Shell Guard policy is loaded
- **WHEN** the policy file is parsed
- **THEN** it MUST support versioned TOML content
- **AND** it MUST support defaults for unknown and parse-error decisions
- **AND** it MUST support a single `rules` list with explicit decision, scope, reason, and matcher-specific fields

#### Scenario: Main config points to policy
- **GIVEN** Shell Guard is configured
- **WHEN** application config is parsed
- **THEN** main config MAY enable Shell Guard and specify mode and policy path
- **AND** persistent policy rules MUST NOT be embedded directly in the main application config

#### Scenario: Runtime and CLI use the same policy engine
- **GIVEN** a Shell Guard policy file exists
- **WHEN** the runtime evaluates policy rules
- **AND** the CLI reads, drafts, or applies policy rules
- **THEN** both surfaces MUST use the same policy parser and matching semantics
- **AND** a rule reviewed through the CLI MUST match commands the same way at runtime

### Requirement: Policy matching respects safety precedence
Shell Guard SHALL apply policy rules with a precedence order that prevents broad allow rules from overriding hard semantic safety decisions.

#### Scenario: Hard semantic deny wins over allow
- **GIVEN** a command matches a hard semantic deny category
- **AND** the command also matches an allow rule
- **WHEN** Shell Guard evaluates the command in active mode
- **THEN** the effective decision MUST be `deny`

#### Scenario: Scoped allow may satisfy hard semantic ask
- **GIVEN** a command matches a hard semantic ask category
- **AND** the command also matches an exact-command allow rule
- **WHEN** Shell Guard evaluates the command in active mode
- **THEN** the effective decision MAY be `allow`
- **AND** the metadata MUST record that a scoped allow satisfied the hard ask

#### Scenario: Broad allow cannot satisfy hard semantic ask
- **GIVEN** a command matches a hard semantic ask category
- **AND** the command also matches a binary, binary-global, or semantic allow rule
- **WHEN** Shell Guard evaluates the command in active mode
- **THEN** the broad allow rule MUST NOT override the hard semantic ask
- **AND** the effective decision MUST remain `ask` or `deny`

#### Scenario: Unknown and parse-error defaults ask
- **GIVEN** Shell Guard cannot confidently classify a command shape
- **WHEN** no stronger policy rule applies
- **THEN** the effective decision MUST be `ask`

#### Scenario: Binary-global allow is restricted
- **GIVEN** a candidate command shape is high-risk, critical-risk, or unknown-risk
- **WHEN** Shell Guard offers or applies policy scopes
- **THEN** binary-global allow MUST NOT be available for that candidate

### Requirement: Classify observations can be converted into policy candidates
Shell Guard SHALL provide a CLI-oriented workflow that converts classify-mode observations into reviewable policy candidates.

#### Scenario: Candidates command groups observations
- **GIVEN** classify-mode JSONL observations exist
- **WHEN** the user runs `python -m shell_guard policy candidates`
- **THEN** the CLI MUST group observations by normalized command shape, exact command, argv prefix, binary, and semantic category where appropriate
- **AND** each candidate MUST include observed count, sample commands, first and last seen timestamps, LLM action/risk/confidence summary, suggested scope, and reason

#### Scenario: Draft command produces reviewable TOML
- **GIVEN** classify-mode observations exist
- **WHEN** the user runs `python -m shell_guard policy draft`
- **THEN** the CLI MUST produce a human-reviewable TOML policy draft or patch-like output
- **AND** it MUST NOT mutate the active policy file by default

#### Scenario: Apply command backs up and mutates policy
- **GIVEN** policy candidates are available
- **WHEN** the user runs `python -m shell_guard policy apply`
- **THEN** the CLI MUST ask yes or no for each candidate by default
- **AND** accepted candidates MUST be merged into the active policy file
- **AND** a timestamped backup of the previous policy file MUST be created after a completed apply
- **AND** accepted rules MUST preserve explainable provenance including source, observed count or sample evidence where available, and creation time

#### Scenario: Apply all accepts candidates as-is
- **GIVEN** policy candidates are available
- **WHEN** the user runs `python -m shell_guard policy apply --all`
- **THEN** the CLI MUST apply all candidates as-is
- **AND** it MUST still create a timestamped backup of the previous policy file
- **AND** accepted rules MUST preserve explainable provenance including source, observed count or sample evidence where available, and creation time

### Requirement: Policy updates and observation reads are concurrency-safe
Shell Guard SHALL avoid partial policy writes, missing backups, and malformed observation reads when the daemon and CLI operate concurrently.

#### Scenario: Policy apply writes atomically after backup
- **GIVEN** an active Shell Guard policy file exists
- **WHEN** `python -m shell_guard policy apply` mutates the policy
- **THEN** the previous policy file MUST be copied to a timestamped backup before replacement
- **AND** the new policy MUST be written via an atomic replace operation so readers see either the old complete policy or the new complete policy
- **AND** partial policy content MUST NOT be exposed as the active policy

#### Scenario: Runtime handles policy changes deterministically
- **GIVEN** the CLI has applied a new policy file while the daemon is running
- **WHEN** the runtime evaluates a shell command
- **THEN** the runtime MUST use either the previously loaded complete policy or the newly loaded complete policy
- **AND** the design MUST NOT require reading a partially-written policy file
- **AND** the implementation MUST define whether policy reload is automatic or requires restart/reload before the new policy takes effect

#### Scenario: CLI reads append-only JSONL observations safely
- **GIVEN** the daemon is appending Shell Guard metadata events
- **WHEN** the CLI reads classify observations for candidates or drafts
- **THEN** the CLI MUST ignore incomplete trailing JSONL lines
- **AND** it MUST process only complete parseable events
- **AND** it MUST NOT corrupt or truncate the metadata file

## MODIFIED Requirements

## REMOVED Requirements
