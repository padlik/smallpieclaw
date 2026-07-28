## Context

The agent runs as a systemd user service. All agent state — source code, data, config, logs, vault, trusted-dirs — naturally lives under `/home/<user>/`. The nsjail shell sandbox was designed around a `project_dir` concept: a single directory mounted RW into the jail as the working directory. In practice, `project_dir` was wired to `_AGENT_DIR` (the directory containing `main.py`), which means the **entire agent installation** is mounted RW inside every sandboxed shell command.

ADR-0012 (nsjail for shell isolation) established the sandbox and its confirmation model. ADR-0015 (state outside sandbox write scope) established that `trusted_dirs.json` must live outside the sandbox's write scope to prevent mount-list injection. This change extends the same principle to the agent's code, memory, and config: none of these should be writable from inside the sandbox.

The `/home` entry in `_BLOCKED_SYSTEM_PREFIXES` was added as a defense-in-depth blanket block, but it makes the targeted blocks (`~/.ssh`, `~/.local`, `~/.config`) redundant and prevents legitimate user paths from being mounted as trusted dirs or skills dirs.

The vault file lives at `~/.local/share/<agent>/secrets.toml` (XDG_DATA_HOME) while all other agent state lives at `~/.local/state/<agent>/` (XDG_STATE_HOME). This split follows the XDG spec literally (data vs. state) but creates operational fragmentation — two directories to back up, inspect, and manage.

### Current nsjail sandbox mount layout

```
┌─────────────────────────────────────────────────────────┐
│                    Host filesystem                       │
│                                                          │
│  /home/paul/piclaw/          ← _AGENT_DIR (project_dir)  │
│  ├── main.py                                           │
│  ├── config.toml                                       │
│  ├── data/                  ← memory, scheduler, etc.   │
│  │   └── shell_logs/                                    │
│  ├── skills/                                           │
│  └── prompts/                                          │
│                                                          │
│  /home/paul/.agents/skills/ ← skills_dir (external)     │
│  /home/paul/.local/state/   ← trusted_dirs.json, logs   │
│  /home/paul/.local/share/   ← vault (secrets.toml)      │
│  /tmp/nsjail-tmp-*/         ← session tmpdir            │
└─────────────────────────────────────────────────────────┘
         │
         │  nsjail bind mounts
         ▼
┌─────────────────────────────────────────────────────────┐
│              nsjail sandbox (inside jail)                │
│                                                          │
│  /usr, /bin, /lib, ...     ← RO, system executables     │
│  /dev/null, /dev/zero      ← RO, shell redirections     │
│  /home/paul/piclaw/        ← RW (project_dir) ← PROBLEM │
│  /tmp                       ← RW (session tmpdir)        │
│  /home/paul/.agents/skills/ ← RO (REJECTED by /home)    │
│  trusted dirs               ← REJECTED by /home block   │
│  cwd: /home/paul/piclaw/   ← PROBLEM                   │
└─────────────────────────────────────────────────────────┘
```

### Target nsjail sandbox mount layout

```
┌─────────────────────────────────────────────────────────┐
│              nsjail sandbox (inside jail)                │
│                                                          │
│  /usr, /bin, /lib, ...     ← RO, system executables     │
│  /dev/null, /dev/zero      ← RO, shell redirections     │
│  /tmp                       ← RW (session tmpdir)        │
│  /home/paul/.agents/skills/ ← RO (skills_dir, ACCEPTED) │
│  trusted dirs               ← ACCEPTED (no /home block) │
│  cwd: /tmp                  ← safe scratch space         │
│                                                          │
│  _AGENT_DIR is NOT mounted ← agent code is isolated     │
└─────────────────────────────────────────────────────────┘
```

## Goals / Non-Goals

**Goals:**
- Remove the agent's code directory from the nsjail sandbox's writable scope
- Allow legitimate user paths under `/home` to be mounted as trusted dirs and skills dirs
- Consolidate all agent state files under `XDG_STATE_HOME` (`~/.local/state/<agent>/`)
- Maintain backward compatibility for existing vault deployments via one-time migration

