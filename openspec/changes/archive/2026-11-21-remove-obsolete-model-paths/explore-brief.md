# Explore Brief: remove-obsolete-model-paths

Source: `docs/model-runtime-council-findings.md` ("Remove obsolete production paths"), with user decisions overriding its conservative recommendations. Live behavior is preserved except the accepted breaks; removed public APIs/config are all intentionally **BREAKING** (graph helpers included). Source cleanup implements ADR-0042; explicit graph extraction config is unchanged.

## Removal / replacement map

| Removed | Replacement / kept |
|---|---|
| `prepare_backfill_prerequisites` → `resolve_graph_prerequisites` → `ModelRegistry.resolve_graph_model` | `resolve_graph_startup`; explicit graph config unchanged |
| `ModelSettings` combined TOML API, `default_model`, `preference=None` branches | `ModelCache` + `ModelPreference` required |
| `models-config.toml` code, `models_config_file` path, old-file notice | Nothing; legacy files left unread/untouched |
| Empty-response curl diagnostics helper + `agent.diagnose_empty_responses` config, plumbing, docs, tests | Key rejected via existing `_reject_removed_fields` ("remove key; diagnostics removed"); no shim, no global unknown-key change. Context monitor and LLM empty-response classification/retries kept |
| `chat_with_fallback`, `chat_with_tools_fallback` (clients, Protocol, ReAct, probe, test doubles) | `chat`, `chat_with_tools` |
| Unused Google descriptor storage | Implementation-only; structural validation and credential scans kept |
| Registry-absent Telegram model mutation/list fallback | Registry-only path; auth-scoped stale `model:` callback says reopen `/models` |
| Unused ReAct definition hash | Implementation-only; token-estimator revision keys/provenance kept |
| Provider single-call retry stubs `make_on_retry`, `run_with_retry`, `_with_retry` | Live cancellation checks, retry count/delay/classification, revisions, and send gate kept; `llm-error-recovery` spec unchanged |
| ReAct native capability probe | Redefined on canonical `chat_with_tools`; must not silently disable native |

Kept: shared mapping validators; TOML `_quote` (used by preference). No new dependencies.

## Spec delta mapping

- `provider-model-catalog`: REMOVE old-file existence notice requirement; MODIFY obsolete agent keys to include `agent.diagnose_empty_responses`.
- `native-tool-calling`: MODIFY `LLMProvider` protocol extension requirement to canonical methods.
- `xdg-path-resolution`: remove only `models_config_file`; other paths untouched.
- `telegram-command-surface`: only add explicit stale legacy `model:<name>` callback refusal (distinct from opaque-token case); startup requires registry, no registry-absent fallback.
- Graph helper removal is a deliberate accepted API removal (BREAKING even if internal; no wrappers by user choice).

## ADR impact (later ADR stage)

New records only (never amend accepted ADRs): partially supersede ADR-0040 only its notice clause (line 29) and ADR-0009 only its alias API compatibility clause (line 35). Rest of ADR-0040, ADR-0045 selection, and ADR-0009 native-first stay, bounded by ADR-0035. ADR-0042, ADR-0022, ADR-0043 not superseded.

## Flow

Startup builds `ModelCache` and `ModelPreference` and calls `resolve_graph_startup`. Clients expose only `chat`/`chat_with_tools`; ReAct and the native probe call them directly. Telegram `/models` uses the registry only; stale callbacks get a reopen message.

## Rejected alternatives

- Deprecated wrappers or no-op shims for removed APIs/keys.
- Migrating or deleting legacy `models-config.toml` files.
- Relaxing Google validation.
- Broad error-recovery redesign (retry policy, compaction, checkpoint, prompt, WAL, runner fixes out of scope).

## Open questions

None; all product decisions are settled.
