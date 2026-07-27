# trusted-dir-management Specification

## Purpose

Define the operator interface for managing user-defined trusted directories: the `/dir` Telegram command and the `[Add to trusted]` inline button on out-of-zone confirmation prompts.

## ADDED Requirements

### Requirement: /dir list shows only user-added trusted directories

The `/dir list` command MUST display only user-added trusted directories. Default protected directories (`workspace_dir`, `downloads_dir`, `tmp_dir`) MUST NOT appear in the listing.

#### Scenario: /dir list shows user-added dirs with enumeration and mode
- **GIVEN** the user has added `/Users/paul/projects` (rw) and `/srv/data` (r) to trusted dirs
- **WHEN** the operator sends `/dir list`
- **THEN** the response lists both directories with sequential numbers starting at 1
- **AND** the listing is sorted by directory path name
- **AND** each entry shows its mode annotation (`[rw]` or `[r]`)
- **AND** default trusted directories are not shown

#### Scenario: /dir list shows empty state message when no dirs added
- **GIVEN** no user-added trusted directories exist
- **WHEN** the operator sends `/dir list`
- **THEN** the response is `No custom trusted directories added yet.`

### Requirement: /dir del removes a user-added trusted directory without confirmation

The `/dir del N` command MUST remove the Nth user-added trusted directory immediately, without any confirmation prompt. Removal is always safe because the next access to that path will simply prompt again.

#### Scenario: /dir del removes a valid entry
- **GIVEN** `/dir list` shows entry 2 as `/srv/data`
- **WHEN** the operator sends `/dir del 2`
- **THEN** `/srv/data` is removed from the trusted list
- **AND** the response confirms the removal: `Removed: /srv/data`
- **AND** subsequent `/dir list` no longer shows that entry

#### Scenario: /dir del with invalid index returns an error
- **GIVEN** the user-added list has 2 entries
- **WHEN** the operator sends `/dir del 5`
- **THEN** the response is `No trusted directory #5.`
- **AND** no entry is removed

#### Scenario: /dir del renumbers remaining entries
- **GIVEN** `/dir list` shows entries 1, 2, 3
- **WHEN** the operator sends `/dir del 1`
- **THEN** the remaining two entries are renumbered starting from 1 in subsequent `/dir list` output

### Requirement: [Add to trusted] button persists the directory permanently

Tapping **[Add to trusted]** on an out-of-zone confirmation prompt MUST persist the parent directory of the requested path to `data/trusted_dirs.json` and allow the current operation to proceed.

#### Scenario: Add to trusted persists the parent directory
- **GIVEN** the agent requests access to `/Users/paul/projects/myapp/README.md`
- **AND** the prompt is shown with `[Add to trusted]`
- **WHEN** the operator taps `[Add to trusted]`
- **THEN** `/Users/paul/projects/myapp` is added to the trusted directory list with `mode: "rw"`
- **AND** the file operation proceeds
- **AND** future accesses to any path under `/Users/paul/projects/myapp/` are silent

#### Scenario: Added directory appears immediately in /dir list
- **GIVEN** the operator has just tapped `[Add to trusted]` for `/Users/paul/projects/myapp`
- **WHEN** the operator sends `/dir list`
- **THEN** `/Users/paul/projects/myapp` appears in the listing

#### Scenario: data/trusted_dirs.json is created on first add
- **GIVEN** `data/trusted_dirs.json` does not exist
- **WHEN** the operator taps `[Add to trusted]` for the first time
- **THEN** `data/trusted_dirs.json` is created with the new entry
- **AND** the agent starts with an empty user-added list when the file is absent (no error)

### Requirement: Default trusted directories cannot be removed

Default protected directories (`workspace_dir`, `downloads_dir`, `tmp_dir`) MUST remain trusted regardless of any `/dir del` command or UI action.

#### Scenario: Default dirs are not removable
- **GIVEN** default trusted dirs are fixed by config
- **WHEN** the operator interacts with `/dir`
- **THEN** default dirs do not appear in `/dir list` enumeration
- **AND** there is no mechanism to remove them via `/dir del`

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
