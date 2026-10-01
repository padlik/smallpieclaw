## MODIFIED Requirements

### Requirement: Sub-agent grants are scoped to the sub-agent's run

The ledger MUST remain single and depth-0-owned. A prompt-lifetime grant arising from a **turn-owned child** file-operation confirmation MUST carry the child run identity and MUST expire when that child run completes; session-lifetime grants from such confirmations behave as normal session grants. The active **operator turn** may provide the existing confirmation channel to its own on-demand children under the existing path policy and grant rules. A **scheduled run** MUST NOT consult the interactive grant ledger or confirmation coordinator, regardless of whether an operator turn is active or whether a matching prompt- or session-lifetime grant exists. A scheduled operation that requires additional approval MUST be denied promptly without rendering a prompt or staging a token. Classification-before-grant ordering, prohibited-path immunity, and sink-side vetoes remain unchanged.

#### Scenario: Sub-agent prompt grant dies with the sub-agent
- **GIVEN** a turn-owned child's file-operation confirmation receives a prompt-lifetime Confirm
- **AND** the grant scope is annotated to that child run
- **WHEN** the child run completes
- **THEN** the grant is removed from the depth-0 ledger
- **AND** the operator's file operations to that directory prompt again unless independently authorised

#### Scenario: No upward privilege leak
- **GIVEN** an active operator turn has spawned a turn-owned child
- **AND** the child has a prompt-lifetime grant for `(file_write, /data)`
- **WHEN** the operator calls `file_write` on a path under `/data`
- **THEN** a confirmation prompt is sent because the child's scoped grant does not cover the operator

#### Scenario: Scheduled run never consults an existing grant
- **GIVEN** an operator turn is active and the depth-0 ledger contains a matching session grant
- **WHEN** a scheduled run attempts an operation requiring additional approval
- **THEN** the operation is denied without consulting the grant ledger
- **AND** no confirmation prompt or pending token is created

#### Scenario: Scheduled denial applies outside ordinary file operations
- **GIVEN** a scheduled run attempts a vision, MCP, or sensitive built-in operation that would require operator approval
- **WHEN** the approval boundary is reached
- **THEN** the operation is denied without consulting the interactive ledger or coordinator
- **AND** no confirmation prompt is rendered
