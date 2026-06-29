## ADDED Requirements

### Requirement: Provider-level model defaults
The application MUST allow model entries to inherit supported credentials and transport defaults from a matching provider-level configuration, while preserving explicit model-level overrides.

Feature: Provider credential inheritance
Rule: Models may inherit credentials and provider defaults from a shared provider configuration.

#### Scenario: Model inherits provider credentials
- **GIVEN** a provider configuration defines an API key source for `openai`
- **AND** a model entry uses provider `openai` without its own API key source
- **WHEN** the application parses the configuration
- **THEN** the model uses the provider-level API key source
- **AND** the model remains selectable by its configured name and model identifier

#### Scenario: Model overrides provider credentials
- **GIVEN** a provider configuration defines an API key source for `openai`
- **AND** a model entry using provider `openai` defines its own API key source
- **WHEN** the application parses the configuration
- **THEN** the model uses the model-level API key source
- **AND** the provider-level API key source remains available to other models

#### Scenario: Model inherits provider transport defaults
- **GIVEN** a provider configuration defines shared transport defaults such as base URL, request timeout, retry count, and retry delay
- **AND** a model entry for that provider omits those fields
- **WHEN** the application parses the configuration
- **THEN** the omitted model fields are populated from provider defaults
- **AND** model-specific generation settings such as model identifier, max tokens, temperature, vision, reasoning, and aliases remain model-specific

#### Scenario: Existing explicit model configuration remains valid
- **GIVEN** a configuration uses explicit per-model API keys and provider settings without a provider defaults section
- **WHEN** the application parses the configuration
- **THEN** the configuration remains valid
- **AND** the resolved model settings match the previous behavior

### Requirement: Provider-level embedding defaults
The application MUST allow embeddings configuration to inherit supported credentials and transport defaults from a matching provider-level configuration when embeddings-specific values are omitted.

Feature: Provider credential inheritance
Rule: Embeddings may inherit credentials and transport defaults from matching provider configuration.

#### Scenario: Embeddings inherit matching provider credentials
- **GIVEN** a provider configuration defines an API key source and base URL for `openai`
- **AND** the embeddings configuration uses provider `openai` without its own API key source or base URL
- **WHEN** the application parses the configuration
- **THEN** embeddings use the matching provider-level credentials and base URL

#### Scenario: Embeddings preserve existing fallback when no provider credential exists
- **GIVEN** embeddings are configured without an API key source
- **AND** no matching provider-level credential is configured
- **WHEN** the application initializes embeddings
- **THEN** embeddings may continue to fall back to the active model key as before

## MODIFIED Requirements

## REMOVED Requirements
