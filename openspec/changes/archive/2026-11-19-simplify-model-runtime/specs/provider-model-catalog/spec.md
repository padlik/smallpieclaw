# provider-model-catalog Delta

## Purpose

Baseline: this delta edits the pending predecessor capability added by `openspec/changes/provider-model-catalog/specs/provider-model-catalog/spec.md` (implementation baseline `a57ab26`). The capability does not exist in canonical `openspec/specs/` on `main`; this delta MUST be synchronized only after the predecessor has been archived, so that every MODIFIED and REMOVED name below refers to a predecessor requirement. Each MODIFIED block keeps every predecessor scenario name, with updated content. Unlisted predecessor requirements (`Cloud Ollama is restricted to the official service`, `Graph shutdown preserves active writer dependencies`) remain unchanged.

## MODIFIED Requirements

### Requirement: Provider connections and canonical model identity

The application SHALL accept `[providers.<connection-id>]` entries containing only `type`, optional `api_key` source, `base_url`, and `request_timeout`, with adapters `openai`, `openrouter`, `anthropic`, `google`, and cloud-only `ollama`, and no per-model entries, generation tuning, or retry settings. A selector SHALL be `<connection-id>/<upstream-id>`, split only at the first slash; both parts MUST be non-empty and used exactly as supplied; display names SHALL NOT be selectors.

#### Scenario: Upstream ID contains slashes
- **GIVEN** connection `router-main` is configured
- **WHEN** selector `router-main/vendor/model/revision` is resolved
- **THEN** the connection is `router-main`
- **AND** inference receives the exact upstream ID `vendor/model/revision`

#### Scenario: Unsupported per-model tuning is rejected
- **GIVEN** configuration contains a per-model tuning field or provider retry override
- **WHEN** the configuration is validated
- **THEN** validation reports the unsupported field and its replacement path
- **AND** the field is not silently ignored or promoted to hidden runtime tuning

#### Scenario: Malformed selector shape is rejected locally
- **GIVEN** a selector is `gpt-x`, `/gpt-x`, `openai-prod/`, or an empty string
- **WHEN** it is resolved
- **THEN** resolution fails locally with an actionable selector-shape error
- **AND** no provider request is sent

#### Scenario: Selector is not normalized
- **GIVEN** connection `openai-prod` is configured
- **WHEN** selector ` openai-prod/gpt-x` or `OpenAI-Prod/gpt-x` is resolved
- **THEN** the connection part is used exactly and is reported as an unknown connection
- **AND** no trimmed, case-folded, or aliased connection is substituted

#### Scenario: Padded upstream ID is transmitted unchanged
- **GIVEN** connection `openai-prod` is configured
- **WHEN** selector `openai-prod/ gpt-x ` is admitted
- **THEN** the non-empty upstream part ` gpt-x ` is pinned and sent unchanged to the provider
- **AND** any provider rejection is reported normally without trimming or substituting another model

### Requirement: Provider inventory refresh is complete and atomic

Bootstrap discovery and `/models` refresh SHALL query every configured connection using all supported pages and atomically replace that provider's advisory-cache candidates only with a validated complete response, including a valid empty one. A failed, malformed, or incomplete response SHALL keep its last-good candidates marked stale while other providers update. Refresh SHALL change only the cache. No fingerprint, configuration generation, or catalog revision SHALL gate refresh results.

#### Scenario: Complete pagination replaces inventory
- **GIVEN** a provider returns multiple pages and all pages validate
- **WHEN** refresh completes
- **THEN** the cache replaces that provider's candidates with the union of all pages
- **AND** the provider is marked fresh

#### Scenario: Failed refresh preserves stale inventory
- **GIVEN** a provider has last-good candidates and its refresh fails or ends on an incomplete page
- **WHEN** refresh completes
- **THEN** the prior candidates remain listed and visibly stale
- **AND** no partial response replaces them, while other providers' successful results are committed

#### Scenario: Valid empty inventory removes prior models
- **GIVEN** all pages of a successful provider response validate and contain no models
- **AND** the active selection names a model of that provider
- **WHEN** refresh commits
- **THEN** that provider's cached candidates become empty
- **AND** the active selection and saved preference are unchanged and remain usable for later admissions

