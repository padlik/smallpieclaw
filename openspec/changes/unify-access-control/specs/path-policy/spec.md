# path-policy Specification

## Purpose

Define the single three-tier path classification policy that governs all file-tool access and nsjail mount derivation, replacing the scattered blocklists, zone checker, and sensitive-path overlay.

## ADDED Requirements

### Requirement: Every path is classified into exactly one tier before any file operation

The system MUST resolve every path via `os.path.realpath()` (with `expanduser`) and classify it via the frozen `PathPolicy` object into exactly one verdict: `PROHIBITED`, `ALLOWED` (with mode `r` or `rw`), or `UNRECOGNISED`. Classification MUST use separator-boundary matching (a path is inside a policy directory only if it equals the directory exactly or starts with the directory followed by the OS path separator, normcase-aware) to prevent sibling-prefix bypass.

#### Scenario: Sibling path with shared prefix is not covered
- **GIVEN** `/srv/shared` is an operator allowed dir
- **WHEN** a `file_*` tool is invoked with path `/srv/shared-evil/secret.txt`
- **THEN** the path classifies as UNRECOGNISED and triggers the grant/prompt flow

#### Scenario: Symlink bypass is prevented
- **GIVEN** a path contains `..` components or is a symlink inside an allowed dir that resolves outside it
- **WHEN** classification runs
- **THEN** the resolved real path is used for tier comparison and the path is not treated as allowed

### Requirement: Tier 0 — Prohibited paths are absolutely denied

`PathPolicy` MUST compose the prohibited set from: (a) a hardcoded dict of system directories that is a **superset of the current `_BLOCKED_SYSTEM_PREFIXES`** (`/etc`, `/proc`, `/sys`, `/dev`, `/boot`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/usr`, `/root`, `/var`, `/run`) plus credential homes (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`); (b) derived entries at startup: the agent XDG data home (`$XDG_DATA_HOME/<agent_name>`), XDG state home (`$XDG_STATE_HOME/<agent_name>`), config home (`~/.config/<agent_name>`), and the vault file; (c) config-appended entries from `[security] prohibited_dirs` (append-only — config can only extend the set). Each prohibited entry carries a human-readable reason string.

#### Scenario: Credential home is denied with a reason
- **GIVEN** the agent calls `file_read` with a path under `~/.ssh`
- **WHEN** classification runs
- **THEN** the tool returns a hard error `Permission denied: prohibited path (<reason>)` with `error_type: "prohibited_path"`
- **AND** no confirmation prompt is rendered

#### Scenario: Agent XDG state home is denied
- **GIVEN** the agent calls `file_read` with a path under `$XDG_STATE_HOME/<agent_name>/`
- **WHEN** classification runs
- **THEN** the tool returns a hard `prohibited_path` error
- **AND** the suggestion directs the agent to use dedicated tools (`log_query`, `memory_*`, `secret_get`) instead

#### Scenario: Config can extend but never shrink the prohibited set
- **GIVEN** `[security] prohibited_dirs = ["~/vaults"]` in config
- **WHEN** PathPolicy is constructed
- **THEN** `~/vaults` is prohibited in addition to the hardcoded and derived entries
- **AND** no config field exists to remove a hardcoded or derived prohibited entry

#### Scenario: Operator recklessness cannot weaken Tier 0
- **GIVEN** the operator configures `allowed_dirs = ["/"]`
- **WHEN** the agent calls `file_read` with a path under `~/.ssh`
- **THEN** the call still fails with a `prohibited_path` hard error
- **AND** Tier 0 semantics are unchanged in every respect

### Requirement: Prohibited files are denied by inode alias

For prohibited **files** (the vault file, the config file), PathPolicy MUST stat the entry at construction and deny any path whose `(st_dev, st_ino)` matches, so a hardlink alias of a prohibited file cannot bypass denial even when the alias path is outside every prohibited directory prefix.

#### Scenario: Hardlink alias of the vault is denied
- **GIVEN** the operator (or a prior process) created `/tmp/vault-link` as a hardlink to the vault file
- **AND** `/tmp/<agent>` is an allowed dir
- **WHEN** the agent calls `file_read("/tmp/vault-link")`
- **THEN** the call fails with a `prohibited_path` hard error

