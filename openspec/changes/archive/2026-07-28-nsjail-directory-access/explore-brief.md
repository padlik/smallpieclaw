# Explore Brief: nsjail-directory-access

## Problem

The nsjail sandbox mounts `_AGENT_DIR` (the agent's own code directory, e.g. `/home/paul/piclaw/`) as `project_dir` **read-write** and sets it as `cwd`. This exposes all agent internals — source code, memory store, config, scheduler state, tool index — to any sandboxed shell command. A compromised or buggy LLM-issued command can read secrets, corrupt memory, or overwrite agent code.

Additionally, the `/home` entry in `_BLOCKED_SYSTEM_PREFIXES` prevents any path under `/home` from being mounted as a trusted dir or skills dir — which is the natural location for user files in a systemd-user deployment. The targeted blocks (`~/.ssh`, `~/.local`, `~/.config`) already cover sensitive subdirs; the blanket `/home` block is redundant and harmful.

Finally, the vault file (`secrets.toml`) lives under `XDG_DATA_HOME` (`~/.local/share/<agent>/`) while all other agent state files live under `XDG_STATE_HOME` (`~/.local/state/<agent>/`). Consolidating improves operational simplicity.

## Alternatives Rejected

1. **Keep `project_dir` mount, add seccomp/permission filtering** — rejected: too complex, fragile, and doesn't fix the fundamental issue that agent code shouldn't be in the sandbox.
2. **Replace `project_dir` with `workspace_dir` as the RW mount** — rejected: `workspace_dir` (`~/Documents`) is too broad and shouldn't be auto-mounted; user files should go through the trusted-dirs approval mechanism.
3. **Make `project_dir` configurable, default to `workspace_dir`** — rejected: same issue; no directory should be blanket-mounted RW by default. The sandbox should only get `/tmp`, system dirs, and explicitly-approved trusted dirs.

## Final Approach

### 1. Remove `project_dir` RW mount
- Remove the `project_dir` parameter from `NsjailConfigBuilder.__init__` and the RW mount in `build()`.
- Set sandbox `cwd` to `/tmp` (the session tmpdir, already mounted RW).
- Remove `nsjail_project_dir` parameter from `BuiltinExecutor.__init__` and the `nsjail_project_dir=_AGENT_DIR` wiring in `main.py`.

### 2. Fix `/home` blocklist
- Remove `/home` from `_BLOCKED_SYSTEM_PREFIXES`.
- Keep targeted blocks in `_blocked_user_prefixes`: `~/.ssh`, `~/.local`, `~/.config`.
- Add `~/.gnupg` to `_blocked_user_prefixes` for extra safety.
- Add project-dir carve-out in `_load_trusted_mounts`: skip (not reject) trusted-dir entries that are under `project_dir` — they're already accessible. (After removing `project_dir`, this becomes a no-op for the agent dir, but remains useful if a project dir is reintroduced later.)

### 3. Consolidate vault to XDG_STATE_HOME
- Change `vault_path()` in `config_schema.py` to default to `~/.local/state/<agent>/secrets.toml` instead of `~/.local/share/<agent>/secrets.toml`.
- Add one-time migration in `main.py`: if old vault path exists and new doesn't, copy it.
- Update `_blocked_user_prefixes` — `~/.local` remains blocked (vault now lives there, but the block prevents the *directory* from being mounted as a trusted dir; the vault file itself is already classified as UNRECOGNISED by `TrustedZoneChecker`).

### 4. Fix `shell_env` tool return contract (already done)
- Already fixed in this session: `shell_env_set/unset/list/get` now return `output` and `error` keys.
- `react_loop.py` hardened to use `.get()` instead of direct subscript.
- Tests updated.

### 5. Mount `/dev/null` and `/dev/zero` (already done)
- Already fixed in this session: minimal `/dev` nodes added to nsjail config.

## Cross-Module Data Flows

- `main.py` → `BuiltinExecutor(nsjail_project_dir=...)` → `NsjailConfigBuilder(project_dir=...)` → `build()` mounts `project_dir` RW + sets `cwd`. **Removing `project_dir` breaks this chain.**
- `main.py` → `vault_path(cfg)` → `BuiltinExecutor(vault_path=...)` → `SecretsTools`. **Vault path change is isolated to `config_schema.py` + `main.py` migration.**
- `nsjail_config.py` `_BLOCKED_SYSTEM_PREFIXES` → `_load_trusted_mounts()` blocklist + `skills_dir` blocklist. **Removing `/home` affects both paths.**
- `nsjail_config.py` `_blocked_user_prefixes` → `_load_trusted_mounts()` blocklist only. **Adding `~/.gnupg` is additive.**

## Open Questions

1. Should `skills_dir` default change from `_AGENT_DIR/skills` to `~/.agents/skills` (XDG-style)? — **No, keep as-is.** The skills_dir RO mount will work once `/home` is unblocked. Users can override via config.
2. Should `workspace_dir` and `downloads_dir` be auto-added to trusted dirs for nsjail? — **No.** They're already default trusted zones for the file tools (host-side). For shell access, users should explicitly approve them via the trusted-dirs mechanism. Auto-mounting would bypass the approval gate.
3. Does removing `project_dir` break any existing tests? — Yes, `test_nsjail_config.py` has 20+ tests that assert `project_dir` mount and `cwd`. These need updating.