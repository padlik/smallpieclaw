# XDG Data Migration Specification

## Purpose

Define `migrate.py` one-shot migration from the old agent-home-relative layout to XDG Base Directory paths, and the `_check_migration()` auto-trigger in `main.py`.

## Requirements

### Requirement: One-shot file migration from old layout to XDG paths

`migrate.py` MUST copy agent data files from the old agent-home-relative layout to their XDG Base Directory destinations. All copy operations MUST be non-destructive (source preserved), except `data/tool_index.json` which is deleted from source rather than copied. Each destination file is skipped if it already exists. After all steps succeed, a migration sentinel is written to `paths.state_home`.

Feature: XDG data migration
Rule: `python migrate.py --agent-name <name>` resolves all XDG target paths and copies each listed source file to its XDG destination using `shutil.copy2` via an atomic write-to-tmp-then-rename; existing destinations are not overwritten; `data/tool_index.json` is deleted from source and not copied.

#### Scenario: Full migration copies all listed files to XDG locations
- **GIVEN** an old agent home directory at `/home/user/piclaw/` containing `config.toml`, `scheduler.toml`, `data/memory.json`, `data/graph_memory` (plus `.wal` and `.wal.checkpoint` siblings), `data/scheduler_state.json`, `data/scheduler_commands.json`, `data/scheduler_jobs.json`, `data/job_execution_log.jsonl`, `data/results_memory.json`, `data/longterm_memory.json`, `data/graph_memory_backfill_state.json`, `skills/` (directory with contents), and `data/tool_index.json`
- **AND** no migration sentinel exists in `~/.local/state/piclaw/`
- **AND** none of the XDG destination files exist
- **WHEN** `python migrate.py --agent-name piclaw --source /home/user/piclaw` is run
- **THEN** `config.toml` is copied to `~/.config/piclaw/config.toml`
- **AND** `scheduler.toml` is copied to `~/.config/piclaw/scheduler.toml`
- **AND** `data/memory.json` is copied to `~/.local/share/piclaw/memory.json`
- **AND** each of `data/graph_memory`, `data/graph_memory.wal`, and `data/graph_memory.wal.checkpoint` is copied to `~/.local/share/piclaw/<filename>`
- **AND** `data/scheduler_state.json` is copied to `~/.local/state/piclaw/scheduler_state.json`
- **AND** `data/scheduler_commands.json` is copied to `~/.local/state/piclaw/scheduler_commands.json`
- **AND** `data/scheduler_jobs.json` is copied to `~/.local/state/piclaw/scheduler_jobs.json`
- **AND** `data/job_execution_log.jsonl` is copied to `~/.local/state/piclaw/job_execution_log.jsonl`
- **AND** `data/results_memory.json` is copied to `~/.local/share/piclaw/results_memory.json`
- **AND** `data/longterm_memory.json` is copied to `~/.local/share/piclaw/longterm_memory.json`
- **AND** `data/graph_memory_backfill_state.json` is copied to `~/.local/share/piclaw/graph_memory_backfill_state.json`
- **AND** the `skills/` directory is recursively copied to `~/.local/state/piclaw/skills/`
- **AND** all source files except `data/tool_index.json` remain in the source directory
- **AND** a `migrated_from_<timestamp>.sentinel` file is written to `~/.local/state/piclaw/`

#### Scenario: `data/tool_index.json` is deleted from source and not copied
- **GIVEN** `data/tool_index.json` exists in the source directory
- **AND** all other migration steps succeed
- **WHEN** `python migrate.py --agent-name piclaw --source /home/user/piclaw` is run
- **THEN** `data/tool_index.json` is removed from the source directory
- **AND** no `tool_index.json` is created at any XDG destination path
- **AND** the tool index regenerates automatically on the next agent startup to `~/.cache/piclaw/tool_index.json`

