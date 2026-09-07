# skills-dir-sandbox-mount Delta

## Purpose

Skills relocate from the XDG state home to `~/.<agent>/skills` (outside the now-prohibited XDG homes), are mounted read-only from the new location, and migrate one-time on first run. The XDG-derived-path rule is replaced by the agent dot-home rule.

## MODIFIED Requirements

### Requirement: skills_dir is mounted read-only inside the nsjail sandbox

When the skills directory exists on the host filesystem, the nsjail config builder MUST emit a read-only bind-mount entry for it inside the sandbox. The skills directory is `~/.<agent_name>/skills/` — a Tier 1 agent-controlled entry in PathPolicy (hardcoded, operator cannot modify, read-only). The mount MUST be skipped silently (with a debug log) if the directory does not exist. Because skills is a hardcoded Tier 1 entry whose path is fixed relative to the agent dot-home, no blocklist interaction exists: the path cannot collide with Tier 0 by construction, and no exemption logic is needed.

Feature: skills-dir-sandbox-mount
Rule: `skills_dir` is `~/.<agent_name>/skills/` — hardcoded Tier 1, read-only, outside the XDG homes. It is not configurable.

#### Scenario: Skill script is executable inside the jail
- **GIVEN** the agent has a `skills/` directory at `~/.piclaw/skills/` containing an executable script `deploy.sh`
- **WHEN** the agent runs `shell("~/.piclaw/skills/deploy.sh")` inside the nsjail sandbox
- **THEN** the command executes successfully
- **AND** the script output is returned to the agent

#### Scenario: Skill directory is read-only inside the jail
- **GIVEN** the skills dir is mounted inside the nsjail sandbox
- **WHEN** the agent runs `shell("echo hacked > ~/.piclaw/skills/deploy.sh")` inside the jail
- **THEN** the write fails with a permission error
- **AND** the original file on the host is unchanged

#### Scenario: Missing skills_dir is skipped gracefully
- **GIVEN** `~/.<agent>/skills/` does not exist on the host filesystem
- **WHEN** the session nsjail config is generated
- **THEN** no mount entry for skills is emitted
- **AND** a debug log records that the directory was skipped
- **AND** shell commands proceed without error

#### Scenario: skills_dir uses the agent dot-home path
- **GIVEN** `agent_name` is `"piclaw"` and there is no `skills_dir` config parameter
- **WHEN** the session nsjail config is generated
- **THEN** the mount entry points to `~/.piclaw/skills/`
- **AND** the directory is accessible inside the jail at the same host path

#### Scenario: skills_dir uses the XDG-derived path
- **GIVEN** `agent_name` is `"piclaw"` and there is no `skills_dir` config parameter
- **WHEN** the session nsjail config is generated
- **THEN** the mount entry points to `~/.piclaw/skills/` (agent dot-home, not the XDG state home)
- **AND** the legacy XDG-derived path `~/.local/state/piclaw/skills/` is NOT mounted directly

#### Scenario: skills_dir under /home is accepted
- **GIVEN** the agent dot-home skills dir resolves to `~/.piclaw/skills/` (under `/home/<user>`)
- **WHEN** the session nsjail config is generated
- **THEN** the directory is mounted read-only inside the jail
- **AND** no "restricted system path" warning is logged

#### Scenario: skills_dir on a blocked system path is rejected
- **GIVEN** `skills_dir` is hardcoded to `~/.<agent>/skills/` which is always under the user home
- **WHEN** the session nsjail config is generated
- **THEN** the path is never on a blocked system prefix (e.g. `/etc`, `/usr`) by construction
- **AND** no rejection or skip occurs for the skills mount

#### Scenario: skills_dir under a blocked user-home prefix is accepted
- **GIVEN** the skills dir at `~/.<agent>/skills/` is a hardcoded Tier 1 entry in PathPolicy
- **WHEN** the session nsjail config is generated
- **THEN** the directory is mounted read-only inside the jail regardless of XDG prefix overlap
- **AND** the mount entry has `rw: false`

## ADDED Requirements

### Requirement: Skills migrate one-time from the XDG state home on first run

On startup, if `~/.<agent>/skills/` does not exist (or is empty) and the legacy XDG-derived skills directory (`$XDG_STATE_HOME/<agent_name>/skills/`) exists and is non-empty, the system MUST copy the legacy directory contents into `~/.<agent>/skills/` before PathPolicy construction and MUST leave the legacy directory untouched (copy, not move). The copy MUST be idempotent (subsequent startups skip it when the target is populated).

#### Scenario: First run copies legacy skills
- **GIVEN** `~/.local/state/piclaw/skills/` contains two skill folders and `~/.piclaw/skills/` does not exist
- **WHEN** the agent starts
- **THEN** `~/.piclaw/skills/` is created containing both skill folders
- **AND** the legacy directory still exists unchanged

#### Scenario: Populated target skips the copy
- **GIVEN** `~/.piclaw/skills/` already contains a skill folder
- **WHEN** the agent starts
- **THEN** no migration copy runs (idempotent)
- **AND** existing skills are untouched