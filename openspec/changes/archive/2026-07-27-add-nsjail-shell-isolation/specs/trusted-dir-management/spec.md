## ADDED Requirements

### Requirement: Trusted directories are used as nsjail shell sandbox mount points

Trusted directories from `data/trusted_dirs.json` MUST be bind-mounted at their original host paths inside the nsjail jail when the nsjail shell backend is active. Directories with `mode: "rw"` are mounted read-write; directories with `mode: "r"` are mounted read-only. This extends the trusted directory concept from file-access-zone classification (ADR-0010) to shell sandbox filesystem isolation.

Feature: trusted-dir-management
Rule: The single source of truth for trusted dirs is `data/trusted_dirs.json`, managed by `/dir` commands. No separate nsjail-specific mount config exists.

#### Scenario: RW trusted dir is mounted read-write in jail
- **GIVEN** `/home/user/.cache` is in `data/trusted_dirs.json` with `mode: "rw"`
- **AND** the nsjail shell backend is active
- **WHEN** the nsjail config is generated for a shell call
- **THEN** the config contains a mount entry for `/home/user/.cache` with `rw: true`
- **AND** the agent can read and write files in `/home/user/.cache` from inside the jail

#### Scenario: RO trusted dir is mounted read-only in jail
- **GIVEN** `/srv/archive` is in `data/trusted_dirs.json` with `mode: "r"`
- **AND** the nsjail shell backend is active
- **WHEN** the nsjail config is generated for a shell call
- **THEN** the config contains a mount entry for `/srv/archive` with `rw: false`
- **AND** the agent can read files in `/srv/archive` but cannot write from inside the jail

#### Scenario: Newly added trusted dir appears in subsequent shell calls
- **GIVEN** the operator runs `/dir add /new/path` during a session
- **WHEN** the next shell call generates an nsjail config
- **THEN** `/new/path` appears as a mount entry in the config
- **AND** the directory is accessible inside the jail at its original path

#### Scenario: Removed trusted dir disappears from subsequent shell calls
- **GIVEN** `/old/path` was in `data/trusted_dirs.json` and the operator runs `/dir del N`
- **WHEN** the next shell call generates an nsjail config
- **THEN** `/old/path` does not appear as a mount entry
- **AND** the directory is not accessible inside the jail

#### Scenario: Trusted dirs are not mounted when nsjail backend is inactive
- **GIVEN** `shell_backend` is `"subprocess"` or `"pty"` (nsjail not active)
- **WHEN** the agent calls the shell tool
- **THEN** trusted dirs are not bind-mounted (no nsjail jail is created)
- **AND** the shell command runs with full host filesystem access (subject to confirmation flow)