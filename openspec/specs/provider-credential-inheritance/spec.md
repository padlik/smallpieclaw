# Provider Credential Inheritance Specification

## Purpose

Define how model and embeddings configuration inherits credentials and transport defaults from provider-level configuration.

## Requirements

### Requirement: Chat credentials and transport come only from the owning connection

A chat run target, whether discovered or named explicitly without discovery, SHALL use transport and credentials only from its owning `[providers.<connection-id>]` connection. Per-model keys, endpoints, retry settings, and tuning SHALL be rejected actionably and not converted. Credentials SHALL resolve only for the matching provider client and never enter the cache, the preference, prompts, UI, errors, or representations.

#### Scenario: Model inherits provider credentials
- **GIVEN** connection `openai-prod` defines an API key source
- **WHEN** a run admitted as `openai-prod/gpt-x` constructs its provider client
- **THEN** it uses that connection's credential

#### Scenario: Model inherits provider transport defaults
- **GIVEN** a provider connection defines endpoint and request timeout
- **WHEN** a run naming a model on that connection is admitted, whether or not the model is cached
- **THEN** the run target uses the connection's transport
- **AND** generation tuning is not inherited or created

#### Scenario: Per-model credential override is rejected
- **GIVEN** a human model entry attempts to override its connection credential
- **WHEN** configuration is validated
- **THEN** the override is rejected actionably and no value from it is converted or used

#### Scenario: Legacy model configuration is rejected without migration
- **GIVEN** legacy `[[models]]` configuration is present
- **WHEN** configuration is validated
- **THEN** startup reports an actionable error directing the operator to `[providers.<connection-id>]`
- **AND** no migration preview or conversion is offered

### Requirement: Embedding configuration is fully explicit

Embeddings SHALL require explicit `[embeddings]` `provider`, `base_url`, `api_key`, and `model`, and SHALL NOT inherit any value from a chat connection (including one of the same adapter type), from chat selection, or from graph extraction configuration. Missing or invalid embedding configuration SHALL keep the bot operational with degraded indexing and disable graph memory with an actionable warning.

#### Scenario: Matching provider connection is not inherited
- **GIVEN** `[embeddings]` selects provider `openai` and omits `api_key` and `base_url`
- **AND** chat connection `openai-prod` of type `openai` defines both
- **WHEN** the embedding client initializes
- **THEN** the chat connection's key and URL are not used
- **AND** indexing is degraded with an actionable warning naming the missing fields

#### Scenario: Embeddings do not fall back to chat credentials
- **GIVEN** embeddings have no credential and the active chat model has one
- **WHEN** embedding initialization runs
- **THEN** the chat credential and endpoint are not used
- **AND** the bot runs with degraded indexing and graph disabled actionably

#### Scenario: Complete explicit embedding configuration is used
- **GIVEN** `[embeddings]` defines all four fields
- **WHEN** the embedding client initializes
- **THEN** it uses exactly those values for indexing and graph vectors

### Requirement: Graph extraction credentials are independent

When graph memory is enabled, graph extraction SHALL require explicit `[graph_memory]` `extraction_provider`, `extraction_base_url`, `extraction_api_key`, and `extraction_model`, SHALL NOT inherit any value from a chat connection (including one of the same adapter type), chat selection, or `[embeddings]`, and SHALL resolve its credential only for the graph extraction client.

#### Scenario: Extraction does not inherit from embeddings or chat
- **GIVEN** `[embeddings]` and a chat connection both define credentials for provider `openai`
- **AND** graph extraction uses `extraction_provider = "openai"` but omits `extraction_api_key`
- **WHEN** graph memory starts
- **THEN** neither credential is used and graph memory is disabled with an actionable warning
- **AND** chat and indexing remain available

#### Scenario: Extraction credential is sent only to its endpoint
- **GIVEN** graph extraction defines its own credential and endpoint
- **WHEN** an extraction request is sent
- **THEN** only the extraction credential is sent, and only to `extraction_base_url`
