# 0026-unify-file-access-into-path-policy

## Status

Accepted, supersedes ADR-0010, supersedes ADR-0018

## Date

2026-09-03

## Supersedes

ADR-0010: Use zone-based file access control
ADR-0018: Mount default trusted zones into nsjail

## Context

ADR-0010 established zone-based file access control (`TrustedZoneChecker`, trusted_dirs.json, sensitive-path overlay, request grants). Over time the single question "may the agent touch this path?" became scattered across 7+ decision lists in 5 files: three nsjail blocklist tuples (`nsjail_config.py:23-43`), zone tiers with inode overrides (`access_control.py`), sensitive-path patterns (`patterns.py`), an approve-all allowlist (`telegram_callbacks.py:24`), `GrantTracker` request grants, and `auto_approve_tools`. Two defect classes emerged:

1. **Store/jail divergence**: `trusted_dirs.json` is re-read per nsjail call, so a persisted entry the builder refuses to mount is silently skipped — the trust store and jail reality drift apart with no operator-visible error.
2. **Validation at read instead of write**: blocklists re-filter every classification and every mount build, compensating for an unvalidated dynamic store.

ADR-0018 extended the dynamic trust store into jail mounts, inheriting both defects. The SQLite log store (`log_query`, ADR-0025) now makes the agent's state home closeable without losing log access, and the C-16 review showed the approval plane needs one consent mechanism, not several.

## Considered Options

- **Keep the zone checker, add validation**: patch `add_trusted` to validate on write, keep per-call filtering. Smallest diff; leaves 7 lists, the dual-plane re-filtering, and the per-call nsjail rebuild in place.
- **Layered access manifests (per-surface visibility)**: expressive ("file-ok, jail-no") but adds config vocabulary with no current use case; YAGNI.
- **Single three-tier static path policy**: one frozen `PathPolicy` object constructed at startup (hardcoded prohibited dict + derived entries + config appends; hardcoded agent-controlled dirs; static operator `allowed_dirs`), consumed by both the file tools and the nsjail builder; jail config built once per session.

## Decision Outcome

We will unify all filesystem access decisions into a single frozen, session-static `PathPolicy` with three tiers: **Tier 0 Prohibited** (hardcoded superset of the former blocklists incl. credential homes; derived at startup: XDG homes, vault, config; config-appended `prohibited_dirs`; absolute priority; hard tool error, never a prompt, never grantable), **Tier 1 Agent-controlled** (workspace, downloads, `/tmp/<agent>`, skills r, results rw — hardcoded, operator-immutable), **Tier 2 Operator allowed** (static config `allowed_dirs`, rw-only, extend-only). The nsjail mount table derives from the same tiers and is built once per session. Conflicts (allowed containing prohibited) are validated once at startup; prohibited wins and the conflicting dir is not mounted (no partial bind mounts).

## Consequences

- Good: the divergence bug class is structurally impossible (one store, one consumer contract); "what can the agent touch?" has one auditable answer; jail builds become once-per-session; prohibited paths get hard denial instead of fatigue-prone prompts.
- Good: `TrustedZoneChecker`, `trusted_dirs.json`, `GrantTracker`, the sensitive-path overlay, and the blocklist tuples are deleted — net code removal.
- Bad: adding a directory now requires a config edit plus restart (session grants cover the interim); operators lose the runtime "Add to trusted" convenience (deliberate trade).
- Bad: a single validation gap in policy construction ships a gap — mitigated by a containment test asserting Tier 0 ⊇ legacy blocklists, and fail-closed startup errors.
- Neutral: previously trusted dirs prompt again until re-added via config; no migration (stale trusted_dirs.json silently ignored).
- Follow-up: the grant mechanism replacing approve-all is recorded in ADR-0027; the XDG-home closure and dot-home relocation in ADR-0028.