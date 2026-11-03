# 0042-configure-auxiliary-inference-independently-of-chat

## Status

accepted, supersedes ADR-0034, ADR-0039 (partial; see Supersedes)

## Date

2026-11-01

## Supersedes

ADR-0034 (partial: remove catalog identity/limits as graph prerequisites; retain chat connection credential isolation). ADR-0039 (partial: replace graph catalog selector/pin, post-credential catalog/fingerprint rechecks, saved-catalog prerequisite and startup/per-extraction budget checks with explicit extraction settings; keep independence from Main, component/invocation-stable configuration, per-extraction owned clients, graph-only failure containment, dry-run non-effects, and bounded drain before closure). Matching-chat-provider embedding inheritance comes from the pending predecessor specifications, not ADR-0034; this decision reverses that pending contract. ADR-0036's graph-specific ordinary-admission scope was already partially superseded by ADR-0039; do not revive it.

## Context

ADR-0039 correctly separated graph extraction from Main selection and gave each extraction an owned client, but still makes its model and budget depend on a catalog. The predecessor also lets embeddings inherit credentials or transport from a matching chat provider. This implicit endpoint/credential inheritance obscures which service owns the embedding route, while catalog dependence ties optional graph availability to unrelated chat discovery. A durable service boundary must keep the chat, embedding and extraction routes explicit and separate.

## Considered Options

- Keep graph as a third catalog entry and matching-provider embedding inheritance: less explicit configuration but couples graph startup and endpoint credentials to inventory/chat state.
- Add a fourth graph-specific embedding route: more isolated, but duplicates the same vector service with no demonstrated need.
- Configure embeddings and extraction independently (chosen): requires operators to specify each endpoint/key/model but makes ownership and failure isolation auditable.

## Decision Outcome

`[embeddings]` has explicit `provider`, `base_url`, `api_key`, `model`; enabled `[graph_memory]` extraction has independent `extraction_provider`, `extraction_base_url`, `extraction_api_key`, `extraction_model`. Neither inherits any field from a chat route (even the same adapter), Main, cache or each other. Indexing and graph vectors share the one configured embedding service, without a fourth vector configuration. Startup and live backfill validate these required settings, credential references, adapters and endpoints before database construction; neither needs catalog discovery or Main. Invalid optional embedding configuration degrades indexing and disables dependent graph, invalid extraction configuration disables graph only, with actionable sanitized warnings; valid chat remains available. Structural chat configuration errors still fail startup. Live backfill performs no preference/cache I/O or discovery; dry run resolves no credential and creates no client/database/inference.

Graph uses settled extraction configuration per component or backfill invocation and creates a separately owned LLM client for each extraction, closed in `finally` on success, failure or cancellation; no component-long chat client or live re-resolution. Provider failure follows graph handling without another model. Preserve fixed graph wire `max_tokens=1024`, `temperature=0.1` subject to adapter compatibility, without a budget gate. Preserve writer flush/join and bounded shutdown before dependencies close, as well as private-home and ownership boundaries of ADR-0019/0028/0033.

## Consequences

- Good: an unselected Main or failed discovery does not disable correctly configured indexing/graph; credential-to-endpoint separation is explicit.
- Bad: deployments must supply four fields per enabled auxiliary service; missing credentials intentionally reduce optional functionality and require operator repair.
- Follow-up: validate before DB side effects and exercise graph closure/drain and independent service failure in the later authorized apply.
