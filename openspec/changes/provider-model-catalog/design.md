## Context

The frozen proposal replaces operator-maintained `[[models]]` with provider connections and an app-owned discovered catalog. The catalog must be one source for `/models`, prompt/tool model descriptions, main-model selection, and model admission; it must not become an LLM configuration container or a new executor. `main.py` remains the composition root and `AgentRuntime` remains the sub-agent construction boundary. `SubAgentSupervisor` retains lifecycle/scheduling ownership. Embeddings remain independently hard-configured.

The accepted-ADR inventory is `adr/0001-use-file-backed-provider-secrets.md`, `0002-vault-secret-manager.md`, `0003-use-toml-vault-format.md`, `0004-structured-primary-agent-logging.md`, `0005-use-sub-agent-supervisor-boundary.md`, `0006-use-source-categories-for-agent-visibility.md`, `0007-use-agent-runtime-for-construction.md`, `0008-use-facade-handler-package-for-builtin-tools.md`, `0009-native-tool-calling.md`, `0010-zone-based-file-access-control.md`, `0011-per-prompt-approval-scope.md`, `0012-use-nsjail-for-shell-isolation.md`, `0013-use-ulid-for-globally-unique-prompt-ids.md`, `0014-use-dual-write-archive-for-prompt-registry.md`, `0015-nsjail-state-outside-sandbox-write-scope.md`, `0016-remove-project-dir-from-nsjail-sandbox.md`, `0017-mount-session-logs-readonly-in-nsjail.md`, `0018-mount-default-trusted-zones-into-nsjail.md`, `0019-xdg-base-directory-layout-for-agent-storage.md`, `0020-remove-fallback-models-add-per-model-context-window.md`, `0021-use-disk-persisted-checkpoints-for-llm-error-recovery.md`, `0022-use-push-model-context-monitor.md`, `0023-record-exactly-once-lifecycle-logging.md`, `0024-unify-confirmation-signaling-in-coordinator.md`, `0025-use-sqlite-wal-store-for-structured-logs.md`, `0026-unify-file-access-into-path-policy.md`, `0027-single-grant-ledger-for-standing-consent.md`, `0028-agent-dot-home-and-closed-xdg-state.md`, `0029-preserve-oauth-refresh-tokens-across-silent-refreshes.md`, `0030-noninteractive-long-lived-mcp-oauth-sessions.md`, `0031-restore-oauth-provider-state-from-persisted-tokens.md`, `0032-use-list-detail-split-for-log-introspection.md`, and `0033-separate-turn-owned-and-scheduled-executions.md` (all IDs 0001–0033, no gaps).

Supersession graph (newer ADR → superseded scope): `0002→0001`, `0003→0002`; `0012→0011` (main-agent shell invariant only), `0016→0012` (project-directory mount only), `0018→0016` (Decision 3 wording only), `0026→0010` and `0018`; `0024→0011`, `0027→0011` and `0024`; `0028→0017`; `0020→0007` (`RuntimeOptions.fallback_models` only); `0025→0004` (structured-store/query mechanism only). All other in-repository ADRs have no `Supersedes` edge; `0017` also supersedes an archived change decision. Thus `0001`, `0002`, `0010`, `0011`, `0017`, `0018`, and `0024` are fully superseded. The unaffected portions of `0004`, `0007`, `0012`, and `0016` remain in force, as do the other non-superseded accepted ADRs.

Relevant live constraints: ADR-0007 keeps runtime construction separate from supervision and requires compatible runtime products; ADR-0009 accepts native-first calling with a same-model JSON path; ADR-0020 removes cross-model fallback but currently commits to configurable per-model context metadata and vision scanning; ADR-0019 makes `xdg.py` the single resolver for XDG paths; ADR-0028 keeps XDG private homes unavailable to file tools and the jail; ADR-0021's checkpoint is a recovery unit that does not re-execute prior tools; ADR-0033 preserves job-owned scheduled execution and existing supervisor/runtime boundaries. The new retry/JSON behavior narrows ADR-0009; catalog-sourced context metadata and explicit vision selection narrow ADR-0020. Do not edit those ADRs: the later ADR step must propose narrowly scoped superseding decisions, preserving ADR-0009's native-first path and ADR-0020's no-model-fallback and single-model-per-run constraints.