### Requirement: Tier 1 — Agent-controlled directories are hardcoded and read-write except skills

`PathPolicy` MUST include as agent-controlled (operator cannot add, remove, or modify): `workspace_dir` (rw), `downloads_dir` (rw), `/tmp/<agent_name>` (rw), `~/.<agent>/skills` (r), and `~/.<agent>/results` (rw). The read-only mode of the skills dir is hardcoded; no operator-facing configuration can alter any Tier 1 entry or its mode.

#### Scenario: Workspace access proceeds without confirmation
- **GIVEN** `workspace_dir` is `~/Documents`
- **WHEN** `file_write` is invoked with path `~/Documents/notes.txt`
- **THEN** classification returns ALLOWED (rw) and the write executes without confirmation

#### Scenario: Skills dir is read-only for write operations
- **GIVEN** skills dir at `~/.piclaw/skills`
- **WHEN** `file_write` is invoked with a path under `~/.piclaw/skills/`
- **THEN** the write is not auto-allowed by Tier 1 (skills is `r`-mode; write falls through to UNRECOGNISED)
- **AND** reads under the skills dir are auto-allowed

### Requirement: Tier 2 — Operator allowed dirs come from static config only

Operator-extended access MUST come exclusively from the `[security] allowed_dirs` config list. Entries are read-write; no mode syntax exists. Config can only extend the allowed set; it cannot remove Tier 1 defaults. A nonexistent `allowed_dirs` entry MUST produce a startup warning but MUST NOT fail construction (same treatment as `prohibited_dirs` entries that do not resolve).

#### Scenario: Allowed dir grants recursive access
- **GIVEN** `allowed_dirs = ["~/projects"]` in config
- **WHEN** `file_write` is invoked with path `~/projects/myapp/src/main.py`
- **THEN** classification returns ALLOWED (rw) and the write executes without confirmation

#### Scenario: Allowed dir cannot remove a Tier 1 default
- **GIVEN** any `allowed_dirs` configuration
- **WHEN** PathPolicy is constructed
- **THEN** `workspace_dir`, `downloads_dir`, `/tmp/<agent_name>`, skills, and results retain their Tier 1 classification

### Requirement: Conflicts are validated once at startup and prohibited wins

At PathPolicy construction, any Tier 2 allowed dir that contains or is contained by a Tier 0 prohibited path MUST trigger exactly one startup warning, and the prohibited classification MUST win for the overlapping region. An allowed dir that *contains* a prohibited path MUST NOT be jail-mounted (no partial bind mounts) while its file-plane behavior remains as configured.

#### Scenario: Allowed dir containing a prohibited path warns and is not mounted
- **GIVEN** `allowed_dirs = ["~/work"]` and `~/work/vaults` appears in `prohibited_dirs`
- **WHEN** PathPolicy is constructed
- **THEN** exactly one startup warning is emitted describing the conflict
- **AND** file access to `~/work/report.txt` is ALLOWED
- **AND** file access to `~/work/vaults/*` fails with `prohibited_path`
- **AND** `~/work` does not appear in the nsjail mount table

### Requirement: PathPolicy is frozen and session-static; construction failure fails startup

PathPolicy MUST be constructed once at startup from config + hardcoded + derived entries, exposed as a frozen object with no runtime mutators, and shared by the file tools and the nsjail builder. If construction cannot complete (e.g. invalid config structure), the agent MUST fail to start rather than degrade to a weaker policy (no "checker unwired" fallback exists).

#### Scenario: No runtime mutation of the policy
- **GIVEN** the agent is running
- **WHEN** any runtime event occurs (confirmations, tool calls, Telegram callbacks)
- **THEN** the PathPolicy object's tier contents are unchanged since construction

#### Scenario: Invalid security config fails startup
- **GIVEN** `[security] allowed_dirs` is not a list of strings
- **WHEN** the agent starts
- **THEN** startup fails with an actionable config error
- **AND** the agent does not run with a partial or default-weak policy