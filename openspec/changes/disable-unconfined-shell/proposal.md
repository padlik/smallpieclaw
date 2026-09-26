## Why

The current shell contract permits unsandboxed host command execution whenever nsjail is unavailable or the host is not Linux, including an explicit `subprocess`/`pty` configuration. This silent loss of confinement turns a backend limitation into a security boundary failure; the agent should remain useful without shell rather than silently crossing that boundary.

## What Changes

- **BREAKING**: Change the default `shell_backend` from `subprocess` to `nsjail`. An omitted backend on Linux offers shell only when nsjail is healthy. Permit model-issued shell only on Linux while a healthy nsjail backend is active; explicit `subprocess` and `pty` settings, missing/broken nsjail, startup failure, and macOS make shell unavailable. Never run host shell as fallback.
- Keep the agent and non-shell tools available when shell cannot be offered. Give the main, scheduled, and sub-agent LLM contexts an explicit capability notice, and omit shell from callable definitions and semantic discovery while unavailable. Withdraw future offers after runtime backend loss.
- Reject stale shell calls nonrecoverably with actionable `shell_unavailable` feedback. A command's ordinary nonzero exit is not backend loss.
- Treat stderr written by an ordinarily exiting child as child output, not evidence of nsjail backend loss.
- Scheduled agents are scheduled prompts: provide the same capability notice and stale-call guard as other agents; add no job-level `requires_shell` flag or permission change. Generic scheduler status/outcome correction (ARCH-03) is deferred. A prompt may reply that it cannot run shell without calling the tool, and existing scheduler behavior may record success; that limitation is accepted for this change. Do not add an unsafe opt-in. A confined macOS backend is separate future work.
- Replace the existing nsjail-missing/non-Linux subprocess-fallback contract and its documentation with fail-closed shell unavailability; update configuration defaults and examples.
- Keep nsjail static and per-call configuration files outside every jail-writable mount, preserving ADR-0015. Place the session `/tmp` mount source in a host-private location outside every other jail-writable mount: otherwise a jailed process could rename and replace it with a symlink into an arbitrary host directory before a subsequent launch. Add Linux Lima adversarial regressions proving jailed commands cannot alter config used by subsequent launches or redirect the session scratch source. The host-private artifact set for this boundary is static config, per-call config, and per-call supervisor diagnostic log.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `nsjail-shell-sandboxing`: Require a healthy Linux jail for shell; make nsjail the default, replace missing/non-Linux fallback with unavailable shell, specify startup/runtime loss behavior, keep generated config outside jail-writable mounts, and keep the session `/tmp` mount source host-private and outside other jail-writable mounts, with Linux Lima adversarial proof of both invariants.
- `builtin-tool-execution`: Define runtime shell availability, fail-closed stale dispatch, and nonrecoverable `shell_unavailable` results while preserving other tools.
- `native-tool-calling`: Make offered callable definitions capability-aware, including removal of shell when unavailable and refreshed offers after backend loss.

## Impact

Changes affect shell initialization/execution and dispatch (`main.py`, `config_schema.py`, `builtin_executor.py`, `builtin_tools/shell.py`, `nsjail_config.py`), configuration examples (`config.toml.example`), README/setup docs, tool schema/semantic discovery (`builtin_tools/schemas.py`, `builtin_tools/descriptors.py`, `react_loop.py`, `tool_index.py`), and prompt/runtime capability propagation (`prompt_builder.py`, system prompts, scheduler and sub-agent runtime). `main.py` must place the session scratch source in a host-private location, and `nsjail_config.py` must enforce that source is outside every other jail-writable mount; the Linux Lima adversarial lane will verify jailed rename-plus-symlink replacement cannot redirect a later mount into an arbitrary host directory. Generated static and per-call nsjail config files must also remain outside jail-writable mounts so jailed commands cannot modify state used by later launches; Lima will cover this integrity boundary too. No schedule-tool-surface contract changes: scheduled agents receive capability notice and stale-call handling like other agents, but generic scheduler status/outcome correction is deferred to separate ARCH-03 work. Consequently, a scheduled prompt may answer that shell is unavailable without calling shell while existing scheduler behavior records success. `openspec/specs/nsjail-shell-sandboxing/spec.md` and `docs/nsjail-setup.md` currently promise subprocess fallback on missing nsjail/non-Linux; deployments relying on that behavior will lose model-issued shell unless healthy Linux nsjail is active. Changing the default to nsjail also means Linux installations without a working jail, and macOS installations, start with shell unavailable. No new dependency or host-shell escape hatch is introduced.