## Goals / Non-Goals

**Goals:** One managed catalog and selection owner; safe refresh and atomic persistence; immutable admission-time model/connection/metadata snapshots; faithful provider limits and explicit unknown-state handling; separate chat, embeddings, and graph-extraction policy; recoverable configuration migration.

**Non-Goals:** Reworking `main.py` as composition root, replacing `AgentRuntime`/`SubAgentSupervisor`, changing scheduled-run ownership or permissions, inventing aliases or model tuning, guaranteeing inference access from catalog presence, rewriting conversations/history/jobs, building a generic pagination framework, or making embeddings follow chat selection.

## Decisions

### Component boundaries and data flow

```text
Operator -- /models --> Telegram commands/callbacks --> ModelRegistry
                                                      +--> ModelDiscovery --> provider catalog APIs
                                                      +--> ModelSettings --atomic--> models-config.toml
Prompt builder / alternate prompt loader / tool descriptions <-- registry view
Main turn / AgentRuntime / scheduler admission --> registry.admit() --> frozen RunModel
                                                                     +--> main controller or AgentRuntime
                                                                          +--> run-local LLMClient --> one provider
Explicit [embeddings] --> EmbeddingClient --> embedding provider --> index/memory
Explicit graph extraction --> pinned extractor --> graph writer / backfill
```

`model_registry.py` owns immutable `ModelRecord` values and the catalog state machine: view, select, admit, refresh/merge. It owns one state/write lock. `model_discovery.py` fetches and normalizes complete provider inventories and optional exact-match supplement metadata, using existing `httpx`; it does not own cached state. `model_settings.py` narrowly loads, validates, and atomically replaces the generated TOML; it does not resolve credentials or apply selection policy. `main.py` constructs these once and wires them. `AgentRuntime` receives admission-resolved model data while preserving its existing profile/product boundary. `llm_client.py` receives one immutable run snapshot and owns neither the inventory nor a mutable active-model index; `interfaces.py` carries the run-local provider contract without inventory ownership. Its `chat_with_fallback`-named compatibility wrappers may not introduce model routing or a separate retry budget. The model-tuning fields `max_tokens`, `temperature`, and `top_p` are removed from `builtin_tools/schemas.py` and `builtin_tools/descriptors.py` spawn schemas, builtin argument parsing/validation, `main.py` sub-agent factory signatures, and `RuntimeOptions`/`AgentRuntime` forwarding. If obsolete fields are supplied, return an actionable tool-validation error before model admission or any LLM call; never silently ignore them or create hidden tuning. Preserve other runtime options (model, context, label, iteration limit, trace, profile, supervision, callbacks).

`RunModel` freezes the selected `ModelRecord`, resolved immutable provider connection, selection/admission revision, and model metadata. Secret-bearing values are excluded from persistence and `repr`; frozen objects contain no mutable dict/list aliases. The provider credential is resolved for the run, passed only to the matching provider client, and never included in prompts, UI, errors, or managed settings. A run pins endpoint, model, and metadata; refresh or a later main selection cannot mutate it.

### Identity, inventory, and managed settings

The canonical model ID is `<provider-connection-id>/<exact-upstream-id>`; split only at the first slash. Connection ID chooses the adapter/account/endpoint; upstream ID is the exact ID sent to inference. Display names are not selectors. Do not infer aliases, add/remove suffixes, or use resource names in place of inference IDs.

Provider discovery uses complete pagination where supported and only reliable provider metadata:

| Adapter | Inventory and metadata contract |
|---|---|
| OpenAI | Inventory IDs only; no reliable context/output/capability inference from that inventory. |
| Anthropic | Follow every cursor page; retain nullable `max_input_tokens`, `max_tokens`, and `capabilities.image_input.supported`. |
| Google | Follow every page; retain `inputTokenLimit`, `outputTokenLimit`, `supportedGenerationMethods`, and inference `baseModelId` separately from resource name. |
| OpenRouter | Retain catalog context and modality data; output limits remain unknown when absent. |
| Ollama | Cloud tags/IDs only; metadata may be sparse. Default the cloud connection origin to `https://ollama.com`, require authentication, and join discovery at `/api/tags` without duplicating `/api`. The SDK/native inference path uses the same origin with its adapter-correct path. Reject explicit localhost base URLs. Do not use suffix guessing or `/show` assumptions. |

