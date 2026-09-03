# 0028-agent-dot-home-and-closed-xdg-state

## Status

Accepted, supersedes ADR-0017

## Date

2026-09-03

## Supersedes

ADR-0017: Mount session logs read-only in nsjail

## Context

ADR-0017 mounted the session-logs folder read-only inside the jail so sandboxed commands could read prior large outputs. That made the agent's conversation history (session logs) visible to arbitrary jail processes — an exfiltration surface: an injected command could read the logs and echo their contents back through the agent itself. Meanwhile skills — the only operational content the jail needs from the state tree — forced the XDG state home to remain partially mountable (`nsjail_config.py:495-499`), and the sensitive-path overlay attempted (and failed — trivially renamed) to guard credential-named files.

Two enablers changed the calculus: (1) the SQLite WAL log store with `log_query` (ADR-0025) provides indexed, structured access to the same history without any filesystem exposure; (2) the unified PathPolicy (ADR-0026) provides a clean vehicle for closing whole subtrees with hard denial.

## Considered Options

- **Keep the RO logs mount, restrict via patterns**: rejected — pattern-based guards are bypassable by renaming; the mount itself is the exposure.
- **Mount logs via a FUSE/filtered view**: rejected — complexity far exceeds value for a read-only convenience.
- **Close the XDG homes entirely; move exchange surfaces to a dedicated dot-home**: skills and oversized-result artifacts move to `~/.<agent>/skills/` and `~/.<agent>/results/<trace-id>/`; XDG data/state/config homes become Tier 0 prohibited.

## Decision Outcome

The agent's XDG private homes (`$XDG_DATA_HOME/<agent>`, `$XDG_STATE_HOME/<agent>`, `~/.config/<agent>`) become Tier 0 prohibited: never readable by file tools, never mounted in the jail. All operator-facing exchange surfaces move to the agent dot-home `~/.<agent>/`: `skills/` (read-only, jail-mounted r; one-time first-run copy from the legacy XDG state location, legacy untouched) and `results/` (read-write, jail-mounted rw; run-scoped `<trace-id>/` subdirectories receiving oversized shell output artifacts — the relocated `_finalize_shell_log` target — with vault-secret redaction retained; retention is manual-clean only). The session-logs jail mount is removed; agents read history via `log_query` SQL. XDG *path resolution* (ADR-0019) is unchanged — only access policy to those paths changes.

## Consequences

- Good: the in-jail conversation-history exfiltration surface is eliminated; the config file (which may hold credentials) is agent-unreadable by construction.
- Good: oversized-output artifacts gain trace correlation with the SQLite log and a stable, documented home; skills leave the state tree the jail previously had to see.
- Good: results rw jail mount gives sub-agents and shell a real large-payload transfer channel, which context-size limits previously lacked.
- Bad: everything in `results/` is readable by any later jail process in the session — accepted; results is the designated exchange surface; sensitive payloads belong in the vault.
- Bad: `results/` grows unboundedly under manual-clean retention — accepted by operator choice; future tooling may add cleanup.
- Neutral: ADR-0015's placement of nsjail state outside the sandbox write scope is unaffected (the state home is now simply never mounted). ADR-0019's XDG layout remains in force for resolution.
- Follow-up: startup warning-free silent ignore of legacy `trusted_dirs.json` (per ADR-0026 migration posture).