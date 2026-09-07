# nsjail-shell-sandboxing Specification

## Purpose

The jail's mount table is derived from the PathPolicy tiers and built once per session; the session-logs read-only mount is removed (logs are served by the SQLite store via `log_query`). Execution-plane isolation semantics (namespaces, cgroups, env, DNS/TLS, network) are unchanged.

## Requirements

### Requirement: nsjail shell backend provides kernel-level isolation

When `shell_backend` is set to `"nsjail"` and the nsjail binary is available, the shell tool MUST execute commands inside an nsjail sandbox with mount, PID, net, user, IPC, UTS, and cgroup namespace isolation and cgroup v2 resource limits. Seccomp-bpf syscall filtering is deferred — isolation currently relies on namespaces and cgroup limits only. If the nsjail binary is not found, the system MUST fall back to the subprocess backend and log a warning.

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

### Requirement: Mount table is derived from PathPolicy and built once per session

The jail mount table MUST be derived exclusively from the frozen PathPolicy: Tier 1 entries (workspace rw, downloads rw, `/tmp/<agent_name>` rw, `~/.<agent>/skills` r, `~/.<agent>/results` rw) plus Tier 2 operator `allowed_dirs` (rw), minus Tier 0 overlap. The generated nsjail config MUST be built once at session start and reused for every shell call in the session (no per-call rebuilds, no per-call store reads); only the per-call parts (command, `-E` env injection, `time_limit`) vary between calls. An allowed dir that contains a prohibited path MUST NOT be mounted (no partial bind mounts) — its file-plane behavior remains as configured, and the conflict is reported once at startup by PathPolicy validation.

Feature: nsjail-shell-sandboxing
Rule: One config per session. The mount table has a single auditable answer: "what can the jail see this session".

#### Scenario: Config is generated once and reused
- **GIVEN** the nsjail backend is active
- **WHEN** two shell calls execute in the same session
- **THEN** both run against the same generated nsjail config (mount section identical)
- **AND** no per-call trusted-store reads occur

#### Scenario: Allowed dir containing a prohibited path is not mounted
- **GIVEN** `allowed_dirs = ["~/work"]` and `~/work/keys` is prohibited
- **WHEN** the session's nsjail config is generated
- **THEN** `~/work` does not appear in the mount table
- **AND** a startup warning described the conflict (PathPolicy validation)
- **AND** the jail still mounts all other Tier 1 entries (the jail is never silently reduced to no mounts beyond plumbing)

#### Scenario: Skills and results mounts come from Tier 1
- **GIVEN** the nsjail backend is active
- **WHEN** the session config is generated
- **THEN** `~/.<agent>/skills` is mounted `rw: false` and `~/.<agent>/results` is mounted `rw: true`, both at their real host paths

### Requirement: Trusted directories are mounted at their original host paths

Operator extended directories from the static config `[security] allowed_dirs` MUST be bind-mounted at their original host paths inside the jail, read-write (rw-only — no mode syntax exists in config). Tier 1 agent-controlled directories are mounted per their hardcoded modes (skills read-only, results/workspace/downloads/tmp read-write). Paths whose classification is PROHIBITED are never mounted. The three user-prefix/system blocklist tuples are removed from the builder — their job moved to PathPolicy construction-time validation, so the builder performs no path filtering of its own beyond receiving the already-validated mount set.

Feature: nsjail-shell-sandboxing

#### Scenario: RW allowed dir is writable inside jail
- **GIVEN** `/home/user/projects` is in `[security] allowed_dirs`
- **WHEN** the agent runs `shell("echo data > /home/user/projects/file.txt")` inside the jail
- **THEN** the file is written to the host filesystem at `/home/user/projects/file.txt`

#### Scenario: Prohibited path is not mounted under any circumstances
- **GIVEN** `~/.ssh` is a Tier 0 prohibited entry
- **WHEN** the session nsjail config is generated
- **THEN** no mount entry exposes `~/.ssh` inside the jail
- **AND** no config field can cause it to be mounted

#### Scenario: Config change requires a restart to affect mounts
- **GIVEN** the operator edits `[security] allowed_dirs` while the agent is running
- **WHEN** subsequent shell calls execute in the same session
- **THEN** the mount table is unchanged (PathPolicy and the config are session-static)
- **AND** the new dir becomes mounted after agent restart

#### Scenario: Trusted dir under /home is accepted
- **GIVEN** `/home/user/projects/myproject` is in `allowed_dirs`
- **WHEN** the session nsjail config is generated
- **THEN** the directory is mounted read-write inside the jail at its original path

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

### Requirement: Agent's default temp directory is bind-mounted into the jail at its real host path

