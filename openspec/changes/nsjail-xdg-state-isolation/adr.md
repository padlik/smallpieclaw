# ADR Review Manifest

- Status: completed
- Review date: 2026-07-24

## Review Summary

ADR review completed for this change. This change introduces one major durable architectural decision: the principle that nsjail sandbox configuration state must reside outside the sandbox's write scope. A new repository-level ADR (ADR-0015) was created.

## In-Force ADRs Reviewed

- ADR-0010: `0010-zone-based-file-access-control.md` — file-access zone classification; trusted dirs inform zone boundaries; unchanged by this change.
- ADR-0012: `0012-use-nsjail-for-shell-isolation.md` — establishes nsjail as the shell isolation backend; this change strengthens its security guarantees without modifying the decision itself.

## New Durable ADRs Created

- ADR-0015: `0015-nsjail-state-outside-sandbox-write-scope.md` — nsjail sandbox configuration state (mount lists, policy files) must reside outside the sandbox's write scope. Stores `trusted_dirs.json` under `$XDG_STATE_HOME/<agent_name>/nsjail/`. Blocks agent installation dir and XDG dirs from trusted-mount injection. Supersedes no prior ADR; extends ADR-0012 with a security constraint not previously captured.
