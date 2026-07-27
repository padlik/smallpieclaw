## Why

The `shell_nsjail_network` string field (`"none"` | `"host"`) exists but is easy to overlook and non-intuitive. A boolean `allow_net` switch with clear semantics improves UX. Additionally, the `skills/` directory is referenced in the system prompt as a source of scripts and binaries, yet it is not mounted inside the nsjail sandbox — commands like `cd <skill_dir> && ./scripts/run.sh` fail with "No such file or directory". We should fix both gaps.

## What Changes

- **Add `allow_net` boolean config field** (default `false`) to `AgentConfig` in `config_schema.py`. When `true`, nsjail shell commands can access the network (`clone_newnet: false`). When `false`, network is isolated (`clone_newnet: true`).
- **BREAKING**: Remove `shell_nsjail_network` string field from `AgentConfig`. The boolean replaces it with clearer semantics.
- **Mount `skills_dir` read-only** inside the nsjail config by default, so skill scripts referenced in the system prompt are executable inside the sandbox.
- **Skip `shell_logs` mount** — it is an internal overflow directory, not used for inter-script exchange.
- Update `NsjailConfigBuilder` to receive `skills_dir` and add a read-only mount entry when the directory exists.
- Update `builtin_executor.py` constructor and `main.py` wiring to pass `skills_dir` to the nsjail builder.
- Update tests for config schema, nsjail config builder, and e2e mount behaviour.
- Update `vulture_whitelist.py` if new symbols are flagged as unused.

## Capabilities

### New Capabilities
- `shell-network-toggle`: Allow operators to enable network access inside the nsjail shell via a simple boolean `allow_net` config field (default `false`).
- `skills-dir-sandbox-mount`: Automatically mount the `skills_dir` read-only inside the nsjail sandbox so skill scripts and binaries referenced in AVAILABLE SKILLS are accessible to shell commands.

### Modified Capabilities
- `nsjail-shell-sandboxing`: The "Network is isolated by default" requirement is updated to reference `allow_net` instead of `shell_nsjail_network`.

## Impact

- `config_schema.py`: Add `allow_net: bool = False`, remove `shell_nsjail_network: str`.
- `nsjail_config.py`: Add `skills_dir` parameter; emit read-only mount in generated config.
- `builtin_executor.py`: Constructor parameter swap; pass `skills_dir` to builder.
- `builtin_tools/shell.py`: `_should_confirm` logic update (check `allow_net` instead of `shell_nsjail_network == "none"`).
- `main.py`: Wire `skills_dir` into executor constructor.
- `tests/test_config_schema.py`, `tests/test_nsjail_config.py`, `tests/nsjail/test_nsjail_mounts.py`: Update assertions.
- `vulture_whitelist.py`: May need new entries.
