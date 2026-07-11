## proposal Round 1 — 2026-07-08 10:40

### 🔴 Fixed

- Guard metadata JSONL sink and remote-script artifacts could bypass existing secret-redaction expectations → proposal now explicitly requires guard metadata/artifacts to honor secret redaction and calls out security impact.
- `telegram-command-surface` was listed as modified even though Shell Guard uses callback flows, not slash-command discovery changes → removed it and added `agent-scoped-directories` for new local state paths.
- “Replace the current shell-safety foundation” contradicted transparent-mode preservation → reworded to supersede regex-only safety as Shell Guard source of truth while preserving existing checks in transparent mode/migration.

### 🟡 Addressed

- Added proposal-scope safety invariants for unknown/parse-failed ask fallback, broad allow not overriding hard semantic denies, binary-global allow restrictions, and read-only/no-self-approval LLM advisor behavior.
- Added required remote-script inspection as an explicit v0.1 change.
- Clarified agent-scoped local state impact for policy, metadata, artifacts, and backups.

### 🔴 Outstanding

- None after Round 1 fixes; pending Round 2 review.

## proposal Round 2 — 2026-07-08 10:45

### ✅ Fixed (verified)

- Secret redaction requirement present and consistent with structured-event-logging spec.
- `telegram-command-surface` removed from Modified Capabilities; `agent-scoped-directories` retained.
- Supersede wording resolves the transparent-mode contradiction.

### 🔴 Fixed

- Sub-agent/headless (`caller_depth >= 1`) scope was undefined, risking removal of existing auto-deny at the shared shell choke point → proposal now states Shell Guard active/classify applies only to interactive operator-attended depth-0 shell calls in v0.1, while headless/sub-agent shell retains fail-closed auto-deny behavior.

### 🟡 Addressed

- Added invariant that Shell Guard deny decisions cannot be overridden by existing approve-all/auto-approve flows.
- Restated that classify mode remains observational and does not prompt, block, mutate policy, or otherwise enforce Shell Guard decisions.

### 🔴 Outstanding

- None; superseded by design Round 2 re-review and freeze.

## final consistency review — 2026-07-08 11:55

### ✅ Verified

- `openspec validate "guard-shell-tool-execution" --strict` passes.
- OpenSpec status reports all five artifacts complete.
- Proposal, design, specs, and tasks now consistently state that remote-script fetching is active-mode behavior; classify mode records that active mode would require inspection without fetching by default.
- Specs and design consistently state that global/generic approve-all remains available outside Shell Guard, while Shell Guard prompts do not show approve-all and do not honor existing approve-all state.

### 🟡 Addressed

- Updated tasks to explicitly cover no approve-all button in Shell Guard Telegram prompts and prompt-rendering tests.
- Cleaned stale design Round 1 review-log wording after Round 2 freeze.

### 🔴 Outstanding

- None.

## openspec-reviewer advisory implementation — 2026-07-08 12:05

### ✅ Updated

- Added testable runtime spec scenario for unconfigured/default Shell Guard behavior resolving to transparent behavior.
- Clarified classify-mode LLM behavior: non-remote command shapes may use LLM with local context only; classify does not perform remote-script downloads or web/docs lookups by default; metadata records whether LLM was used.
- Added dedicated runtime requirement that the Shell Guard advisor is read-only and non-authoritative: cannot self-approve, mutate policy, execute shell commands, or write files as part of classification.
- Pinned storage bases: metadata under `~/.local/state/<agent_name>/shell_guard/`; policy, backups, and artifacts under `~/.local/share/<agent_name>/shell_guard/`.
- Updated tasks to cover the new explicit defaults, classify-mode LLM behavior, advisor restrictions, and path bases.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes, and @openspec-reviewer re-check passed with all four advisories resolved.

## design open questions update — 2026-07-08 12:15

### ✅ Updated

- Added six architecture consistency open questions to `design.md` covering Telegram/policy alignment, CLI/runtime policy consistency, metadata secrecy, confirmation-state conflicts, classify-to-policy provenance, and cross-surface testing.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after adding open questions.

## prompt-scoped approval leases — 2026-07-08 14:45

### ✅ Updated

- Replaced legacy broad approve-all removal with prompt/run-scoped approval leases for eligible non-shell tools.
- Shell and Shell Guard asks remain ineligible and continue to use explicit one-time decisions or Shell Guard policy/rules.
- Initial eligible tool is `file_patch` only; `file_write`, `secret_get`, and `memory_graph_store` remain per-operation only.
- Leases are tied to current run/trace and expire at run end/reset; they do not apply to scheduled runs, sub-agents, or later prompts.
- Updated design, approval spec, and tasks accordingly.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after prompt-scoped approval lease updates.

## final council remediation — 2026-07-08 15:00

