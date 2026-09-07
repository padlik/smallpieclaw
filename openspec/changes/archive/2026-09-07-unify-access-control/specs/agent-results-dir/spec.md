# agent-results-dir Specification

## Purpose

Define the run-results exchange directory `~/.<agent>/results/` — the agent's designated surface for transferring oversized payloads between shell runs, sub-agents, and the main agent — and its retention contract.

## ADDED Requirements

### Requirement: Results live under the agent dot-home in run-scoped subdirectories

The results directory MUST be `~/.<agent_name>/results/` (sibling of `~/.<agent_name>/skills/`, outside the prohibited XDG homes). Artifacts MUST be organized into per-run subdirectories named by the existing trace ID (`results/<trace-id>/`, `r-<8hex>` from `trace_context.py`), correlating each artifact with its run in the SQLite structured log.

#### Scenario: Sub-agent deposits an oversized report
- **GIVEN** a sub-agent produced a report too large for its result handoff via context
- **WHEN** it writes the payload via `file_write` to `~/.<agent>/results/<its-trace-id>/report.md`
- **THEN** the write classifies as ALLOWED (Tier 1 rw) and executes without confirmation

#### Scenario: Main agent paginates a result into context
- **GIVEN** a sub-agent's report at `~/.<agent>/results/r-ab12cd34/report.md`
- **WHEN** the main agent calls `file_read` with `offset`/`max_bytes` windowing on that path
- **THEN** the read classifies as ALLOWED and returns the requested window

### Requirement: Oversized shell output artifacts are stored in the results dir

Shell output exceeding the inline limit MUST be saved as an artifact under `~/.<agent>/results/<trace-id>/` (relocating the current `_finalize_shell_log` artifact location), and vault-secret redaction MUST be applied before the artifact is retained. The artifact path MUST be reported to the agent in the tool result as the pointer for subsequent `file_read` access.

#### Scenario: Oversized output lands in the run's results subdir
- **GIVEN** the nsjail (or fallback) shell backend is active and a command produces output above the inline limit
- **WHEN** the shell call completes
- **THEN** the full output is saved under `~/.<agent>/results/<current-trace-id>/`
- **AND** the tool result tells the agent the artifact path instead of inlining the output

#### Scenario: Vault secrets are redacted from retained artifacts
- **GIVEN** the shell output contains a value that exists in the vault
- **WHEN** the artifact is saved to the results dir
- **THEN** the retained file has the secret value redacted

### Requirement: The results dir is mounted read-write in the nsjail jail

When the nsjail backend is active, `~/.<agent>/results/` MUST be bind-mounted read-write at its real host path (src == dst). The whole `results/` directory is mounted once per session (not per-trace subdirectories), because the jail config is session-static while trace IDs are per-run.

#### Scenario: Shell writes an artifact retrievable by file tools
- **GIVEN** the nsjail backend is active with the results mount
- **WHEN** the agent runs `shell("pytest > ~/.<agent>/results/<trace-id>/report.txt")` inside the jail
- **THEN** the file exists at that host path
- **AND** `file_read` on the same path outside the jail returns its contents

#### Scenario: Shell can read a previously deposited result
- **GIVEN** a result artifact was written via `file_write` to the results dir
- **WHEN** a later jail process runs `shell("cat <that path>")`
- **THEN** the command succeeds (the mount is read-write at the real host path)

### Requirement: Retention is manual-clean only

The system MUST NOT automatically delete anything under `~/.<agent>/results/` — no TTL, no startup sweep, no orphans cleanup. Removal is exclusively by operator action (direct filesystem removal or future tooling).

#### Scenario: No automatic cleanup ever
- **GIVEN** results from runs completed weeks ago exist under `results/`
- **WHEN** the agent restarts, `/reset` is sent, and sessions pass
- **THEN** every artifact remains on disk until the operator removes it manually