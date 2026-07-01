# Vault Runtime Lookup Specification

## Purpose

Define the `secret_get` built-in tool that allows the agent (LLM) to retrieve vault values at runtime with user confirmation.

## ADDED Requirements

### Requirement: Built-in tool `secret_get` retrieves vault values

The application MUST provide a `secret_get` built-in tool that reads a value from the vault by key and returns it to the agent.

Feature: Vault runtime lookup
Rule: `secret_get` requires user confirmation BEFORE consulting the vault. The vault is never read during the confirmation phase.

#### Scenario: Agent requests a vault value
- **GIVEN** the vault contains `"OLLAMA_HOST": "http://localhost:11434"`
- **AND** a SKILL.md references an unbound API endpoint or key variable `OLLAMA_HOST`
- **AND** the agent calls `secret_get` with `key = "OLLAMA_HOST"`
- **WHEN** the user approves the confirmation
- **THEN** the tool consults the vault for the first time
- **AND** the tool returns `"http://localhost:11434"` in the `output` field
- **AND** the tool result has `success = true`

#### Scenario: User denies vault value retrieval
- **GIVEN** the agent calls `secret_get` with `key = "SECRET_KEY"`
- **WHEN** the user denies the confirmation
- **THEN** the tool does NOT consult the vault
- **AND** the tool returns an error indicating the request was denied
- **AND** the agent must handle the error (e.g. abort the task)

#### Scenario: Missing key in vault after user approval
- **GIVEN** the agent calls `secret_get` with `key = "MISSING_KEY"`
- **AND** the user approves the confirmation
- **AND** the vault does not contain `MISSING_KEY`
- **WHEN** the tool consults the vault after approval
- **THEN** it returns an error indicating the key was not found in the vault
- **AND** the agent must report the error to the user

#### Scenario: Vault is NOT consulted before confirmation
- **GIVEN** the agent calls `secret_get` with `key = "EXISTING_KEY"`
- **WHEN** the tool is dispatched
- **THEN** the built-in executor returns `requires_confirmation = True`
- **AND** the vault is NOT consulted during confirmation setup
- **AND** the Telegram UI shows an approval dialog without vault knowledge

## MODIFIED Requirements

## REMOVED Requirements
