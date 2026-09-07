# nsjail-shell-sandboxing Delta

## Purpose

The jail's mount table is now derived from the PathPolicy tiers and built once per session; the session-logs read-only mount is removed (logs are served by the SQLite store via `log_query`). Execution-plane isolation semantics (namespaces, cgroups, env, DNS/TLS, network) are unchanged.

## ADDED Requirements

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

## MODIFIED Requirements

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
- **WHEN** the session nsjail config is generated
- **THEN** the per-session scratch `/tmp` mount entry appears before the `/tmp/{agent_name}` mount entry
- **AND** the `/tmp/{agent_name}` mount entry has `is_bind: true`, `rw: true`, `mandatory: true`, and `src`/`dst` both equal to `/tmp/{agent_name}`

#### Scenario: A missing directory fails the shell call, not a degraded jail
- **GIVEN** `/tmp/{agent_name}` has been removed from the host after agent startup
- **WHEN** the agent runs a `shell(...)` command
- **THEN** the shell call fails with a non-zero exit and an error
- **AND** no command executes inside a jail that's missing this mount

## REMOVED Requirements

### Requirement: session_logs folder is mounted read-only inside the jail
**Reason**: The agent's XDG state home (which contains session_logs) is now Tier 0 prohibited, and mounting prohibited paths inside the jail is forbidden by construction. Logs are accessible to the agent via the indexed `log_query` SQLite store, which is strictly more capable than raw file reads (ADR-0017's read-only mount is reversed).

**Migration**: Agents read past output via `log_query` (structured, indexed, 30-day store) or via result artifacts in `~/.<agent>/results/<trace-id>/` (see `agent-results-dir`). No filesystem path to session logs exists for the agent or the jail.