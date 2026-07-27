# ADR Review Manifest

- Status: completed
- Review date: 2026-07-27

## Review Summary

ADR review completed for this change. The change is tactical (config field rename + mount wiring) and does not introduce a new long-term architectural commitment. No new repository-level ADR was created.

## In-Force ADRs Reviewed

The following in-force ADRs were reviewed for coherence with this change:

- **ADR-0012** — Use nsjail for shell command isolation with configurable confirmation. This change amends the network-isolation control mechanism (`shell_nsjail_network` string → `allow_net` boolean) and adds a read-only `skills_dir` mount, both within the framework established by ADR-0012. ADR-0012 remains fully in force.
- **ADR-0015** — nsjail sandbox configuration state must reside outside the sandbox's write scope. The new `skills_dir` mount is read-only, consistent with ADR-0015's principle that writable blast radius must be minimized. No state that influences sandbox configuration is introduced.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced.
