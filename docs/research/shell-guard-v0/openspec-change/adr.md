# ADR Review Manifest

- Status: completed
- Review date: 2026-07-08

## Review Summary

ADR review completed for this change. The change introduces a durable architectural pattern for guarding interactive shell tool decisions with Shell Guard, distinct from transient implementation details such as exact module names.

## In-Force ADRs Reviewed

- `adr/0001-use-file-backed-provider-secrets.md` — accepted historically; secret handling concerns remain relevant where not superseded.
- `adr/0003-use-toml-vault-format.md` — accepted, supersedes ADR-0002; constrains known-secret source and TOML preference for agent-scoped operator-managed files.
- `adr/0004-structured-primary-agent-logging.md` — accepted; constrains normal logging, event taxonomy, trace identity, and known-secret redaction expectations.

## New Durable ADRs Created

- `adr/0005-use-shell-guard-for-interactive-shell-tool-decisions.md` — records Shell Guard as the architectural pattern for interactive depth-0 shell tool decisions.