### ✅ Updated

- Implemented Option A precedence: hard deny always wins; exact-command allow may satisfy hard semantic ask; broad binary/binary-global/semantic allow cannot satisfy hard semantic ask.
- Reframed remote artifact references to local referenced-script or local classification evidence artifacts because Shell Guard no longer fetches remote content before user decision.
- Pinned v0.1 prompt/run-scoped lease eligibility to `file_patch` only; `file_write`, `secret_get`, `memory_graph_store`, `shell`, and Shell Guard asks are ineligible.
- Clarified scheduled-depth wording: scheduled jobs usually run at depth>=1 today, but explicit run-origin/operator-attended signal protects against legacy fallback or future unattended depth-0 paths.
- Added missing/ambiguous run-origin fail-safe scenario: treat as non-interactive/unattended and preserve existing behavior.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after final council remediation.

## approve-all removal update — 2026-07-08 12:25

### ✅ Updated

- Changed scope from bypassing legacy approve-all for Shell Guard to removing legacy broad approve-all/auto-approve behavior entirely.
- Updated proposal, design, approval spec, and tasks to require per-operation decisions for generic confirmations and no approve-all state/callback/button.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes and @openspec-reviewer scheduled-job re-review passed.

## approve-all removal review follow-up — 2026-07-08 12:35

### ✅ Updated

- Broadened `shell-guard-approval` capability description to include generic per-operation confirmation behavior after approve-all removal.
- Reworded stale active-mode rationale so it no longer references approve-all as a remaining mechanism.
- Updated tasks to explicitly mention ReAct-loop auto-confirm branch removal and `resume_approve_all` call paths.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after approve-all follow-up updates.

## consistency open questions resolution — 2026-07-08 12:45

### ✅ Updated

- Added Shell Guard approval requirement that Telegram actions must match the effective decision and cannot offer allow actions for hard deny.
- Added policy requirement that runtime and CLI use the same policy parser and matching semantics.
- Added policy apply requirement to preserve explainable provenance in accepted rules.
- Added cross-surface end-to-end test task covering runtime, Telegram actions, metadata, CLI candidate/draft/apply, and redaction consistency.
- Removed resolved open questions from `design.md`; only future UX replacement for removed approve-all remains open.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after resolving consistency open questions.

## openspec-reviewer final scope review — 2026-07-08 12:55

### ✅ Verified

- @openspec-reviewer re-read the updated proposal, design, specs, tasks, and review log.
- Approve-all removal is consistently represented with no stale contradiction.
- All six consistency points are backed by testable specs/tasks.
- Remaining open question is future UX replacement for removed approve-all and is non-blocking.
- Capability placement for generic per-operation confirmation under `shell-guard-approval` is acceptable after broadening the proposal description.

### 🟡 Advisory

- Cosmetic only: `shell-guard-approval` name remains Shell-Guard-scoped while it now also owns generic per-operation confirmation behavior. Reviewer says this is fine to leave or rename in a future change if the generic confirmation surface grows.

### 🔴 Outstanding

- None. @openspec-reviewer recommendation: PASS.

## council blocker fixes — 2026-07-08 13:20

### 🔴 Fixed

- Council blocker: run-origin/operator-attended signal was undefined → design/spec/tasks now require a typed run-origin plus operator-attended signal threaded from interactive entry points, scheduler entry points, sub-agent context, `ReactContext`, and `BuiltinExecutor.execute()` into `_exec_shell()`.
- Council blocker: active-mode `ask` with no approval channel was undefined → runtime spec and design now require fail-closed denial with clear error and metadata/log event.
- Council blocker: remote-script GET before approval needed stronger constraints → runtime/approval specs and design now require mandatory blocking for private, loopback, link-local, and metadata-service targets, Telegram network-request disclosure, and no classify-mode remote fetches.

### 🔴 Outstanding

- None. Superseded by later validation/review-log entries.

## council advisory improvements — 2026-07-08 13:35

### ✅ Updated

- Enumerated all approve-all removal points in tasks: `auto_approve_tools`, `signal_approve_all()`, `clear_auto_approve()`, `resume_approve_all()`, `confirm_all:` callback, Telegram button, and ReAct-loop auto-confirm branch.
- Renamed/strengthened classify remote behavior scenario to state classify mode never performs remote fetches.
- Confirmed scheduled/unattended bypass before parsing/LLM/metadata/policy remains specified and tested.
- Added policy/CLI concurrency requirements for timestamped backup before atomic replacement, runtime complete-policy reload semantics, and safe JSONL observation reads that ignore incomplete trailing lines.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after council advisory improvements.

## classify LLM cost/latency controls — 2026-07-08 13:45

### ✅ Updated

