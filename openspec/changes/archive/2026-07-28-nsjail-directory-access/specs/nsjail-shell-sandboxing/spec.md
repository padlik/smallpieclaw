## REMOVED Requirements

### Requirement: Project directory is mounted at its original host path

**Reason**: The `project_dir` was wired to `_AGENT_DIR` (the agent's own code directory), mounting all agent internals — source code, memory, config, scheduler state — read-write inside every sandboxed shell command. This is a security hole: a compromised or buggy LLM-issued command can read secrets, corrupt memory, or overwrite agent code. No directory should be blanket-mounted RW by default; the sandbox should only get `/tmp` (scratch), system dirs (executables), skills dir (RO), and explicitly-approved trusted dirs.

**Migration**: Operators who previously relied on shell commands accessing files under `_AGENT_DIR` without an explicit trusted-dir entry must add those paths via `/dir add`. The sandbox `cwd` changes from the project directory to `/tmp` (the session tmpdir, already mounted RW). Shell commands that relied on `cwd` being the agent directory must use absolute paths or `cd` to a trusted dir.

## MODIFIED Requirements

### Requirement: Trusted directories are mounted at their original host paths

Trusted directories from `trusted_dirs.json` (managed by `/dir` commands) MUST be bind-mounted at their original host paths inside the jail. Directories with `mode: "rw"` are mounted read-write; directories with `mode: "r"` are mounted read-only. Paths under `/home` are accepted — the blanket `/home` block has been removed. Sensitive subdirs (`~/.ssh`, `~/.local`, `~/.config`, `~/.gnupg`) remain blocked by the targeted user-prefix blocklist.

Feature: nsjail-shell-sandboxing

#### Scenario: RW trusted dir is writable inside jail
- **GIVEN** `/home/user/.cache` is a trusted directory with `mode: "rw"`
- **WHEN** the agent runs `shell("echo data > /home/user/.cache/file.txt")` inside the jail
- **THEN** the file is written to the host filesystem at `/home/user/.cache/file.txt`

#### Scenario: RO trusted dir is read-only inside jail
- **GIVEN** `/srv/archive` is a trusted directory with `mode: "r"`
- **WHEN** the agent runs `shell("echo data > /srv/archive/file.txt")` inside the jail
- **THEN** the write fails with a permission error
- **AND** no file is created on the host

#### Scenario: Trusted dir changes are reflected in jail config
- **GIVEN** the operator adds `/new/dir` via `/dir add` during a session
- **WHEN** the next shell call generates an nsjail config
- **THEN** `/new/dir` appears as a mount entry in the config
- **AND** the directory is accessible inside the jail at its original path

#### Scenario: Trusted dir under /home is accepted
- **GIVEN** `/home/user/projects/myproject` is a trusted directory with `mode: "rw"`
- **WHEN** the nsjail config is generated
- **THEN** the directory is mounted read-write inside the jail at its original path
- **AND** no "restricted system path" warning is logged

#### Scenario: Sensitive subdir under /home is rejected
- **GIVEN** `~/.ssh` is listed in `trusted_dirs.json`
- **WHEN** the nsjail config is generated
- **THEN** the directory is NOT mounted inside the jail
- **AND** a warning is logged that the path is a restricted user path

### Requirement: nsjail config is generated dynamically per shell call

The nsjail config MUST be generated as a tempfile per shell call, combining static parts (namespaces, seccomp, system mounts, base envars) with dynamic parts (time_limit, cwd, trusted mounts, /tmp mount, command). The config MUST set `cwd` to `/tmp` (the session tmpdir). The tempfile MUST be deleted after the command completes.

Feature: nsjail-shell-sandboxing

#### Scenario: Config includes per-call timeout and command
- **GIVEN** the nsjail backend is active
- **WHEN** the agent calls `shell("make test", timeout=60)`
- **THEN** the generated nsjail config contains `time_limit: 60`
- **AND** the config contains the command as the exec target

#### Scenario: Config sets cwd to /tmp
- **GIVEN** the nsjail backend is active
- **WHEN** the agent calls `shell("pwd")`
- **THEN** the output is `/tmp`
- **AND** the generated nsjail config contains `cwd: "/tmp"`

#### Scenario: Config tempfile is cleaned up after execution
- **GIVEN** the nsjail backend generates a config tempfile for a shell call
- **WHEN** the shell command completes (success, failure, or timeout)
- **THEN** the tempfile is deleted from the filesystem

## ADDED Requirements

### Requirement: Minimal /dev nodes are mounted for shell redirections

The nsjail config MUST bind-mount `/dev/null` and `/dev/zero` read-only inside the sandbox so that shell redirections like `2>/dev/null` and `dd if=/dev/zero` work correctly. The mounts MUST use `mandatory: false` so the jail still starts even if the host lacks these device nodes.

Feature: nsjail-shell-sandboxing

#### Scenario: Shell redirection to /dev/null works inside jail
- **GIVEN** the nsjail backend is active
- **WHEN** the agent runs `shell("ls /nonexistent 2>/dev/null")` inside the jail
- **THEN** the command succeeds with empty stderr
- **AND** no "cannot create /dev/null" error occurs

#### Scenario: /dev/zero is accessible inside jail
- **GIVEN** the nsjail backend is active
- **WHEN** the agent runs `shell("dd if=/dev/zero of=/tmp/zero bs=1 count=1")` inside the jail
- **THEN** the command succeeds
- **AND** `/tmp/zero` contains a single null byte