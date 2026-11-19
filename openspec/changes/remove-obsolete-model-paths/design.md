## Context

The proposal (frozen after Round 2) removes superseded model-runtime paths. ADR-0042 already superseded the catalog-dependent graph chain, so graph cleanup is source-only. Two in-force ADR clauses still describe retained behavior and need later narrow supersession: ADR-0040 line 29 (retired `models-config.toml` existence notice) and ADR-0009 line 35 (`chat_with_fallback` / `chat_with_tools_fallback` compatibility). Retry ownership is already shared per ADR-0035, so the provider retry stubs are only compatibility names.

Remaining module boundaries after the change (C4-inspired, component level):

```
 Telegram (/models, model: cb) --requires--> ModelRegistry
                                               |  ModelCache (advisory) + ModelPreference (deliberate, _quote)
 main.py --resolve_graph_startup--> graph extraction clients (explicit config, ADR-0042)
 main.py --> LLMClient [public: chat, chat_with_tools] --> providers/*.chat(...)
               ^  _check_cancelled        |  single invocation, cancel check kept
 react_loop --checks callable chat_with_tools--> native-first, local pre-send unsupported handling
 config_schema --_reject_removed_fields--> agent.diagnose_empty_responses (remove-key guidance)
 xdg_paths: model_selection_file, models_cache_file   (no models_config_file)
```

## Goals / Non-Goals

**Goals:**
- One explicit path per concern; migrate every live caller to canonical APIs, then delete the old ones.
- Preserve live behavior except the accepted breaks in the proposal.

**Non-Goals:**
- Retry policy, compaction, checkpoint, prompt, WAL, or runner changes; new dependencies.
- Touching legacy files on disk, context monitor (ADR-0022/0043), or LLM empty-response classification/retries.

## Decisions

- **Graph:** delete `prepare_backfill_prerequisites` → `resolve_graph_prerequisites` → `ModelRegistry.resolve_graph_model`; `resolve_graph_startup` is the sole entry. Explicit graph configs, owned per-extraction clients, and Main independence are unchanged. Deliberate break, no wrappers.
- **Settings:** delete the `ModelSettings` combined TOML API and `default_model` / `preference=None` branches; `ModelPreference` is required. Tests move to real `ModelCache` + `ModelPreference`. Shared mapping validators and TOML `_quote` stay.
- **Legacy file:** remove `models_config_file`, its XDG exposure, and the notice code. The XDG requirement is MODIFIED with a full restatement. Disk is never read, migrated, or deleted.
- **Diagnostics:** delete the empty-response curl helper, `agent.diagnose_empty_responses` config, provider plumbing, docs, tests. The key is rejected by the existing `_reject_removed_fields` with fixed remove-key guidance; no global unknown-key change, no shim.
- **Aliases:** migrate ReAct calls to `chat` / `chat_with_tools`; redefine the native capability probe as `callable(getattr(ctx.llm, "chat_with_tools", None))`. The probe never invokes the method. Keep native-first and local pre-send unsupported handling; no post-send fallback. Remove alias methods from clients, the `LLMProvider` Protocol, and test doubles; MODIFY the existing protocol-extension requirement (not delete).
- **Google descriptors:** remove unused storage only. Resource/method accept/reject validation and the raw credential scan stay.
- **Telegram:** registry required at startup; delete the legacy mutation/list fallback. Add a raw legacy `model:<name>` callback scenario under the existing models requirement, distinct from the opaque-token case: authenticated refusal, state unchanged, reopen `/models`.
- **Definition hash:** remove the unused ReAct hash only; token-estimator revision keys and provenance stay.
- **Retry stubs:** remove `make_on_retry`, `run_with_retry`, `_with_retry` (providers/_utils.py) and their tests in `tests/test_providers.py`; they have no production callers. Live cancellation stays at `LLMClient._check_cancelled` and the shared attempt owner's checks; add no new check. Retry count/delay/classification, revisions, and the send gate are untouched. Clean stale comments at `sub_agent_registry.py:63` and `providers/_errors.py:7,25`.
- **ADR stage (later, no files now):** new records narrowly, partially supersede ADR-0040 (notice clause only) and ADR-0009 (alias compatibility clause only); accepted ADR files are never edited. ADR-0040 selection rules, ADR-0045 selection, and ADR-0009 native-first remain, bounded by ADR-0035. ADR-0042, ADR-0022, ADR-0043 are not superseded. Supersession graph: new-A → 0040 (partial), new-B → 0009 (partial).

## Risks / Trade-offs

- Stub deletion could be mistaken for removing live cancellation → retain coverage of the existing `_check_cancelled` boundary; add no new check.
- Probe redefinition could mark native unsupported → test native-first still used with canonical method.
- Broken external callers of removed APIs/key → intended; documented in the Migration Plan.
- Test migration to real cache/preference is larger than mocks → accepted for fidelity.

## Migration Plan

Explicit API break; no shims. Operators remove `agent.diagnose_empty_responses` manually (startup rejects it with guidance). No disk migration; any legacy `models-config.toml` is ignored and left in place. Rollback is a code revert only, since no data format changes.

## Open Questions

None.