**Non-Goals:**
- Auto-mounting `workspace_dir` or `downloads_dir` into the sandbox — these remain accessible via host-side file tools and the trusted-dirs approval mechanism
- Changing the `skills_dir` default location — it stays at `_AGENT_DIR/skills` (configurable); the RO mount will work once `/home` is unblocked
- Adding seccomp-bpf policies or additional syscall filtering — the existing namespace isolation is sufficient for this change's scope
- Changing the nsjail confirmation model (ADR-0012) — `shell_nsjail_confirm_mode` is unaffected

## Decisions

### D1: Remove `project_dir` mount entirely (no replacement RW mount)

**Decision**: Remove the `project_dir` parameter from `NsjailConfigBuilder` and `BuiltinExecutor`. Do not replace it with `workspace_dir` or any other default RW mount. Set sandbox `cwd` to `/tmp`.

**Rationale**: The `project_dir` concept assumed the agent works on a user codebase that should be writable from the sandbox. In reality, it was wired to the agent's own code directory. No directory should be blanket-mounted RW by default — the sandbox should only get `/tmp` (scratch), system dirs (executables), skills dir (RO), and explicitly-approved trusted dirs. If the agent needs to write to a host directory via shell, the operator approves it via `/dir add`, which adds it to `trusted_dirs.json` with the appropriate mode.

**Alternatives considered**:
- Replace with `workspace_dir` (`~/Documents`) — rejected: too broad, bypasses the approval gate
- Make `project_dir` configurable with no default — rejected: adds config surface for a concept that shouldn't exist; trusted dirs already cover the use case

### D2: Remove `/home` from `_BLOCKED_SYSTEM_PREFIXES`, keep targeted blocks

**Decision**: Remove `/home` from the system blocklist. Keep `_blocked_user_prefixes` with `~/.ssh`, `~/.local`, `~/.config`, and add `~/.gnupg`.

**Rationale**: The targeted blocks already prevent sensitive subdirs from being mounted. The blanket `/home` block makes the targeted blocks redundant and prevents all legitimate user paths from being mounted — which is the primary deployment scenario for systemd-user mode. Adding `~/.gnupg` covers GPG keyrings, a common sensitive path not yet blocked.

**Alternatives considered**:
- Keep `/home` blocked, add per-path carve-outs — rejected: complex, fragile, and doesn't solve the fundamental issue that `/home` is the natural location for user files in this deployment model
- Move all agent state outside `/home` (e.g., `/var/lib/<agent>/`) — rejected: breaks systemd-user deployment model, requires root privileges

### D3: Add project-dir carve-out in `_load_trusted_mounts`

**Decision**: In `_load_trusted_mounts`, if a trusted-dir entry is under a known mounted directory (e.g., `session_tmpdir` or a previously-accepted trusted dir), silently skip it rather than attempting a redundant mount.

**Rationale**: After removing `project_dir`, there's no blanket RW mount to check against. But trusted-dir entries that are nested under an already-mounted path would produce redundant mount entries. The carve-out prevents noise and redundant mounts. This is a minor implementation detail, not a security decision.

### D4: Move vault to `XDG_STATE_HOME`, migrate existing deployments

**Decision**: Change `vault_path()` default from `~/.local/share/<agent>/secrets.toml` to `~/.local/state/<agent>/secrets.toml`. Add one-time migration in `main.py`: if the old path exists and the new path doesn't, copy it. If both exist, prefer the new path and log a warning.

**Rationale**: All other agent state already lives under `~/.local/state/<agent>/` (trusted_dirs.json, logs). Consolidating the vault there means one directory to back up, inspect, and manage. The XDG spec distinguishes data (`XDG_DATA_HOME`) from state (`XDG_STATE_HOME`), but the vault is a single TOML file that changes when the operator adds/removes secrets — it's state, not cached data. The migration is non-destructive (copy, not move) so rollback is trivial.