#### Scenario: Display-name whitespace does not invalidate the inventory
- **GIVEN** a provider's complete response contains a candidate whose provider-reported display text has leading or trailing whitespace, or is blank after trimming
- **AND** every candidate identity is well formed and no provider-supplied text contains the resolved credential
- **WHEN** refresh completes
- **THEN** the display text is shown trimmed, or as the exact raw upstream ID when blank
- **AND** the complete inventory is committed fresh with every candidate retained
- **AND** the candidate's canonical and upstream IDs are exactly as the provider reported them, never trimmed or normalized
- **AND** a malformed candidate identity or any provider text containing the resolved credential still fails the whole refresh and keeps the last-good candidates stale

#### Scenario: Late refresh cannot restore a deleted provider
- **GIVEN** a connection was removed from `config.toml` and the daemon restarted
- **AND** the cache still holds that connection's old candidates
- **WHEN** `/models` lists or refreshes candidates
- **THEN** the removed connection's candidates are not listed and are dropped from the next cache write
- **AND** no refresh result for it is committed

#### Scenario: Refresh changes only the cache
- **GIVEN** a refresh runs, whether it commits or fails
- **WHEN** it completes
- **THEN** connections, the saved preference, the active selection, run targets, and running work SHALL be unchanged

### Requirement: Managed settings separate remembered selection from active selection

The durable selection preference SHALL be `model_selection_file` (`$XDG_CONFIG_HOME/<agent>/model-selection.toml`) holding exactly one deliberate selector as `model = "<connection-id>/<upstream-id>"`. Only a deliberate `/models` selection SHALL write it, by temporary file and atomic replace, before the new active selection is published. The active selection SHALL be held in memory and an automatic choice SHALL NOT be persisted.

#### Scenario: Saved model is absent at startup
- **GIVEN** the preference saves `openai-prod/gpt-x`, connection `openai-prod` is configured, and the cache has no `openai-prod/gpt-x` candidate
- **WHEN** startup restores state
- **THEN** `openai-prod/gpt-x` is the active selection
- **AND** no discovery request is made and all commands remain available

#### Scenario: No saved selection leaves service commands available
- **GIVEN** no preference file exists
- **WHEN** startup completes, whether or not a candidate could be chosen automatically
- **THEN** daemon and commands remain available
- **AND** no preference file is created

#### Scenario: Corrupt settings are preserved
- **GIVEN** the preference file exists but cannot be read or parsed, and the cache contains candidates
- **WHEN** startup completes
- **THEN** no active selection exists, no cached candidate is chosen, and no bootstrap discovery runs
- **AND** the file bytes are unchanged and an actionable notice that does not reveal file content is reported
- **AND** commands and valid explicit model requests remain available

#### Scenario: Selection write fails
- **GIVEN** a model is deliberately selected while the preference cannot be written
- **WHEN** persistence fails
- **THEN** the prior active selection remains active and the preference keeps its prior bytes
- **AND** the operator receives a failure rather than a false success

#### Scenario: Deliberate selection is persisted before it is published
- **GIVEN** an operator deliberately selects a model in `/models`
- **WHEN** the selection is processed
- **THEN** the preference file is atomically replaced with that selector first
- **AND** only then does the active selection change and success get reported

#### Scenario: Deliberate selection recovers a corrupt preference
- **GIVEN** the preference is corrupt and selection is unavailable
- **WHEN** the operator deliberately selects a model in `/models`
- **THEN** the preference is atomically replaced and the selection becomes active immediately without a restart

#### Scenario: Preference and cache hold no credentials or provider data
- **GIVEN** the selection preference or the cache is written
- **WHEN** its contents are inspected
- **THEN** neither SHALL contain credentials, credential references, endpoint URLs, raw responses, or error bodies

#### Scenario: Invalid preference is never overwritten automatically
- **GIVEN** the preference is unreadable, unparseable, or invalid
- **WHEN** startup or any automatic or runtime process runs
- **THEN** the active selection is unavailable and the preference SHALL NOT be overwritten
- **AND** only a deliberate selection SHALL replace it, atomically and without a restart

