# provider-model-catalog Specification

## Purpose

Define provider-connected discovery, persistent catalog state, explicit model selection, run admission, migration, and provider metadata policy.

## ADDED Requirements

### Requirement: Provider connections and canonical model identity

The application SHALL accept provider connection entries containing only adapter type, optional credential reference, base URL, and request timeout. Supported adapters SHALL be `openai`, `openrouter`, `anthropic`, `google`, and cloud-only `ollama`. Human configuration SHALL NOT define per-model entries, generation tuning, or provider retry settings. A model identity SHALL be `<connection-id>/<exact-upstream-id>`, split only at the first slash; display names SHALL NOT be selectors.

#### Scenario: Upstream ID contains slashes
- **GIVEN** connection `router-main` discovers upstream ID `vendor/model/revision`
- **WHEN** the catalog creates its canonical identity
- **THEN** the identity is `router-main/vendor/model/revision`
- **AND** inference receives the exact upstream ID `vendor/model/revision`

#### Scenario: Unsupported per-model tuning is rejected
- **GIVEN** configuration contains a per-model tuning field or provider retry override
- **WHEN** the configuration is validated
- **THEN** validation reports the unsupported field and its replacement path
- **AND** the field is not silently ignored or promoted to hidden runtime tuning

### Requirement: Cloud Ollama is restricted to the official service

The application SHALL accept Ollama only at the official HTTPS origin `https://ollama.com`, require its credential, normalize API path joining exactly once, and SHALL NOT forward its credential to another origin.

#### Scenario: Non-cloud Ollama endpoint is rejected
- **GIVEN** an Ollama connection names localhost, a LAN host, a self-hosted host, or any non-official origin
- **WHEN** configuration is validated
- **THEN** startup reports an actionable cloud-only endpoint error
- **AND** no discovery or inference request is sent

#### Scenario: Cloud discovery and inference use one origin
- **GIVEN** a valid authenticated cloud Ollama connection
- **WHEN** discovery and inference run
- **THEN** discovery uses `/api/tags` and inference uses the adapter-correct path on `https://ollama.com`
- **AND** neither request contains a duplicated `/api/api` path

#### Scenario: Missing Ollama credential is rejected
- **GIVEN** an official-cloud Ollama connection has no valid credential source
- **WHEN** configuration is validated
- **THEN** startup reports an actionable credential error
- **AND** no unauthenticated discovery/inference request is sent

### Requirement: Provider inventory refresh is complete and atomic

Startup and `/models` SHALL refresh every configured provider using all supported pages. A validated complete response SHALL atomically replace that provider's inventory, including a valid empty inventory. A failed, malformed, or incomplete response SHALL preserve last-good entries and mark them stale. Refresh results SHALL be discarded when the captured connection fingerprint is no longer current or a newer generation has completed. Removing a provider SHALL purge its inventory.

#### Scenario: Complete pagination replaces inventory
- **GIVEN** a provider returns multiple pages and all pages validate
- **WHEN** refresh completes
- **THEN** the catalog replaces that provider's inventory with the union of all pages
- **AND** the provider is marked fresh

#### Scenario: Failed refresh preserves stale inventory
- **GIVEN** a provider has last-good models and its refresh fails or ends on an incomplete page
- **WHEN** refresh completes
- **THEN** the prior models remain available and visibly stale
- **AND** no partial response replaces the inventory

#### Scenario: Valid empty inventory removes prior models
- **GIVEN** all pages of a successful provider response validate and contain no models
- **WHEN** refresh commits
- **THEN** the provider inventory becomes empty
- **AND** any removed active model becomes unavailable for future admissions

#### Scenario: Late refresh cannot restore a deleted provider
- **GIVEN** a refresh is in flight for a provider connection
- **AND** the connection is deleted or changed before the refresh returns
- **WHEN** the old response completes
- **THEN** its fingerprint/generation check rejects the response
- **AND** deleted-provider models remain purged

### Requirement: Provider metadata and public supplementation preserve provenance

Discovery SHALL retain documented limits/capabilities with per-field provenance. It SHALL distinguish combined `C`, input `T`, output `O`, and unknown; never infer `C` from `T+O` absent documented semantics.

#### Scenario: Supplement fills only an unknown field
- **GIVEN** provider metadata has `vision=false` and no context value
- **AND** an exact adapter/upstream match in `models.dev` has `vision=true` and a documented context value
- **WHEN** metadata is merged
- **THEN** provider `vision=false` remains authoritative
- **AND** only the unknown context value may be supplemented with `models.dev` provenance

#### Scenario: Provider metadata maps to distinct limit types
- **GIVEN** Anthropic returns `max_input_tokens` and `max_tokens`, and Google returns `inputTokenLimit` and `outputTokenLimit`
- **WHEN** metadata is normalized
- **THEN** each input value maps to `T` and each output value maps to `O`
- **AND** OpenRouter `context_length` maps to `C`
- **AND** OpenAI inventory IDs do not imply limits

