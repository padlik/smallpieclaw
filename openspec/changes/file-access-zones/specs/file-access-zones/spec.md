# file-access-zones Specification

## Purpose

Define zone-based access control for all `file_*` built-in tool operations. A `TrustedZoneChecker` classifies each path into a zone and either auto-allows or triggers a confirmation prompt with extended response options.

## ADDED Requirements

### Requirement: Paths are classified into zones before any file operation

The system MUST resolve every path via `os.path.realpath()` and classify it into exactly one zone before executing any `file_*` tool operation.

#### Scenario: Agent-internal path requires confirmation
- **GIVEN** a `file_*` operation is invoked with a path inside an agent-internal directory (`data/`, `tools/`, `tools_generated/`, `skills/`, `prompts/`, log dir, vault dir)
- **WHEN** zone classification runs
- **THEN** the operation is staged and a confirmation prompt is sent
- **AND** the LLM should use dedicated built-in tools (`memory_read`, `secret_get`, `log_query`) for internal data access instead

#### Scenario: Default trusted path is auto-allowed without confirmation
- **GIVEN** a `file_*` tool is invoked with a path inside a default trusted directory (`workspace_dir`, `downloads_dir`, `tmp_dir`)
- **WHEN** zone classification runs
- **THEN** the operation proceeds immediately without any confirmation prompt

#### Scenario: User-added trusted path is auto-allowed without confirmation
- **GIVEN** a user has added `/srv/shared` to the trusted directory list (with default `"rw"` mode)
- **AND** a `file_*` tool is invoked with a path under `/srv/shared/`
- **WHEN** zone classification runs
- **THEN** the operation proceeds immediately without any confirmation prompt

#### Scenario: Unrecognised path triggers a confirmation prompt
- **GIVEN** a `file_*` tool is invoked with a path outside all trusted zones
- **WHEN** zone classification runs
- **THEN** the operation is staged and a confirmation prompt is sent
- **AND** the prompt includes options: `[Approve]`, `[Deny]`, `[Allow this request]`, `[Add to trusted]`
- **AND** the prompt shows `[Allow this request]` and `[Add to trusted]` because the zone is UNRECOGNISED
- **AND** the file operation has not yet been performed

#### Scenario: Path resolution uses realpath to prevent bypass
- **GIVEN** a path contains `..` components or is a symlink pointing outside a trusted zone
- **WHEN** zone classification runs
- **THEN** the resolved absolute real path is used for zone comparison
- **AND** a symlink inside a trusted dir that resolves to a path outside all trusted zones is treated as unrecognised

### Requirement: Trusted directories support read-only mode

A user-added trusted directory entry MAY carry a `mode` field: `"r"` (read-only) or `"rw"` (read-write, default). The mode is checked at classify time based on the requested operation type passed by each `file_*` tool.

#### Scenario: Read-only trusted dir auto-allows reads
- **GIVEN** `/srv/archive` is a trusted directory with `mode: "r"`
- **WHEN** `file_read` is invoked with a path under `/srv/archive/`
- **THEN** the operation proceeds immediately without any confirmation prompt

#### Scenario: Read-only trusted dir requires confirmation for writes
- **GIVEN** `/srv/archive` is a trusted directory with `mode: "r"`
- **WHEN** `file_write` is invoked with a path under `/srv/archive/`
- **THEN** the operation is staged and a confirmation prompt is sent

### Requirement: Sensitive pattern gate stacks on top of zone classification

The existing sensitive-path confirmation (matching `.key`, `.env`, `secrets.*`, SSH keys, etc.) MUST apply even when a path is inside a trusted zone.

#### Scenario: Sensitive file in trusted zone still prompts
- **GIVEN** a path inside `workspace_dir` matches a sensitive pattern (e.g. `~/Documents/.env`)
- **WHEN** `file_read` is invoked
- **THEN** a confirmation prompt is sent despite the path being in a trusted zone

#### Scenario: Sensitive file in trusted zone shows only Approve/Deny
- **GIVEN** a path inside `workspace_dir` matches a sensitive pattern (e.g. `~/Documents/.env`)
- **WHEN** `file_read` is invoked
- **THEN** a confirmation prompt is sent with only `[Approve]` and `[Deny]`
- **AND** `[Allow this request]` and `[Add to trusted]` are NOT shown

### Requirement: file_write and file_patch inside trusted zones do not require confirmation

`file_write` and `file_patch` MUST NOT stage confirmation for paths in trusted (`rw`) or request-granted zones (unless the sensitive-pattern gate applies).

#### Scenario: file_write to workspace proceeds without confirmation
- **GIVEN** `workspace_dir` is `~/Documents` (default)
- **WHEN** `file_write` is invoked with path `~/Documents/notes.txt`
- **THEN** the write executes immediately without any confirmation prompt

#### Scenario: file_write outside trusted zones is staged for confirmation
- **GIVEN** a path is outside all trusted zones (including agent-internal directories)
- **WHEN** `file_write` is invoked
- **THEN** the operation is staged and a confirmation prompt is sent

### Requirement: Request grant allows a directory for the duration of one request

Approving **[Allow this request]** MUST grant access to the parent directory of the requested path for all subsequent `file_*` calls within the same user request cycle, and MUST NOT persist across request boundaries.

#### Scenario: Allow-this-request grants the parent directory
- **GIVEN** the agent requests access to `/Users/paul/work/reports/q1.txt`
- **AND** the user taps `[Allow this request]`
- **WHEN** the agent subsequently accesses `/Users/paul/work/reports/q2.txt` in the same request
- **THEN** the second access proceeds without a confirmation prompt

#### Scenario: Allow-this-request grant does not cover parent directories
- **GIVEN** `/Users/paul/work/reports/` was granted via `[Allow this request]`
- **WHEN** the agent accesses `/Users/paul/work/other.txt` in the same request
- **THEN** a confirmation prompt is sent (parent dir is not covered)

#### Scenario: Request grants are cleared at the start of each new user request
- **GIVEN** a directory was granted via `[Allow this request]` during a previous request
- **WHEN** the user sends a new message and the react loop starts
- **THEN** that directory grant is no longer active
- **AND** accessing a path in that directory prompts again

### Requirement: Zone containment uses separator-boundary matching

Path containment MUST use separator-aware prefix matching to prevent sibling-path bypass.
A path is contained in a zone directory only if it equals the zone directory exactly or
starts with the zone directory followed by the OS path separator.

#### Scenario: Sibling path with shared prefix is not in the zone
- **GIVEN** `/srv/shared` is a trusted directory
- **AND** a `file_*` tool is invoked with path `/srv/shared-evil/secret.txt`
- **WHEN** zone classification runs
- **THEN** `/srv/shared-evil/secret.txt` is classified as UNRECOGNISED
- **AND** a confirmation prompt is sent

#### Scenario: Exact zone directory match is allowed
- **GIVEN** `/srv/shared` is a trusted directory
- **WHEN** a `file_*` tool is invoked with path `/srv/shared/file.txt`
- **THEN** the operation proceeds without confirmation