- Added classify-mode LLM configuration switch and timeout behavior.
- Added fail-open-for-execution classify behavior: LLM failure/timeout records `decision_if_active = ask` and metadata error but does not block shell flow.
- Added metadata requirements for `llm.used`, errors/timeouts, duration, and cache use.
- Added optional per-run cache for repeated normalized command shapes.
- Updated tasks to cover configuration, behavior, metadata, cache, and tests.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after classify LLM cost/latency controls.

## trusted executable model — 2026-07-08 14:00

### ✅ Updated

- Added trusted executable identity model to proposal/design as the safe replacement for broad binary-global allow.
- Added policy requirements for trusted executable entries using path, realpath, SHA-256, provenance, and permission checks.
- Added unit-scoped bypass semantics so trusted executables do not trust surrounding shell operators, pipelines, redirects, substitutions, or chained commands.
- Added CLI requirements for `trust add/list/verify/remove` and metadata requirements for trusted bypass/mismatch logging.
- Added tasks and tests for trusted executable policy, identity checks, CLI, unit-scoped bypass, and metadata.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after trusted executable model updates.

## web research tools and shell network routing — 2026-07-08 14:15

### ✅ Updated

- Reviewed Ollama web search/fetch documentation: `POST https://ollama.com/api/web_search` accepts `query` and optional `max_results`; `POST https://ollama.com/api/web_fetch` accepts `url` and returns title, content, and links.
- Added `web-research-tools` capability for built-in `web_search` and `web_fetch` with provider abstraction and Ollama as the only v0.1 provider.
- Added config expectations for web tool enablement, provider, Ollama API key source, timeout, result limits, and content limits.
- Added web tool constraints: no shell invocation, no fetched-content execution, no default file writes, no arbitrary headers/secrets.
- Updated Shell Guard network semantics: shell remote URL/network commands remain approval-sensitive, but prompts should steer web exploration toward built-in `web_search`/`web_fetch`.
- Added tasks and tests for web research tools and shell-network prompt suggestions.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after adding web research tools and shell network routing semantics.

## user blocker decisions — 2026-07-08 14:15

### ✅ Updated

- Remote network handling changed from guard-side active fetch/SSRF protection to user-decision handling: Shell Guard v0.1 asks the user before remote URL/network utility and remote-download-to-interpreter commands, may provide visible URL/command context and LLM advice, and does not fetch remote content before the user decides.
- Verified current scheduled jobs normally use `spawn_agent`/`SubAgentRunner` with `depth=1`, with a legacy fallback that can call the main agent directly. Existing specs remain depth-independent and require explicit run-origin/operator-attended gating.
- Trusted executable bypass hardened: common shells, interpreters, meta-execution tools, and GTFOBins-like binaries are ineligible for trusted executable bypass in v0.1 and must use normal Shell Guard classification.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after user blocker decision updates.

## proportionality remediation — 2026-07-08 14:30

### ✅ Updated

- Added deterministic careless-operation categories and a never-weaker-than-legacy invariant so active mode cannot allow, without confirmation, command shapes that legacy dangerous-shell confirmation would gate.
- Added most-restrictive-wins aggregation for compound shell commands.
- Removed trusted executable identity model from v0.1 scope; exact/argv-prefix policy rules remain the proportional mechanism for known safe command shapes.
- Removed `web_search`/`web_fetch` built-ins and Ollama provider work from this change; shell network commands remain approval-sensitive and web research tools can be proposed separately.
- Kept approve-all removal as requested by the user, despite council preference to scope it to shell only; future replacement UX remains an open follow-up.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after proportionality remediation.

## scheduled-job scope review fix — 2026-07-08 13:05

### 🔴 Fixed

- @openspec-reviewer found scheduled jobs are top-level depth-0 runs, so prior `interactive depth-0` wording did not operationally exclude them. Proposal, design, runtime spec, and tasks now state Shell Guard active/classify applies only to operator-attended interactive depth-0 shell calls and scheduled/unattended shell calls bypass Shell Guard enforcement, prompting, classify telemetry, and policy restrictions in v0.1 regardless of depth.

### 🟡 Addressed

- Design now requires an explicit run-origin/operator-attended signal in addition to `caller_depth`; `caller_depth == 0` alone is not sufficient.
- Tasks now require integration and tests for scheduled/unattended depth-0 bypass behavior.

### 🔴 Outstanding

- Pending validation/re-review.

## design Round 2 — 2026-07-08 11:10

### ✅ Fixed

- Prior serious issues verified fixed by reviewer: per-mode shell flow now preserves legacy gating in classify mode and nested metadata redaction now requires recursive string redaction.

### 🟡 Addressed

