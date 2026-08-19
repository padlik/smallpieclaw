## MODIFIED Requirements

### Requirement: LLMProvider protocol extension

The `LLMProvider` protocol SHALL support optional native tool calling methods.

#### Scenario: chat_with_tools returns structured response
- **GIVEN** an `LLMProvider` implementation supports native tool calling
- **WHEN** `chat_with_tools(messages, tools, system)` is called
- **THEN** it SHALL return a `ChatResponse` with either `text` or `tool_calls` populated

#### Scenario: chat_with_tools_fallback operates single-model
- **GIVEN** an `LLMProvider` implementation supports native tool calling
- **WHEN** `chat_with_tools_fallback(messages, tools, system)` is called
- **AND** the primary model fails with a transient error
- **THEN** it SHALL propagate the error to the caller
- **AND** it SHALL NOT try any fallback model (the LLM client is single-model)

#### Scenario: Existing chat methods unchanged
- **GIVEN** an `LLMProvider` implementation
- **WHEN** `chat()` or `chat_with_fallback()` is called
- **THEN** the method signatures and return types SHALL be unchanged from before this change
- **AND** `chat_with_fallback()` SHALL operate single-model (no fallback chain)