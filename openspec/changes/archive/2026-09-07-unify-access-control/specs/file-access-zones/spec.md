# file-access-zones Delta

## Purpose

Replaces the TrustedZoneChecker-based gating with PathPolicy classification + the GrantLedger confirmation flow, while preserving realpath discipline and confirm-before-read for `file_patch`.

## MODIFIED Requirements

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

## REMOVED Requirements

### Requirement: file_write and file_patch inside trusted zones do not require confirmation
**Reason**: Subsumed by the MODIFIED requirement "Paths are classified into zones before any file operation": the ALLOWED verdict (Tier 1 + Tier 2) now carries exactly this guarantee — file_write and file_patch proceed without confirmation for allowed paths — and the deleted concepts this requirement depended on (request-granted zones, the sensitive-pattern gate) no longer exist.

**Migration**: None. The behavior is preserved via the classification requirement's ALLOWED branch and the GrantLedger (see `approval-grants`).

### Requirement: Trusted directories support read-only mode
**Reason**: The dynamic trust store and its r/rw mode juggling are removed. Read-only-ness exists only in hardcoded Tier 1 (skills dir). Operator config (`allowed_dirs`) is rw-only by design.

**Migration**: Operators who used r-mode trusted dirs either add them to `allowed_dirs` (rw) or leave them unrecognised (prompt-gated). No config syntax for read-only entries exists.

### Requirement: Sensitive pattern gate stacks on top of zone classification
**Reason**: Naming-convention gating protected nothing (renaming trivially bypassed it) and generated approval-fatigue noise. Credential *directories* are now hard-denied (Tier 0); secrets belong in the vault.

**Migration**: None. Files named `.env`, `config.toml`, `.pem`, etc. inside allowed dirs are now freely accessible per the operator's tier decision; sensitive locations should be prohibited via `prohibited_dirs` or stored in the vault.

### Requirement: Request grant allows a directory for the duration of one request
**Reason**: The per-message `GrantTracker` mechanism is superseded by the unified GrantLedger, whose prompt-lifetime grants use the same clearing boundary (react loop entry) with the same scope semantics (parent dir, recursive).

**Migration**: The `[Allow this request]` button is removed; its function is covered by the prompt-lifetime grant created by `[✅ Confirm]` (see `approval-grants`).

### Requirement: Zone containment uses separator-boundary matching
**Reason**: Not removed conceptually — the matching rule moves into the `path-policy` capability where classification now lives, together with its scenarios (sibling-prefix bypass prevention, exact-match allowance).

**Migration**: See `path-policy` spec, requirement "Every path is classified into exactly one tier before any file operation".