The nsjail config MUST bind-mount `/tmp/{agent_name}` (`agent_name` = `app_cfg.agent.agent_name`, which always has a config default) read-write inside the jail at its real host path (`src == dst`, `is_bind: true`, `rw: true`, `mandatory: true`). There is no other way to configure this path — it is not derived from any separately-overridable setting. The mount MUST be emitted immediately after the existing per-session `/tmp` scratch mount, so it is not shadowed. This is a Tier 1 agent-controlled system mount (see `path-policy`) — it does not require operator approval and is not subject to any blocklist, because the mount table is derived from the already-validated PathPolicy tiers. The directory is guaranteed to exist by agent startup (before the shell tool is ever invoked); `mandatory: true` means a shell call fails loudly if that guarantee is somehow violated, rather than silently degrading.

Feature: nsjail-shell-sandboxing
Rule: The mount closes the stuck-loop bug where a sandboxed script writes results to the agent's default temp directory but the jail boundary makes them invisible to `file_read` outside.

#### Scenario: File written inside the jail is visible outside via file_read
- **GIVEN** the nsjail backend is active with `agent.agent_name` at its default, so the mount resolves to `/tmp/piclaw`
- **WHEN** the agent runs `shell("echo data > /tmp/piclaw/result.txt")` inside the jail
- **THEN** `file_read("/tmp/piclaw/result.txt")` outside the jail returns `data`

#### Scenario: File placed by the agent is visible inside the jail
- **GIVEN** the mount resolves to `/tmp/piclaw` and the agent has written `/tmp/piclaw/input.json` via `file_write`
- **WHEN** the agent runs `shell("cat /tmp/piclaw/input.json")` inside the jail
- **THEN** the command succeeds and returns the file's contents

#### Scenario: Mount entry has the correct attributes and ordering
- **GIVEN** the nsjail backend is active
- **WHEN** the nsjail config is generated
- **THEN** the per-session scratch `/tmp` mount entry appears before the `/tmp/{agent_name}` mount entry
- **AND** the `/tmp/{agent_name}` mount entry has `is_bind: true`, `rw: true`, `mandatory: true`, and `src`/`dst` both equal to `/tmp/{agent_name}`

#### Scenario: A missing directory fails the shell call, not a degraded jail
- **GIVEN** `/tmp/{agent_name}` has been removed from the host after agent startup
- **WHEN** the agent runs a `shell(...)` command
- **THEN** the shell call fails with a non-zero exit and an error
- **AND** no command executes inside a jail that's missing this mount

### Requirement: nsjail config is generated dynamically per shell call

The static parts of the nsjail config (namespaces, system mounts, base envars, tier-derived mounts, limits) MUST be generated once per session and cached; the per-call parts (command as exec target, `time_limit`, `-E` env flags) MUST be supplied per shell call. The per-call parts MUST set `cwd` to `/tmp` (the session tmpdir). Any config tempfile created per call (for the per-call command block) MUST be deleted after the command completes.

#### Scenario: Per-call parts include timeout and command
- **GIVEN** the nsjail backend is active
- **WHEN** the agent calls `shell("make test", timeout=60)`
- **THEN** the per-call invocation uses `time_limit: 60` and the command as the exec target
- **AND** the session-static mount section is identical to the previous call's

#### Scenario: Config sets cwd to /tmp
- **GIVEN** the nsjail backend is active
- **WHEN** the agent calls `shell("pwd")`
- **THEN** the output is `/tmp`
- **AND** the invocation uses `cwd: "/tmp"`

#### Scenario: Per-call tempfile is cleaned up after execution
- **GIVEN** the nsjail backend generates a per-call config tempfile
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

When cgroup v2 with systemd user delegation is available, the nsjail command MUST be wrapped in `systemd-run --user --scope --property=Delegate=yes` and use `use_cgroupv2` with an explicit `cgroupv2_mount` pointing to the user's delegated cgroup subtree. When cgroup v2 or systemd is unavailable, the system MUST fall back to rlimits only (`rlimit_as`, `rlimit_nproc`, `rlimit_fsize`, `rlimit_nofile`) and log a warning that resource limits are weaker.

#### Scenario: systemd delegation provides hard memory limit
- **GIVEN** systemd is available and cgroup v2 is detected
- **WHEN** the nsjail backend runs a command with `shell_nsjail_memory_mb: 256`
- **THEN** the command is wrapped in `systemd-run --user --scope --property=Delegate=yes`
- **AND** the nsjail config sets `cgroup_mem_max` to 268435456 (256 MB)
- **AND** if the command exceeds 256 MB RSS, the kernel kills it

