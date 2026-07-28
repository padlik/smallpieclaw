# ADR Review Manifest

- Status: completed
- Review date: 2026-07-28

## Review Summary

ADR review completed for this change. One new durable architectural decision was identified that supersedes a portion of ADR-0012.

## In-Force ADRs Reviewed

- ADR-0003: Use TOML for agent-scoped vault files (in force, not superseded — vault format unchanged)
- ADR-0007: Use agent runtime for construction (in force — unaffected)
- ADR-0008: Use facade handler package for builtin tools (in force — unaffected)
- ADR-0009: Native tool calling (in force — unaffected)
- ADR-0010: Zone-based file access control (in force — unaffected, vault path references updated in specs)
- ADR-0012: Use nsjail for shell isolation (in force — partially superseded by ADR-0016)
- ADR-0013: Use ULID for globally unique prompt IDs (in force — unaffected)
- ADR-0014: Use dual-write archive for prompt registry (in force — unaffected)
- ADR-0015: nsjail state outside sandbox write scope (in force — coherent with this change, strengthened by it)

## New Durable ADRs Created

- `adr/0016-remove-project-dir-from-nsjail-sandbox.md` — Removes the `project_dir` RW mount from the nsjail sandbox, changes `cwd` to `/tmp`, removes `/home` from the system blocklist, and consolidates the vault under `XDG_STATE_HOME`. Supersedes ADR-0012 (partial — project_dir mount scope only; `shell_nsjail_confirm_mode` decision preserved).