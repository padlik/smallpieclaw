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