#### Scenario: rlimits fallback when systemd unavailable
- **GIVEN** systemd is not available or cgroup v2 is not detected
- **WHEN** the nsjail backend runs a command
- **THEN** the nsjail config uses `rlimit_as`, `rlimit_nproc`, `rlimit_fsize`, `rlimit_nofile` instead of cgroup limits
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

The nsjail config MUST set `clone_newnet: true` by default, giving the jail an empty network namespace with no network access. The `allow_net` config field controls whether the network namespace is created: when set to `false` (default), `clone_newnet` is `true` (network isolated, no access); when set to `true`, `clone_newnet` is `false` (jail shares the host network namespace, no network isolation). When `allow_net` is `true`, the system CA certificate store MUST be mounted read-only and `SSL_CERT_FILE` / `SSL_CERT_DIR` env vars MUST be injected so TLS-dependent tools (`curl`, `git`, Python `ssl`) can verify certificates. Future connectivity options (pasta userland NAT, loopback-only) are out of scope.

#### Scenario: Default config has no network
- **GIVEN** `allow_net` is not set (defaults to `false`)
- **WHEN** the agent runs `shell("curl https://example.com")` inside the jail
- **THEN** the command fails with a network error (empty network namespace, no interfaces)

#### Scenario: Network isolation can be disabled via config
- **GIVEN** `allow_net` is set to `true`
- **WHEN** the nsjail config is generated
- **THEN** `clone_newnet` is set to `false`
- **AND** the jail shares the host network namespace (network access is available, subject to host networking)
- **AND** the system CA certificate store is mounted read-only
- **AND** `SSL_CERT_FILE` and `SSL_CERT_DIR` env vars are injected

### Requirement: Environment variables are isolated with three-layer injection

The nsjail config MUST set `keep_env: false` — the shell does NOT inherit the agent process's `os.environ`. A base set of envars (`PATH`, `HOME`, `LANG`, `TERM`, `TMPDIR`, `TMP`, `TEMP`) is always injected via nsjail config `envar` entries as a fallback; `TMPDIR`, `TMP`, and `TEMP` are all set to `/tmp` — the per-session scratch mount, not the agent's default temp directory (see "Agent's default temp directory is bind-mounted into the jail at its real host path") — so tools that call `mktemp`/`tempfile` without an explicit path keep landing in throwaway scratch space, exactly as they do today. When `allow_net` is `true`, `SSL_CERT_FILE` and `SSL_CERT_DIR` are also injected via config `envar` entries (not per-call `-E` flags) to enable TLS certificate verification. Session env vars (managed by `shell_env_set`) are injected via nsjail `-E` flags per call, overriding config `envar` entries.

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

#### Scenario: SSL cert env vars present when allow_net=true
- **GIVEN** `allow_net` is `true` and the host is Debian/Ubuntu
- **WHEN** the agent runs `shell("echo $SSL_CERT_FILE")` inside the jail
- **THEN** the output is `/etc/ssl/certs/ca-certificates.crt`

#### Scenario: TMPDIR/TMP/TEMP point at the ephemeral scratch mount
- **GIVEN** the nsjail backend is active
- **WHEN** the agent runs `shell("echo $TMPDIR $TMP $TEMP")` inside the jail
- **THEN** the output is `/tmp /tmp /tmp`

#### Scenario: Session env vars override TMPDIR/TMP/TEMP
- **GIVEN** the agent has called `shell_env_set("TMPDIR", "/custom/tmp")`
- **WHEN** the agent runs `shell("echo $TMPDIR")` inside the jail
- **THEN** the output is `/custom/tmp` (the `-E` flag overrides the config `envar`, same as any other base envar)

### Requirement: System CA certificate store is mounted read-only when networking is enabled

When `allow_net` is `true`, the nsjail config MUST include a read-only, non-mandatory bind mount of the system CA certificate store, detected distro-aware: Debian/Ubuntu (`/etc/ssl/certs` directory), Alpine (`/etc/ssl/cert.pem` file), Fedora/RHEL (`/etc/pki/tls/certs` directory). The mount uses `mandatory: false` so the jail still starts if the path is absent. This is a system mount that bypasses `_BLOCKED_SYSTEM_PREFIXES` by construction. When `allow_net` is `false` (default), no CA cert mount is added.

Feature: nsjail-shell-sandboxing
Rule: The CA cert mount is conditional on `allow_net=true`. When networking is isolated, certs are irrelevant.

