## ADDED Requirements

### Requirement: nsjail shell backend provides kernel-level isolation

When `shell_backend` is set to `"nsjail"` and the nsjail binary is available, the shell tool MUST execute commands inside an nsjail sandbox with mount, PID, net, user, IPC, UTS, and cgroup namespace isolation, seccomp-bpf syscall filtering, and cgroup v2 resource limits. If the nsjail binary is not found, the system MUST fall back to the subprocess backend and log a warning.

Feature: nsjail-shell-sandboxing
Rule: The nsjail backend is opt-in via config. It is Linux-only. On macOS or when the binary is missing, the subprocess backend is used.

#### Scenario: nsjail backend runs command in sandbox
- **GIVEN** `shell_backend` is set to `"nsjail"` and the nsjail binary is installed
- **WHEN** the agent calls the shell tool with a command
- **THEN** the command executes inside an nsjail sandbox with mount/PID/net/user/IPC/UTS/cgroup namespace isolation
- **AND** stdout and stderr are streamed in real-time to the agent process
- **AND** the child process exit code is passed through faithfully (0→0, 1→1, 42→42, etc.)

#### Scenario: nsjail binary missing falls back to subprocess
- **GIVEN** `shell_backend` is set to `"nsjail"` but the nsjail binary is not found on PATH
- **WHEN** the agent calls the shell tool
- **THEN** the command executes via the subprocess backend (no sandboxing)
- **AND** a warning is logged that nsjail is not available

#### Scenario: Timeout enforcement via nsjail time_limit
- **GIVEN** the nsjail backend is active and a shell command is called with a timeout
- **WHEN** the command runs longer than the timeout
- **THEN** nsjail sends SIGKILL to the child process
- **AND** the shell tool returns exit code 137 with a timeout error message

#### Scenario: Unmounted paths are invisible inside the jail
- **GIVEN** the nsjail backend is active
- **WHEN** the agent runs a command that accesses a path not explicitly mounted in the jail config
- **THEN** the path is not found (No such file or directory)
- **AND** the host filesystem outside mounted paths is inaccessible

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

### Requirement: Per-session /tmp persists across nsjail invocations

A per-session temp directory (created at agent startup, cleaned up at agent shutdown) MUST be bind-mounted as `/tmp` inside every nsjail invocation. Files written to `/tmp` in one shell call MUST be visible in subsequent shell calls within the same session.

#### Scenario: File written to /tmp persists across shell calls
- **GIVEN** the nsjail backend is active with a per-session /tmp bind mount
- **WHEN** the agent runs `shell("echo data > /tmp/cache.txt")` in one call
- **AND** the agent runs `shell("cat /tmp/cache.txt")` in a subsequent call
- **THEN** the second call returns `data`
- **AND** the file exists on the host in the per-session temp directory

#### Scenario: Per-session /tmp is cleaned up at agent shutdown
- **GIVEN** the agent has been running with a per-session /tmp directory
- **WHEN** the agent process shuts down
- **THEN** the per-session temp directory is removed from the host filesystem

### Requirement: nsjail config is generated dynamically per shell call

The nsjail config MUST be generated as a tempfile per shell call, combining static parts (namespaces, seccomp, system mounts, base envars) with dynamic parts (time_limit, cwd, trusted mounts, /tmp mount, command). The config MUST set `cwd` to `/tmp` (the session tmpdir). The tempfile MUST be deleted after the command completes.

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

### Requirement: Cgroup delegation uses systemd-run with rlimits fallback

When cgroup v2 with systemd user delegation is available, the nsjail command MUST be wrapped in `systemd-run --user --scope --property=Delegate=yes` and use `use_cgroupv2` with an explicit `cgroupv2_mount` pointing to the user's delegated cgroup subtree. When cgroup v2 or systemd is unavailable, the system MUST fall back to rlimits only (`rlimit_as`, `rlimit_cpu`, `rlimit_fsize`, `rlimit_nofile`) and log a warning that resource limits are weaker.

