# ADR review: provider-model-catalog

ADR review is complete for this change. Three major durable decisions are recorded in new repository-level ADRs; these records passed independent review and are accepted. Existing ADR files remain unchanged.

## In-force ADR context reviewed

All 33 existing repository ADRs were read and their supersession relationships checked. The reviewed in-force context is:

- ADR-0003 `adr/0003-use-toml-vault-format.md` — vault format; ADR-0001 and ADR-0002 are fully superseded by the existing chain.
- ADR-0004 `adr/0004-structured-primary-agent-logging.md` — structlog backbone and event contract; storage was partially superseded by ADR-0025.
- ADR-0005 `adr/0005-use-sub-agent-supervisor-boundary.md` — sub-agent supervision boundary.
- ADR-0006 `adr/0006-use-source-categories-for-agent-visibility.md` — execution visibility and capacity categories.
- ADR-0007 `adr/0007-use-agent-runtime-for-construction.md` — runtime construction boundary, profiles, and compatible products; model-resolution and per-call-generation scope is superseded by ADR-0036.
- ADR-0008 `adr/0008-use-facade-handler-package-for-builtin-tools.md` — built-in tool façade and handler boundary.
- ADR-0009 `adr/0009-native-tool-calling.md` — native-first dispatch and protocol surface; retry/error-fallback scope is superseded by ADR-0035.
- ADR-0012 `adr/0012-use-nsjail-for-shell-isolation.md` — nsjail shell isolation and confirmation policy, subject to the existing partial amendments.
- ADR-0013 `adr/0013-use-ulid-for-globally-unique-prompt-ids.md` — prompt identity.
- ADR-0014 `adr/0014-use-dual-write-archive-for-prompt-registry.md` — prompt registry persistence and bounded memory.
- ADR-0015 `adr/0015-nsjail-state-outside-sandbox-write-scope.md` — sandbox configuration outside writable scope.
- ADR-0016 `adr/0016-remove-project-dir-from-nsjail-sandbox.md` — no default project mount and sandbox write scope, subject to ADR-0018's partial wording amendment.
- ADR-0019 `adr/0019-xdg-base-directory-layout-for-agent-storage.md` — central XDG path resolver; unchanged by this change.
- ADR-0020 `adr/0020-remove-fallback-models-add-per-model-context-window.md` — no cross-model fallback and single-model clients remain; configured per-model limits, all-model vision scans, and ignored-fallback behavior are superseded by ADR-0034.
- ADR-0021 `adr/0021-use-disk-persisted-checkpoints-for-llm-error-recovery.md` — user-directed checkpoint recovery and no tool replay; attempt-state persistence is extended by ADR-0035.
- ADR-0022 `adr/0022-use-push-model-context-monitor.md` — context monitoring and tool-definition accounting.
- ADR-0023 `adr/0023-record-exactly-once-lifecycle-logging.md` — exactly-once lifecycle logging and component isolation.
- ADR-0025 `adr/0025-use-sqlite-wal-store-for-structured-logs.md` — structured log storage and query mechanism.
- ADR-0026 `adr/0026-unify-file-access-into-path-policy.md` — frozen path policy and shared file/jail classification.
- ADR-0027 `adr/0027-single-grant-ledger-for-standing-consent.md` — standing consent and grant ledger.
- ADR-0028 `adr/0028-agent-dot-home-and-closed-xdg-state.md` — private XDG homes and agent dot-home exchange surfaces; unchanged by this change.
- ADR-0029 `adr/0029-preserve-oauth-refresh-tokens-across-silent-refreshes.md` — refresh-token preservation.
- ADR-0030 `adr/0030-noninteractive-long-lived-mcp-oauth-sessions.md` — non-interactive long-lived MCP OAuth sessions.
- ADR-0031 `adr/0031-restore-oauth-provider-state-from-persisted-tokens.md` — restore OAuth state from persisted tokens.
- ADR-0032 `adr/0032-use-list-detail-split-for-log-introspection.md` — bounded log list and record detail contract.
- ADR-0033 `adr/0033-separate-turn-owned-and-scheduled-executions.md` — execution ownership and scheduled-run isolation.

The supersession graph was checked, including the partial edges `0002→0001`, `0003→0002`, `0012→0011`, `0016→0012`, `0018→0016`, `0026→0010,0018`, `0024→0011`, `0027→0011,0024`, `0028→0017`, `0020→0007`, and `0025→0004`. ADR-0001, ADR-0002, ADR-0010, ADR-0011, ADR-0017, ADR-0018, and ADR-0024 are fully superseded in the existing graph. Highest existing sequence was 0033.

## New repository-level ADRs

- `adr/0034-provider-catalog-owns-model-metadata-and-selection.md` — catalog-owned provider metadata, explicit pinned vision selection, and the metadata boundary to context budgeting; partially supersedes ADR-0020 as scoped above.
- `adr/0035-share-retry-state-across-a-logical-model-request.md` — agent-owned retry state and same-form transient retry across a logical request; partially supersedes ADR-0009 while retaining native-first behavior and extending ADR-0021 checkpoint state.
- `adr/0036-pin-admission-resolved-models-at-runtime-construction.md` — immutable admission snapshot and removal of per-call generation knobs; partially supersedes ADR-0007 while preserving its construction boundary and products.