#### Scenario: Existing destination file is not overwritten
- **GIVEN** `~/.local/share/piclaw/memory.json` already exists at the XDG destination
- **AND** `data/memory.json` also exists in the source directory
- **WHEN** `python migrate.py --agent-name piclaw --source /home/user/piclaw` is run
- **THEN** `~/.local/share/piclaw/memory.json` is left unchanged
- **AND** the source `data/memory.json` is preserved
- **AND** migration continues processing all remaining files

#### Scenario: `--source` points to a directory other than the script's parent
- **GIVEN** the old agent home is at `/opt/bots/piclaw/`
- **AND** `migrate.py` resides in a different directory
- **WHEN** `python migrate.py --agent-name piclaw --source /opt/bots/piclaw` is run
- **THEN** source files are read from `/opt/bots/piclaw/` and its `data/` and `skills/` subdirectories
- **AND** destination paths resolve under the XDG directories keyed by `piclaw`
- **AND** migration proceeds identically to the default-source case

### Requirement: Dry-run mode reports the plan without modifying state

`migrate.py` MUST support a `--dry-run` flag that prints the intended migration operations without copying or deleting any files and without writing the migration sentinel.

Feature: Dry-run migration preview
Rule: Under `--dry-run`, all planned copy and delete operations are printed to stdout; no files are created, overwritten, or removed; the migration sentinel is not written.

#### Scenario: `--dry-run` prints the migration plan without writing any files
- **GIVEN** an old layout exists at the source directory with all migration-eligible files present
- **AND** no migration sentinel exists in `paths.state_home`
- **WHEN** `python migrate.py --agent-name piclaw --source /home/user/piclaw --dry-run` is run
- **THEN** each planned copy and the planned deletion of `data/tool_index.json` are printed to stdout
- **AND** no files are created or modified at any XDG destination path
- **AND** `data/tool_index.json` is not deleted from the source directory
- **AND** no migration sentinel file is written to `~/.local/state/piclaw/`

### Requirement: Migration sentinel ensures idempotency

After a successful migration, `write_migration_sentinel(paths)` MUST write a timestamped `migrated_from_<timestamp>.sentinel` file to `paths.state_home`. On any subsequent invocation, `migration_sentinel_exists(paths)` returning `True` MUST cause the script to exit immediately without re-processing any files.

Feature: Migration sentinel
Rule: A `migrated_from_*.sentinel` file in `state_home` is the canonical marker that migration has already run; its presence causes any further invocation to exit without copying or deleting files; the sentinel is never written under `--dry-run`.

#### Scenario: Sentinel file prevents re-migration on subsequent runs
- **GIVEN** a prior migration completed successfully
- **AND** a `migrated_from_*.sentinel` file exists in `~/.local/state/piclaw/`
- **WHEN** `python migrate.py --agent-name piclaw --source /home/user/piclaw` is run again
- **THEN** the script exits immediately without copying or deleting any files
- **AND** the existing sentinel file is not modified

### Requirement: `_check_migration` auto-triggers migration at agent startup

`main.py` MUST call `_check_migration(paths, agent_name)` during startup. If no sentinel exists and `config.toml` is found alongside `main.py`, migration runs automatically using `Path(__file__).parent` as the source. Results are logged at INFO level.

Feature: Startup auto-migration
Rule: `_check_migration` invokes migration when `Path(__file__).parent / "config.toml"` exists and no sentinel is present; it returns immediately when a sentinel is already in `paths.state_home`; all outcomes are logged at INFO level.

#### Scenario: `_check_migration` auto-triggers when old `config.toml` is present alongside `main.py`
- **GIVEN** no migration sentinel exists in `paths.state_home`
- **AND** a `config.toml` file exists in the same directory as `main.py`
- **WHEN** the agent starts
- **THEN** `_check_migration` invokes migration with `source=Path(__file__).parent`
- **AND** migration results are logged at INFO level

#### Scenario: `_check_migration` skips when the sentinel already exists
- **GIVEN** a `migrated_from_*.sentinel` file exists in `paths.state_home`
- **WHEN** the agent starts
- **THEN** `_check_migration` returns immediately without running migration
- **AND** no files are copied or deleted
