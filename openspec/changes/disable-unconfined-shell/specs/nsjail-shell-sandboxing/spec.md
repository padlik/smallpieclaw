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

#### Scenario: Runtime launch failure or failed trusted re-probe latches unavailable
- **GIVEN** shell is available after a successful Linux nsjail probe
- **WHEN** nsjail launch raises an `OSError` or a trusted backend re-probe fails after setup/launch failure
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

### Requirement: nsjail session scratch source is isolated from other writable mounts

The host directory mounted as the jail's session `/tmp` source MUST reside beneath an owner-only host-private `state_home/nsjail-session` parent and outside every *other* writable jail mount source. The agent MUST NOT place it under a system temporary directory or another jail-writable mount source. The system MUST verify the pinned real directory and its ownership and mode whenever it builds nsjail configuration. If the source is unsafe, changed, or unverifiable, shell MUST become unavailable with an actionable reason; the system MUST NOT substitute another writable source or fall back to host subprocess/PTY execution. A jailed command MUST NOT be able to rename or replace the source with a symlink to an arbitrary host directory that a subsequent launch will mount as session `/tmp`.

Feature: nsjail-shell-sandboxing
Rule: Session scratch is writable only within its isolated source; unsafe source identity or layout disables shell without fallback.

#### Scenario: Session scratch source is private and disjoint from other writable mounts
- **GIVEN** the nsjail configuration builder is preparing a jail with a writable session `/tmp` mount
- **WHEN** it validates the session mount source
- **THEN** the source resolves to the pinned real directory beneath an owner-only host-private parent
- **AND** it is not equal to, nested beneath, or an ancestor of any other writable jail mount source
- **AND** the source and parent are not placed under a system temporary directory or another jail-writable mount

#### Scenario: Changed or unsafe session source disables shell without fallback
- **GIVEN** the session `/tmp` source is unsafe, has changed identity or ownership/mode, or cannot be verified
- **WHEN** the system builds nsjail configuration for a shell launch
- **THEN** shell becomes unavailable with an actionable reason
- **AND** no replacement writable source is used
- **AND** no command executes through host subprocess or PTY

#### Scenario: Linux Lima denies session-source rename and symlink substitution
- **GIVEN** the Linux Lima nsjail smoke environment has mounted the verified session source as jail `/tmp`
- **WHEN** a jailed command attempts to rename or replace the source with a symlink to an arbitrary host directory and a subsequent launch is attempted
- **THEN** the source cannot be replaced or the builder rejects the changed source before constructing the subsequent jail
- **AND** the arbitrary host directory is not mounted as `/tmp` and remains unchanged
- **AND** no host subprocess or PTY fallback occurs

### Requirement: nsjail supervisor diagnostics are private and distinct from child output

The system MUST capture each nsjail invocation's supervisor diagnostics in a separate per-call host-private log file in the verified configuration directory. Each log MUST be mode `0600`, outside jail-writable mount sources and aliases, and unavailable for jailed commands to write or forge. Startup MUST use a real jail probe to verify the installed nsjail supports and can open the requested log destination; if the log is absent or unusable, including when nsjail falls back to stderr, the probe MUST fail and shell MUST remain unavailable. The system MUST treat child stdout and stderr solely as child output: stderr text, including text resembling nsjail diagnostics such as `[E][`, MUST NOT by itself indicate backend loss. Ordinary child nonzero exit and timeout MUST NOT cause backend loss. A genuine nsjail setup/launch failure MUST lead to a trusted re-probe; a failed trusted re-probe or launch `OSError` follows the existing one-way runtime-loss latch. No minimum nsjail version is required; the real probe determines whether the installed binary provides the needed log behavior.

Feature: nsjail-shell-sandboxing
Rule: Only private nsjail supervisor diagnostics and trusted probe/launch failures inform backend health; child output does not.

#### Scenario: Startup probe requires separate usable supervisor log
- **GIVEN** the system is probing the installed nsjail binary for shell availability
- **WHEN** the requested per-call supervisor log cannot be opened or nsjail writes diagnostics to stderr instead
- **THEN** the real probe fails and shell remains unavailable
- **AND** stderr is not accepted as a substitute supervisor log
- **AND** no host subprocess or PTY fallback occurs

#### Scenario: Linux Lima prevents jailed writes to or forgery of supervisor logs
- **GIVEN** the Linux Lima nsjail smoke environment has created a mode-`0600` per-call supervisor log in the verified host-private configuration directory
- **WHEN** a jailed command attempts to discover, overwrite, or forge that log and the host performs a subsequent probe and confined launch
- **THEN** the log directory is absent from jail mounts and the jailed write/forgery attempt is denied
- **AND** the host-side supervisor log remains unchanged
- **AND** the subsequent probe and confined command use separate valid supervisor logs

#### Scenario: Child stderr resembling nsjail diagnostics remains child output
- **GIVEN** shell is available and the nsjail backend launch succeeds
- **WHEN** a child writes text such as `[E][` to stderr and exits nonzero
- **THEN** the shell tool returns the child's stderr and exit status as ordinary command output
- **AND** shell remains available
