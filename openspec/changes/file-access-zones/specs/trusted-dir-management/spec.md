# trusted-dir-management Specification

## Purpose

Define the operator interface for managing user-defined trusted directories: the `/dir` Telegram command and the `[Add to trusted]` inline button on out-of-zone confirmation prompts.

## ADDED Requirements

### Requirement: /dir list shows only user-added trusted directories

The `/dir list` command MUST display only user-added trusted directories. Default protected directories (`workspace_dir`, `downloads_dir`, `tmp_dir`) MUST NOT appear in the listing.

#### Scenario: /dir list shows user-added dirs with enumeration
- **GIVEN** the user has added `/Users/paul/projects` and `/srv/data` to trusted dirs
- **WHEN** the operator sends `/dir list`
- **THEN** the response lists both directories with sequential numbers starting at 1
- **AND** the listing is sorted by directory path name
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
- **THEN** `/Users/paul/projects/myapp` is added to the trusted directory list
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
