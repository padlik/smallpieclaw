## ADDED Requirements

### Requirement: Trusted dirs list is stored outside sandbox write scope

The nsjail backend MUST read `trusted_dirs.json` from the XDG state directory (`$XDG_STATE_HOME/<agent_name>/nsjail/trusted_dirs.json`), NOT from the agent installation data directory. This prevents a sandboxed shell command from overwriting the file and injecting arbitrary mount paths on the next build.

#### Scenario: trusted_dirs.json is read from XDG state dir, not agent data dir
- **GIVEN** the agent is started with `shell_backend: "nsjail"`
- **AND** `$XDG_STATE_HOME` is set to `/home/user/.local/state`
- **AND** the agent name is `myagent`
- **WHEN** a shell command triggers nsjail config generation
- **THEN** the builder reads trusted dirs from `/home/user/.local/state/myagent/nsjail/trusted_dirs.json`
- **AND** no trusted dirs file is read from the agent installation directory

#### Scenario: XDG state dir is created on first use if absent
- **GIVEN** `/home/user/.local/state/myagent/nsjail/` does not exist
- **WHEN** the first nsjail shell command is executed
- **THEN** the directory is created automatically
- **AND** an empty trusted dirs list is used for that build

#### Scenario: nsjail_state_dir config field overrides XDG default
- **GIVEN** `nsjail_state_dir: "/opt/agent/nsjail-state"` is set in agent config
- **WHEN** nsjail config generation runs
- **THEN** trusted dirs are read from `/opt/agent/nsjail-state/trusted_dirs.json`
- **AND** the XDG default path is not used

### Requirement: Agent installation directory is blocked from trusted mounts

The nsjail backend MUST reject any trusted directory entry whose resolved real path equals or starts with the agent installation directory. This prevents re-mounting the agent source, config, or secrets inside the jail.

#### Scenario: Entry pointing at agent dir is silently rejected
- **GIVEN** `trusted_dirs.json` contains an entry for the agent installation directory
- **WHEN** nsjail config generation runs `_load_trusted_mounts`
- **THEN** the agent dir entry is skipped with a WARNING log
- **AND** the agent dir is NOT mounted inside the jail

#### Scenario: Entry pointing at a subdirectory of agent dir is rejected
- **GIVEN** `trusted_dirs.json` contains an entry for `<agent_dir>/data`
- **WHEN** nsjail config generation runs `_load_trusted_mounts`
- **THEN** that entry is skipped with a WARNING log

#### Scenario: Entry pointing at XDG state dir is rejected
- **GIVEN** `trusted_dirs.json` contains an entry for the XDG state directory
- **WHEN** nsjail config generation runs `_load_trusted_mounts`
- **THEN** that entry is skipped with a WARNING log
