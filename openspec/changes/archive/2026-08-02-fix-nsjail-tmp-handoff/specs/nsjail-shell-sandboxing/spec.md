## ADDED Requirements

### Requirement: Agent's default temp directory is bind-mounted into the jail at its real host path

The nsjail config MUST bind-mount `/tmp/{agent_name}` (`agent_name` = `app_cfg.agent.agent_name`, which always has a config default) read-write inside the jail at its real host path (`src == dst`, `is_bind: true`, `rw: true`, `mandatory: true`). There is no other way to configure this path — it is not derived from any separately-overridable setting. The mount MUST be emitted immediately after the existing per-session `/tmp` scratch mount, so it is not shadowed. This is a system mount, like the `session_logs_dir` mount — it does not require operator approval and is not validated against the trusted-directory blocklist. The directory is guaranteed to exist by agent startup (before the shell tool is ever invoked); `mandatory: true` means a shell call fails loudly if that guarantee is somehow violated, rather than silently degrading.

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

## MODIFIED Requirements

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
