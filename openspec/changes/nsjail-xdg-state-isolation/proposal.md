# Proposal: nsjail XDG State Isolation

## Why

The nsjail shell backend introduced in `add-nsjail-shell-isolation` has a sandbox escape vector: `trusted_dirs.json` lives inside `{data_dir}/` which is within the agent installation directory. The agent installation directory is bind-mounted read-write into every nsjail jail as the working project directory. An LLM-driven shell command can therefore overwrite `data/trusted_dirs.json` from inside the sandbox, injecting arbitrary mount paths that take effect on the next `build()` call — granting the sandboxed process read or write access to any host directory (e.g., `/root/.ssh`, `/etc`). The sandbox provides no isolation guarantee while this vector exists.

A secondary issue: the agent installation directory itself is not in the trusted-mount blocklist, so a crafted `trusted_dirs.json` entry can re-mount or expose the agent dir and its secrets.

Shipping the nsjail backend without this fix is unsafe. This change closes the vector by moving nsjail-specific state files to an XDG state directory that is outside the sandbox's write scope, and by adding the agent dir and XDG dirs to the trusted-mount blocklist.

## What Changes

- **Move `trusted_dirs.json` to XDG state dir**: The file moves from `{data_dir}/trusted_dirs.json` to `~/.local/state/<agent_name>/nsjail/trusted_dirs.json`. This path is outside the nsjail sandbox's bind-mount scope, so shell commands running inside the jail cannot reach or modify it.

- **New `nsjail_state_dir` config field**: `AgentConfig` gains `nsjail_state_dir: str = ""`. When set, overrides the default XDG-computed path. When empty, `main.py` computes `os.path.join(xdg_state_home, agent_name, "nsjail")` using `$XDG_STATE_HOME` (default `~/.local/state`). The directory is created on first use.

- **Block agent dir and XDG dirs in trusted-mount validator**: `NsjailConfigBuilder` accepts a new `agent_dir: str` constructor parameter. `_load_trusted_mounts` adds `agent_dir` and the resolved `nsjail_state_dir` to the per-call blocklist alongside `_BLOCKED_SYSTEM_PREFIXES`. Entries pointing into or at those paths are dropped with a warning.

- **Shell logs placement**: Any future nsjail-specific state files (execution logs, config caches) will be written to `nsjail_state_dir`, not the agent data dir.

## Capabilities

### Modified Capabilities

- `nsjail-shell-sandboxing`: `trusted_dirs.json` is no longer writable from inside the sandbox. The trusted-mount blocklist is extended to cover the agent dir and XDG nsjail state dir. The nsjail state directory is XDG-compliant and user-overridable.

- `builtin-tool-execution`: `NsjailConfigBuilder` constructor gains `agent_dir` param. `main.py` computes and passes `nsjail_state_dir` and `agent_dir` at startup.

## Impact

- **`nsjail_config.py`**: `NsjailConfigBuilder.__init__` gains `agent_dir: str = ""` param. `_load_trusted_mounts` adds `agent_dir` and resolved nsjail state dir to the blocklist. Constructor reads `trusted_dirs.json` from `nsjail_state_dir` instead of `data_dir`.

- **`builtin_executor.py`**: Pass `agent_dir=nsjail_project_dir` to `NsjailConfigBuilder`. Pass `nsjail_state_dir` from config.

- **`config_schema.py`**: New `nsjail_state_dir: str = ""` field on `AgentConfig`.

- **`main.py`**: Compute `nsjail_state_dir` from XDG env or default; `os.makedirs` on startup; pass to `BuiltinExecutor`.

- **`tests/test_nsjail_config.py`**: Update builder construction in affected tests to pass `agent_dir`. Add tests for agent-dir and XDG-dir blocklist.

- **Migration**: Existing `data/trusted_dirs.json` files will no longer be read. On first run after upgrade, trusted dirs are empty — the user must re-add them via `/dir` commands. Document in release notes.
