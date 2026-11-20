## MODIFIED Requirements

### Requirement: LLMProvider protocol extension

`LLMProvider` SHALL retain its native and embedding methods unchanged. Its chat interface SHALL be exactly the canonical `chat` and `chat_with_tools` methods with their existing signatures and return types, and the `chat_with_fallback` and `chat_with_tools_fallback` aliases SHALL NOT exist. Each call SHALL remain single-model and propagate errors to the shared retry owner. The native capability probe SHALL be defined on the canonical `chat_with_tools` method, SHALL NOT invoke the provider or send a request, and SHALL NOT disable native calling for a provider that implements it.

#### Scenario: chat_with_tools returns structured response
- **GIVEN** an implementation supports native calling
- **WHEN** `chat_with_tools(messages,tools,system)` is called
- **THEN** it returns `ChatResponse` with text or tool calls populated

#### Scenario: chat_with_tools_fallback operates single-model
- **GIVEN** the pinned provider call fails transiently
- **WHEN** `chat_with_tools` returns control
- **THEN** the failure is passed to the shared retry owner
- **AND** another model is not tried

#### Scenario: Existing chat methods unchanged
- **GIVEN** an `LLMProvider` implementation
- **WHEN** `chat()` is called
- **THEN** established method signatures and return types remain unchanged
- **AND** the call remains single-model

#### Scenario: Fallback-named aliases are absent
- **GIVEN** the `LLMProvider` protocol and its implementations
- **WHEN** their methods are inspected
- **THEN** neither `chat_with_fallback` nor `chat_with_tools_fallback` exists

#### Scenario: Capability probe uses the canonical method without sending
- **GIVEN** a provider implementing `chat_with_tools`
- **WHEN** the ReAct loop probes native capability
- **THEN** the probe only resolves the canonical callable and never invokes the provider or sends a request
- **AND** the existing native-unsupported boundary is unchanged: native-first dispatch is used, while a provider without the method or a locally detected unsupported case is handled before sending, with no post-send fallback
