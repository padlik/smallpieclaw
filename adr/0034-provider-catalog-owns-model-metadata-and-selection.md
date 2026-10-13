# Provider catalog owns model metadata and selection

## Status

accepted, supersedes ADR-0020 (partial: configured per-model context metadata, rejection of provider metadata discovery, all-model vision routing, and ignored fallback-setting behavior only)

## Date

2026-10-13

## Supersedes

ADR-0020 (partial: replace configured per-model context metadata and its rejection of metadata discovery with provider-discovered and exact-match public supplemental metadata; replace all-model vision scans with explicit capability validation on the pinned model; replace warn-and-ignore fallback settings with actionable rejection. ADR-0020's single-model clients and no cross-model fallback remain in force.)

## Context

ADR-0020 established single-model clients and removed cross-model fallback, but paired those commitments with manually configured per-model context limits, an all-model vision scan, and warn-and-ignore handling for legacy fallback settings. The provider-model catalog introduces app-owned persisted provider inventory and metadata, with provider-sourced fields and exact-match public supplementation. The design also requires active selection and per-run model snapshots to be explicit; it does not revise the XDG path or private-home boundaries in ADR-0019 and ADR-0028.

## Decision

The app-owned provider catalog is the source of discovered model identity, selection, and metadata. Provider metadata takes precedence; exact adapter/upstream matches from public supplemental data may fill unknown fields only, with per-field provenance. The remembered main-model ID is persisted separately from active selection. No first-entry or other automatic default is inferred; absent or unavailable selection leaves model work inactive until explicit selection. Context limits remain distinct as combined `C`, input `T`, output `O`, or unknown. The catalog passes the exact pinned metadata and source labels to the separate context-budgeting policy; it does not invent provider limits. Only when neither a combined window `C` nor a separate input ceiling `T` is known is `agent.ctx_max_tokens` used, as an explicitly labelled assumed local combined budget; a known `T` is never reduced by it or by `O`. Ordinary chat, native-tool, and JSON requests omit `temperature` and `top_p` and omit optional output caps; graph extraction retains its fixed `temperature=0.1` and `max_tokens=1024` exception.

Vision requests use only the explicitly admitted, pinned model when its catalog metadata verifies vision support. False or unknown capability fails locally; the system does not scan or switch models. Obsolete fallback settings are rejected actionably. No default is inferred or substituted, and a catalog entry does not promise account callability. XDG resolution and private-home access boundaries remain unchanged.

## Consequences

- Model metadata and its provenance are shared across selection, presentation, admission, and the budgeting owner without turning the catalog into a tuning or execution container.
- Operators must explicitly select a discovered model and explicitly select a verified vision-capable model for image work; missing or unknown state fails safely instead of silently routing.
- Provider limits remain distinct from local assumptions, and required unknown output limits remain the responsibility of the budgeting policy to reject.
- Existing per-model context configuration and warn-and-ignore fallback behavior are no longer supported; migration reports discarded tuning without promoting it.
- ADR-0020's no-cross-model-fallback and single-model-per-run commitments, and ADR-0019/0028's path and access boundaries, remain in force.
