## ADDED Requirements

### Requirement: LLM error card with inline retry/cancel buttons

The progress panel SHALL handle a new `__LLM_ERROR__:{token}:{json}` progress marker by rendering an error card with inline buttons. The JSON payload SHALL contain: error type, classified message, model name, current step, max steps, count of preserved tool results, truncated error detail (first 200 chars), and retryable flag. When retryable is true, the card SHALL show `[🔄 Retry]` and `[❌ Cancel]` buttons. When retryable is false, the card SHALL show only `[❌ Cancel]`. The callback data SHALL use the format `llm_retry:{token}:{response}` where response is "retry" or "cancel".

Feature: Telegram progress panel
Rule: LLM errors get a dedicated error card with actionable buttons, not a buried text message.

#### Scenario: Retryable error card renders with both buttons
- **GIVEN** a `__LLM_ERROR__` progress marker arrives with retryable=true
- **WHEN** the progress panel processes the marker
- **THEN** an error card is rendered showing the error type icon and message
- **AND** the card shows the model name, step/max-steps, and preserved tool results count
- **AND** the card shows truncated error detail (first 200 chars)
- **AND** the card has a [🔄 Retry] button with callback_data `llm_retry:{token}:retry`
- **AND** the card has a [❌ Cancel] button with callback_data `llm_retry:{token}:cancel`

#### Scenario: Non-retryable error card renders with only Cancel
- **GIVEN** a `__LLM_ERROR__` progress marker arrives with retryable=false
- **WHEN** the progress panel processes the marker
- **THEN** an error card is rendered showing the error type icon and message
- **AND** the card shows the model name, step/max-steps, and preserved tool results count
- **AND** the card has only a [❌ Cancel] button with callback_data `llm_retry:{token}:cancel`
- **AND** no [🔄 Retry] button is shown

#### Scenario: Error card is sent while typing indicator persists
- **GIVEN** the typing indicator is active and an `__LLM_ERROR__` marker arrives
- **WHEN** the error card is rendered
- **THEN** the typing indicator continues until the user responds or the retry timeout expires
- **AND** the error card is sent as a reply to the original message

#### Scenario: LLM error callback unblocks agent thread
- **GIVEN** an error card is showing and the agent thread is blocked
- **WHEN** the user presses [🔄 Retry] or [❌ Cancel]
- **THEN** the callback handler calls `confirmation.signal_retry(token, response)`
- **AND** the agent thread unblocks
- **AND** the error card is updated to show the user's choice