#### Scenario: systemd delegation provides hard memory limit
- **GIVEN** systemd is available and cgroup v2 is detected
- **WHEN** the nsjail backend runs a command with `shell_nsjail_memory_mb: 256`
- **THEN** the command is wrapped in `systemd-run --user --scope --property=Delegate=yes`
- **AND** the nsjail config sets `cgroup_mem_max` to 268435456 (256 MB)
- **AND** if the command exceeds 256 MB RSS, the kernel kills it

#### Scenario: rlimits fallback when systemd unavailable
- **GIVEN** systemd is not available or cgroup v2 is not detected
- **WHEN** the nsjail backend runs a command
- **THEN** the nsjail config uses `rlimit_as`, `rlimit_cpu`, `rlimit_fsize`, `rlimit_nofile` instead of cgroup limits
- **AND** a warning is logged that resource limits are weaker (no hard RSS limit, no PID limit, no CPU quota)

### Requirement: System mounts are auto-detected at startup

At agent startup, the system MUST detect the host filesystem layout and generate appropriate mount entries. `/usr` is always mounted read-only. For each of `/bin`, `/sbin`, `/lib`, `/lib64`, `/lib32`: if the path is a symlink, it is mounted with `mandatory: false` (nsjail skips if redundant); if it is a real directory, it is mounted with `mandatory: true`; if it does not exist, it is skipped.

#### Scenario: Symlinked system dirs use mandatory: false
- **GIVEN** the host has `/bin → usr/bin` (symlink) and `/lib → usr/lib` (symlink)
- **WHEN** the system mount auto-detection runs at startup
- **THEN** `/usr` is mounted read-only with `mandatory: true`
- **AND** `/bin` and `/lib` are mounted with `mandatory: false`
- **AND** commands inside the jail can access `/bin/sh` via the symlink resolving to `/usr/bin/sh`

#### Scenario: Real system dirs use mandatory: true
- **GIVEN** the host has `/bin` as a real directory (not a symlink)
- **WHEN** the system mount auto-detection runs at startup
- **THEN** `/bin` is mounted read-only with `mandatory: true`
- **AND** if the mount fails, the jail fails to start

### Requirement: Network is isolated by default

The nsjail config MUST set `clone_newnet: true` by default, giving the jail an empty network namespace with no network access. The `allow_net` config field controls whether the network namespace is created: when set to `false` (default), `clone_newnet` is `true` (network isolated, no access); when set to `true`, `clone_newnet` is `false` (jail shares the host network namespace, no network isolation). Future connectivity options (pasta userland NAT, loopback-only) are out of scope.

#### Scenario: Default config has no network
- **GIVEN** `allow_net` is not set (defaults to `false`)
- **WHEN** the agent runs `shell("curl https://example.com")` inside the jail
- **THEN** the command fails with a network error (empty network namespace, no interfaces)

#### Scenario: Network isolation can be disabled via config
- **GIVEN** `allow_net` is set to `true`
- **WHEN** the nsjail config is generated
- **THEN** `clone_newnet` is set to `false`
- **AND** the jail shares the host network namespace (network access is available, subject to host networking)

### Requirement: Environment variables are isolated with three-layer injection

The nsjail config MUST set `keep_env: false` — the shell does NOT inherit the agent process's `os.environ`. A base set of envars (`PATH`, `HOME`, `LANG`, `TERM`) is always injected via nsjail config `envar` entries as a fallback. Session env vars (managed by `shell_env_set`) are injected via nsjail `-E` flags per call, overriding config `envar` entries.

#### Scenario: Shell does not see agent process secrets
- **GIVEN** the agent process has `OPENAI_API_KEY=sk-...` in its `os.environ`
- **WHEN** the agent runs `shell("echo $OPENAI_API_KEY")` inside the jail
- **THEN** the output is empty (the variable is not visible)

#### Scenario: Base envars are always present
- **GIVEN** the nsjail backend is active with `keep_env: false`
- **WHEN** the agent runs `shell("echo $PATH")` inside the jail
- **THEN** the output contains the base PATH from the config `envar` entry

#### Scenario: Session env vars override base envars
- **GIVEN** the agent has called `shell_env_set("PATH", "/custom/bin")`
- **WHEN** the agent runs `shell("echo $PATH")` inside the jail
- **THEN** the output is `/custom/bin` (the `-E` flag overrides the config `envar`)