## Decision boundary

These records document planning decisions only. They do not authorize implementation, task drafting, archive, or changes to prior ADRs.

## Amendment — 2026-10-20: scheduler and graph resolution records (completed; accepted after ADR review Round 2)

**Status:** completed — accepted after ADR review Round 2
**Review date:** 2026-10-20

This amendment appends three scoped records for exactly the settled decisions the accepted 0034–0036 batch left for a later ADR stage (design "Open Questions"): scheduler configuration isolation and legacy read-only compatibility, the scheduled pre-admission selector exception behind a root-owned admission, and independent graph model resolution. Per the authorized planning chain (no code, commit, or merge), the new records were drafted and logged as **Proposed** for review; their decision bodies passed Round 1 content review, and the Round 2 review passed the corrected manifest links and wording, after which the parent accepted the records on 2026-10-20, promoted the record statuses (status-only), and completed this amendment. Existing ADR history, statuses, and the scoped edges earlier records created (`0034→0020` partial, `0035→0009` partial, `0036→0007` partial) remain intact; none is edited or repealed whole.

### New records (Accepted)

- [`adr/0037-isolate-scheduler-configuration-and-preserve-read-only-legacy-jobs.md`](../../../adr/0037-isolate-scheduler-configuration-and-preserve-read-only-legacy-jobs.md) (Supersedes: none) — the parsed raw scheduler document is authoritative with a derived runtime view; startup never depends on job or file validity; job/file errors are contained with type-strict raw values; mutations and reloads publish document, hash, runtime view, and registrations as one unit behind the frozen lock order with an explicit reload-required latch; legacy `scheduled_jobs.json` jobs stay runnable and read-only; state JSON is a safe projection. ADR-0033 ownership and R6 `_on_attempt` disposition are unchanged.
- [`adr/0038-resolve-scheduled-selectors-before-root-owned-model-pinning.md`](../../../adr/0038-resolve-scheduled-selectors-before-root-owned-model-pinning.md) (Partially supersedes ADR-0034 and ADR-0036, scoped as above) — scheduled saved-selector classification happens only at execution inside a root-owned `admit_scheduled` that still requires a valid main model, warns bounded and redacted for malformed/unknown selectors, and inherits main; once an effective model is chosen there is no substitution in any error class; the trusted-scheduled path exempts only the model-shape guard; legacy per-job `fallback_models` is warn-and-ignored at execution instead of rejected. Ordinary admission, immutable pins, and no-cross-model-retry all remain live.
- [`adr/0039-pin-graph-models-independently-of-main-selection.md`](../../../adr/0039-pin-graph-models-independently-of-main-selection.md) (Partially supersedes ADR-0034 and ADR-0036, scoped as above) — a separate `resolve_graph_model` entry pins the explicit canonical selector without any main-selection requirement and never mutates main selection, settings, or revisions; one pin per graph component or backfill invocation with per-extraction owned clients closed in `finally`; prerequisites and the saved-catalog-only backfill/dry-run safety rules complete before any database or inference side effect; budget rules and the fixed `temperature=0.1`/`max_tokens=1024` exception are preserved.

### Supersession edges added by this amendment

`0037` adds none (it leaves ADR-0033 fully in force). `0038→0034` and `0038→0036` are partial (scoped to scheduled-selector classification and the legacy per-job fallback-setting rejection only). `0039→0034` and `0039→0036` are partial (scoped to the pending-main-selection inactivity requirement and ordinary-run admission scope for graph resolution only). The pre-existing partial edges `0034→0020`, `0035→0009`, and `0036→0007` are not whole repeals and remain exactly as originally scoped.

### Explicit ADR-0020 determination — no new edge

No supersession edge against ADR-0020 is created by 0038. ADR-0020's decision removes the LLMClient fallback chain (single-model clients) and its consequences record the loss of backup-model retry only after a provider failure; neither clause prohibits classifying an invalid, missing, or unknown selector before any model has been chosen. ADR-0020's own obsolete fallback-setting handling was already superseded by ADR-0034's actionable-rejection clause, which 0038 now narrows only for the legacy per-job `fallback_models` setting. Within one logical model request there is still exactly one model, and an inference, credential, capability, context, or provider failure after a model is chosen never selects another: ADR-0020's no-cross-model-fallback and single-model commitments therefore remain in force, unedited, and the scheduled classification is recorded as a pre-admission selector resolution, not a fallback. The same reasoning covers 0039: graph requests that hit provider failures also keep their single pinned model. R6 guard, ticket, and budget scenarios continue to reference frozen policy; no new ADR is required for them.

### Review status of this amendment

New records 0037, 0038, 0039: **Accepted** — 2026-10-20, after the independent ADR review Round 2 PASS and correct file-relative validation. The amendment is complete as a planning-only record: it authorizes no implementation, commit, merge, or apply work.
