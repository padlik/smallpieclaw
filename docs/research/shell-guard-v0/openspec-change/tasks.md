## 1. Configuration, dependency, and storage foundations

- [ ] 1.1 Add `bashlex` to project dependencies and document the MIT/Aegish attribution approach for adapted code or prompt text.
- [ ] 1.2 Add typed Shell Guard configuration for enabled/mode/policy path/metadata path/artifact path/classify LLM use/classify LLM timeout with explicit transparent/default-disabled behavior and tests for unconfigured defaults.
- [ ] 1.3 Implement agent-scoped path resolution with metadata under `~/.local/state/<agent_name>/shell_guard/` and policy/backups/artifacts under `~/.local/share/<agent_name>/shell_guard/`, shared by daemon and CLI.
- [ ] 1.4 Add owner-only directory/file creation helpers for Shell Guard metadata and artifact paths where supported.

## 2. Policy model and redaction primitives

- [ ] 2.1 Implement shared TOML policy loading with version, defaults, provenance, and `[[rules]]` entries for exact, argv-prefix, binary, and semantic scopes used by both runtime and CLI.
- [ ] 2.2 Build in-memory policy indexes and decision precedence for hard semantic deny, policy deny, exact allow satisfying hard semantic ask, hard semantic ask, policy ask/allow, and unknown/parse-error defaults.
- [ ] 2.3 Enforce binary/binary-global/semantic allow restrictions so broad allow rules cannot satisfy hard semantic ask or deny categories; exact-command allow may satisfy hard semantic ask.
- [ ] 2.4 Extract shared known-secret string redaction logic from normal logging and apply it recursively to nested Shell Guard metadata and artifact summaries.

## 3. Parser, canonicalization, and classifier pipeline

- [ ] 3.1 Implement Shell Guard canonicalization and annotation helpers adapted from Aegish concepts without classification-time execution.
- [ ] 3.2 Implement `bashlex` parsing and command-unit extraction for simple commands, pipelines/lists, redirects, substitutions, and parse-error fallback.
- [ ] 3.3 Implement deterministic careless-operation categories seeded from existing `_DANGEROUS_SHELL_PATTERNS`, including remote-download-to-interpreter, host namespace escape, destructive recursive delete, raw device/disk writes, filesystem format commands, critical-path clobbering redirects, broad permission/ownership changes, history rewrites or forced pushes, and broad absolute-path mutations.
- [ ] 3.4 Implement read-only, non-self-approving LLM classifier prompt/response parsing with Aegish-style `allow`/`warn`/`block`, risk, confidence, and reason.
- [ ] 3.5 Implement active-mode remote URL handling that detects remote network commands and remote-download-to-interpreter shapes, asks the user with visible destination/context, and never fetches or executes remote content before the user decides.
- [ ] 3.6 Ensure classify mode never performs remote fetches, never performs web/docs lookups, uses configurable timeout-bound LLM classification with local context only for non-remote shapes, continues on LLM failure/timeout with `decision_if_active = ask`, records `llm.used`/`llm.error`/duration/cache metadata, and records that active mode would require inspection where applicable.
- [ ] 3.7 Optionally cache repeated normalized command shapes within a run to avoid duplicate classify-mode LLM calls while recording cache use in metadata.

## 4. Shell execution integration

- [ ] 4.1 Thread a typed run-origin/operator-attended signal from interactive entry points, scheduler entry points, sub-agent context, `ReactContext`, and `BuiltinExecutor.execute()` into `_exec_shell()`, defaulting missing or ambiguous origin to unattended.
- [ ] 4.1a Integrate Shell Guard into `BuiltinExecutor._exec_shell()` using the run-origin/operator-attended signal plus `caller_depth`, while preserving transparent mode and existing depth>=1 dangerous-shell behavior.
- [ ] 4.2 Implement classify mode so Shell Guard records `decision_if_active` only for operator-attended interactive depth-0 runs while existing regex dangerous-command confirmation continues to gate execution.
- [ ] 4.2a Ensure scheduled or unattended depth-0 shell calls bypass Shell Guard active/classify behavior entirely and retain existing behavior.
- [ ] 4.3 Implement active mode allow/ask/deny results without routing Shell Guard asks through legacy generic shell confirmation.
- [ ] 4.3a Implement most-restrictive-wins aggregation across command units in pipelines, lists, and chained shell commands.
- [ ] 4.4 Replace legacy broad approve-all/auto-approve state with prompt/run-scoped approval leases for eligible non-shell tools, repurposing or replacing `auto_approve_tools`, `signal_approve_all()`, `clear_auto_approve()`, `AgentController.resume_approve_all()`, `confirm_all:` callback handling, Telegram approve-all button rendering, and the ReAct-loop auto-confirm branch.
- [ ] 4.5 Implement fail-closed active-mode `ask` behavior when no Shell Guard approval channel is available, including Telegram not configured, bot unreachable, prompt send failure, and approval timeout/no operator response, with clear error and metadata/log event.

