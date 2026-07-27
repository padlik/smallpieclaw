### Requirement: skills_dir is mounted read-only inside the nsjail sandbox

When the `skills_dir` directory exists on the host filesystem and is NOT nested under the project directory (which is already mounted read-write), the nsjail config builder MUST emit a read-only bind-mount entry for it inside the sandbox. This makes skill scripts and binaries referenced in the system prompt's AVAILABLE SKILLS section accessible to shell commands running inside the jail. The mount MUST be skipped silently (with a debug log) if the directory does not exist, and skipped with a warning log if it is nested under the project directory (already accessible via the project mount).

Feature: skills-dir-sandbox-mount

#### Scenario: Skill script is executable inside the jail
- **GIVEN** the agent has a `skills/` directory containing an executable script `skills/deploy.sh`
- **AND** `skills_dir` is configured (default: `"skills"`, resolved relative to the agent home)
- **AND** the current working directory inside the jail is the project directory
- **WHEN** the agent runs `shell("./skills/deploy.sh")` inside the nsjail sandbox
- **THEN** the command executes successfully
- **AND** the script output is returned to the agent

#### Scenario: Skill directory is read-only inside the jail
- **GIVEN** the `skills_dir` is mounted inside the nsjail sandbox
- **WHEN** the agent runs `shell("echo hacked > skills/deploy.sh")` inside the jail
- **THEN** the write fails with a permission error
- **AND** the original file on the host is unchanged

#### Scenario: Missing skills_dir is skipped gracefully
- **GIVEN** the configured `skills_dir` does not exist on the host filesystem
- **WHEN** the nsjail config is generated for a shell command
- **THEN** no mount entry for `skills_dir` is emitted
- **AND** a debug log records that the directory was skipped
- **AND** the shell command proceeds without error

#### Scenario: skills_dir uses the configured path
- **GIVEN** the operator has set `skills_dir = "my_skills"` in the `[paths]` section
- **AND** the directory `my_skills/` exists relative to the agent home
- **WHEN** the nsjail config is generated
- **THEN** the mount entry points to the resolved absolute path of `my_skills/`
- **AND** the directory is accessible inside the jail at the same host path

#### Scenario: skills_dir nested under project directory is skipped
- **GIVEN** `skills_dir` is configured as `"skills"` and resolves to a path under the project directory
- **WHEN** the nsjail config is generated
- **THEN** no separate mount entry for `skills_dir` is emitted
- **AND** a warning log records that the directory was skipped (already accessible via the project mount)
- **AND** the shell command proceeds without error