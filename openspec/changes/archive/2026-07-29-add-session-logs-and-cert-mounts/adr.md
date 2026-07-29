# ADR Review Manifest

- Status: completed
- Review date: 2026-07-29

## Review Summary

ADR review completed for this change. One durable architectural decision was identified: the read-only reversal of the archived "shell_logs not mounted" decision for the renamed `session_logs` directory.

## In-Force ADRs Reviewed

- ADR-0012: Use nsjail for shell command isolation with configurable confirmation — unaffected (this change adds mounts and env vars, not confirmation logic).
- ADR-0015: nsjail sandbox configuration state must reside outside the sandbox's write scope — not violated (`session_logs` is output, not configuration; mount is read-only).
- ADR-0016: Remove project_dir mount from nsjail sandbox — coherent (extends the XDG state home consolidation to `session_logs` and `conversations/`).

## New Durable ADRs Created

- `adr/0017-mount-session-logs-readonly-in-nsjail.md` — Records the decision to mount the active conversation's `session_logs` folder read-only inside the nsjail jail at the same host path, superseding the archived "shell_logs not mounted" decision from `2026-07-28-add-shell-isolation-improvements` Decision 3.