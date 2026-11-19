# Exploration brief: simplify model runtime

## Outcome and scope

Make model discovery an advisory convenience rather than an execution authority. Follow up on `provider-model-catalog` at `a57ab26`; do not rewrite its completed planning history. Publication and apply gates are recorded in `planning-route.md`.

## Confirmed direction

| Area | Intended rule |
| --- | --- |
| Chat providers | Connections fixed for process lifetime; changes require restart. |
| Catalog | Disposable reference cache, never a model whitelist or execution gate; no public supplement or per-field provenance. |
| Selection | Use an existing selector without inventory validation; otherwise choose the first candidate in stable canonical-ID order, not randomly. Explicit model requests do not require Main selection. |
| First-run discovery | Bootstrap automatically at startup only when both saved selection and cached candidates are absent. Otherwise use saved selection/cache; `/models` provides later discovery refreshes. |
| Selection persistence | Persist deliberate `/models` choices separately from the catalog cache. Automatic first-model choice stays in memory; if selection remains unsaved, choose from the cache again on next startup. |
| Scheduler | Missing model inherits chat selection; explicit model is attempted exactly; malformed selectors or provider errors fail only that job, never substitute another model. Keep current TOML scheduling, history, ownership, notifications, and safe persistence. |
| Embeddings / graph extraction | Independently configured provider, endpoint, key reference, and model; no inheritance from chat or each other and no graph-catalog dependency. |
| Graph vectors | Use the same explicitly configured embedding service as other indexing. Graph extraction has its own LLM configuration; no fourth graph-specific embedding configuration. |
| Context | Informational token statistics only; remove fit enforcement, reserves, model-switch guards, and automatic overflow repair. Mandatory API output parameters remain a separate simple request policy. |
| Compaction | Existing summarizer, one configured model-independent threshold; keep task and recent complete turns; publish a replacement only on successful summary, otherwise preserve history and report failure. |
| Automatic compaction failure | Warn, continue with unchanged history, and disable further automatic compaction attempts for that run. Provider context errors remain visible; no overflow-repair retry. |
| Manual compression failure | Existing `/compress` retains original history and reports failed or empty summarization; no truncated fallback replacement or new command. |
| Removal | Automated model-configuration migration and model/scheduler configuration compatibility, including obsolete scheduler representations. Keep unrelated storage-layout migration, historical log/execution-record readers, memory formats, permissions, and cancellation behavior unchanged. Removed configuration gets actionable errors, not converters, shims, or silent reinterpretation. |
| Safety / tests | Preserve matching credential routing, redaction, per-run targets, message integrity, cancellation/resource ownership, and atomic basic writes. Inventory tests as keep/rewrite/retire/add before deletion; preserve unrelated regressions hidden in removed suites. |

## Cross-module flow

Fixed chat configuration + explicit selector or selection snapshot -> direct resolver -> immutable run target -> existing client/provider adapter. Discovery -> advisory cache -> `/models` listing and selection; refresh never invalidates an existing selection or run. Explicit embeddings configuration -> embedding client; independent extraction configuration -> graph client/backfill. Context counts -> configured threshold -> one summarization attempt -> validated history replacement on success.

## Alternatives rejected

No authoritative catalog membership checks, runtime connection hot reload, random startup model, scheduled model repair/fallback, public metadata supplementation, capacity-derived budgeting, smaller-model ticket machinery, automated config conversion, or legacy compatibility bridges. Keep useful runtime and compaction building blocks instead of replacing the runtime. `/stop` and retry/checkpoint-policy redesign are separate work.

## Proposed defaults, not yet confirmed

Use one configured output-cap value only where required by an API.

## Open questions before freezing proposal

Numeric output-cap and compaction-threshold defaults remain design choices; no other proposal-level scope questions remain. The proposed defaults above are not evidence of user approval.