Supplement from a fetched public `models.dev` catalog only; send no account, endpoint, credential, or per-model/request data. Match adapter and upstream ID locally and exactly. Provider data wins field-by-field; supplement only unknown fields and label every field's source. A supplement cannot create inventory entries and does not use fuzzy aliases or inferred dates; supplement failure does not invalidate provider inventory. Capability booleans are tri-state: `true`, `false`, absent/unknown. A provider's explicit `false` is never overwritten. Discovery and inference use the same configured connection/endpoint/auth policy; existing hard-coded Anthropic/Google inference endpoints must be moved behind that connection boundary.

Version 1 managed file shape (no raw API responses, credentials, secret references, base URLs, or error bodies):

```toml
schema_version = 1
revision = 0
default_model = "openai/gpt-example" # omitted until selected

[providers.openai]
adapter = "openai"
last_success_at = "<timestamp>"
status = "fresh" # fresh | stale
# optional sanitized_last_error_code only

[[models]]
id = "openai/gpt-example"
provider_id = "openai"
upstream_id = "gpt-example"
display_name = "Example"
context_tokens = 100000 # optional combined prompt-input + completion window
input_tokens = 96000   # optional separate input ceiling
output_tokens = 4000   # optional output ceiling
vision = true          # each capability optional; absence means unknown
tool_calling = true
reasoning = false

[models.sources]
context_tokens = "models.dev" # each field: provider | models.dev

[[supplemental]]
adapter = "openai"
upstream_id = "gpt-example"
fetched_at = "<timestamp>"
# only the same normalized metadata fields; never inventory or credentials
```

All token limits must be positive integers (booleans are not integers for validation). `models.sources` records the source per metadata field. Optional catalog-level sanitized error codes cannot expose endpoint, account, body, credential, or `env:`/`sec:` reference. A missing or corrupt managed file leaves commands available with no active model; preserve corrupt input before any repair/overwrite.

`XDGPaths` exposes a pure `models_config_file` path under the resolved agent `config_home`, including custom `XDG_CONFIG_HOME`; callers do not reconstruct paths locally. The managed file remains in the XDG private home, inaccessible to file tools and the jail under ADR-0028. Catalog presentation is through authorized application surfaces only.

### Refresh, persistence, and selection state machine

Persisted remembered ID and runtime active selection are distinct. Startup restores active selection only if the remembered ID is in the validated current catalog; otherwise preserve the ID but stay inactive. No model admission—including an explicit job/sub-agent override—is allowed until `/models` makes a valid main selection active. Every selection must resolve to a discovered entry and persist successfully before it becomes active. On write failure, keep the prior active selection and report failure.

Refresh fetches outside the registry lock against a captured connection fingerprint and refresh generation. Discard results if the connection changed/deleted or a newer refresh generation completed. Validate all pages before merge: a complete successful response replaces that provider's inventory, including a valid empty inventory; incomplete, malformed, or failed responses preserve last-good entries and mark them stale. A deleted connection purges its entries despite cache. Supplement lookup failure leaves good provider inventory intact. Merge with the latest main selection under the shared lock; atomically persist the candidate before publishing it as durable. A refresh persistence failure is reported as non-durable; runtime admission must still reject a removed/changed connection safely.

If a successful refresh removes the active/saved model, retain the remembered ID, invalidate active selection for future admissions, and leave running `RunModel` snapshots untouched. Rediscovery in the same process does not reactivate it; explicit selection is required. Stale entries from a failed refresh remain discoverable/selectable and are visibly labelled stale. Status labels distinguish **selection** (`active`, `no selection`, `saved selection unavailable`), **inventory** (`fresh`, `stale`; discovery is not account callability), **capability** (`known`, `unsupported`, `unknown`), and **budget** (`provider metadata`, `assumed local context`, `assumed local completion reserve`).

`/models` shows a simple list sized for roughly 20 entries. Callback data uses short opaque tokens (at most 64 bytes), never model names/aliases. Expired tokens ask the user to reopen `/models`; callbacks revalidate current inventory before selection. Prompt builders use a shared registry view that presents the pinned current run model separately when it differs from or has disappeared from current inventory. This bounds Telegram payloads without a general pagination system.

