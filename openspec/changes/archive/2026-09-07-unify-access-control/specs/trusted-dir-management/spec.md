# trusted-dir-management Delta

## Purpose

The dynamic trust store is removed in its entirety. This capability's requirements are all REMOVED, superseded by the static `allowed_dirs` config consumed by `path-policy`, and by the GrantLedger for in-session consent.

## REMOVED Requirements

### Requirement: /dir list shows only user-added trusted directories
**Reason**: The dynamic trust store (`trusted_dirs.json`) is deleted; there is no user-added list to enumerate. Standing filesystem access now comes from static `allowed_dirs` config, inspected by editing config.toml.

**Migration**: Operators list their `allowed_dirs` config entries by opening config.toml. No `/dir` list equivalent exists.

### Requirement: /dir del removes a user-added trusted directory without confirmation
**Reason**: With no dynamic store, there is nothing to delete at runtime. Removing a directory from the allowed set is a config edit followed by a restart (PathPolicy is session-static by design).

**Migration**: Remove the entry from `[security] allowed_dirs` in config.toml and restart the agent.

### Requirement: [Add to trusted] button persists the directory permanently
**Reason**: The zone-button mechanism (and the whole staged-trust prompt row) is deleted. Permanent access is granted via config only; the button also carried the dead-token permanent-trust hole (oracle finding #5) which dies with it.

**Migration**: Use `[security] allowed_dirs` in config.toml for persistent access; use the prompt/session grants (`[✅ Confirm]` / `[✅✅ Till /reset]`) for in-session consent.

### Requirement: Default trusted directories cannot be removed
**Reason**: Superseded by `path-policy` Tier 1, which states the same guarantee with stronger semantics: Tier 1 entries are hardcoded, cannot be removed or modified by any operator action, config included.

**Migration**: See `path-policy`, requirement "Tier 1 — Agent-controlled directories are hardcoded and read-write except skills".

### Requirement: Trusted directories are used as nsjail shell sandbox mount points
**Reason**: The mount table is now derived once per session from the PathPolicy tiers (Tier 1 + Tier 2 minus prohibited overlap), not from `trusted_dirs.json`. The per-call store re-read (and its persisted-but-never-mounted divergence bug) is eliminated.

**Migration**: Dirs previously trusted via `trusted_dirs.json` are mounted in the jail only after being added to `[security] allowed_dirs` in config.toml (and only if they do not contain prohibited paths). See `nsjail-shell-sandboxing` delta.