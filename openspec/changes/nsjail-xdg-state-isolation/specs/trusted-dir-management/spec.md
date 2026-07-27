## MODIFIED Requirements

### Requirement: [Add to trusted] button persists the directory permanently

Tapping **[Add to trusted]** on an out-of-zone confirmation prompt MUST persist the parent directory of the requested path to the XDG state file (`$XDG_STATE_HOME/<agent_name>/nsjail/trusted_dirs.json`) and allow the current operation to proceed.

#### Scenario: Add to trusted persists the parent directory
- **GIVEN** the agent requests access to `/Users/paul/projects/myapp/README.md`
- **AND** the prompt is shown with `[Add to trusted]`
- **WHEN** the operator taps `[Add to trusted]`
- **THEN** `/Users/paul/projects/myapp` is added to the trusted directory list with `mode: "rw"`
- **AND** the entry is written to `$XDG_STATE_HOME/<agent_name>/nsjail/trusted_dirs.json`
- **AND** the file operation proceeds
- **AND** future accesses to any path under `/Users/paul/projects/myapp/` are silent

#### Scenario: Added directory appears immediately in /dir list
- **GIVEN** the operator taps `[Add to trusted]` for a new directory
- **WHEN** the operator sends `/dir list`
- **THEN** the newly added directory appears in the listing