### Run admission, wire limits, and model switching

At main-turn, scheduled, on-demand, or plan admission, resolve any explicit selector exactly against the active registry; otherwise snapshot the active main selection. All must fail closed if no main selection is active. Pass the resulting `RunModel` to the main controller or existing runtime factory; scheduled ownership, context isolation, callbacks, and result recording remain unchanged. For scheduled work, mint the execution ID/record before model resolution so an obsolete/missing selector becomes the existing recorded launch failure and alert. On-demand spawn reports an actionable tool error; a plan-step records the normal failed step. Remove startup-frozen `agent.background_model`. Obsolete persisted job selectors produce an actionable launch error; there is no job converter or model fallback. Each LLM client is single-model for its full run.

Before sending, budget the actual target wire payload: system prompt, messages, tool definitions, native-call arguments/framing, and image estimates. The current estimator's omission of native-call arguments must be corrected. A documented combined window `C` limits prompt input plus completion reserve; separate provider input ceiling `T` and output ceiling `O` remain distinct, and `T` is never reduced by output reserve. Do not invent a combined `C` from `T` and `O` unless provider documentation explicitly defines that combined semantics. With no combined metadata, use `agent.ctx_max_tokens` as visibly assumed local `C` only when neither `C` nor `T` is known; never represent it as provider capacity. A provider may reject requests that exceed actual unknown limits; surface that error without rerouting. Retain the existing 85% compaction trigger and 256-token floor as heuristics; neither overrides an exhausted hard budget.

Reserve output and compute the hard input budget as follows:

| Documented input/context metadata | Optional output cap: wire behavior and reserve | Required output cap: wire behavior | Hard input budget |
|---|---|---|---|
| Combined `C` only | Omit cap. If known positive `O`, reserve its full value against `C`; if `O` unknown, reserve 1024 as assumed local accounting only. | Require known positive `O`; send `min(O, C-I)` when positive, otherwise fail. | `C - reserve`. |
| Separate input `T` only; no documented combined `C` | Omit cap. `O` does not reduce `T`; do not apply `agent.ctx_max_tokens`. | Require known positive `O`; send `O` because there is no documented combined window from which to compute a smaller cap. | `T`. |
| Both combined `C` and separate input `T` | Omit cap. If known positive `O`, reserve its full value against `C`; if `O` unknown, reserve 1024 as assumed local accounting only. | Require known positive `O`; send `min(O, C-I)` when positive, otherwise fail. | `min(C - reserve, T)`. |
| Neither known | Use assumed local `C = agent.ctx_max_tokens`; same optional-cap reserve as combined `C` only. | Require known positive `O`; send `min(O, C-I)` when positive, otherwise fail. | `C - reserve`. |
| Graph extraction (overlays the applicable row above) | Fixed internal cap/reserve 1024; never lower it. | Fixed internal cap 1024; this is policy, not discovered `O`. | Use applicable combined/local and separate-input constraints; fail an oversized extraction request. |

Here `I` is the estimated complete prompt/input payload. For known `C`, required output cap is bounded by positive `C-I`; with only `T`, it is bounded only by known `O`. For combined budgeting, a known optional `O >= C` leaves no positive hard input budget and MUST reject locally rather than silently reducing the reserve. A separate-input-only `T` remains the hard limit even when `agent.ctx_max_tokens` is lower. After selecting a cap/reserve, require a positive hard input budget and verify actual `I` does not exceed it or `T`. If the budget is non-positive or required `O` unknown, fail before network I/O. Example: Gemini `T=1,000,000`, `O=65,536`, no documented `C`, and `agent.ctx_max_tokens=90,000` has hard input budget `1,000,000`, not `90,000-65,536=24,464`. Estimate the complete target wire form, including tool definitions and native-call args; cache estimates only by exact assembled payload, target model/tokenizer, and definition revision, and recompute when any changes. For positive hard-input budget `B`, the existing compaction trigger remains `max(int(B * 0.85), 256)`; it is only a heuristic, and neither the 256 floor nor compaction can authorize a payload larger than `B`. Ordinary chat/native/JSON requests omit `temperature` and `top_p`. Graph extraction uses its fixed internal `temperature=0.1` policy and `max_tokens=1024` cap; existing adapter compatibility may strip unsupported sampling fields for specific models (for example, OpenAI reasoning models). The catalog does not add a temperature capability field, and stripping sampling is not a reason to choose another model.

