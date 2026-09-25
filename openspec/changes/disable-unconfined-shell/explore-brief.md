# Explore brief: disable unconfined shell

## Confirmed decisions

Model-issued host shell execution is prohibited in every form, including explicitly selected `subprocess` and `pty`. Change the default `shell_backend` from `subprocess` to `nsjail`: an omitted backend on Linux offers shell only when nsjail confinement is healthy. Shell is available only while a healthy Linux nsjail confinement backend is available. Missing nsjail, startup/configuration failure, or later backend loss disables shell; the agent and non-shell tools continue, with no host fallback. macOS shell remains unavailable in this change, including with the default backend. Main, scheduled, and sub-agent LLM contexts must reflect runtime capability: omit shell from callable tool definitions and semantic discovery when unavailable, and include an explicit prompt notice. A stale shell call must fail nonrecoverably as `shell_unavailable`, with actionable guidance; backend loss also withdraws future offers. Ordinary command nonzero exit is not backend loss. Scheduled agents are scheduled prompts, with no job-level `requires_shell` flag or permission changes: they receive the same capability notice and stale-call guard as other agents. If a scheduled prompt does not call unavailable shell and instead replies that it cannot run shell, existing scheduler behavior may record success; generic status/outcome correction (ARCH-03) is deferred to separate work. No unsafe opt-in; a confined macOS backend is separate future work.

| Runtime state | Shell offer/execution | Agent and other tools |
|---|---|---|
| Linux, default/omitted backend, healthy nsjail | Offered; execute only inside nsjail | Continue normally |
| Linux, default/omitted backend, nsjail missing or startup failure | Not offered; stale call returns `shell_unavailable` | Continue; never fall back to host |
| Linux, nsjail lost after startup | Failing call returns `shell_unavailable`; disable future offers | Continue; never fall back to host |
| Explicitly configured `subprocess` or `pty` (any platform) | Not offered/executable as host shell | Continue normally |
| macOS, default/omitted backend or explicit `nsjail` | Not offered; unavailable | Continue normally |
| Healthy nsjail, command exits nonzero | Still offered; return ordinary command result/exit status | Continue normally; backend stays healthy |
| Scheduled prompt, shell unavailable, prompt does not call shell | No shell; prompt receives capability notice | May be recorded successful by existing scheduler if it replies without calling shell; generic outcome fix deferred (ARCH-03) |

## Cross-module flow and scope

Startup backend/capability is established in `main.py`, `config_schema.py`, and `builtin_executor.py` (`_init_nsjail`, currently selecting host subprocess fallback); actual shell execution lives in `builtin_tools/shell.py`, with jail configuration in `nsjail_config.py`. Callable schemas are assembled by `builtin_tools/schemas.py` and used/cached in `react_loop.py`; prompts are assembled in `prompt_builder.py` and `prompts/system/03-capabilities.md`. Semantic tool discovery/indexing is in `tool_index.py` and exposed through `builtin_tools/descriptors.py`. Capability must reach the main run plus scheduled and sub-agent construction paths (`scheduler.py`, `agent_runtime.py`/`sub_agent_supervisor.py`) so tool offers and notices agree with execution. Stale dispatch remains guarded in `builtin_executor.py`. There is no job-level shell requirement or schedule permission contract; generic scheduled status/outcome correction (ARCH-03) is deferred. Canonical baseline conflicts with this policy: `openspec/specs/nsjail-shell-sandboxing/spec.md` explicitly specifies subprocess fallback on missing/non-Linux, while `docs/nsjail-setup.md` documents macOS/subprocess fallback and README/config examples use `subprocess` as default; update those contracts/docs/examples in the eventual proposal implementation.

## Rejected alternatives

Failing the entire agent at startup when confinement is absent; an explicit unsafe-mode opt-in; bringing up a VM-backed macOS jail in this change. All rejected: they respectively sacrifice non-shell availability, restore the prohibited escape hatch, or expand scope into a distinct backend.

## Unresolved technical questions

Define the concrete runtime health/loss signal for nsjail and how it atomically invalidates already-built/cached tool definitions across active main, scheduled, and sub-agent runs. Generic scheduler status/outcome behavior when a scheduled prompt responds without calling unavailable shell is explicitly deferred to ARCH-03, not an unresolved decision in this change.