### Requirement: Selection, refresh, and admission are serialized safely

`/models` callbacks SHALL carry opaque tokens of at most 64 bytes resolving server-side to render-captured selectors; expired or unknown tokens SHALL change nothing. Deliberate selection SHALL check only selector shape and configured connection, not cache membership. Selection and refresh SHALL serialize without partial publication; refresh SHALL NOT overwrite selection or preference. Prompt and tool consumers SHALL share a view distinguishing a run's pinned target from future active selection.

#### Scenario: Concurrent refresh and selection do not lose selection
- **GIVEN** a refresh and a deliberate model selection overlap
- **WHEN** both operations complete
- **THEN** the deliberate selection remains active and persisted
- **AND** the cache contains the refresh result

#### Scenario: Expired callback is revalidated
- **GIVEN** a `/models` callback token has expired or is unknown
- **WHEN** the callback is used
- **THEN** no selection or preference changes
- **AND** the user is told to reopen `/models`

#### Scenario: Current-run model remains discoverable to prompt consumers
- **GIVEN** refresh removed a model pinned by a running task
- **WHEN** `prompt_builder`, the alternate prompt loader, or tool descriptions query the shared view
- **THEN** all use the same view
- **AND** show the pinned run target separately from the future active selection
- **AND** reveal no credentials or credential references

#### Scenario: Shared registry reports metadata state without promising callability
- **GIVEN** cached display data shows known, unknown, or stale values
- **WHEN** prompt builder, the alternate loader, or tool model descriptions render the shared view
- **THEN** each presents them as advisory discovery data with active and pinned identity, without provenance labels
- **AND** none claims that catalog presence guarantees account callability or gates a request

#### Scenario: Callback for a candidate removed by a later refresh still selects
- **GIVEN** a valid unexpired token names a candidate that a later refresh removed from the cache
- **WHEN** the callback is used
- **THEN** the captured selector is selected and persisted, because the cache is not a whitelist

#### Scenario: Catalog presence does not promise account callability
- **GIVEN** a model appears in the catalog
- **WHEN** any consumer presents or uses it
- **THEN** catalog presence SHALL NOT be presented as a promise that the account can call the model

### Requirement: Embeddings and graph extraction remain explicit exceptions

Embeddings SHALL be configured only by `[embeddings]` `provider`, `base_url`, `api_key`, and `model`, and graph extraction only by `[graph_memory]` `extraction_provider`, `extraction_base_url`, `extraction_api_key`, and `extraction_model`, all required when graph memory is enabled. Neither SHALL inherit from a chat connection, chat selection, the cache, or the other service. A missing or invalid value SHALL degrade only the affected service with an actionable warning and SHALL NOT stop the bot.

#### Scenario: Missing embedding configuration degrades without stopping bot
- **GIVEN** an `[embeddings]` field is missing or invalid
- **WHEN** application startup completes
- **THEN** the bot remains operational with indexing degraded
- **AND** graph memory, which needs vectors, is disabled with an actionable warning

#### Scenario: Invalid graph extraction prerequisites disable graph only
- **GIVEN** graph memory is enabled and an extraction field is missing or invalid
- **WHEN** startup validates graph configuration
- **THEN** it warns actionably and disables graph memory alone
- **AND** chat, indexing, and commands remain available and no model is inferred from a chat connection, main selection, or catalog entry

#### Scenario: Live backfill fails on invalid prerequisites
- **GIVEN** the extraction or embedding configuration is invalid for a live backfill
- **WHEN** backfill validates it
- **THEN** it fails actionably before any database construction or write

#### Scenario: Graph request exceeds fixed allowance
- **GIVEN** a graph extraction input is large
- **WHEN** the extraction is attempted
- **THEN** it is sent with the fixed 1024 cap and no local fit check
- **AND** a provider rejection fails only that extraction, the cap is not reduced, and the bot remains available

#### Scenario: Graph output ceiling is known below fixed allowance
- **GIVEN** cached display data reports an output limit below 1024 for the extraction upstream model
- **WHEN** graph memory starts
- **THEN** graph is not disabled because no catalog data is consulted
- **AND** any provider rejection is handled by existing graph failure handling