After a manual switch to a smaller model, install a conversation-revision guard. Minimal per-controller runtime state records the selection revision associated with the current conversation and the pending guard revision; restored history under a different selection is guarded without migrating the history. For the first request under that selection, assemble and estimate the complete candidate read-only before history mutation, summarization, or compactor invocation, using the same `C`/`T`/`O` budget cases above. If oversized, reject while preserving selected model, history, and source; do not auto-truncate or change the selection. The guard remains until reset or a manual change to a larger model. If it fits, proceed normally and clear the guard after success. Ordinary compaction remains available for other requests; recheck the complete outgoing payload before transmission. Do not blanket-disable or rewrite the existing compactor. When conversation history contains native tool-wire turns from an earlier provider, use the existing 1:1 linearization for incompatible request paths; never re-execute the recorded tool effects.

All image-bearing requests—including direct image messages and `vision_query`—use only the current run's pinned model and client. A verified `vision=true` sends the image through that client; `vision=false` or unknown capability rejects locally with an actionable instruction to select a verified vision model via `/models` or launch a new task with its canonical ID. `vision_query` takes no model override. Rejection does not alter the current run, main preference, or endpoint; image estimates are included in budget checks and no provider call is made on rejection.

For cloud Ollama, default the connection origin to the official `https://ollama.com` and require its API key. Route discovery to `/api/tags` and inference through that same official origin using the SDK's adapter-correct host/path convention. Normalize/join the API path exactly once; never construct `/api/api/...`. Reject localhost, LAN, self-hosted, or any other non-official origin; never forward the credential to another host. No suffix inference changes the selected host.

### Request retry and independent consumers

Use one `CallAttemptState` per current logical model request. A logical request starts when one model response is needed for the current ReAct step and ends only when a usable response/action is accepted, terminal failure is finalized, or cancellation occurs. Empty-response re-requests, native/JSON control-flow re-requests, safe parse-validation re-prompts, and human/confirmation waits before acceptance retain the same state; mode changes and loops never create/reset state. A successful accepted action ends that request; the next independent model call gets a new state while retaining prior tool results in history. A separate operator-initiated task is a new task, not a hidden retry loop. The state tracks mode, outbound attempts, and automatic retry budget. While a classified retry/recovery card is waiting for the operator, the logical request remains pending; its state is checkpointed and restored unchanged by explicit Retry/`/resume`, so recovery does not implicitly reset the automatic budget. The existing operator retry card and classification options remain; `unknown` remains operator-retryable. Each outbound call counts as one attempt. A user-directed retry is not automatic and authorizes only its explicit continuation call outside the exhausted automatic budget; it does not reset/refill that budget. Unexpected typed/untyped terminal errors are not automatically retried, without removing existing user-directed recovery options.

Every outbound call counts as one attempt, including a call that fails; local mode conversion does not. `[agent] max_retries=4` means four additional retries (five total outbound attempts), default `retry_delay=2.0s` with 2/4/8/16-second exponential delays; zero means initial attempt only. Preserve existing typed transient/permanent classification. An initially selected JSON path retries JSON on recognized transient errors under the same budget. If native calling is locally unsupported (`NotImplementedError` before network I/O), select JSON for the same pinned model; this consumes no attempt, and JSON calls use the shared budget. A recognized transient native network error retries the same native form until budget exhaustion; never switch to JSON to hide provider availability failure. Permanent or other recognized terminal errors propagate. An unexpected untyped exception is logged/sanitized and terminates the request; it never triggers JSON fallback or a new budget. A call that emitted partial output or caused a tool effect is not automatically retried. Earlier completed tool turns do not prevent retrying a later failed request; never replay those tools or restart a whole run automatically. Exhaustion terminates the run while the bot remains available.

