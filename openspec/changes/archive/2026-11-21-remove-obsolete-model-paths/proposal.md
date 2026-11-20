## Why

Superseded paths and redundant naming persist after the model-runtime simplification: a legacy graph-model resolution chain, the combined `ModelSettings` TOML API, `models-config.toml` references, the empty-response diagnostics helper/config, `*_fallback`-named chat aliases (still actively called by the ReAct loop), unused Google descriptor storage, a registry-absent Telegram model fallback, an unused ReAct definition hash, and provider single-call retry stubs. Removing them leaves one explicit path per concern. Live behavior is preserved except the explicitly accepted breaks below. Source cleanup implements ADR-0042; explicit graph extraction config is unchanged.

## What Changes

- **BREAKING** (deliberate accepted API removal even if internal; no wrappers by user choice): Remove `prepare_backfill_prerequisites` → `resolve_graph_prerequisites` → `ModelRegistry.resolve_graph_model`; graph startup uses only `resolve_graph_startup` (explicit graph config contracts unchanged).
- **BREAKING**: Remove the `ModelSettings` combined TOML API and its `default_model` / `preference=None` branches; `ModelCache` and `ModelPreference` are required.
- **BREAKING**: Remove all `models-config.toml` code, the `models_config_file` path, and the old-file notice. Legacy files on disk are never read, migrated, or deleted.
- **BREAKING**: Remove the empty-response diagnostics curl helper and its config (`agent.diagnose_empty_responses`), provider plumbing, docs, and tests. The key is rejected via the existing `_reject_removed_fields` with actionable "remove key; diagnostics removed" guidance; no no-op shim and no global unknown-key change. Context monitor and LLM empty-response classification/retries are preserved.
- **BREAKING**: Remove `chat_with_fallback` and `chat_with_tools_fallback` from clients, the `LLMProvider` Protocol, the ReAct loop, the native capability probe (redefined on canonical `chat_with_tools`; must not silently disable native), and test doubles; `chat` and `chat_with_tools` remain.
- Remove unused Google descriptor storage (implementation-only); keep structural validation and credential scans.
- Remove the registry-absent Telegram model mutation/list fallback; keep the auth-scoped stale `model:` callback reply telling the user to reopen `/models`.
- Remove the unused ReAct definition hash (implementation-only; token-estimator revision keys and provenance kept).
- Remove provider single-call retry stubs (`make_on_retry`, `run_with_retry`, `_with_retry`), preserving live cancellation checks, retry count/delay/classification, revisions, and the send gate. `llm-error-recovery` spec is unchanged.
- Keep shared mapping validators and the TOML `_quote` used by preference. No new dependencies. No retry-policy, compaction, checkpoint, prompt, WAL, or runner changes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `provider-model-catalog`: REMOVE the old-file (`models-config.toml`) existence notice requirement; MODIFY obsolete agent keys to reject `agent.diagnose_empty_responses` through `_reject_removed_fields`. Google descriptor storage and graph helper retirement are implementation-only; validation and explicit graph config contracts are unchanged.
- `native-tool-calling`: MODIFY the existing `LLMProvider` protocol extension requirement to the canonical `chat` / `chat_with_tools` methods (requirement kept, not deleted). Definition-hash removal is implementation-only.
- `xdg-path-resolution`: REMOVE only the `models_config_file` exposure (requirement text and scenarios); all other paths unchanged.
- `telegram-command-surface`: ONLY add an explicit refusal of stale legacy `model:<name>` callbacks (distinct from the opaque-token case; auth-scoped, says reopen `/models`). Startup requires the registry; no registry-absent mutation/list fallback.

## Impact

- Code: graph startup/backfill wiring, model registry/settings/cache modules, `llm_client.py`, `interfaces.py`, `react_loop.py`, native capability probe, XDG path module, Telegram model commands/callbacks, config schema.
- Tests and doubles referencing removed APIs are deleted or updated; `vulture_whitelist.py` entries pruned; docs/config examples drop `models-config.toml` and diagnostics mentions.
- Removed public APIs and config break callers intentionally; no migration shims.
- ADRs: a later ADR stage adds new records that narrowly, partially supersede ADR-0040 only for the notice clause (line 29) and ADR-0009 only for the alias API compatibility clause (line 35). Accepted ADR files are never amended. The rest of ADR-0040, ADR-0045 selection, and ADR-0009 native-first stay as bounded by ADR-0035. ADR-0042, ADR-0022, and ADR-0043 are not superseded (explicit graph settings and context monitor unchanged).
