# 0039-pin-graph-models-independently-of-main-selection

## Status

accepted, supersedes ADR-0034 and ADR-0036 (partial; see Supersedes)

## Date

2026-10-20

## Supersedes

ADR-0034 (partial: only the requirement that model work remain inactive pending an explicit main selection, as applied to explicit graph extraction and live backfill) and ADR-0036 (partial: only the ordinary-run admission scope, as applied to graph component/invocation resolution; snapshot immutability itself is not superseded). All other clauses of both remain live: per-call generation overrides stay rejected, ordinary admission keeps its active-main gate, and ADR-0007 construction, ADR-0019/0028 path boundaries, and ADR-0033 ownership are unchanged.

## Context

ADR-0034/0036 tie model resolution to an active main selection and immutable per-run pins. Graph extraction is background infrastructure whose canonical selector is explicitly configured on its own: requiring a selected main model before graph startup couples unrelated operator chat selection to that subsystem, and routing graph through ordinary admission would confuse the ordinary gate. Current behavior (`build_extraction_llm_call`) infers `agent.default_model`/first-model with a new per-call client, which ADR-0034 forbids. Graph needs an explicit, settled pin resolved independently of main selection, without mutating it.

## Considered Options

- Resolve graph through the main selection, a default, or the first catalog model: rejected — ADR-0034 forbids defaults/first-model inference, and the coupling breaks graph whenever chat selection churns.
- Re-resolve the pin for each extraction call: rejected — repeated resolution invites mid-component drift and redundant credential/fingerprint checks.
- One component-shared long-lived client: rejected — per-extraction owned clients preserve the existing ownership shape (built from the same pin each call, closed in `finally`).

## Decision Outcome

- **A third, separate registry entry.** The proposed `ModelRegistry.resolve_graph_model(selector)` (not an existing API) resolves the explicit canonical selector verbatim (no trimming or aliasing; stale discovered entries remain resolvable) independently of the main selection — it works when main is unselected, missing, or switching — and never mutates main selection, settings, or revisions. Tool, prompt, Telegram, spawn, and scheduler paths cannot reach it; the composition root hands the resulting pin only to graph code. Current catalog/selection revisions ride on the returned pin read-only for provenance and gate nothing.
- **One resolution-time check burst.** It follows the trusted capture/credential/recheck shape of ordinary admission — snapshot model record, provider fingerprint, and configuration generation under the lock, resolve credentials outside it, then recheck those same values once — failing typed with no re-lookup or retry. No main-selection, selection-revision, or document-identity check exists, so unrelated main changes never fail graph.
- **One pin per component or invocation; per-extraction owned clients.** Daemon startup resolves one pin after the normal startup refresh (fresh and stale-last-good entries resolvable); live backfill resolves one pin per invocation. The extractor closure builds a new owned `LLMClient.for_graph_extraction` from that same pin per extraction and closes it in `finally`; a provider rejection (including a credential rotated or removed after the pin) flows through existing graph failure handling — no re-resolution, no fallback — and the next component start or backfill resolves anew. Shutdown flushes and joins the writer before the store and its dependencies close; independent explicit global `[embeddings]` are unchanged. No new R6 guard subcontract, ticket/budget channel, or architecture: those scenarios continue to reference frozen policy.
- **Prerequisites complete before any side effect.** Startup resolves the pin, credentials, the independent global embedding model/provider, and a `compute_request_budget` check before constructing `GraphMemoryStore`; any failure warns actionably and disables graph only. Standalone live backfill uses the saved catalog only — no discovery refresh, no managed-settings writes; a missing or corrupt managed catalog or an unresolvable selector fails actionably before any database construction. Dry run creates no pins, resolves no credentials, builds no clients, and performs no database, embedding, or inference work.
- **Budget rules are the preserved ones.** Unknown output limits are allowed with the fixed 1024 cap/reserve; unknown combined/input limits use the labelled assumed local budget (a known separate input ceiling is never reduced); a known output limit below 1024, malformed limits, or non-positive headroom fail. Each extraction rechecks with its actual estimated input before inference and accounting. Graph's fixed `temperature=0.1` and `max_tokens=1024` remain the named exception; Google resource names and supported generation methods are discovery-only and never imply capabilities.

## Consequences

- Good: Graph runs regardless of chat selection state, an explicit selector is honored verbatim without mutating main, and prerequisite failures are actionable and side-effect-free.
- Bad: A pin can outlive credential rotation or model removal; such staleness is handled by existing graph failure processing, not re-resolution or fallback, and is corrected at the next component start or backfill invocation.
- Neutral: ADR-0036 snapshot immutability, per-call override rejection, and ordinary-run admission are unchanged; no refactor of runtime construction or supervision.
