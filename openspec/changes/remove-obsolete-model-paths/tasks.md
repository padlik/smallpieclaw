## 1. Graph helper chain and combined settings

- [x] 1.1 Red→green: move graph helper-chain tests to the live explicit startup/backfill path (`resolve_graph_startup`, explicit graph config, Main independence unchanged); delete `prepare_backfill_prerequisites`, `resolve_graph_prerequisites`, `ModelRegistry.resolve_graph_model`; run graph tests.
- [x] 1.2 Add or reuse a shared test fixture for real `ModelCache` + `ModelPreference`; migrate model registry/settings tests while the old API still exists, rewriting only retired-API assertions; run that group green.
- [x] 1.3 Migrate controller, runtime, admission, ReAct/native and provider test callers to the cache/preference fixture; keep the old API available and run this group green.
- [x] 1.4 Migrate graph, backfill, startup, Telegram and remaining test callers to the fixture; retain live behavior assertions and run this group green.
- [x] 1.5 Delete the `ModelSettings` combined TOML API and `default_model` / `preference=None` branches after callers are migrated; keep shared validators, TOML `_quote` and atomic writes; run model tests.

## 2. Chat aliases and retry stubs

- [x] 2.1 Red→green: migrate ReAct calls and its test doubles to canonical methods while aliases still exist; redefine the native probe to resolve the `chat_with_tools` callable without invoking the provider; assert native-first, local pre-send unsupported handling and no post-send fallback.
- [x] 2.2 Red→green: remove `chat_with_fallback` / `chat_with_tools_fallback` from clients, the `LLMProvider` Protocol and remaining test doubles; assert aliases are absent and `chat`, `chat_with_tools`, `embed`, `embed_batch` remain.
- [x] 2.3 Remove `make_on_retry`, `run_with_retry`, `_with_retry` (`providers/_utils.py`) and their tests in `tests/test_providers.py` (no production callers); existing `_check_cancelled` and attempt-owner tests must still pass, with no new check; retain retry count/delay/classification, revisions and the send gate.

## 3. Config, paths, providers

- [x] 3.1 Red→green: remove `agent.diagnose_empty_responses` config, the empty-response curl helper, provider plumbing, docs, and tests; reject the key via `_reject_removed_fields` with fixed remove-key guidance; test other unknown keys are unchanged and the context monitor and LLM empty-response classification/retries still pass.
- [x] 3.2 Red→green: remove `models_config_file` from `XDGPaths` and the old-file existence check/notice; test that an existing `models-config.toml` is never read, written or deleted, emits no notice, and other XDG paths are unchanged.
- [x] 3.3 Remove unused Google descriptor storage; keep and run resource/method accept/reject validation and the raw credential scan tests unchanged.

## 4. Telegram and ReAct cleanup

- [x] 4.1 Red→green: require the registry at Telegram startup and remove the registry-absent model mutation/list fallback; add tests that raw legacy `model:<name>` callbacks are refused (authenticated, with reopen `/models`; unauthenticated unchanged) with selection and preference bytes untouched, and opaque-token rules unchanged.
- [x] 4.2 Remove the unused ReAct definition hash only (keep token-estimator revision keys and provenance); fix stale comments in `providers/_errors.py` (lines 7, 25) and `sub_agent_registry.py` (line 63).
- [x] 4.3 Update `vulture_whitelist.py`, imports, and fixtures for removed symbols without deleting unrelated behavioral tests or adding dependencies.

## 5. Documentation and verification

- [x] 5.1 Add an upgrade note to README/docs and config examples: the legacy `models-config.toml` is ignored and left untouched, select a model via `/models`, remove `agent.diagnose_empty_responses`, and removed APIs have no wrappers; drop stale `models-config.toml` and diagnostics mentions.
- [x] 5.2 After each group run focused regressions plus `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80`; fix root causes without weakening rules.
- [x] 5.3 Once at the end run `make check` (complete suite, zero new skips) and `openspec validate remove-obsolete-model-paths --type change --strict`.
- [x] 5.4 Hand off for verification; wait for explicit user approval before merge and archive (no automatic merge or archive).
