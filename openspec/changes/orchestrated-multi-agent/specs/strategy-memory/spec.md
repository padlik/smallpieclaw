## ADDED Requirements

### Requirement: Strategy storage per task type
The system SHALL persist strategies keyed by task type (kebab-case derived from user goal classification). Each strategy entry contains: `task_type`, `approach` (string description), `confidence` (float 0-1), `success_count` (int), `failure_count` (int), `last_used` (ISO timestamp).

#### Scenario: Store successful PDF strategy
- **WHEN** a task classified as `pdf-to-text` succeeds using vision model
- **THEN** a strategy entry is created: `task_type: "pdf-to-text", approach: "Use vision model for scanned PDFs", confidence: 0.8, success_count: 1`

### Requirement: Strategy extraction from execution outcomes
After any execution completes (ReAct loop or plan execution), a background extraction pass SHALL analyze the outcome and update relevant strategies. Extraction is fire-and-forget and does not block the user response.

#### Scenario: Background strategy update
- **WHEN** a task completes
- **THEN** an extraction LLM call analyzes: task type, approach used, success/failure, lessons learned; the strategy store is updated asynchronously

### Requirement: Strategy context injection
When building the system prompt, the prompt loader SHALL query StrategyMemory for strategies matching the current task type and inject the top-K (default 2) into a `{{strategies}}` variable.

#### Scenario: Strategy influences planning
- **WHEN** the user asks to "convert a scanned PDF to text"
- **THEN** the system prompt includes: `STRATEGIES: For pdf-to-text, use vision model (confidence: 0.8)`

### Requirement: Strategy confidence decay
Strategies SHALL have confidence decay over time if not reinforced. A strategy unused for 30 days has confidence multiplied by 0.9. Strategies with confidence below 0.2 are archived (not injected) but retained for analysis.

#### Scenario: Old strategy suppressed
- **WHEN** a strategy has confidence 0.15 after decay
- **THEN** it is not injected into prompts but remains in storage

### Requirement: Strategy conflict resolution
If multiple strategies exist for the same task type, the system SHALL select by confidence. If two strategies have nearly equal confidence (within 0.1), both are injected with a note that the LLM should evaluate which applies.

#### Scenario: Two viable strategies
- **WHEN** strategies A (confidence 0.75) and B (confidence 0.72) both apply
- **THEN** both are injected: `STRATEGIES: A (0.75), B (0.72) — evaluate which applies`

### Requirement: Storage backend
StrategyMemory SHALL use a JSON file (`data/strategies.json`) as the primary storage. If graph memory is enabled, strategies MAY additionally be stored there for richer querying, but the JSON file remains the source of truth.

#### Scenario: Strategy storage with JSON primary
- **WHEN** a strategy is extracted from a successful execution
- **THEN** it is persisted to `data/strategies.json` immediately; if graph memory is enabled, it is also stored there for querying