## 5. Telegram approval flow

- [ ] 5.1 Add Shell Guard-specific pending decision state and callback handling, and replace legacy approve-all Telegram callback/state handling with prompt/run-scoped lease handling for eligible non-shell tools.
- [ ] 5.2 Render compact Shell Guard prompts with per-operation actions and no approval lease; render generic confirmation prompts with per-operation actions plus prompt/run-scoped lease actions only for eligible non-shell tools; ensure Shell Guard Telegram actions are derived from the effective decision and cannot contradict policy.
- [ ] 5.3 Implement `Details` and `Back to decision` callbacks without consuming, regenerating, or reclassifying the pending command.
- [ ] 5.4 Implement allow-this-time and deny-this-time callbacks with atomic pending decision consumption.
- [ ] 5.5 Implement create-allow-rule, create-ask-rule, and create-deny-rule flows with explicit scope selection and current-command behavior from the specs.

## 6. Observability and classify metadata

- [ ] 6.1 Implement append-only Shell Guard JSONL metadata writer with schema version, event id, trace, mode, parsed units, semantic flags, LLM fields, decision, and decision source.
- [ ] 6.2 Add normal structured/prose log summaries with Shell Guard event types and metadata references.
- [ ] 6.3 Store bulky local referenced-script or classification-evidence artifacts by reference and hash rather than embedding content in each metadata event.
- [ ] 6.4 Add tests proving recursive known-secret redaction covers nested metadata and artifact summaries.

## 7. Policy conversion CLI

- [ ] 7.1 Add `python -m shell_guard policy candidates` to group classify observations by shape, exact command, argv prefix, binary, and semantic category.
- [ ] 7.2 Add `python -m shell_guard policy draft` to generate human-reviewable TOML policy drafts without mutating active policy.
- [ ] 7.3 Add `python -m shell_guard policy apply` with interactive yes/no candidate confirmation, timestamped policy backups, atomic policy replacement, runtime reload semantics, and provenance preservation in accepted rules.
- [ ] 7.4 Add `python -m shell_guard policy apply --all` / `-a` to apply all candidates as-is while still creating a backup, using atomic replacement, and preserving provenance.
## 8. Tests and validation

- [ ] 8.1 Add unit tests for policy parsing, indexing, precedence, exact allow satisfying hard semantic ask, broad allow not satisfying hard semantic ask/deny, and scope restrictions.
- [ ] 8.2 Add unit tests for parser extraction, parse-error unknown fallback, classify LLM disabled/failure/timeout behavior, advisor cannot self-approve or mutate policy, and no substitution execution during classification.
- [ ] 8.3 Add unit tests for transparent/classify/active shell integration, including run-origin/operator-attended gating, classify preserving legacy regex gating, scheduled depth-0 shell calls bypassing Shell Guard before parsing/LLM/metadata/policy, fail-closed ask without approval channel, and absence of broad approve-all auto-confirm behavior.
- [ ] 8.4 Add unit tests for deterministic careless-operation categories, never-weaker-than-legacy behavior, and most-restrictive-wins compound command aggregation.
- [ ] 8.5 Add Telegram callback tests for generic prompt-scoped approval leases on eligible non-shell tools, no lease option for shell/secret_get/memory_graph_store/Shell Guard asks, Shell Guard details/back flow, one-time decisions, rule creation behavior, and shell-network approval prompts.
- [ ] 8.6 Add CLI tests for candidates, draft, apply, backup creation, atomic replacement, incomplete trailing JSONL line handling, and shared daemon/CLI path resolution.
- [ ] 8.7 Add end-to-end cross-surface tests proving one sample decision stays consistent across runtime decision, Telegram actions, metadata log, CLI policy candidate/draft/apply, and redaction.
- [ ] 8.8 Run `openspec validate guard-shell-tool-execution --strict` and fix any change validation issues.
- [ ] 8.9 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80`.
- [ ] 8.10 Run `make test` or targeted tests covering Shell Guard behavior.