### Requirement: Public metadata supplementation is privacy bounded

Supplement SHALL use public `models.dev` data only, match exact adapter/upstream IDs locally, fill unknown fields only, and retain provenance. It SHALL NOT transmit account/request data or create catalog entries, aliases, or inferred dates.

#### Scenario: No private data is sent to public metadata service
- **GIVEN** model discovery supplements public metadata
- **WHEN** the request is made to `models.dev`
- **THEN** it contains no account identity, endpoint, credential, prompt, request, or per-model lookup data

### Requirement: Managed settings separate remembered selection from active selection

The app SHALL maintain versioned settings under resolved XDG config home. Settings SHALL exclude credentials/references, URLs, raw responses, and error bodies. Remembered ID and active selection SHALL remain distinct; missing/corrupt state leaves commands available but model work inactive.

#### Scenario: Saved model is absent at startup
- **GIVEN** settings remember a model that is absent from the validated catalog
- **WHEN** startup restores state
- **THEN** the remembered ID is preserved but active selection stays unset
- **AND** all commands remain available

#### Scenario: No saved selection leaves service commands available
- **GIVEN** managed settings contain no remembered model
- **WHEN** startup completes
- **THEN** daemon and commands remain available
- **AND** all model work is gated until explicit `/models` selection

#### Scenario: Corrupt settings are preserved
- **GIVEN** managed settings cannot be parsed
- **WHEN** startup initializes the catalog
- **THEN** corrupt input is preserved before repair or overwrite
- **AND** model work remains inactive

#### Scenario: Selection write fails
- **GIVEN** a valid model is selected while settings cannot be written
- **WHEN** selection persistence fails
- **THEN** the prior active model remains active
- **AND** the user receives a failure rather than a false success

### Requirement: Selection, refresh, and admission are serialized safely

Selection and refresh persistence SHALL share one lock and merge against current selection. Callback data SHALL be opaque and at most 64 bytes; callbacks revalidate expiry and inventory. Catalog presence SHALL NOT promise account callability.

#### Scenario: Concurrent refresh and selection do not lose selection
- **GIVEN** a refresh and a user model selection overlap
- **WHEN** both operations commit
- **THEN** refresh merges against the latest selection revision
- **AND** neither operation overwrites the other's durable state

#### Scenario: Expired callback is revalidated
- **GIVEN** a `/models` callback token has expired or its model disappeared
- **WHEN** the callback is used
- **THEN** no selection changes
- **AND** the user is told to reopen `/models`

#### Scenario: Current-run model remains discoverable to prompt consumers
- **GIVEN** refresh removed a model pinned by a running task
- **WHEN** `prompt_builder`, alternate prompt loader, or tool descriptions query the registry
- **THEN** all use the same catalog view
- **AND** show the pinned run model separately from future active selection
- **AND** reveal no credentials or secret references

#### Scenario: Shared registry reports metadata state without promising callability
- **GIVEN** model metadata contains known, unknown, stale, or unsupported capability values
- **WHEN** prompt builder, alternate loader, or tool model descriptions render the shared view
- **THEN** each exposes those states with field provenance and active/pinned model identity
- **AND** none reveals secrets or claims catalog presence guarantees account callability

### Requirement: Model admission requires an active main selection and pins a run snapshot

Every run admission SHALL resolve an explicit canonical ID or snapshot active main selection. Admission fails when no active selection exists. The admitted model, connection, endpoint, and metadata SHALL remain pinned through that run.

#### Scenario: Saved-but-inactive model blocks explicit job override
- **GIVEN** a remembered model exists but active selection is unset
- **WHEN** a scheduled job requests an explicit canonical model
- **THEN** admission fails without provider I/O
- **AND** the job's existing launch-failure handling records and alerts the failure

#### Scenario: Obsolete job selector fails after execution record creation
- **GIVEN** a scheduled job has an obsolete or unavailable selector
- **WHEN** the execution ID/record is minted and model resolution fails
- **THEN** the launch failure is recorded and existing alert behavior runs
- **AND** no job converter or fallback model is used

#### Scenario: Removed model does not change a running task
- **GIVEN** a run has a pinned model snapshot and refresh removes that model
- **WHEN** the run makes its next provider call
- **THEN** it continues using the pinned provider and model
- **AND** a new admission using the removed model is rejected

#### Scenario: Rediscovery does not reactivate removed selection
- **GIVEN** refresh removed the remembered active model and later refresh rediscovers it
- **WHEN** the new inventory is committed
- **THEN** the remembered ID remains but active selection stays unset
- **AND** explicit selection through `/models` is required

#### Scenario: Obsolete agent selector is rejected
- **GIVEN** configuration contains obsolete `agent.default_model`, `agent.background_model`, or fallback keys
- **WHEN** configuration is validated
- **THEN** it reports an actionable error and does not infer a default

