# Provider Credential Inheritance Specification

## Purpose

Define provider-connection credentials and independent embedding credentials.

## REMOVED Requirements

### Requirement: Provider-level model defaults

**Reason:** Per-model configuration and model-level credential overrides are replaced by connections-only provider configuration.

**Migration:** `migrate_models.py` converts only unambiguous legacy provider/model identities, preserves unresolved secret references, and does not promote per-model tuning or credentials.

### Requirement: Provider-level embedding defaults

**Reason:** Embedding configuration remains explicit and independent; active chat credential/endpoint fallback is removed.

**Migration:** Preserve explicit embedding settings and matching-provider inheritance. Do not migrate active-chat credentials to embeddings; missing embedding configuration degrades indexing and disables graph only.

## ADDED Requirements

### Requirement: Provider connections own chat credentials and transport

Human model configuration SHALL use provider connections with `type`, optional `api_key`, `base_url`, and `request_timeout`. Discovered model records inherit transport and credentials only from their owning connection. Per-model keys/endpoints/retry settings/tuning SHALL be rejected. Credentials SHALL resolve only for the matching provider client and never enter managed catalog state, prompts, UI, errors, or repr.

#### Scenario: Model inherits provider credentials
- **GIVEN** connection `openai-prod` defines an API key source and discovers `gpt-x`
- **AND** the admitted canonical model is `openai-prod/gpt-x`
- **WHEN** the run's provider client is constructed
- **THEN** it uses that connection's credential and remains selectable by canonical ID

#### Scenario: Per-model credential override is rejected
- **GIVEN** a human model entry attempts to override its provider connection credential
- **WHEN** configuration is validated
- **THEN** the unsupported per-model override is rejected actionably
- **AND** other models continue using their connection's credential

#### Scenario: Legacy per-model credential reference is preserved during migration
- **GIVEN** a legacy per-model credential field exists in source configuration
- **WHEN** migration previews it
- **THEN** the reference is preserved byte-for-byte without resolving it
- **AND** it is not promoted to a connection or managed catalog secret

#### Scenario: Model inherits provider transport defaults
- **GIVEN** a provider connection defines endpoint and request timeout
- **AND** it discovers a model with no model-specific transport fields
- **WHEN** a run is admitted
- **THEN** the run snapshot uses the provider connection's transport
- **AND** generation tuning is not inherited or created

#### Scenario: Legacy model configuration is routed to migration
- **GIVEN** legacy explicit `[[models]]` configuration is present
- **WHEN** the migration preview runs
- **THEN** only unambiguous model/connection identities are proposed
- **AND** unsupported layouts or ambiguous identities stop for operator resolution

### Requirement: Embedding credentials remain independent of chat selection

Embeddings SHALL use explicit embedding model/provider settings and may inherit credentials and endpoint only from the matching provider connection. They SHALL NOT fall back to the active chat model's credentials or endpoint. Missing embedding configuration SHALL keep the bot operational with degraded indexing and disable graph memory with an actionable warning.

#### Scenario: Embeddings inherit matching provider credentials
- **GIVEN** explicit embeddings select provider `openai` and omit their own key/base URL
- **AND** matching provider connection defines them
- **WHEN** the embedding client initializes
- **THEN** it uses the matching connection key and URL

#### Scenario: Embeddings do not fall back to chat credentials
- **GIVEN** embeddings have no credential and no matching provider credential
- **AND** the active chat model has a credential
- **WHEN** embedding initialization runs
- **THEN** the chat credential and endpoint are not used
- **AND** the bot runs with degraded indexing and graph disabled actionably