- Classify-mode outbound side effects were ambiguous → design now states classify mode does not perform remote-script downloads or web/docs lookups by default, records that active mode would require remote inspection, and may classify from local command structure only.
- Approve-all bypass implementation risk clarified → design now states reusing `tool_name="shell"` with the normal confirmation contract is insufficient.
- CLI/daemon path drift clarified → design now requires both to resolve the same agent-scoped locations.
- JSONL append behavior clarified → design calls for complete event writes as atomic lines.

### 🔴 Outstanding

- None. Design batch frozen.

## specs Round 1 — 2026-07-08 11:20

### ✅ Fixed / Verified

- Created spec deltas for all proposal capabilities: `shell-guard-runtime`, `shell-guard-approval`, `shell-guard-policy`, `shell-guard-observability`, `structured-event-logging`, and `agent-scoped-directories`.
- `openspec validate "guard-shell-tool-execution" --strict` passes.
- Modified requirements for `structured-event-logging` and `agent-scoped-directories` preserve the full existing requirement blocks and add Shell Guard behavior without dropping existing scenarios.
- Specs capture frozen proposal/design safety invariants: transparent/classify/active mode behavior, classify non-enforcement, active Shell Guard-specific confirmation, no legacy approve-all bypass, no command-substitution execution during classification, active remote-script inspection, classify no remote fetch by default, separate TOML policy, JSONL metadata, recursive known-secret redaction, CLI policy conversion, and agent-scoped storage.

### 🟡 Addressed

- Direct review was completed after delegated reviewer sessions were interrupted/stuck due model availability.

### 🔴 Outstanding

- None. Specs batch frozen.

## adr Round 1 — 2026-07-08 11:30

### ✅ Fixed / Verified

- Reviewed in-force repository ADR context and created `adr/0005-use-shell-guard-for-interactive-shell-tool-decisions.md` for the durable Shell Guard architectural pattern.
- Created change-local ADR manifest at `openspec/changes/guard-shell-tool-execution/adr.md`.
- `openspec validate "guard-shell-tool-execution" --strict` passes after ADR creation.

### 🔴 Outstanding

- None. ADR batch frozen.

## tasks Round 1 — 2026-07-08 11:35

### ✅ Fixed / Verified

- Created implementation checklist with checkbox tasks grouped by dependency order.
- Tasks cover configuration/dependency/storage, policy/redaction, parser/classifier, shell integration, Telegram approval, observability, CLI conversion, and verification.
- Each task is small enough for bounded implementation work and includes explicit validation tasks for OpenSpec, ruff, vulture, and tests.
- `openspec validate "guard-shell-tool-execution" --strict` passes and OpenSpec status reports the change complete.

### 🔴 Outstanding

- None. Tasks batch frozen.

## specs/design clarification — 2026-07-08 11:45

### ✅ Updated

- Clarified that global/generic approve-all is not removed by this change.
- Clarified that Shell Guard ask prompts do not show approve-all and do not honor existing approve-all state.
- Clarified that Shell Guard prompts use one-time decisions and explicit scoped rule creation instead of broad approve-all.

### 🔴 Outstanding

- None. `openspec validate "guard-shell-tool-execution" --strict` passes after clarification.

## proposal Round 3 — 2026-07-08 10:50

### ✅ Fixed

- Prior Round 2 sub-agent/headless scope issue verified fixed by reviewer.

### 🟡 Addressed

- Reviewer noted Shell Guard `ask` could be silently bypassed by existing tool-scoped approve-all if left undefined → proposal now states Shell Guard ask/deny decisions cannot be silently bypassed by existing approve-all/auto-approve flows.
- Reviewer noted “existing auto-deny behavior” overstated the depth≥1 path → proposal now names the existing regex-gated dangerous-shell deny behavior.

### 🔴 Outstanding

- None. Proposal batch frozen.

## design Round 1 — 2026-07-08 11:00

### 🔴 Fixed

- Per-mode shell control flow was under-specified and classify mode could be read as disabling existing dangerous-command confirmation → design now states transparent keeps legacy regex only, classify observes alongside unchanged legacy regex gating, and active uses Shell Guard as the depth-0 decision-maker without routing asks through bypassable generic shell confirmation.
- Guard metadata redaction relied on the existing normal-log redactor even though current redaction only handles top-level string fields → design now requires extracting shared string redaction logic and recursively applying it to every string in nested guard metadata and artifact summaries.

### 🟡 Addressed

- Simplified ADR wording around ADR-0003/ADR-0004 secret source and logging constraints.
- Pinned metadata JSONL to XDG state and policy/backups to agent-scoped data/config storage rather than repo-relative data paths.
- Added private/link-local blocking where practical and documented residual SSRF/webhook side-effect risk for remote-script inspection.
- Clarified classify migration step: Shell Guard itself does not prompt/block, while existing regex confirmation still gates execution.

### 🔴 Outstanding

- Pending re-review.
