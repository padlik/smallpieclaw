# Use file-backed provider secrets for production credentials

## Status

Accepted

## Date

2026-06-29

## Supersedes

None

## Context and Problem Statement

`smallpieclaw` supports `env:VAR` config references, but production `systemd --user` deployments should avoid placing long-lived API keys and bot tokens directly in the process environment. Environment-value secrets can be visible through process inspection depending on host hardening and are inherited by shell, tool, script, and MCP subprocesses. At the same time, model configuration repeats provider credentials across many entries.

## Considered Options

- Keep per-model `env:VAR` API keys and document `EnvironmentFile=` usage.
- Add direct integrations for cloud secret managers such as AWS Secrets Manager, Google Secret Manager, Vault, 1Password, Doppler, or Infisical.
- Add provider-level defaults and explicit file-backed secret fields that can read systemd credentials or files created by external secret managers.

## Decision Outcome

Chosen option: "Add provider-level defaults and explicit file-backed secret fields", because it reduces repeated model credentials while supporting `systemd --user` `LoadCredential=` deployments without binding the application to a specific cloud secret manager.

### Consequences

- Good, because production services can keep secret values out of environment variables and pass only credential file paths to the process.
- Good, because multiple models can inherit credentials and transport defaults from one provider configuration.
- Good, because external secret managers remain usable when they can materialize protected files or credential files before startup.
- Bad, because secrets are still read into process memory during config parsing.
- Bad, because live secret rotation still requires restart or a future reload mechanism.
- Bad, because deployments on older systemd versions may need to use environment-value secrets as a fallback with documented caveats.
