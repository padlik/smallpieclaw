# ADR Review Manifest

- Status: completed
- Review date: 2026-11-01

## Review Summary

ADR review completed for this change against the 39 existing records (`adr/0001`–`adr/0039`), the frozen proposal/design and nine pending follow-up deltas. This is a planning review, not an implementation acceptance or permission to apply, commit, merge or synchronize. The predecessor `provider-model-catalog` at `a57ab26` is implemented but its deltas remain pending; synchronize predecessor first only in a separately authorized lifecycle step. Reviewed planning and new ADRs must later be published through a docs-only branch based on `main`, with the exact planning state committed and merged to `main` and brought back to the implementation branch before separately authorized apply.

## In-Force ADRs Reviewed

Full-document supersession history was checked: 0001→0002→0003, 0010→0026, 0011→0024→0027 (with partial earlier edges), 0017→0028, 0018→0026; partial edges do not kill entire older records. The affected live constraints are:

| ADR | Remaining live boundary / scoped replacement |
| --- | --- |
| [0007](../../../adr/0007-use-agent-runtime-for-construction.md), [0036](../../../adr/0036-pin-admission-resolved-models-at-runtime-construction.md) | Keep `main.py` composition, `AgentRuntime` profiles/products and one immutable admitted pin; replace only catalog-based resolution/metadata assumptions in 0036 via 0041. |
| [0009](../../../adr/0009-native-tool-calling.md), [0035](../../../adr/0035-share-retry-state-across-a-logical-model-request.md), [0021](../../../adr/0021-use-disk-persisted-checkpoints-for-llm-error-recovery.md) | Keep native-first/local unsupported conversion, logical-request retries, checkpoints and no replay; budget removal does not undo protocol validation. |
| [0019](../../../adr/0019-xdg-base-directory-layout-for-agent-storage.md), [0028](../../../adr/0028-agent-dot-home-and-closed-xdg-state.md), [0003](../../../adr/0003-use-toml-vault-format.md) | Keep central pure XDG resolver, private-home boundary, vault/credential isolation and unrelated storage-layout migration; 0040 adds two correctly bucketed leaves. |
| [0020](../../../adr/0020-remove-fallback-models-add-per-model-context-window.md), [0022](../../../adr/0022-use-push-model-context-monitor.md) | 0043 replaces still-live per-window compaction formula and reserve/headroom/danger/trigger, not single-model/no-fallback or MAIN-only per-turn reference-swap/counts. Previously superseded all-model vision scan is not restored. |
| [0033](../../../adr/0033-separate-turn-owned-and-scheduled-executions.md) | Preserve turn/job ownership, job context/record, origin isolation, cancellation and bounded drain. |
| [0034](../../../adr/0034-provider-catalog-owns-model-metadata-and-selection.md) | 0040 replaces catalog authority/metadata/supplementation/capability/default policy; 0041 replaces explicit Main/cache gate; 0042 replaces graph prerequisite; 0043 replaces metadata-derived required-output-limit budgeting/rejection with a configured mandatory adapter cap while retaining optional-cap omission; 0044 replaces model migration. Keep no-cross-model fallback, actionable agent fallback rejection and credential routing. |
| [0037](../../../adr/0037-isolate-scheduler-configuration-and-preserve-read-only-legacy-jobs.md) | 0044 replaces only runnable JSON, legacy shadow refusal/fingerprints and legacy schedule derivation; preserve raw TOML/atomic writes/TOML hashes and generations/locks/freshness, type-strict job containment, mutation refusal timing, reload latch and records. |
| [0038](../../../adr/0038-resolve-scheduled-selectors-before-root-owned-model-pinning.md) | 0041 replaces the restated ordinary active-Main gate for explicit requests, scheduled Main prerequisite, warn-inherit and live rechecks; implicit requests still need Main. 0044 replaces ignore of per-job fallback. Keep trusted-source minimal exemption, bounded evidence, root single admission and job lifecycle. |
| [0039](../../../adr/0039-pin-graph-models-independently-of-main-selection.md) | 0042 replaces catalog pin/fit prerequisites and rechecks, retaining Main independence, settled extraction route, per-extraction owned clients, pre-DB validation and bounded shutdown. |

Other reviewed current records (unaffected in this scope): [0004](../../../adr/0004-structured-primary-agent-logging.md) (structlog/redaction only), [0005](../../../adr/0005-use-sub-agent-supervisor-boundary.md), [0006](../../../adr/0006-use-source-categories-for-agent-visibility.md), [0008](../../../adr/0008-use-facade-handler-package-for-builtin-tools.md), [0012](../../../adr/0012-use-nsjail-for-shell-isolation.md) (remaining shell confirmation), [0013](../../../adr/0013-use-ulid-for-globally-unique-prompt-ids.md), [0014](../../../adr/0014-use-dual-write-archive-for-prompt-registry.md), [0015](../../../adr/0015-nsjail-state-outside-sandbox-write-scope.md), [0016](../../../adr/0016-remove-project-dir-from-nsjail-sandbox.md) (remaining mount constraints), [0023](../../../adr/0023-record-exactly-once-lifecycle-logging.md), [0025](../../../adr/0025-use-sqlite-wal-store-for-structured-logs.md), [0026](../../../adr/0026-unify-file-access-into-path-policy.md), [0027](../../../adr/0027-single-grant-ledger-for-standing-consent.md), [0029](../../../adr/0029-preserve-oauth-refresh-tokens-across-silent-refreshes.md), [0030](../../../adr/0030-noninteractive-long-lived-mcp-oauth-sessions.md), [0031](../../../adr/0031-restore-oauth-provider-state-from-persisted-tokens.md), [0032](../../../adr/0032-use-list-detail-split-for-log-introspection.md). Historical fully superseded 0001/0002/0010/0011/0017/0018/0024 and their partial-edge histories were reviewed, not treated as newly live contracts.

## New Durable ADRs Created

- [ADR-0040: Separate advisory cache from deliberate selection](../../../adr/0040-separate-advisory-model-cache-from-deliberate-selection.md) — scoped 0034.
- [ADR-0041: Admit exact chat selectors once](../../../adr/0041-admit-exact-chat-selectors-once-without-catalog-authority.md) — scoped 0034/0036/0038.
- [ADR-0042: Independently configure auxiliary inference](../../../adr/0042-configure-auxiliary-inference-independently-of-chat.md) — scoped 0034/0039.
- [ADR-0043: Input counts and success-only compaction](../../../adr/0043-use-input-counts-and-success-only-summary-compaction.md) — scoped 0020/0022/0034.
- [ADR-0044: Retire model and scheduler compatibility](../../../adr/0044-retire-model-and-scheduler-compatibility-without-conversion.md) — scoped 0034/0037/0038.
