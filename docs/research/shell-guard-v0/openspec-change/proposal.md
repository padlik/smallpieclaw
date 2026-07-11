## Why

Agents can overuse the shell tool while trying to solve problems, and the current regex-based dangerous-command confirmation is too shallow to give users confidence or useful guidance. Shell Guard introduces an Aegish-inspired, LLM-assisted guard around interactive shell tool execution so commands can be classified, explained, logged, and either allowed, confirmed, or denied before execution.

## What Changes

- Add Shell Guard as an optional guard for interactive built-in shell tool calls, with `transparent`, `classify`, and `active` modes.
- Supersede regex-only shell safety as the source of truth in Shell Guard active/classify decisions while preserving existing checks in transparent mode and during migration. Add an Aegish-inspired validation pipeline: canonicalization, `bashlex` shell structure parsing, subcommand/substitution inspection without classification-time execution, and final LLM decision-tree classification.
- Add a separate TOML policy file for persistent `allow`, `ask`, and `deny` rules scoped by exact command, binary plus argv prefix, binary, or semantic category.
- Add detailed Shell Guard JSONL metadata logging, with normal application logs carrying basic guard summaries and references to detailed metadata. Guard metadata logs and local classification evidence artifacts must honor the same secret-redaction expectations as normal logs.
- Add Telegram approval UX for active-mode `ask` decisions, including compact reasoning, details/back-to-decision flow, one-time allow/deny decisions, and persistent rule creation.
- Add classify-mode telemetry and a CLI-oriented policy conversion workflow that groups observations into reviewable policy candidates or TOML drafts, then applies approved candidates with backup creation. Classify mode remains observational only: it records what active mode would have done, but does not prompt, block, mutate policy, or otherwise enforce Shell Guard decisions.
- Add active-mode handling for commands that intend to access remote URLs or download-and-execute scripts. Shell Guard does not try to become a network-security product in v0.1: it asks the user before command execution, may provide LLM advice from command/URL context, and does not fetch remote scripts itself by default.
- Replace legacy broad approve-all/auto-approve confirmation behavior with prompt/run-scoped approval leases for eligible non-shell tools. Shell and Shell Guard asks remain ineligible and use Shell Guard policy or explicit one-time decisions.
- Add explicit Shell Guard safety invariants: unknown and parse-failed commands ask in active mode, broad allow rules do not override hard semantic denies, binary-global allow is unavailable for high/critical/unknown-risk commands, Shell Guard ask/deny decisions cannot be silently bypassed by broad tool-scoped auto-approval, and the LLM advisor remains read-only and cannot approve its own recommendation.
- Preserve the existing headless/sub-agent shell behavior in v0.1. Shell Guard active/classify applies only to interactive, operator-attended depth-0 shell calls; sub-agent/headless shell calls (`caller_depth >= 1`) must retain the existing regex-gated dangerous-shell deny behavior and must not be weakened by Shell Guard superseding regex-only safety as the interactive source of truth.
- Keep scheduled jobs, OS/process isolation, shell replacement, Landlock-style sandboxing, automatic policy promotion, and bulk Telegram policy curation out of v0.1 scope. Shell Guard active/classify applies only to operator-attended interactive depth-0 shell calls; scheduled or unattended shell calls are excluded from Shell Guard enforcement, prompting, classify telemetry, and policy restrictions in v0.1 regardless of depth until a future dedicated scheduled-job policy design changes that.
- Add an explicit run-origin/operator-attended signal threaded into shell preflight; `caller_depth` alone must not determine Shell Guard applicability.
- Fail closed when active mode decides `ask` but no Shell Guard approval channel is available.
- Treat remote URL access and remote-download-to-interpreter as user-decision points rather than attempting deep network-attack protection in v0.1.

## Capabilities

### New Capabilities

- `shell-guard-runtime`: Interactive shell tool guarding, mode behavior, Aegish-inspired classification, policy decisions, and safe fallback behavior.
- `shell-guard-approval`: Telegram confirmation behavior for Shell Guard `ask` decisions and generic prompt/run-scoped approval leases for eligible non-shell tools, including details navigation and rule-creation actions.
- `shell-guard-policy`: Separate TOML policy storage, rule matching semantics, classify observation conversion, and CLI policy promotion behavior.
- `shell-guard-observability`: Detailed Shell Guard metadata JSONL records, normal log summaries, artifact references, and classify telemetry content.

### Modified Capabilities

- `structured-event-logging`: Basic Shell Guard summaries and metadata references are added to normal structured/prose logs without replacing the existing dual-sink logging contract.
- `agent-scoped-directories`: Shell Guard policy files, metadata logs, artifact storage, and policy backups add new agent-scoped local state paths.

## Impact

- Affected shell execution path: `builtin_executor.py` shell preflight and confirmation gating before subprocess/PTY dispatch.
- Affected orchestration path: `react_loop.py` confirmation handling may receive richer Shell Guard ask/deny results.
- Affected Telegram surface: `telegram_interface.py` and `telegram_commands.py` need Shell Guard-specific prompt rendering and callback handling.
- Affected generic confirmation surface: legacy broad approve-all state, callbacks, and auto-confirm behavior should be replaced with prompt/run-scoped approval leases for eligible non-shell tools.
- Affected configuration: `config_schema.py`, `main.py`, and example config need Shell Guard enablement, mode, and policy/log path settings.
- New implementation area: Shell Guard modules for parsing/canonicalization, validation, policy matching, metadata logging, and CLI policy conversion.
- New dependency: `bashlex` for mature shell structure parsing; Aegish code/prompt ideas may be adapted under MIT with attribution where directly ported.
- New local data: Shell Guard policy TOML with rules, metadata JSONL, optional artifact files for local referenced scripts or other classification evidence, and policy backups.
- Security impact: Shell Guard metadata and artifacts can include commands, argv, URLs, and local referenced script content, so these outputs must apply secret redaction and avoid exposing known vault/config secret values.
