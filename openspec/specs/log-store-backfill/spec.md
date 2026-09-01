# Log Store Backfill Specification

## Purpose

Define the one-time CLI (`backfill_log_store.py`) that imports the legacy JSONL structured-log archives (`agent.jsonl` and `agent.jsonl.*.gz`) into the SQLite log store (`agent_logs.sqlite`), additively and idempotently, without touching the source archives.

## Requirements

### Requirement: One-time backfill of JSONL archives into the store

The application MUST provide a standalone CLI (`backfill_log_store.py`) that imports existing `agent.jsonl` and all `agent.jsonl.*.gz` files from the logs directory into `agent_logs.sqlite`, mapping each parsed record to the store schema (wide columns, `extra` JSON, and the casefolded `search_text` computed identically to live ingestion).

Feature: log-store-backfill
Rule: The CLI is distinct from `backfill_graph_memory.py` and never touches the component log or its CLI. Backfill is additive only — source files are never modified, renamed, or deleted; they remain as a forensic fallback.

#### Scenario: Archives are imported into the store
- **GIVEN** the logs directory contains `agent.jsonl` and two `agent.jsonl.*.gz` archives holding valid records
- **WHEN** the user runs the backfill CLI
- **THEN** all valid records from all three files are inserted into `agent_logs.sqlite` with correct wide-column mapping
- **AND** each imported row's `search_text` matches the same casefolded serialization a live write would produce
- **AND** the source files remain on disk unchanged

#### Scenario: Malformed lines are skipped without failing the import
- **GIVEN** an archive contains malformed or blank lines interleaved with valid records
- **WHEN** the backfill runs
- **THEN** valid records are imported
- **AND** malformed lines are skipped with a reported count rather than aborting

#### Scenario: Re-running the backfill does not duplicate rows
- **GIVEN** the backfill has already run
- **WHEN** the user runs it again
- **THEN** no records are imported a second time
- **AND** the store contains exactly one row per source record