Extract existing embedding dispatch and cache into an injected narrow `EmbeddingClient` independent of chat model/client. Require explicit `[embeddings]` model/provider; use only its own configured values or matching provider connection inheritance, never active-chat credentials or endpoint. Missing embedding configuration keeps the bot operational with degraded-index behavior; graph alone is disabled with an actionable warning. Graph enablement/backfill uses one helper to resolve explicit canonical `graph_memory.extraction_model` plus valid explicit global embedding model/provider. Missing/invalid prerequisites warn actionably and disable graph only; never infer from main/first catalog item. At graph construction, if the selected model's known output ceiling is below 1024, warn and disable graph rather than tune or replace the model. Preserve graph extraction's fixed internal parameters as above; provider adapter compatibility may strip unsupported sampling fields. If a particular extraction request cannot fit its source plus the fixed 1024 cap under current input/combined budgets, fail that extraction actionably; do not lower the cap, truncate, reroute, or abort the bot. Unknown required ordinary output limits still fail before invocation; graph's fixed internal allowance is not fabricated catalog metadata. If catalog limits are unknown and the provider later rejects the fixed request, use existing graph failure resilience without trying another model.

## Risks / Trade-offs

- [Provider metadata can be incomplete, stale, or list models unavailable to this account] -> show source/freshness/unknown state; never promise callability or silently select/fallback.
- [Token estimators and image accounting are approximate] -> label assumed budgets/reserves; enforce known hard caps and fail safely on required unknown outputs.
- [Concurrent refresh, selection, or operator config edits can lose updates] -> one registry write lock, connection fingerprints/generations, atomic settings writes, and stopped-process/lock requirement for migration.
- [Credentials/endpoints could leak through snapshots or errors] -> keep secrets runtime-only, `repr=False`, never persist them, sanitize errors to codes, and retain ADR-0028 private-home restrictions.
- [Provider native/JSON semantics differ] -> one pinned provider per run, same-form retries for typed transient failures, local-only JSON routing for unsupported native calls, terminal unexpected errors, and no replay after effects; preserve tool-turn linearization.
- [Migration may encounter comments, custom TOML layout, or ambiguous identities] -> preview by default, bound edits to known model/provider keys, prove unrelated parsed values unchanged, stop rather than guess, and require backups/recovery verification.

## Migration Plan

1. Require the daemon stopped and acquire the agent process/config lock. `migrate_models.py --agent-name NAME` is preview-only by default; `--apply` is explicit. Show source keys, inferred provider/model identities, unresolved references, discarded tuning/retry settings, and the candidate result without resolving secrets.
2. Convert only unambiguous provider connections and model identities. Preserve `env:`/`sec:` references byte-for-byte. Require operator choice for ambiguous account selectors. Remove old generation tuning without promotion/hidden overrides; apply the new `[agent]` retry defaults rather than promoting conflicting provider/model retry values. Seed only the demonstrable old effective startup default (the first entry only when legacy startup actually selected it because no default was configured, and identity mapping is unambiguous); report it as selection intent, not catalog discovery or first-discovered inference. Local Ollama requires explicit cloud replacement.
3. Back up both human config and managed settings before applying. Stage and validate both candidates, retain a recovery manifest with hashes/progress, and atomically replace each file; this is recoverable, not a cross-file atomic transaction. Verify both on success. On failure restore only files still matching the migration's written hashes; never overwrite intervening operator edits. Preserve unrelated config values/bytes where possible; if bounded text edits cannot prove unrelated parsed values unchanged, stop and request manual resolution.
4. Do not rewrite scheduler/job definitions or selectors, job history, prompts, logs, memories, vectors, or manual-switch history. Old job model/fallback selectors fail actionably after the scheduled run record is created; existing failure handling/alerts remain.

## Open Questions

No product-policy questions remain. The later ADR step must record narrow partial supersessions: ADR-0007 only for removed per-call generation overrides (runtime boundary/profiles/products remain); ADR-0009 for same-form typed transient retry, unsupported-native local JSON routing, and unexpected-exception terminal behavior (native-first dispatch remains); ADR-0020 for provider/supplement metadata replacing manual per-model context metadata, dynamic context discovery, explicit pinned-model vision behavior, and actionable rejection replacing warn-and-ignore legacy fallback settings. Preserve ADR-0020's no cross-model fallback and single-model-per-run commitments. ADR-0019's central resolver, ADR-0028's private-home boundary, ADR-0022's tool-definition compaction accounting, ADR-0021's no-tool-replay checkpoint, and ADR-0033's scheduled ownership remain unchanged. These ADR updates are required history work, not blockers or permission to edit existing ADR files.
