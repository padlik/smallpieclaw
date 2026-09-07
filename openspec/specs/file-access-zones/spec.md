# file-access-zones Specification

## Purpose

Define zone-based access control for all `file_*` built-in tool operations. A `TrustedZoneChecker` classifies each path into a zone and either auto-allows or triggers a confirmation prompt with extended response options.

## Requirements

### Requirement: Paths are classified into zones before any file operation

The system MUST resolve every path via `os.path.realpath()` and classify it via the frozen `PathPolicy` (see `path-policy` capability) into exactly one verdict — `PROHIBITED`, `ALLOWED`, or `UNRECOGNISED` — before executing any `file_*` tool operation. `PROHIBITED` paths MUST fail with a hard error (`error_type: "prohibited_path"`, reason string included); `ALLOWED` paths MUST proceed without confirmation; `UNRECOGNISED` paths MUST consult the `GrantLedger` (see `approval-grants` capability) and, on a miss, stage a confirmation prompt with `[✅ Confirm]`, `[✅✅ Till /reset]`, and `[❌ Deny]` buttons. No file content may be read before the decision for operations that stage confirmation.

#### Scenario: Prohibited path fails with a hard error
- **GIVEN** a `file_*` operation is invoked with a path under a Tier 0 prohibited entry (agent-internal directory, credential home, or config-appended entry)
- **WHEN** classification runs
- **THEN** the tool returns `Permission denied: prohibited path (<reason>)` with `error_type: "prohibited_path"`
- **AND** no confirmation prompt is rendered
- **AND** the LLM should use dedicated built-in tools (`memory_read`, `secret_get`, `log_query`) for internal data access instead

#### Scenario: Allowed path is auto-allowed without confirmation
- **GIVEN** a `file_*` tool is invoked with a path inside a Tier 1 agent-controlled dir (`workspace_dir`, `downloads_dir`, `/tmp/<agent_name>`, skills r-mode for reads, results) or a Tier 2 operator allowed dir
- **WHEN** classification runs
- **THEN** the operation proceeds immediately without any confirmation prompt

#### Scenario: Unrecognised path triggers the grant-then-prompt flow
- **GIVEN** a `file_*` tool is invoked with a path outside every allowed tier
- **WHEN** classification returns UNRECOGNISED
- **THEN** the GrantLedger is consulted for `(tool, parent-dir)`
- **AND** on a grant hit the operation proceeds without a prompt
- **AND** on a miss the operation is staged and a confirmation prompt is sent with Confirm / Till /reset / Deny buttons
- **AND** the file operation has not yet been performed

#### Scenario: Path resolution uses realpath to prevent bypass
- **GIVEN** a path contains `..` components or is a symlink pointing outside an allowed tier
- **WHEN** classification runs
- **THEN** the resolved absolute real path is used for tier comparison
- **AND** a symlink inside an allowed dir that resolves to an unrecognised or prohibited path is treated by its real path's classification

#### Scenario: Vault file path is prohibited
- **GIVEN** the vault file (`~/.local/state/<agent>/secrets.toml`) is a derived Tier 0 entry
- **WHEN** `file_read` is invoked with the vault file path (or a hardlink alias of it)
- **THEN** the call fails with a `prohibited_path` hard error
- **AND** the `secret_get` built-in tool remains the only interface for reading secrets

### Requirement: file_diff zone-checks both paths independently

`file_diff` MUST classify both `path_a` and `path_b` before executing. If either path is PROHIBITED, the entire operation MUST fail with a `prohibited_path` hard error. If either path is UNRECOGNISED (with no covering grant), the entire operation is staged for confirmation.

#### Scenario: file_diff with a prohibited path fails outright
- **GIVEN** `path_a` is inside `workspace_dir` and `path_b` is under `~/.ssh`
- **WHEN** `file_diff` is invoked
- **THEN** the operation fails with a `prohibited_path` hard error naming the prohibited path
- **AND** neither path has been read

#### Scenario: file_diff with one unrecognised path stages confirmation
- **GIVEN** `path_a` is inside `workspace_dir` (ALLOWED)
- **AND** `path_b` is outside every allowed tier (UNRECOGNISED, no covering grant)
- **WHEN** `file_diff` is invoked
- **THEN** the operation is staged and a confirmation prompt is sent
- **AND** neither path has been read at the time of the prompt

#### Scenario: file_diff with both allowed paths proceeds without confirmation
- **GIVEN** both `path_a` and `path_b` classify as ALLOWED
- **WHEN** `file_diff` is invoked
- **THEN** the diff executes immediately without any confirmation prompt