#### Scenario: CA cert directory mounted on Debian when allow_net=true
- **GIVEN** `allow_net` is `true` and the host is Debian/Ubuntu with `/etc/ssl/certs` as a directory
- **WHEN** the nsjail config is generated
- **THEN** the config contains a read-only bind mount of `/etc/ssl/certs` with `mandatory: false`

#### Scenario: CA cert file mounted on Alpine when allow_net=true
- **GIVEN** `allow_net` is `true` and the host is Alpine with `/etc/ssl/cert.pem` as a file (no directory)
- **WHEN** the nsjail config is generated
- **THEN** the config contains a read-only bind mount of `/etc/ssl/cert.pem` with `mandatory: false`

#### Scenario: No CA cert mount when allow_net=false
- **GIVEN** `allow_net` is `false` (default)
- **WHEN** the nsjail config is generated
- **THEN** no CA cert mount entry appears in the config

#### Scenario: Missing CA cert path does not break the jail
- **GIVEN** `allow_net` is `true` but no known CA cert path exists on the host
- **WHEN** the nsjail config is generated
- **THEN** no CA cert mount entry appears in the config
- **AND** no SSL_CERT_FILE or SSL_CERT_DIR env vars are injected
- **AND** the jail starts normally

### Requirement: SSL_CERT_FILE and SSL_CERT_DIR env vars injected when networking is enabled

When `allow_net` is `true` and a CA cert path was detected, the nsjail config MUST inject `SSL_CERT_FILE` (set to the detected cafile) and `SSL_CERT_DIR` (set to the detected capath) as `envar` entries in the config (not per-call `-E` flags). These env vars bypass the `/usr/lib/ssl/cert.pem → /etc/ssl/certs/...` broken-symlink problem by telling programs directly where to find certs. They are honored by Python `ssl`, OpenSSL, `curl`, `git`, and `httpx`. When `allow_net` is `false` or no CA cert path is detected, these env vars are not injected.

Feature: nsjail-shell-sandboxing

#### Scenario: SSL_CERT_FILE and SSL_CERT_DIR set on Debian
- **GIVEN** `allow_net` is `true` and the host is Debian/Ubuntu
- **WHEN** the nsjail config is generated
- **THEN** the config contains `envar: "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"`
- **AND** the config contains `envar: "SSL_CERT_DIR=/etc/ssl/certs"`

#### Scenario: SSL_CERT_FILE set on Alpine
- **GIVEN** `allow_net` is `true` and the host is Alpine
- **WHEN** the nsjail config is generated
- **THEN** the config contains `envar: "SSL_CERT_FILE=/etc/ssl/cert.pem"`

#### Scenario: No SSL cert env vars when allow_net=false
- **GIVEN** `allow_net` is `false`
- **WHEN** the nsjail config is generated
- **THEN** no `SSL_CERT_FILE` or `SSL_CERT_DIR` envar entries appear in the config

#### Scenario: curl HTTPS works inside the jail when allow_net=true
- **GIVEN** `allow_net` is `true` and the CA cert store is mounted and env vars are set
- **WHEN** the agent runs `shell("curl https://example.com")` inside the jail
- **THEN** the command succeeds (TLS verification passes using the mounted CA bundle)

### Requirement: DNS resolv.conf injected when networking is enabled

When `allow_net` is `true`, the nsjail config MUST include a `src_content` mount that writes a `nameserver` line to `/etc/resolv.conf` inside the jail. The jail has an isolated mount namespace (`clone_newns: true`), so the host's `/etc/resolv.conf` is not visible and DNS resolution would fail without this entry. The nameserver IP is configurable via the `dns_nameserver` parameter (default `8.8.8.8`). When `allow_net` is `false` (default), no resolv.conf mount is added.

Feature: nsjail-shell-sandboxing
Rule: The resolv.conf mount is conditional on `allow_net=true`. When networking is isolated, DNS is irrelevant.

#### Scenario: resolv.conf injected when allow_net=true
- **GIVEN** `allow_net` is `true`
- **WHEN** the nsjail config is generated
- **THEN** the config contains a `mount` entry with `src_content` set to `"nameserver 8.8.8.8\n"` and `dst` set to `"/etc/resolv.conf"`

#### Scenario: custom nameserver used when configured
- **GIVEN** `allow_net` is `true` and `dns_nameserver` is set to `"1.1.1.1"`
- **WHEN** the nsjail config is generated
- **THEN** the config contains a `mount` entry with `src_content` set to `"nameserver 1.1.1.1\n"`

#### Scenario: no resolv.conf mount when allow_net=false
- **GIVEN** `allow_net` is `false` (default)
- **WHEN** the nsjail config is generated
- **THEN** no `src_content` mount for `/etc/resolv.conf` appears in the config
