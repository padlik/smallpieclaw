# 0040-separate-advisory-model-cache-from-deliberate-selection

## Status

accepted, supersedes ADR-0034 (partial: catalog authority, metadata provenance and supplementation, capability gates, and explicit-only first selection)

## Date

2026-11-01

## Supersedes

ADR-0034 (partial: replace authoritative catalog identity/membership, `C`/`T`/`O` metadata and provenance/supplements, metadata-based vision/tool gating, and no automatic first selection with advisory discovery, separate preference and deterministic unsaved bootstrap; retain single-model/no-fallback policy, rejection of obsolete agent fallback settings, and ordinary generation-field omission except the required output parameter addressed by ADR-0043).

## Context

ADR-0034 made discovery and persisted metadata the authority for selection and request eligibility. This couples bot availability to provider inventory quality and treats display data as evidence of callability. The pending predecessor's combined `models-config.toml` persists inventory alongside selection. Revisit only these clauses: an operator still needs a useful `/models` view and a durable deliberate choice, not a provider-backed whitelist. XDG path resolution and private-home protection from ADR-0019/0028 remain binding.

## Considered Options

- Retain the combined authoritative catalog and public metadata supplementation: preserves existing gates, but a stale or false capability field can disable an otherwise usable request and provider discovery failures can strand explicit targets.
- Keep discovery advisory but persist the automatically chosen first candidate: simpler startup record, but silently turns a temporary provider listing into deliberate operator preference.
- Separate disposable cache and deliberate preference (chosen): more explicit file and failure semantics, but selection can refer to an upstream the provider will later reject.

## Decision Outcome

Connections are fixed at process startup, not hot-reloaded or generation/fingerprint-rechecked. Discovery over those connections populates only an atomic best-effort cache at `$XDG_CACHE_HOME/<agent>/models-cache.json`: canonical candidate IDs, provider fresh/stale state and provider-reported display data. Failed refresh retains stale candidates; corrupt cache is reported and treated as absent. No public supplementation, per-field provenance, metadata limit algebra, or capability-based image/native-tool gate is kept. Genuine adapter/protocol validation and provider errors still apply. Refresh changes neither active selection nor admitted runs.

The separately resolved `$XDG_CONFIG_HOME/<agent>/model-selection.toml` stores only a deliberate selector, with no credentials, credential references or endpoint. On startup, a valid saved selector on a configured connection is active whether cached or not. When no preference file exists, choose the first cached canonical ID by ascending ordinal order in memory only; bootstrap discovery once only if there are also no candidates, then choose its first ID without saving. Empty discovery leaves commands and explicit requests usable; implicit requests fail with actionable `/models` guidance. Invalid, unreadable or corrupt saved preferences prevent automatic choice/bootstrap, are reported without exposing content, and are not overwritten by automatic writes. Authorized textual `/models select <connection>/<upstream>` or candidate-button selection may deliberately and atomically replace even a corrupt preference before publishing the new active choice; on write failure old bytes and in-memory choice remain. Textual selection works without cache or active Main; expired/unknown opaque button tokens are rejected, not candidates merely removed from the cache. The old combined `models-config.toml` receives only an existence check and one actionable notice naming its path and suggesting `/models`; its contents are never read or altered.

## Consequences

- Good: discovery outages and inaccurate metadata do not disable explicit work; selection has an operator-controlled durable meaning independent of the cache.
- Bad: an uncached or stale selected upstream can fail at the provider; display metadata cannot promise request success. Operators must deliberately correct their selection.
- Follow-up: implement separate XDG leaves and safe cache/preference writes and document manual selection and the old-file notice in the later authorized apply; no automatic migration is implied by this planning decision.