#### Scenario: On-demand and plan admission failures are contained
- **GIVEN** on-demand spawn or a plan step names an unavailable model
- **WHEN** admission fails
- **THEN** spawn returns an actionable tool error or the plan records its failed step
- **AND** neither changes scheduler or sub-agent context ownership

### Requirement: Model limits govern actual wire payloads

The registry SHALL provide exact pinned metadata to the single request-budgeting contract in `per-model-context-window`. Provider limits SHALL remain distinct from assumed local budgets; tool definitions count in the target payload.

#### Scenario: Pinned metadata is handed to the budgeting owner
- **GIVEN** an admitted model has sourced `C`, `T`, `O`, or unknown limits
- **WHEN** a run prepares an LLM request
- **THEN** the registry supplies the exact pinned metadata and source labels
- **AND** `per-model-context-window` alone applies budgeting and required-cap policy

### Requirement: Embeddings and graph extraction remain explicit exceptions

Embeddings SHALL use explicit configuration and matching connection inheritance only; active chat credentials/endpoints SHALL NOT be fallbacks. Graph startup and backfill SHALL share prerequisite validation and disable graph alone when prerequisites fail.

#### Scenario: Missing embedding configuration degrades without stopping bot
- **GIVEN** embedding configuration is missing or invalid
- **WHEN** application startup completes
- **THEN** the bot remains operational with degraded indexing
- **AND** graph memory is disabled with an actionable warning

#### Scenario: Invalid graph extraction prerequisites disable graph only
- **GIVEN** graph extraction model or explicit global embedding model/provider is missing or invalid
- **WHEN** startup or graph backfill validates prerequisites
- **THEN** both use the shared validation policy and warn actionably
- **AND** graph alone is disabled while the bot remains operational

#### Scenario: Graph request exceeds fixed allowance
- **GIVEN** one graph extraction request cannot fit its source and fixed 1024 cap
- **WHEN** that extraction is attempted
- **THEN** the extraction fails actionably
- **AND** the cap is not reduced and the bot remains available

#### Scenario: Graph output ceiling is known below fixed allowance
- **GIVEN** selected extraction model has known `O<1024`
- **WHEN** graph is initialized
- **THEN** graph is disabled with an actionable warning
- **AND** no replacement model is selected

#### Scenario: Unknown graph capacity is not fabricated
- **GIVEN** the extraction model has unknown `O` and provider later rejects its fixed request
- **WHEN** graph extraction fails
- **THEN** existing graph failure resilience handles the failure
- **AND** no model fallback is attempted

#### Scenario: Reasoning adapter strips unsupported graph temperature
- **GIVEN** graph uses its fixed temperature `0.1` with an adapter that rejects sampling fields
- **WHEN** the adapter prepares the graph request
- **THEN** it strips unsupported temperature per existing compatibility behavior
- **AND** it does not select another model

### Requirement: Legacy configuration migration is preview-first and recoverable

Migration SHALL be preview-only unless `--apply` is explicit. It SHALL preserve unresolved secret references and stop when bounded edits cannot prove unrelated values unchanged.

#### Scenario: Preview does not alter files
- **GIVEN** a legacy configuration is available
- **WHEN** migration runs without `--apply`
- **THEN** it reports proposed mappings, unresolved references, discarded values, and candidate output
- **AND** original files remain unchanged

#### Scenario: Ambiguous legacy account stops conversion
- **GIVEN** a legacy model could map to multiple provider accounts
- **WHEN** migration previews the configuration
- **THEN** it requests operator choice and does not guess or apply

#### Scenario: Interrupted migration does not overwrite operator edits
- **GIVEN** migration partially wrote a file and an operator subsequently edited it
- **WHEN** recovery resumes or rolls back
- **THEN** it leaves the operator-edited file untouched
- **AND** reports the unresolved recovery action

#### Scenario: Migration backs up both files and reports discarded values
- **GIVEN** apply is requested for an unambiguous legacy setup
- **WHEN** migration prepares to replace configuration and managed settings
- **THEN** both originals are backed up before writes
- **AND** discarded tuning/retry values are reported without promotion

#### Scenario: Only demonstrable old effective default is seeded
- **GIVEN** the old startup selected its first model only because no default was configured
- **AND** provider/account identity mapping is unambiguous
- **WHEN** migration applies
- **THEN** that old effective default may seed remembered selection
- **AND** otherwise no first-entry or first-discovered model is inferred

#### Scenario: Local Ollama replacement requires explicit action
- **GIVEN** legacy configuration targets local Ollama
- **WHEN** migration previews or applies
- **THEN** it requires an explicit cloud replacement choice
- **AND** never guesses from a `:cloud` suffix

#### Scenario: Migration does not rewrite unrelated records
- **GIVEN** migration is applied
- **WHEN** it completes
- **THEN** jobs, job history, prompts, logs, memories, vectors, and conversation history remain untouched

#### Scenario: Idempotent migration can be rerun
- **GIVEN** migration already completed successfully
- **WHEN** it is run again
- **THEN** it makes no duplicate transformations or destructive rewrites
