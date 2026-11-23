## ADDED Requirements

### Requirement: Graph opening performs no application-driven destructive recovery

Each construction SHALL attempt one normal engine open, allowing native replay and fresh-store creation. After any open error, application code SHALL NOT retry repair or delete, rename, truncate, quarantine or recreate database/sidecar files. This includes `corrupted wal`, `invalid wal record` and `wal_record.cpp`. This constrains application recovery, not native engine activity. XDG and empty-directory migrations SHALL remain unchanged.

#### Scenario: Former WAL recovery signatures fail without repair
- **GIVEN** a graph database and `.wal` and `.wal.checkpoint` siblings contain known bytes
- **WHEN** the engine open fails without changing files with any formerly recognized WAL signature
- **THEN** the application makes exactly one engine-open call and performs no repair filesystem operation
- **AND** database and sidecar bytes remain unchanged, with no connection, schema initialization or writer creation

#### Scenario: Missing sidecars do not trigger another open
- **GIVEN** one or both WAL sidecars are absent
- **WHEN** the normal engine open fails
- **THEN** no replacement sidecar, repair or second engine open is attempted
- **AND** files that existed before the attempt are not changed by application recovery

#### Scenario: Unrelated open error follows the same safety boundary
- **GIVEN** a graph database has a non-WAL open error
- **WHEN** graph-store construction is attempted
- **THEN** the failure propagates to containment after one engine-open attempt
- **AND** application code leaves the database and all sidecars in place

#### Scenario: Normal engine-managed opening remains available
- **GIVEN** the engine can successfully open or create the configured graph database
- **WHEN** the store is constructed
- **THEN** normal connection and schema initialization proceed
- **AND** the application neither disables native WAL replay nor performs its own sidecar repair

### Requirement: Graph-open failure degrades only the optional component

Graph factory/startup handling SHALL return unavailable store/writer and leave graph dependencies unwired after open failure while allowing otherwise valid main-agent startup. It SHALL start no graph writer or extraction for that failed opening. Live backfill SHALL exit nonzero before import/extraction, release its owned embedding client and leave source/state files unchanged. Existing dry-run non-effects SHALL remain unchanged.

#### Scenario: Main startup contains a failed graph open
- **GIVEN** valid main configuration and graph prerequisites
- **WHEN** an engine-open failure passes through the real graph factory and startup wiring
- **THEN** graph store/writer are unavailable and no graph writer or extraction starts
- **AND** the main agent remains available without graph memory

#### Scenario: Live backfill cannot open the graph
- **GIVEN** valid live-backfill prerequisites and existing source and progress-state files
- **WHEN** the database engine open fails
- **THEN** backfill exits nonzero without importing records or starting extraction
- **AND** its owned embedding client is closed and database, sidecar, source and progress-state files receive no application recovery mutation

### Requirement: Graph-open failures provide safe operator guidance

Open-failure reports SHALL use fixed guidance: graph unavailable; stop graph users; back up database and all sidecars together; investigate/restore using supported procedures; restart after repair. Guidance SHALL state destructive salvage requires explicit operator approval. Logs/output SHALL contain at most the exception class, never raw engine text, repr or traceback containing it. No Telegram warning or repair command SHALL be added.

#### Scenario: Engine exception text contains sensitive data
- **GIVEN** the engine raises an open exception containing a secret marker
- **WHEN** factory/startup or live-backfill failure is reported
- **THEN** the operator receives the fixed unavailable and backup/repair guidance
- **AND** the marker is absent from emitted logs and command output, including tracebacks
