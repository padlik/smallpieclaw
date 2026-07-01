# Vault Config Resolution Specification

## Purpose

Define how `sec:` prefixed values in config files are resolved against the agent vault at startup.

## Requirements

### Requirement: Config values prefixed with `sec:` resolve from vault

The configuration parser MUST recognize string values that are exactly `sec:KEY_NAME` and resolve them against the vault before validation.

Feature: Vault config resolution
Rule: `sec:` prefix works anywhere `env:` works, including model fields, MCP env, and MCP headers.

#### Scenario: Model API key uses `sec:` prefix
- **GIVEN** a model entry has `api_key = "sec:OPENAI_API_KEY"`
- **AND** the vault contains `"OPENAI_API_KEY": "sk-abc"`
- **WHEN** the application parses the configuration
- **THEN** the model's resolved `api_key` is `"sk-abc"`

#### Scenario: MCP server env uses `sec:` prefix
- **GIVEN** an MCP server entry has `[mcp_servers.env]` with `API_KEY = "sec:MCP_KEY"`
- **AND** the vault contains `"MCP_KEY": "token-xyz"`
- **WHEN** the application parses the configuration
- **THEN** the MCP server's resolved env contains `API_KEY = "token-xyz"`

#### Scenario: Telegram bot token uses `sec:` prefix
- **GIVEN** `[telegram]` has `bot_token = "sec:TELEGRAM_BOT_TOKEN"`
- **AND** the vault contains `"TELEGRAM_BOT_TOKEN": "99999:..."`
- **WHEN** the application parses the configuration
- **THEN** the resolved `bot_token` is `"99999:..."`

#### Scenario: Missing vault key fails startup
- **GIVEN** a config field uses `sec:MISSING_KEY`
- **AND** the vault does not contain `MISSING_KEY`
- **WHEN** the application parses the configuration
- **THEN** startup fails with a `ConfigError` identifying the missing key and the config field

#### Scenario: `sec:` and `env:` prefixes coexist
- **GIVEN** one config field uses `env:HOME_DIR`
- **AND** another config field uses `sec:API_KEY`
- **WHEN** the application parses the configuration
- **THEN** `env:HOME_DIR` resolves from the OS environment
- **AND** `sec:API_KEY` resolves from the vault
- **AND** both fields are available at runtime

#### Scenario: Provider field uses `sec:` prefix
- **GIVEN** `[providers.openai]` has `api_key = "sec:OPENAI_API_KEY"`
- **AND** the vault contains `"OPENAI_API_KEY": "sk-abc"`
- **AND** a model entry uses `provider = "openai"` without its own `api_key`
- **WHEN** the application parses the configuration
- **THEN** the provider's resolved `api_key` is `"sk-abc"`
- **AND** the model inherits `"sk-abc"` as its `api_key`

#### Scenario: OpenAI-compatible provider uses `sec:` prefix
- **GIVEN** a provider entry for xAI Grok uses `provider = "openai"`
- **AND** `[providers.grok]` has `api_key = "sec:XAI_API_KEY"`
- **AND** `[providers.grok]` has `base_url = "https://api.x.ai/v1"`
- **AND** the vault contains `"XAI_API_KEY": "sk-xai-..."`
- **WHEN** the application parses the configuration
- **THEN** the provider's resolved `api_key` is `"sk-xai-..."`
- **AND** models using `provider = "grok"` inherit both the key and base URL
