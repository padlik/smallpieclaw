# ADR Review Manifest

- Status: completed
- Review date: 2026-08-03

## Review Summary

All 18 in-force ADRs reviewed against the xdg-directories change. No existing ADR requires supersession. ADR-0015 and ADR-0016 are the most directly relevant: ADR-0015 already assumed XDG state paths for nsjail config, and ADR-0016 began retiring `_AGENT_DIR` as a storage root — this change completes that retirement globally. One new durable ADR is created to establish XDG Base Directory layout as the canonical path architecture.

## In-Force ADRs Reviewed

- ADR-0001: Use file-backed provider secrets
- ADR-0002: Vault secret manager
- ADR-0003: Use TOML vault format
- ADR-0004: Structured primary agent logging
- ADR-0005: Use sub-agent supervisor boundary
- ADR-0006: Use source categories for agent visibility
- ADR-0007: Use agent runtime for construction
- ADR-0008: Use facade handler package for builtin tools
- ADR-0009: Native tool calling
- ADR-0010: Zone-based file access control
- ADR-0011: Per-prompt approval scope
- ADR-0012: Use nsjail for shell isolation
- ADR-0013: Use ULID for globally-unique prompt IDs
- ADR-0014: Use dual-write archive for prompt registry
- ADR-0015: nsjail state outside sandbox write scope
- ADR-0016: Remove project_dir from nsjail sandbox
- ADR-0017: Mount session logs read-only in nsjail
- ADR-0018: Mount default trusted zones into nsjail

No in-force ADRs require supersession.

**Directly relevant notes:**

- **ADR-0015** already stored nsjail state at `$XDG_STATE_HOME/<agent_name>/nsjail/` — this change formalizes the XDG layout that ADR-0015 assumed. The nsjail state security principle is unaffected. ADR-0015 Consequence 4 anticipated a `nsjail_state_dir` config override for container deployments that was never implemented; setting `XDG_STATE_HOME` is the correct override mechanism under this change.
- **ADR-0016** retired `_AGENT_DIR` as a mount source for the project_dir. This change retires `_AGENT_DIR` entirely as a code concept. ADR-0016's core decision (no project_dir mount, cwd = /tmp) remains fully in force; its historical context reference to `_AGENT_DIR` is now a completed migration.
- **ADR-0017** places session logs at `~/.local/state/<agent>/session_logs/` — already XDG state layout; consistent with and unaffected by this change.
- **ADR-0018** trusts `workspace_dir` and `downloads_dir` as default zones. Under this change `downloads_dir` is derived from `workspace_dir` at startup (not a standalone config field); the runtime value passed to the zone classifier is unchanged. Default-trust semantics are unaffected.
- **ADR-0002/ADR-0003**: Vault path was already at `$XDG_STATE_HOME/<agent_name>/secrets.toml` in the prior layout (established by ADR-0016 Decision 5). This change removes the `file_vault` config override and `SPC_VAULT_FILE` env var, making the XDG state path non-negotiable. Vault mechanism and format (ADR-0002/0003) are unchanged.
- **ADR-0004**: `SPC_LOG_DIR` override and `log_file` config parameter are removed; logs are always `paths.logs_dir`. The structlog backbone decision (ADR-0004) is unaffected.

## New Durable ADRs Created

- `adr/0019-xdg-base-directory-layout-for-agent-storage.md` — Establishes that all agent storage paths are keyed by `agent_name` under XDG Base Directory buckets, resolved exclusively by `xdg.py`'s `XDGPaths` frozen dataclass. No per-path config overrides in `[paths]` (only `workspace_dir` survives). `--agent-name` CLI arg is the required bootstrap identity. `agent_home` as a storage root is retired. This is a long-term architectural constraint that prohibits re-introducing per-path config overrides and mandates XDG bucket assignment for all future agent storage additions.
