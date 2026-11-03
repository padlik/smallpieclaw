# native-tool-calling Delta

## Purpose

Baseline: canonical `openspec/specs/native-tool-calling/spec.md` as already changed by the pending predecessor delta `openspec/changes/provider-model-catalog/specs/native-tool-calling/spec.md` (implementation baseline `a57ab26`), which modifies `Tool definition assembly` to count definitions toward actual-wire budgets. This delta MUST be synchronized only after that predecessor delta. It replaces the budget-enforcement clause with informational accounting and threshold-based compaction. `Native tool calling path`, `Special-case tool interception`, `LLMProvider protocol extension`, and `Provider-native tool calling implementations` remain as left by the predecessor, including native-first dispatch, local unsupported-native conversion, agent-owned retries, and omission of `temperature` and `top_p`.

## MODIFIED Requirements

### Requirement: Tool definition assembly

The system SHALL build OpenAI-format definitions from all 23 existing built-in schemas, the `plan` pseudo-tool schema, and MCP input schemas. It SHALL exclude script tools and cache definitions across loop steps. Complete tool definitions SHALL count toward the informational token estimate and the compaction threshold comparison; they SHALL NOT be used to reject or resize a request locally.

#### Scenario: Built-in tools included
- **GIVEN** `BUILTIN_TOOL_SCHEMAS` contains all 23 existing built-in tools
- **WHEN** `build_tool_definitions()` is called
- **THEN** every built-in produces the established OpenAI function schema

#### Scenario: Pseudo-tools included
- **GIVEN** `PSEUDO_TOOL_SCHEMAS` contains only `plan`
- **WHEN** definitions are built
- **THEN** `plan` is emitted in the established function schema

#### Scenario: MCP tools included
- **GIVEN** an MCP manager has registered tools
- **WHEN** definitions are built
- **THEN** each uses its MCP `input_schema`

#### Scenario: Script tools excluded
- **GIVEN** `.sh` or `.py` script tools are registered
- **WHEN** definitions are built
- **THEN** script tools do not appear in the provider tool array

#### Scenario: Tool definitions cached
- **GIVEN** the ReAct loop iterates multiple steps
- **WHEN** tool definitions are first assembled
- **THEN** the same definitions are reused across loop steps

#### Scenario: Tool definitions count toward request budget
- **GIVEN** the request contains tool definitions whose estimate pushes the total above the compaction threshold
- **WHEN** the request is prepared
- **THEN** the definitions count in the estimate and trigger at most one compaction attempt when automatic compaction is enabled for the run
- **AND** the full definition array is still sent without local rejection, truncation, or removal of tools

## ADDED Requirements

### Requirement: Native history integrity is validated before send

Before any send or compaction, native history SHALL be validated so that every assistant tool-call group has well-formed calls with unique non-empty IDs immediately followed by exactly one string tool result per ID. Malformed history SHALL fail with a fixed sanitized error before provider I/O and SHALL NOT be repaired.

#### Scenario: Malformed native group is rejected
- **GIVEN** history contains an assistant tool call without its matching tool result
- **WHEN** the next request or a compaction is prepared
- **THEN** it fails with the fixed malformed-history error before provider I/O
- **AND** the history is not repaired

### Requirement: Wire payload integrity holds without budget preflight

Removing request budgeting SHALL NOT remove protocol validation. The wire payload's model SHALL equal the pinned upstream ID, and tool definitions sent to every adapter, including Ollama, SHALL be the assembled schemas without truncation, dropping, or size-driven modification. No preview, fit estimate, or budget check SHALL gate the send.

#### Scenario: Payload model must match the pin
- **GIVEN** a prepared payload names a model other than the pinned upstream ID
- **WHEN** the client validates it
- **THEN** it fails permanently before the request is sent

#### Scenario: Ollama receives the complete tool schemas
- **GIVEN** a run pinned to a cloud Ollama connection with many tool definitions
- **WHEN** a native request is sent
- **THEN** the Ollama client receives every assembled tool schema unchanged
- **AND** no tool is dropped to reduce payload size