#### Scenario: Unknown graph capacity is not fabricated
- **GIVEN** the provider rejects a graph extraction request
- **WHEN** graph extraction fails
- **THEN** existing graph failure resilience handles the failure
- **AND** no model fallback is attempted

#### Scenario: Reasoning adapter strips unsupported graph temperature
- **GIVEN** graph uses its fixed temperature `0.1` with an adapter that rejects sampling fields
- **WHEN** the adapter prepares the graph request
- **THEN** it strips unsupported temperature per existing compatibility behavior
- **AND** it does not select another model

#### Scenario: Google discovery fields do not imply capabilities
- **GIVEN** a Google model lists resource names and supported generation methods
- **WHEN** graph extraction uses that upstream model
- **THEN** those fields infer no capability or token cap
- **AND** an unsupported request fails safely through actual provider validation with no alternate model

#### Scenario: Matching chat provider is not inherited
- **GIVEN** a chat connection of type `openai` defines a credential and endpoint
- **AND** `[embeddings]` uses provider `openai` but omits `api_key`
- **WHEN** the embedding client initializes
- **THEN** the chat connection's credential and endpoint are not used
- **AND** indexing is degraded with an actionable missing-field warning

#### Scenario: Graph vectors use the shared embedding service
- **GIVEN** graph memory and indexing are both enabled with valid configuration
- **WHEN** graph memory embeds an entity
- **THEN** it uses the configured `[embeddings]` service and not the graph extraction connection

#### Scenario: No separate graph embedding configuration exists
- **GIVEN** configuration is loaded, whether graph memory is enabled or disabled
- **WHEN** configuration is validated
- **THEN** no graph-specific embedding provider, endpoint, credential, or model setting SHALL exist
- **AND** graph vectors SHALL use the shared embedding service

### Requirement: Graph extraction resolves its model independently of main selection

Graph extraction SHALL build its target only from its own `[graph_memory]` extraction configuration and SHALL NOT read, require, gate on, or change the main selection, the preference, or the cache. Ordinary and scheduled admission SHALL NOT reach the graph extraction target. Each graph component start or backfill invocation SHALL resolve its configuration once; each extraction SHALL build its own owned client from it and close it in `finally` on success, failure, or cancellation.

#### Scenario: Graph resolves without a main selection
- **GIVEN** the main selection is unset, corrupt, or changing, and the cache is empty
- **WHEN** graph memory starts with valid extraction configuration
- **THEN** graph extraction is available
- **AND** the main selection and preference are unchanged

#### Scenario: Selector is exact and never inferred
- **GIVEN** `extraction_model` is a padded or differently cased upstream ID
- **WHEN** graph extraction runs
- **THEN** the exact configured value is sent to the provider
- **AND** no default, first, cached, or main model is used

#### Scenario: One pin with per-call owned clients
- **GIVEN** graph memory or a backfill invocation is running
- **WHEN** extractions succeed, fail, or are cancelled
- **THEN** each extraction uses its own client built from the same resolved configuration and closed in `finally`
- **AND** no component-long shared chat client is created

#### Scenario: Rechecks occur only inside resolution
- **GIVEN** graph memory is running and the main selection changes, the cache is refreshed, or a credential is rotated
- **WHEN** later extractions run
- **THEN** no catalog, selection, or connection recheck occurs and unrelated main changes never fail graph
- **AND** a provider rejection uses existing graph failure handling with no re-resolution or fallback

#### Scenario: Other paths cannot reach graph resolution
- **GIVEN** a tool, prompt, Telegram, spawn, or scheduler path
- **WHEN** it requests a model
- **THEN** it can only use ordinary or scheduled admission

## REMOVED Requirements

### Requirement: Provider metadata and public supplementation preserve provenance

**Reason**: Discovered metadata becomes advisory display data; no per-field provenance, `C`/`T`/`O` limit model, or supplemental merge is kept, because no request decision uses it.

**Migration**: None required. `/models` shows provider-reported display data as advisory.

### Requirement: Public metadata supplementation is privacy bounded

