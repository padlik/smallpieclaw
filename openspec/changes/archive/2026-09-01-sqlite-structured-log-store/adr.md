# ADR Review Manifest

- Status: completed
- Review date: 2026-08-31

## Review Summary

ADR review completed for this change. The change introduces one major durable architectural decision: the structured log sink's storage mechanism changes from append-only JSONL to a SQLite WAL store, superseding ADR-0004's deferred-store position (which ADR-0004 and ADR-0023 both pre-authorized as a mechanism-level supersession).

## In-Force ADRs Reviewed

- ADR-0004 — structured-primary agent logging (structlog backbone: remains in force; its deferred-store position and active-file query mechanism are superseded by this change)
- ADR-0023 — record-exactly-once lifecycle logging with component log isolation (remains in force unchanged; its follow-up note explicitly anticipated this supersession)
- ADR-0002/0003 — vault secret manager / TOML vault format (redaction layer unchanged, reviewed for the secret-redaction requirement)
- ADR-0013 — ULID prompt ids (unchanged; prompt_id exact-match filtering preserved)
- ADR-0019 — XDG base directory layout (store location `~/.local/state/<agent_name>/logs/agent_logs.sqlite` follows it)

## New Durable ADRs Created

- `adr/0025-use-sqlite-wal-store-for-structured-logs.md` — accepted, supersedes ADR-0004 (partial: structured sink storage mechanism and active-file query approach only). Captures: hard swap (no config toggle), wide-columns + `extra` JSON + `search_text` schema, single-writer queue ingestion with WAL, 30-day DELETE retention, full-retention indexed querying, additive-only backfill.