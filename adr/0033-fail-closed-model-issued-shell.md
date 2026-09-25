# Fail closed for model-issued shell execution

## Status

Accepted, supersedes ADR-0012 (partial — default backend and unconfined fallback only)

## Date

2026-09-25

## Supersedes

ADR-0012 — specifically its opt-in `nsjail` default/activation assumption and its subprocess fallback when nsjail is unavailable or the platform is non-Linux. The jail isolation configuration and behavior, and the configurable confirmation modes when nsjail is active, remain in force. Other partial supersessions of ADR-0012 remain unaffected.

## Context

ADR-0012 introduced nsjail as an optional shell backend but allowed subprocess execution when the binary or Linux platform was unavailable. That fallback turns an unavailable security boundary into unconfined model-issued host execution; prompting for confirmation does not provide equivalent confinement. Likewise, failing the whole agent when nsjail is absent would unnecessarily disable every non-shell capability. This decision changes only the shell availability boundary and default; existing nsjail isolation and confirmation decisions remain valuable when the jail is healthy.

## Considered Options

- **Keep subprocess/PTY fallback, with confirmation:** rejected — operator approval is not a security boundary and an explicitly configured host backend remains model-issued host execution.
- **Fail agent startup unless nsjail works:** rejected — shell is optional to useful agent operation; non-shell tools must remain available.
- **Default to nsjail and fail closed for shell:** chosen — shell is exposed only after a healthy Linux jail is established, while capability loss disables shell without taking down the agent.

## Decision Outcome

`shell_backend` defaults to `nsjail`. Model-issued shell execution is available only on Linux after a real nsjail jail probe succeeds. An explicit `subprocess` or `pty` setting, non-Linux platform, missing/broken nsjail, or failed startup probe makes shell unavailable; none may execute commands on the host as a fallback. The agent and non-shell tools continue operating. If the backend is lost at runtime, shell availability latches off until process restart. Ordinary command nonzero exits and command timeouts are not backend loss and retain their ordinary command-result semantics.

When nsjail is healthy, its established isolation semantics, mount policy, and configured shell confirmation modes continue unchanged. When shell is unavailable, confirmation settings cannot re-enable it. Runtime capability is communicated to prompts and gates shell dispatch; process restart is the recovery boundary after backend loss.

## Consequences

- Good: a missing or failed jail can no longer silently route model-issued commands to the host.
- Good: non-shell agent functions remain available when shell is unavailable.
- Good: failure is latched until restart, avoiding ambiguous runtime recovery and capability re-advertisement races.
- Bad: shell is unavailable on macOS, including for operators who previously relied on subprocess or PTY execution.
- Bad: Linux deployments must install and correctly configure nsjail to use model-issued shell; existing explicit `subprocess`/`pty` configurations no longer enable it. The default change and remediation must be documented.
- Bad: runtime backend loss requires an agent restart before shell can be restored, even after an operator repairs the underlying environment.
- Neutral: this partially supersedes ADR-0012 only for backend default/activation and the host fallback; its valid jail and confirmation-mode decisions remain in force. ADR-0016's separate project-mount-scope supersession and ADR-0018/0026 path-policy history are unaffected.
