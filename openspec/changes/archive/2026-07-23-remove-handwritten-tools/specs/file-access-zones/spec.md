# file-access-zones Delta Spec: remove-handwritten-tools

## MODIFIED Requirements

### Requirement: Paths are classified into zones before any file operation

The system MUST resolve every path via `os.path.realpath()` and classify it into exactly one zone before executing any `file_*` tool operation.

#### Scenario: Agent-internal path requires confirmation
- **GIVEN** a `file_*` operation is invoked with a path inside an agent-internal directory (`data/`, `skills/`, `prompts/`, log dir, vault dir)
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

#### Scenario: Vault file path is UNRECOGNISED and confirmation-gated
- **GIVEN** the vault file (`~/.local/share/<agent>/secrets.toml`) is an agent-internal path
- **WHEN** `file_read` is invoked with the vault file path
- **THEN** the path classifies as UNRECOGNISED and a confirmation prompt is sent
- **AND** the `secret_get` built-in tool remains the intended interface for reading secrets

#### Scenario: Trust-store and vault remain UNRECOGNISED even when parent dir is trusted
- **GIVEN** a user has added `data/` or `~/.local/share/<agent>/` to their trusted directories
- **WHEN** `file_read` or `file_write` is invoked with the path of `data/trusted_dirs.json` or the vault file
- **THEN** the path classifies as UNRECOGNISED and a confirmation prompt is sent
- **AND** the parent-dir trust entry does not grant access to the trust store or the vault