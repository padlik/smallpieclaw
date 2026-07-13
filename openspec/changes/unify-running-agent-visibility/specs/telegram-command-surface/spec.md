## ADDED Requirements

### Requirement: Agents command shows source-aware running agents
The `/agents` command SHALL display visible running sub-agent executions with distinct source/category labels.

#### Scenario: Agents list shows source labels
- **GIVEN** active visible agent records exist for multiple source categories
- **WHEN** an authorized operator runs `/agents`
- **THEN** the response includes each visible record
- **AND** each record shows whether it is `on-demand`, `scheduled`, `plan-step`, or `diagnostic`

#### Scenario: Managed cancellation help describes capacity scope
- **GIVEN** an authorized operator views `/agents` help or an empty `/agents` list
- **WHEN** the response mentions managed cancellation
- **THEN** it explains that managed cancellation applies to globally capacity-counted sources

#### Scenario: Explicit cancellation works for all visible sources
- **GIVEN** a visible running agent has any supported source category
- **WHEN** an authorized operator runs `/agents cancel <id-or-label>` for that record
- **THEN** cancellation is requested for the matching active record

### Requirement: Status command active-agent count uses visible registry records
The `/status` command SHALL report the total number of active visible sub-agent records in the global registry.

#### Scenario: Status count includes plan-step and diagnostic records
- **GIVEN** active registry records exist for `on-demand`, `scheduled`, `plan-step`, and `diagnostic` sources
- **WHEN** an authorized operator requests `/status`
- **THEN** the active-agent count includes all visible registered records
- **AND** the count is not limited to globally capacity-counted records

## MODIFIED Requirements

## REMOVED Requirements