**Reason**: Public `models.dev` supplementation is removed entirely, so no supplement request is made.

**Migration**: None required. See `Discovered metadata never gates requests`.

### Requirement: Model admission requires an active main selection and pins a run snapshot

**Reason**: Explicit selectors no longer require an active main selection or catalog membership, and scheduled selectors are no longer classified into a warn-and-inherit fallback.

**Migration**: Replaced by `Explicit selectors need neither a main selection nor cache membership`, `Run admission pins one immutable target without fallback`, and the scheduled-job-execution selector requirements. Requests that relied on fallback to main must name a configured connection or omit the selector.

### Requirement: Model limits govern actual wire payloads

**Reason**: Request budgeting and fit enforcement are removed; model limits no longer govern any send.

**Migration**: Configure `agent.compaction_threshold_tokens` and, where an adapter requires an output parameter, `agent.required_output_cap_tokens` (see `per-model-context-window`).

### Requirement: Graph startup and live backfill validate prerequisites before side effects

**Reason**: Graph startup and backfill no longer depend on discovery, the saved catalog, a graph pin, or budget checks.

**Migration**: Replaced by `Graph startup and live backfill validate their own configuration before side effects`; configure the four `[graph_memory]` extraction fields.

### Requirement: Legacy configuration migration is preview-first and recoverable

**Reason**: The automated model-configuration migration (`migrate_models.py` preview, `--apply`, and `--recover`) is removed; legacy model configuration receives actionable errors instead of conversion.

**Migration**: Manually rewrite removed configuration as `[providers.<connection-id>]` connections, independent `[embeddings]` and `[graph_memory]` fields, and the new `[agent]` numeric keys, then make a deliberate `/models` selection. The unrelated XDG storage-layout migration (`migrate.py`) is unaffected.

## ADDED Requirements

### Requirement: Chat provider connections are fixed for the process lifetime

Chat provider connections SHALL be parsed once at startup into an immutable map that is not changed until the process restarts. Discovery, selection, admission, and runtime construction SHALL use only that map. No live connection replacement, hot reload, connection fingerprint, or configuration generation SHALL exist.

#### Scenario: Edited connection takes effect only after restart
- **GIVEN** the daemon is running and the operator edits `[providers.openai-prod]` `base_url` in `config.toml`
- **WHEN** a refresh or a new run admission happens before restart
- **THEN** the original `base_url` is still used
- **AND** after restart the edited value is used

#### Scenario: Unknown connection in a selector fails locally
- **GIVEN** no connection `staging` is configured
- **WHEN** selector `staging/gpt-x` is resolved
- **THEN** it fails locally with an actionable unknown-connection error
- **AND** no provider request is sent

### Requirement: Discovered catalog is an advisory disposable cache

Discovered candidates SHALL be stored only in the disposable cache resolved as `models_cache_file` (`$XDG_CACHE_HOME/<agent>/models-cache.json`), holding canonical candidate IDs, per-provider fresh/stale status, and provider-reported display data. The cache SHALL NOT be a whitelist: admission, selection, scheduling, and graph extraction SHALL NOT require membership. Cache writes SHALL be atomic and best-effort.

#### Scenario: Uncached explicit model is admitted
- **GIVEN** connection `openai-prod` is configured and the cache is empty
- **WHEN** a request names `openai-prod/gpt-new`
- **THEN** the run is pinned to `openai-prod/gpt-new` and the provider receives upstream `gpt-new`

#### Scenario: Corrupt cache is treated as absent
- **GIVEN** the cache file cannot be read or parsed
- **WHEN** startup or `/models` reads it
- **THEN** it is treated as having no candidates and an actionable notice is reported
- **AND** commands remain available

#### Scenario: Cache write failure is not fatal
- **GIVEN** a refresh succeeds but the cache cannot be written
- **WHEN** the write fails
- **THEN** the operator is told the cache was not saved
- **AND** the preference, active selection, and running work are unchanged

### Requirement: Discovered metadata never gates requests

Discovered capability or limit values, including false or unknown vision or tool-calling support, SHALL NOT block image or tool requests to a pinned model. Genuine adapter or protocol limitations and provider errors SHALL still apply. No public supplement data or per-field provenance SHALL be fetched or stored.

