## Context

`smallpieclaw` currently resolves `env:VAR` references during config parsing and stores resolved strings in typed config dataclasses. Model entries each carry their own `api_key` and related provider settings, so configurations with many models repeat the same secret reference many times.

Production deployments commonly run the agent as a `systemd --user` service. User services do not automatically inherit interactive shell exports, and putting API key values into `Environment=`, `EnvironmentFile=`, or `systemctl --user set-environment` leaves those values in the process environment. The agent also starts shell, tool, one-off script, and MCP stdio subprocesses that inherit the process environment, so env-value secrets can propagate further than intended.

There are no in-force ADRs in `adr/` for this repository.

Current deployment boundary:

```text
┌──────────────────────┐
│ systemd --user unit  │
│ EnvironmentFile=     │
└──────────┬───────────┘
           │ secret values in env
           ▼
┌──────────────────────┐
│ smallpieclaw process │
│ config env:VAR       │
└──────┬────────┬──────┘
       │        │ full env inheritance
       ▼        ▼
  LLM APIs   shell/tools/MCP
```

Target deployment boundary:

```text
┌─────────────────────────────┐
│ systemd --user unit         │
│ LoadCredential=openai_key   │
│ Environment=..._FILE=%d/... │
└──────────────┬──────────────┘
               │ file path in env, secret in credential file
               ▼
┌─────────────────────────────┐
│ config parser               │
│ file:env:OPENAI_API_KEY_FILE│
└──────────────┬──────────────┘
               │ secret string in app config only after parsing
               ▼
┌─────────────────────────────┐
│ LLM / embeddings clients    │
└─────────────────────────────┘
```

## Goals / Non-Goals

**Goals:**

- Allow provider-level credentials/defaults to be defined once and inherited by model entries.
- Allow sensitive string config values to be read from protected files, including paths exposed by systemd credential variables.
- Preserve backward compatibility with existing `api_key = "env:OPENAI_API_KEY"` and per-model configuration.
- Provide clear validation errors for missing provider defaults, missing files, invalid references, or ambiguous secret fields.
- Document production `systemd --user` usage with `LoadCredential=` and `WorkingDirectory=`.

**Non-Goals:**

- Do not implement direct cloud secret-manager SDK integration in this change.
- Do not rotate secrets at runtime or re-read files per request.
- Do not redesign subprocess environment inheritance or shell/MCP sandboxing.
- Do not remove support for environment-value secrets; they remain useful for simple local development.
- Do not change model selection or provider request behavior beyond resolved config values.

## Decisions

### 1. Add provider-level config as inherited defaults

Introduce a top-level provider mapping keyed by provider name:

```toml
[providers.openai]
api_key_file = "file:env:OPENAI_API_KEY_FILE"
base_url = "https://api.openai.com/v1"
request_timeout = 120
max_retries = 5
retry_delay = 2

[[models]]
name = "fast"
provider = "openai"
model = "gpt-4o-mini"
max_tokens = 1024

[[models]]
name = "smart"
provider = "openai"
model = "gpt-4.1"
max_tokens = 8192
```

Model-level fields override provider-level fields. Existing model entries with explicit `api_key`, `base_url`, or retry fields continue to work unchanged.

Alternatives considered:

- **Global `api_keys` map only**: simpler, but too narrow because `base_url` and retry defaults are also provider-scoped and repeated today.
- **Infer env var names from provider**: less config, but too magical and hard to override for OpenRouter-compatible endpoints or multiple accounts.
- **Require every model to keep explicit keys**: avoids parser work but leaves the original problem unsolved.

### 2. Use explicit file secret fields plus a resolver

Support file-backed secrets with explicit fields such as `api_key_file` and `bot_token_file`, while keeping existing value fields (`api_key`, `bot_token`). File fields are string paths after normal env expansion. To support systemd credential paths, the env var should contain the file path:

```toml
[providers.openai]
api_key_file = "env:OPENAI_API_KEY_FILE"

[telegram]
bot_token_file = "env:TELEGRAM_BOT_TOKEN_FILE"
```

