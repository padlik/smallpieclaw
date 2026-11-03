# 0041-admit-exact-chat-selectors-once-without-catalog-authority

## Status

accepted, supersedes ADR-0034, ADR-0036, ADR-0038 (partial; see Supersedes)

## Date

2026-11-01

## Supersedes

ADR-0034 (partial: remove catalog membership and active-Main prerequisite for explicit ordinary requests; keep single-model/no-fallback and obsolete agent-setting rejection). ADR-0036 (partial: replace catalog-resolved `RunModel` metadata/model-resolution assumptions with direct connection/upstream admission; keep immutable pin, single admission, rejection of per-call generation overrides, `AgentRuntime` products/profiles and `main.py` construction). ADR-0038 (partial: replace its restated active-Main gate for explicit ordinary requests, scheduled Main prerequisite, malformed/unknown warn-and-inherit substitution and fingerprint/generation/selection/catalog rechecks; implicit ordinary and scheduled requests still require an active Main selection; keep trusted scheduler source, bounded redacted selector evidence, one root admission, ownership, notifications and post-pin no-fallback). ADR-0038's per-job `fallback_models` clause is separately replaced by ADR-0044.

## Context

ADR-0034/0036 use the discovered catalog and Main selection to admit requests; ADR-0038 adds a scheduled exception that warns and substitutes Main when a saved selector is unknown. These policies were appropriate for an authoritative inventory, but become incorrect once discovery is only a disposable cache. Future callers must be able to name a configured upstream exactly even when discovery fails, without creating an alternative runtime or fallback mechanism.

## Considered Options

- Preserve catalog admission and scheduled warn-and-inherit: protects against stale saved IDs, but silently executes an unattended job on a different model and blocks valid uncached upstreams.
- Admit once from a restart-fixed connection map (chosen): explicit work survives missing Main and cache; malformed targets and actual provider rejections become visible failures instead of substitutions.
- Resolve again at construction or per send: rejected because a changed preference or rotated route could silently change an in-flight run.

## Decision Outcome

Resolve `connection-id/upstream-id` by splitting at the first slash, requiring nonempty parts and an exact configured connection. Never trim, alias, case-fold, or validate upstream existence against the cache; preserve a nonempty upstream remainder, including slashes or whitespace, exactly. A padded/unconfigured connection fails locally, while a configured connection with padded upstream is not rejected just for padding. Resolve only that connection's credential once and pin connection, endpoint, adapter and exact upstream for the entire run; never send credentials to another endpoint. An implicit request snapshots active Main once and fails actionably if unset. Explicit ordinary, on-demand, plan, diagnostic and scheduled requests require neither Main nor cache. Unknown upstreams go to the provider; rejection remains a normal failure, not a switch.

For each **Scheduled run**, absent or `None` `model` inherits active Main; every other raw value is exact or fails that job if blank, non-string, malformed or on an unknown connection, with bounded redacted evidence and the ordinary failed-launch record/alert. The scheduler-only trusted source exempts only the early model-shape guard so root-owned scheduled admission can classify raw values; all other spawn checks remain. No live fingerprint, catalog, selection revision or document-identity race recheck is needed with restart-fixed connections. Runtime construction consumes the one immutable admitted target without re-resolving credentials. Preserve `AgentRuntime`, supervision/origin isolation, per-run closure/cancellation, native-first and agent-owned retry/checkpoint/no-tool-replay behavior under ADR-0007/0009/0021/0033/0035. Per-call generation overrides stay rejected.

## Consequences

- Good: explicit requests and unattended jobs no longer depend on discovery or an unrelated Main selection; a failing explicit selector cannot silently run on Main.
- Bad: misspelled but well-shaped upstream IDs reach the provider and may incur a failed request; operators must correct saved job selectors explicitly.
- Follow-up: adapt ordinary and scheduled root admission and preserve bounded evidence, job failure containment and existing owner cleanup in the separately authorized implementation.