#### Scenario: Discovered false vision does not block an image
- **GIVEN** the cache records `vision=false` for the pinned model
- **WHEN** an image-bearing request is sent through an adapter that can encode images
- **THEN** the request is sent to the pinned model
- **AND** any provider rejection is reported as a normal failure without switching models

#### Scenario: Unknown tool support does not disable native tools
- **GIVEN** the cache records tool calling as false or unknown for the pinned model
- **WHEN** a request is prepared
- **THEN** native tool calling is attempted under the existing native-first path

#### Scenario: No public metadata request is made
- **GIVEN** discovery runs for every configured provider
- **WHEN** it completes
- **THEN** no request is sent to `models.dev` or any other supplement service
- **AND** the cache stores no supplement record or provenance label

### Requirement: Startup uses the saved selection or a deterministic in-memory choice

Startup SHALL load the preference and the cache independently. A saved selector with valid shape and a configured connection SHALL become active without a cache check. Without a preference file, the first candidate in ascending ordinal canonical-ID order SHALL become active in memory only. A saved selector with invalid shape or an unknown connection SHALL be reported actionably and SHALL leave the selection unset, without an automatic choice.

#### Scenario: Cached candidates choose the first canonical ID without saving
- **GIVEN** no preference file exists and the cache holds `b-conn/model-1` and `a-conn/model-9`
- **WHEN** startup completes
- **THEN** `a-conn/model-9` is active
- **AND** no preference file is created and no discovery runs

#### Scenario: Unsaved automatic choice is re-derived on the next start
- **GIVEN** a previous run chose a model automatically and no deliberate selection was made
- **WHEN** the daemon restarts
- **THEN** the active selection is derived again from the cache

#### Scenario: Invalid saved selector does not become an automatic choice
- **GIVEN** the preference saves `staging/gpt-x`, no connection `staging` is configured, and the cache has candidates
- **WHEN** startup completes
- **THEN** the operator receives an actionable notice naming the selector problem
- **AND** no active selection exists and no cached candidate is chosen

### Requirement: Startup discovery runs only when nothing is known

Discovery SHALL run automatically at startup exactly once, and only when no preference file exists and the cache has no candidates. Otherwise startup SHALL perform no discovery. When no active selection results, commands and explicit model requests SHALL remain available and implicit model requests SHALL fail locally with an actionable missing-selection message.

#### Scenario: First run bootstraps discovery once
- **GIVEN** no preference file exists and the cache is absent
- **WHEN** startup runs
- **THEN** one discovery of every configured provider runs and its result is written to the cache
- **AND** the first discovered candidate in canonical-ID order is active in memory only

#### Scenario: Saved selection skips bootstrap
- **GIVEN** a valid preference exists and the cache is absent
- **WHEN** startup runs
- **THEN** no discovery request is made

#### Scenario: All providers fail during bootstrap
- **GIVEN** no preference file and no cache exist, and every provider discovery fails or returns no candidates
- **WHEN** startup completes
- **THEN** no active selection exists and `/models` and other commands remain available
- **AND** an implicit model request fails locally telling the operator to select a model with `/models`
- **AND** a request naming a configured connection's model still runs

### Requirement: Explicit selectors need neither a main selection nor cache membership

An explicit selector from an ordinary, on-demand, plan, diagnostic, or scheduled request SHALL be resolved directly against the fixed connection map and SHALL require neither an active main selection nor cache membership. A request without a selector SHALL snapshot the active main selection at admission and SHALL fail locally with an actionable missing-selection error when none exists. Malformed selectors and unknown connections SHALL fail locally before provider I/O.

#### Scenario: Explicit request without a main selection
- **GIVEN** no active main selection exists
- **WHEN** an on-demand, plan, or diagnostic request names `openai-prod/gpt-x` on configured connection `openai-prod`
- **THEN** the run is admitted and pinned to that target

#### Scenario: Implicit request without a main selection
- **GIVEN** no active main selection exists
- **WHEN** an **operator turn** or a spawn without a selector is admitted
- **THEN** admission fails locally with guidance to select a model with `/models`
- **AND** no provider request is sent

