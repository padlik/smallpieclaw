### Requirement: skills_dir is mounted read-only inside the nsjail sandbox

When the `skills_dir` directory exists on the host filesystem and is not a blocked sensitive **system** path, the nsjail config builder MUST emit a read-only bind-mount entry for it inside the sandbox. This makes skill scripts and binaries referenced in the system prompt's AVAILABLE SKILLS section accessible to shell commands running inside the jail. The mount MUST be skipped silently (with a debug log) if the directory does not exist. The mount MUST be skipped with a warning log if the path is a blocked sensitive **system** path (`/etc`, `/proc`, `/sys`, `/dev`, `/boot`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/usr`, `/root`, `/var`, `/run`). The mount is **exempt** from the user-home prefix blocklist (`~/.ssh`, `~/.local`, `~/.config`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`, `~/.cache`) because `skills_dir` is mounted read-only (`rw: false`) and the blocklist was designed to prevent sensitive read-write trusted-dir mounts. This allows `skills_dir` under common XDG-style paths (e.g. `~/.config/opencode/skills/`, `~/.local/share/agent/skills/`) to be mounted read-only inside the jail.

Feature: skills-dir-sandbox-mount

#### Scenario: Skill script is executable inside the jail
- **GIVEN** the agent has a `skills/` directory containing an executable script `skills/deploy.sh`
- **AND** `skills_dir` is configured (default: `"skills"`, resolved relative to the agent home)
- **WHEN** the agent runs `shell("/home/user/.agents/skills/deploy.sh")` inside the nsjail sandbox
- **THEN** the command executes successfully
- **AND** the script output is returned to the agent

#### Scenario: Skill directory is read-only inside the jail
- **GIVEN** the `skills_dir` is mounted inside the nsjail sandbox
- **WHEN** the agent runs `shell("echo hacked > /home/user/.agents/skills/deploy.sh")` inside the jail
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

#### Scenario: skills_dir under /home is accepted
- **GIVEN** `skills_dir` resolves to `/home/user/.agents/skills`
- **WHEN** the nsjail config is generated
- **THEN** the directory is mounted read-only inside the jail
- **AND** no "restricted system path" warning is logged

#### Scenario: skills_dir on a blocked system path is rejected
- **GIVEN** `skills_dir` resolves to `/etc/skills` (under a blocked system prefix)
- **WHEN** the nsjail config is generated
- **THEN** no mount entry for `skills_dir` is emitted
- **AND** a warning is logged that the path is a restricted system path

#### Scenario: skills_dir under a blocked user-home prefix is accepted
- **GIVEN** `skills_dir` resolves to `~/.local/share/agent/skills` (under a blocked user prefix)
- **WHEN** the nsjail config is generated
- **THEN** the directory is mounted read-only inside the jail
- **AND** no "restricted path" warning is logged
- **AND** the mount entry has `rw: false`