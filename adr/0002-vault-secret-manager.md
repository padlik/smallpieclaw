# Use agent-scoped vault for centralized secret storage

## Status

Accepted, supersedes ADR-0001

## Date

2026-07-01

## Supersedes

ADR-0001: Use file-backed provider secrets for production credentials

## Context and Problem Statement

The existing file-backed secret mechanism (ADR-0001) introduced `[providers.*]` sections and `api_key_file`/`bot_token_file` fields tied to systemd `LoadCredential=` deployment patterns. While functional, it proved inflexible: it requires systemd knowledge, does not support non-secret configuration values (e.g. base URLs, subdomains), and scatters credential logic across provider-level inheritance and per-model overrides. A simpler, more general mechanism is needed.

## Considered Options

- **Keep file-backed provider secrets (ADR-0001)** — Continue with `api_key_file` and `[providers.*]` sections.
- **Add cloud secret manager integrations** — AWS Secrets Manager, 1Password CLI, HashiCorp Vault SDK integrations.
- **Create a single agent-scoped vault with `sec:` prefix** — A JSON key-value store referenced by name from any config field, with runtime LLM access via a built-in tool.

## Decision Outcome

Chosen option: "Create a single agent-scoped vault with `sec:` prefix", because it replaces the complex provider/file mechanism with a single, simple concept that works for any string value (not just secrets) and supports both config-time resolution and runtime agent lookups.

### Consequences

- Good, because operators manage secrets in one JSON file instead of many `*_file` references.
- Good, because `sec:` works for any string value — keys, tokens, URLs, bearer headers, subdomains — not just API keys.
- Good, because the LLM can retrieve vault values at runtime via `secret_get` with user confirmation, enabling skill authors to reference vault keys.
- Good, because there is no systemd dependency or provider inheritance complexity.
- Bad, because the vault file is unencrypted JSON (future `[vault]` types can add encryption).
- Bad, because existing configs using `api_key_file`/`bot_token_file` or `[providers.*]` must be migrated.
- Bad, because removing provider-level defaults means model-level fields must be explicit or use `sec:` references.