#### Scenario: Ordinary explicit selectors remain strict
- **GIVEN** an on-demand, plan, or diagnostic request supplies a selector with a padded connection part that is not configured, an unconfigured alias, a blank or malformed shape, or an unknown connection
- **WHEN** admission runs
- **THEN** it fails for unknown connection or malformed shape without substituting main and without scheduled handling
- **AND** a configured connection with a non-empty padded upstream part is not rejected locally for upstream whitespace

#### Scenario: On-demand and plan admission failures are contained
- **GIVEN** on-demand spawn or a plan step names a malformed selector or unknown connection
- **WHEN** admission fails
- **THEN** spawn returns an actionable tool error or the plan records its failed step
- **AND** neither changes scheduler or sub-agent context ownership

### Requirement: Run admission pins one immutable target without fallback

Every run SHALL be admitted exactly once, resolving the connection credential once and pinning one immutable target of connection, endpoint, adapter, and exact upstream ID for the whole run. Later selection changes or refreshes SHALL NOT change it, and runtime construction SHALL NOT re-admit. A well-formed upstream ID on a configured connection SHALL be sent even when undiscovered; provider rejection SHALL be a normal failure with no fallback, substitution, or retry on another model.

#### Scenario: Unknown upstream is attempted and rejection is not hidden
- **GIVEN** connection `openai-prod` is configured
- **WHEN** a run names `openai-prod/does-not-exist` and the provider rejects it
- **THEN** the rejection is reported as a normal run failure
- **AND** no other model is tried

#### Scenario: Selection change does not affect a running task
- **GIVEN** a run is pinned to `openai-prod/gpt-x`
- **WHEN** the operator selects `anthropic-main/claude-y` during the run
- **THEN** the running task keeps `openai-prod/gpt-x`
- **AND** new implicit admissions use `anthropic-main/claude-y`

#### Scenario: Refresh does not affect a running task
- **GIVEN** a run is pinned to a model and refresh removes that model from the cache
- **WHEN** the run makes its next provider call
- **THEN** it uses the same pinned connection and upstream ID

### Requirement: Legacy model tables are rejected without conversion

Configuration validation SHALL reject a top-level `[[models]]` table and any of its fields (`name`, `provider`, `model`, `api_key`, `base_url`, `request_timeout`, `max_tokens`, `temperature`, `top_p`, `vision`, `reasoning`, `aliases`, `context_window`, `max_retries`, `retry_delay`) with an actionable error naming `[providers.<connection-id>]` and `/models`. No automated model-configuration migration tool SHALL exist, and no error SHALL refer to one.

#### Scenario: Legacy models table is rejected
- **GIVEN** `config.toml` contains a `[[models]]` entry with any of the listed fields
- **WHEN** configuration is validated
- **THEN** startup fails with an error naming `[[models]]` and directing the operator to `[providers.<connection-id>]` and `/models`
- **AND** the error does not mention a migration tool and no value is converted

### Requirement: Obsolete agent model and context keys are rejected

Configuration validation SHALL reject `[agent]` `default_model`, `background_model`, `fallback_model`, `fallback_models`, and `ctx_max_tokens` with an actionable error naming the key and its replacement, and SHALL NOT ignore or reinterpret them.

#### Scenario: Obsolete agent selector is rejected
- **GIVEN** configuration contains `agent.default_model`, `agent.background_model`, `agent.fallback_model`, or `agent.fallback_models`
- **WHEN** configuration is validated
- **THEN** it reports an actionable error pointing to `/models` and does not infer a selection

#### Scenario: Obsolete context limit is rejected
- **GIVEN** configuration contains `agent.ctx_max_tokens`
- **WHEN** configuration is validated
- **THEN** it reports an actionable error directing the operator to `agent.compaction_threshold_tokens`

### Requirement: Provider connections accept only connection fields

Configuration validation SHALL reject any `[providers.<connection-id>]` field other than `type`, `api_key`, `base_url`, and `request_timeout`, including `max_tokens`, `temperature`, `top_p`, `model`, `max_retries`, `retry_delay`, and `retries`, with an actionable error naming the field path.

