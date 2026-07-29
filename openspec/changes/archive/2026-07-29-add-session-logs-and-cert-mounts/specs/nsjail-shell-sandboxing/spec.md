## ADDED Requirements

### Requirement: session_logs folder is mounted read-only inside the jail

When the nsjail shell backend is active and a `session_logs_dir` is provided (non-empty and the directory exists), the active conversation's `session_logs` folder MUST be bind-mounted read-only inside the jail at the same host path (`src == dst`, `is_bind: true`, `rw: false`, `mandatory: false`). This is a system mount, not a trusted-directory mount — it is not subject to the trusted-directory blocklist and does not require operator approval. If `session_logs_dir` is empty or the directory does not exist, the mount is skipped (graceful degradation). The `NsjailConfigBuilder.build()` method receives `session_logs_dir` as a per-call kwarg; the builder remains stateless.

Feature: nsjail-shell-sandboxing
Rule: The agent writes outside the jail; sandboxed shell commands read inside the jail. Read-only — a sandboxed script cannot write to or fill disk in session_logs.

#### Scenario: session_logs folder is mounted read-only at its host path
- **GIVEN** the nsjail backend is active and `session_logs_dir` is `~/.local/state/myagent/session_logs/abc123def456`
- **AND** the directory exists
- **WHEN** the nsjail config is generated
- **THEN** the config contains a mount entry with `src` and `dst` both set to the host path
- **AND** `is_bind: true`, `rw: false`, `mandatory: false`

#### Scenario: Shell can read a prior large output inside the jail
- **GIVEN** the nsjail backend is active and the session_logs folder is mounted
- **AND** a prior shell call saved a large output to `~/.local/state/myagent/session_logs/abc123def456/shell-xxx.log`
- **WHEN** the agent runs `shell("cat ~/.local/state/myagent/session_logs/abc123def456/shell-xxx.log")` inside the jail
- **THEN** the command succeeds and returns the saved output

#### Scenario: Shell cannot write to session_logs inside the jail
- **GIVEN** the nsjail backend is active and the session_logs folder is mounted read-only
- **WHEN** the agent runs `shell("echo data > ~/.local/state/myagent/session_logs/abc123def456/evil.log")` inside the jail
- **THEN** the write fails with a permission error
- **AND** no file is created on the host

#### Scenario: Empty session_logs_dir skips the mount
- **GIVEN** the nsjail backend is active but `session_logs_dir` is an empty string
- **WHEN** the nsjail config is generated
- **THEN** no session_logs mount entry appears in the config
- **AND** the jail starts normally

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

## MODIFIED Requirements

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

The nsjail config MUST set `keep_env: false` — the shell does NOT inherit the agent process's `os.environ`. A base set of envars (`PATH`, `HOME`, `LANG`, `TERM`) is always injected via nsjail config `envar` entries as a fallback. When `allow_net` is `true`, `SSL_CERT_FILE` and `SSL_CERT_DIR` are also injected via config `envar` entries (not per-call `-E` flags) to enable TLS certificate verification. Session env vars (managed by `shell_env_set`) are injected via nsjail `-E` flags per call, overriding config `envar` entries.

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