The resolver reads the file once during config parsing, strips one trailing newline, rejects empty secret files, and stores the resolved secret in the same typed config fields used by existing runtime code.

Alternatives considered:

- **Generic `file:/path` string prefix in any string field**: compact, but it makes every string field capable of filesystem reads and complicates type-specific validation.
- **`file:env:VAR` nested prefix**: expressive, but less consistent with the existing whole-string `env:VAR` resolver and harder to validate cleanly.
- **Runtime secret object passed into LLM client**: stronger separation, but a larger architectural change than needed for this proposal.

### 3. Resolve secrets at startup, not per request

Read file-backed secrets during config parsing and keep them in memory like existing env-backed secrets.

Alternatives considered:

- **Read on every request**: supports live rotation, but increases request latency and adds new failure modes on the hot path.
- **Background watcher/reloader**: useful later, but not required for safer initial deployment.

### 4. Systemd credentials are the recommended production delivery mechanism

Document a user service pattern:

```ini
[Service]
WorkingDirectory=%h/telegram-agent
ExecStart=%h/telegram-agent/.venv/bin/python main.py
LoadCredential=openai_api_key:%h/.local/share/smallpieclaw/secrets/openai_api_key
LoadCredential=telegram_bot_token:%h/.local/share/smallpieclaw/secrets/telegram_bot_token
Environment=OPENAI_API_KEY_FILE=%d/openai_api_key
Environment=TELEGRAM_BOT_TOKEN_FILE=%d/telegram_bot_token
Restart=on-failure
```

Use `%d` or `$CREDENTIALS_DIRECTORY` rather than hard-coded `/run/credentials/...` paths because user-service credential paths differ by systemd version and runtime context.

Alternatives considered:

- **`EnvironmentFile=` with secret values**: easiest today, but weaker because secrets enter the process environment.
- **1Password/Doppler/Infisical wrappers**: useful, but most wrappers still inject secret values into env. They remain compatible through existing `env:VAR` support.
- **Direct AWS/GCP/Vault integration**: more complex and provider-specific; file-based integration gives operators a common boundary.

## Risks / Trade-offs

- [Risk] Secret values are still stored in memory after config parsing -> Mitigation: this matches current behavior and keeps runtime changes bounded; avoid logging config objects containing secrets.
- [Risk] Existing subprocesses still inherit environment variables -> Mitigation: file-based deployment keeps secret values out of env, but document that env-value secrets still propagate.
- [Risk] Adding provider inheritance could make config precedence confusing -> Mitigation: define explicit order: model/embedding field wins, provider field second, built-in default last.
- [Risk] File paths from env can point to the wrong file or a missing credential -> Mitigation: fail startup with field-specific `ConfigError` messages.
- [Risk] `LoadCredential=` support varies by systemd version and distribution -> Mitigation: document `systemd --version` verification and retain `EnvironmentFile=` as a compatibility fallback with caveats.

## Migration Plan

1. Add new config dataclasses and parser support while preserving existing config syntax.
2. Add tests proving existing per-model `api_key` configs still parse identically.
3. Update `config.toml.example` to show both simple env-value secrets and recommended production file-backed provider secrets.
4. Update README deployment guidance with a `systemd --user` service example.
5. Operators can migrate incrementally: first add `[providers.<name>]`, then remove repeated model fields, then switch from `api_key` to `api_key_file` when credential files are available.
6. Rollback is config-only: restore per-model `api_key = "env:..."` entries and remove provider/file-backed fields.

## Open Questions

- Should file-backed secrets strip only one trailing newline or all surrounding whitespace? Recommended: strip one trailing newline to support normal secret files without altering intentional spaces.
- Should `embeddings.provider` inherit credentials from `[providers.<name>]` automatically when `embeddings.api_key` and `embeddings.api_key_file` are absent? Recommended: yes, then preserve the existing fallback to active model key only when no provider credential exists.