#### Scenario: Provider generation or retry field is rejected
- **GIVEN** `[providers.openai-prod]` contains `temperature`, `model`, or `max_retries`
- **WHEN** configuration is validated
- **THEN** it reports the field path and that only `type`, `api_key`, `base_url`, and `request_timeout` are supported, with retries under `[agent]`

### Requirement: The retired combined model settings file receives only an existence notice

The runtime's only contact with the retired combined `models-config.toml` (resolved as `models_config_file`) SHALL be an existence check. When it exists, the runtime SHALL emit one actionable notice naming its path and advising a deliberate `/models` selection, and SHALL NOT read, parse, convert, write, rename, or delete it.

#### Scenario: Old combined file exists
- **GIVEN** `models-config.toml` exists with any content, valid or not
- **WHEN** the daemon starts
- **THEN** one notice names its path and advises a deliberate `/models` selection
- **AND** the file bytes are unchanged and no selection is derived from it

#### Scenario: Old combined file is absent
- **GIVEN** `models-config.toml` does not exist
- **WHEN** the daemon starts
- **THEN** no notice about it is emitted

### Requirement: Graph startup and live backfill validate their own configuration before side effects

Startup SHALL validate graph extraction configuration, its credential reference, and the embedding configuration before graph database setup, without depending on discovery. Live backfill SHALL validate the same before constructing any database and SHALL NOT read the cache or preference, discover, or write either file. Dry run SHALL resolve no credential and create no client, database, embedding, or inference.

#### Scenario: Startup validates without discovery
- **GIVEN** every provider discovery fails or the cache is absent
- **WHEN** startup initializes graph memory with valid extraction and embedding configuration
- **THEN** graph memory starts and no main selection is required

#### Scenario: Invalid configuration creates no database
- **GIVEN** an extraction or embedding field is invalid
- **WHEN** startup or live backfill validates prerequisites
- **THEN** it fails actionably before graph database construction and no database file is created or written

#### Scenario: Live backfill uses no catalog state
- **GIVEN** a standalone live backfill with valid configuration
- **WHEN** it runs
- **THEN** it performs no discovery and reads or writes neither the cache nor the preference

#### Scenario: Dry run touches nothing
- **GIVEN** a backfill dry run
- **WHEN** it executes
- **THEN** it resolves no credential and creates no client, database, embedding, or inference, and reads only input and state

### Requirement: Credentials never leak through catalog state

Credentials SHALL resolve only for their owning connection or service and SHALL NOT appear in the cache, the preference, prompts, Telegram output, errors, logs, or object representations, and each credential SHALL be sent only to its own endpoint.

#### Scenario: Credentials do not leak into catalog files
- **GIVEN** connections and services use vault references and resolved secrets
- **WHEN** discovery, selection, and admission complete
- **THEN** neither the cache nor the preference contains a secret, credential reference, or endpoint URL
- **AND** errors and logs about selectors and providers are redacted

#### Scenario: Endpoint keys stay isolated
- **GIVEN** connections `a` and `b` have different credentials and endpoints
- **WHEN** a run pinned to `a/model` sends a request
- **THEN** only `a`'s credential is sent and only to `a`'s endpoint

### Requirement: Run ownership and recovery contracts are unchanged

Each run's pinned client SHALL be closed by its existing owner, cancellation SHALL propagate without being classified as an ordinary failure, and agent-owned retry state, checkpoint recovery, and the prohibition on replaying completed tools SHALL behave as before.

#### Scenario: Checkpoint resume keeps the checkpointed model and does not replay tools
- **GIVEN** a run pinned to `a/model` was checkpointed after completed tool calls and the active selection later changed
- **WHEN** the checkpoint is resumed
- **THEN** the resumed request uses the checkpointed model ID and saved attempt state rather than the current active selection
- **AND** no completed tool is re-executed and the active selection is unchanged

#### Scenario: Cancellation is not a model failure
- **GIVEN** a pinned run is cancelled during a provider call
- **WHEN** cancellation propagates
- **THEN** the run's client is closed by its owner and no error classification or checkpoint is produced
