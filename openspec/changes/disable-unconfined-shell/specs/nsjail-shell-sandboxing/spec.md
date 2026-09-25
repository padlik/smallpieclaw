## REMOVED Requirements

### Requirement: nsjail shell backend provides kernel-level isolation

**Reason**: Its opt-in default and subprocess fallback scenarios conflict with the fail-closed shell availability policy. The replacement contract has a distinct requirement name so scenario-loss validation does not mistake the changed behavior for a partial modification.

**Migration**: `shell_backend` now defaults to `nsjail`; Linux operators must have a working, probed jail to use model-issued shell. If the jail is unavailable, continue with non-shell tools; there is no subprocess or PTY fallback. Existing nsjail isolation behavior remains in force.

## ADDED Requirements

### Requirement: Model-issued shell requires a healthy Linux nsjail backend

`shell_backend` MUST default to `"nsjail"`. On Linux, the system MUST offer shell only after a real nsjail jail probe succeeds and MUST execute every offered shell command inside that nsjail sandbox with mount, PID, net, user, IPC, UTS, and cgroup namespace isolation and cgroup v2 resource limits. Seccomp-bpf syscall filtering is deferred — isolation currently relies on namespaces and cgroup limits only. An explicit `"subprocess"` or `"pty"` setting, a missing or broken nsjail binary, failed startup/configuration/probe, or non-Linux host MUST make shell unavailable; none may fall back to host execution. When unavailable, the agent and non-shell tools MUST continue running. Runtime nsjail backend loss MUST latch shell unavailable until process restart. Ordinary command nonzero exit and command timeout MUST NOT be classified as backend loss.

Feature: nsjail-shell-sandboxing
Rule: A healthy, probed Linux nsjail is the only shell backend; lack of confinement disables shell, not the agent.

#### Scenario: Default backend runs command in a probed sandbox
- **GIVEN** `shell_backend` is omitted and the host is Linux
- **AND** a real nsjail jail probe succeeds
- **WHEN** the agent calls the shell tool with a command
- **THEN** the command executes inside an nsjail sandbox with mount/PID/net/user/IPC/UTS/cgroup namespace isolation
- **AND** stdout and stderr are streamed in real-time to the agent process
- **AND** the child process exit code is passed through faithfully (0→0, 1→1, 42→42, etc.)

#### Scenario: nsjail binary missing or probe fails
- **GIVEN** the host is Linux and `shell_backend` is omitted or set to `"nsjail"`
- **AND** the nsjail binary is missing, broken, or a real jail probe fails
- **WHEN** the agent starts and a shell call is attempted
- **THEN** the agent remains running and non-shell tools remain available
- **AND** shell is unavailable and a stale call fails as `shell_unavailable`
- **AND** no command executes through a host subprocess or PTY backend

#### Scenario: Explicit unconfined backend is unavailable
- **GIVEN** `shell_backend` is explicitly set to `"subprocess"` or `"pty"`
- **WHEN** the agent starts and a shell call is attempted
- **THEN** shell is unavailable and a stale call fails as `shell_unavailable`
- **AND** the agent and non-shell tools remain available
- **AND** no host command executes

#### Scenario: Non-Linux host has no shell backend
- **GIVEN** the host is macOS or another non-Linux platform
- **WHEN** the agent starts with the default backend or `shell_backend` set to `"nsjail"`
- **THEN** shell is unavailable and the agent and non-shell tools remain available
- **AND** no host command executes through subprocess or PTY

#### Scenario: Runtime backend loss latches unavailable
- **GIVEN** shell is available after a successful Linux nsjail probe
- **WHEN** an nsjail backend launch fails or a backend re-probe fails
- **THEN** the failing call reports `shell_unavailable` rather than executing on the host
- **AND** shell remains unavailable until the agent process restarts
- **AND** the agent and non-shell tools continue running

#### Scenario: Ordinary command nonzero exit is not backend loss
- **GIVEN** the nsjail backend is healthy
- **WHEN** a shell command exits with a nonzero status
- **THEN** the shell tool returns the child's exit status faithfully
- **AND** shell remains available

#### Scenario: Timeout enforcement via nsjail time_limit
- **GIVEN** the nsjail backend is active and a shell command is called with a timeout
- **WHEN** the command runs longer than the timeout
- **THEN** nsjail sends SIGKILL to the child process
- **AND** the shell tool returns exit code 137 with a timeout error message
- **AND** the backend remains available unless an independent backend launch/re-probe failure occurs

#### Scenario: Unmounted paths are invisible inside the jail
- **GIVEN** the nsjail backend is active
- **WHEN** the agent runs a command that accesses a path not explicitly mounted in the jail config
- **THEN** the path is not found (No such file or directory)
- **AND** the host filesystem outside mounted paths is inaccessible

### Requirement: nsjail configuration is host-private and immutable to jailed commands

The system MUST store generated session-static and per-call nsjail configuration files in a verified owner-only host-private location that is not mounted into the jail. The resolved location MUST NOT be equal to, inside, or an ancestor of any jail-writable mount source, and MUST NOT be reachable through a read-write bind alias. This includes the writable host directories mounted as jail `/tmp` and `/tmp/<agent>`. A jailed command MUST NOT be able to rewrite configuration used by a subsequent shell launch or startup re-probe. If the location cannot be created with safe permissions or its non-overlap with writable mounts cannot be verified, shell MUST be unavailable with an actionable reason; the system MUST NOT fall back to a writable temporary location or to host subprocess/PTY execution.

Feature: nsjail-shell-sandboxing
Rule: Configuration controlling nsjail runs is host-private; failure to establish its integrity disables shell without fallback.

#### Scenario: Generated configs are outside every writable jail mount
- **GIVEN** the Linux nsjail backend is being initialized
- **WHEN** the system selects the host-private config location
- **THEN** the resolved directory and both session-static and per-call config files are owner-only
- **AND** the directory is not mounted into the jail and is not equal to, inside, or an ancestor of any jail-writable mount source
- **AND** no read-write bind alias exposes the config directory to jailed commands

#### Scenario: Unsafe config location disables shell without fallback
- **GIVEN** the config location is unavailable, has unsafe permissions, or overlaps a jail-writable mount source
- **WHEN** startup or a later shell launch needs to create or use nsjail config
- **THEN** shell is unavailable with an actionable reason
- **AND** no config is written to `/tmp`, `/tmp/<agent>`, or another jail-writable location
- **AND** no command executes through host subprocess or PTY
- **AND** non-shell agent functionality remains available

#### Scenario: Linux Lima proves jailed config writes cannot affect later runs
- **GIVEN** the Linux Lima nsjail smoke environment has created the static and per-call configs in the verified host-private location
- **WHEN** a jailed command attempts to locate and overwrite the config location, and the host then checks the config files and performs another probe and confined shell launch
- **THEN** the config location is absent from the jail mount table and the jailed write attempt is denied
- **AND** the host-side config bytes remain unchanged
- **AND** the subsequent startup-style probe and confined shell command succeed using intact configuration
