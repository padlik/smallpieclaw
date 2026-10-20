# 0038-resolve-scheduled-selectors-before-root-owned-model-pinning

## Status

accepted, supersedes ADR-0034 and ADR-0036 (partial; see Supersedes)

## Date

2026-10-20

## Supersedes

ADR-0034 (partial: only the unconditional strict resolution/no-substitution requirement as applied to a saved scheduled selector that is malformed or unknown before any model is chosen, and the obsolete-setting actionable rejection as applied to legacy per-job `fallback_models`, which becomes warn-and-ignore at execution) and ADR-0036 (partial: only the uniform explicit-selector resolution as applied to the trusted scheduled construction path). All other clauses of both remain live, including ADR-0034 catalog metadata ownership and strict ordinary selection, and ADR-0036 immutable run pins, rejection of generation overrides, and the AgentRuntime construction boundary. ADR-0033 ownership, ADR-0007 construction, ADR-0009 native-first, and ADR-0035 retry state are unchanged.

## Context

ADR-0036 resolves every model choice through ordinary admission with strict explicit selectors, and ADR-0034/0020 prohibit substitution once a model is chosen. But a scheduled job's saved selector is raw persisted operator data that may be absent, `None`, blank, a non-string, or absent from a catalog that churns; strictly failing every saved selector would make unattended jobs break on routine catalog changes, and a fallback decision can only be made before a run model exists. The ADR-0020 chain removed post-choice backup-model retry because it was rarely used and incoherent across per-run context windows; that rationale never governed resolving an invalid selector where no model has been chosen at all.

## Considered Options

- Strictly fail every malformed, unknown, or stale scheduled selector at load or admission: rejected (user-rejected) — routine catalog churn would fail previously-working unattended jobs.
- A second admission channel (frontend prepare tokens / double admission) so the scheduler classifies selectors itself: rejected — needless parallel channel; the root factory is already the single admission path.
- Post-choice failover to the main model when the pinned model later fails: rejected — prohibited cross-model fallback; ADR-0020's and ADR-0034's line stands after the pin.

## Decision Outcome

- **Ordinary admission is unchanged.** Main-turn, on-demand, plan, and diagnostic admission keeps strict explicit resolution and the active-main gate; a valid explicit canonical selector never bypasses a missing main selection, and explicit selectors never inherit.
- **One root-owned scheduled admission.** A proposed `ModelRegistry.admit_scheduled(raw_selector) -> RunModel` (not an existing API) is called only from the existing `main.py` factory branching on the `SCHEDULED_AGENT` profile: it requires a valid active main model first, then classifies the raw selector — absent/`None` inherits main silently; blank, whitespace-only, or non-string values are malformed and warn-then-inherit main; an exact non-blank string absent from the catalog (including case/padding variants) is unknown and warn-then-inherits main; an exact catalog entry, fresh or stale, is pinned verbatim (staleness is not unknown). Add/load keeps any TOML-representable raw value unchanged; modify with omitted or `None` is a no-change, and no clear operation exists; the main selection is never changed by classification.
- **The main gate applies to scheduled runs too.** With no valid active main model, any scheduled job fails normally before any model call, even one with a valid explicit canonical selector.
- **One choice, one credential resolution, one recheck.** `admit_scheduled` chooses the effective model once, resolves credentials once, and then rechecks provider fingerprint, configuration generation, model record, main selection identity/revision, and catalog document identity; any mismatch fails that attempt as a typed normal job failure with no re-lookup, reclassification, or retry. After the effective model is chosen there is no substitution: credential, capability, context, inference, or provider-outage failures fail the job without another model.
- **Trusted source exemption is minimal.** Only the strict model-shape guard is exempted for scheduler-built spawn options carrying the scheduled source; task, depth, capacity, obsolete-argument, context-key, refusal, and other guards are unchanged; no new public argument, schema key, or supervision channel exists, the public `PlanStep` shape is unchanged, and runtime construction never re-admits or resolves credentials again.
- **Bounded evidence.** Every pre-admission reference to a selector (list/show, the warning, execution records) uses one bounded, redacted representation — `inherit`, a classification label that never reveals non-string contents, or a length-bounded, secret-redacted string — while the raw value stays unchanged in the document; post-admission records store the pinned canonical ID.
- **Lifecycle unchanged.** Run ID/active tracking and `last_run` persist before spawn, the terminal record follows, alerts fire regardless of `notify=False`, and tracking is released; bounded redacted evidence throughout.
- **Legacy per-job fallback.** A saved per-job `fallback_models` stays unrewritten in the document and, when its job executes, is ignored with a runtime warning and never enables cross-model retry; explicit agent-level fallback settings in human configuration remain removed/rejected under ADR-0034.

## Consequences

- Good: Catalog churn no longer breaks unattended jobs, raw selectors are honored with one root admission and no new channels, and legacy per-job settings are reported honestly instead of silently dropped.
- Bad: An intentional race (for example, a refresh inside the credential-resolution window) fails that attempt; operators may need to clean stale selectors and warnings tell them how.
- Neutral: No new retry, fallback, or admission architecture; existing guards, ownership, and lifecycle contracts continue as-is.