**Alternatives considered**:
- Keep vault at `XDG_DATA_HOME`, move everything else there — rejected: logs and trusted_dirs.json are already at `XDG_STATE_HOME` and moving them is a larger migration
- Symlink from old to new path — rejected: fragile, hides the migration from the operator

### D5: ADR-0012 amendment — `project_dir` is no longer mounted

**Decision**: ADR-0012 states "The jail confines the blast radius to the project directory and explicitly trusted RW directories." This change removes the project directory from that statement. The jail now confines the blast radius to `/tmp` (session scratch) and explicitly trusted RW directories only. This is a decision-level change to an in-force ADR and requires a superseding ADR (ADR-0016).

**Rationale**: ADR-0012's context assumed the project directory was a user codebase, not the agent's own code. The principle (kernel-level isolation confines blast radius) remains valid; only the scope of what's mounted changes.

## Risks / Trade-offs

- **[Breaking] Shell commands that read/write files under `_AGENT_DIR` without a trusted-dir entry will fail** → Mitigation: operators add needed paths via `/dir add`. This is the intended security improvement — the agent's code and data should not be writable from the sandbox without explicit approval.

- **[Breaking] Shell commands that rely on `cwd` being the agent directory will fail** → Mitigation: the agent should use absolute paths or `cd` to a trusted dir. The system prompt can be updated to inform the LLM that `cwd` is `/tmp`.

- **[Migration] Vault at old path not found after upgrade if migration fails** → Mitigation: migration is a copy (non-destructive); old path remains. `SPC_VAULT_FILE` env var override still works. Log a warning if old path exists but migration was skipped.

- **[Security] Removing `/home` from blocklist could allow a sensitive path to be mounted if it's not in `_blocked_user_prefixes`** → Mitigation: the targeted blocks cover the known sensitive paths (`~/.ssh`, `~/.local`, `~/.config`, `~/.gnupg`). The trusted-dirs mechanism requires explicit operator approval via `/dir add` — it's not automatic. Skills_dir is mounted RO only.

- **[Compatibility] ADR-0012's "never" confirm mode is less dangerous now** → Positive side effect: with no project_dir RW mount, `rm -rf` in `"never"` mode can only damage `/tmp` and explicitly-trusted RW dirs, not the agent's code.

## Migration Plan

1. **Vault migration** (automatic, at startup):
   - `main.py` checks if `~/.local/share/<agent>/secrets.toml` exists
   - If yes and `~/.local/state/<agent>/secrets.toml` does not exist: copy old → new, log info
   - If both exist: log warning, prefer new path, note old path is stale
   - If neither exists: no action (fresh install)

2. **Trusted dirs** (manual, operator action):
   - Operators who previously relied on shell commands accessing files under `_AGENT_DIR` must add those paths via `/dir add`
   - The agent's system prompt should be updated to inform the LLM that `cwd` is `/tmp` and host files require explicit trusted-dir approval

3. **Rollback**:
   - Vault: set `SPC_VAULT_FILE` to the old path, or copy new → old
   - Sandbox: revert `nsjail_config.py` and `builtin_executor.py` changes (the `project_dir` parameter can be re-added)
   - Blocklist: re-add `/home` to `_BLOCKED_SYSTEM_PREFIXES`

## Open Questions

- **ADR-0012 supersession**: ADR-0012's context references "the project directory" as part of the blast-radius confinement. This change removes the project directory mount. A new ADR (ADR-0016) should supersede the relevant portion of ADR-0012, documenting that the sandbox's writable scope is now `/tmp` + trusted dirs only. The `shell_nsjail_confirm_mode` decision in ADR-0012 remains unchanged.

- **ADR-0015 coherence**: ADR-0015 established that sandbox config state must live outside the sandbox's write scope. This change is fully coherent with ADR-0015 — in fact, it strengthens the principle by removing the largest writable surface (the agent code directory) from the sandbox entirely. No supersession needed for ADR-0015.

- **System prompt update**: Should the system prompt be updated to tell the LLM that `cwd` is `/tmp` and that host files require trusted-dir approval? This is an implementation detail for the tasks phase, not a design-level question.