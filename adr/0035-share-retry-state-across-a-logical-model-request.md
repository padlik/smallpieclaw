# Share retry state across a logical model request

## Status

accepted, supersedes ADR-0009 (partial: error-triggered JSON fallback and retry ownership only)

## Date

2026-10-13

## Supersedes

ADR-0009 (partial: replace its error-triggered protocol fallback and provider-owned retry assumptions with agent-owned logical-request retry state. Native-first dispatch, native tool payloads, protocol methods, tool-definition surface, and same-model JSON handling of native text remain in force.)

## Context

ADR-0009 established native-first tool calling with a JSON path as a universal fallback, including fallback when native calls fail. The catalog change requires bounded retries that do not hide provider failures by changing request form and do not replay completed tool effects. ADR-0021 already defines explicit disk-persisted checkpoint recovery as user-directed recovery without replaying prior tools; the retry budget can extend the checkpoint state without changing that recovery unit or its ownership.

## Decision

The agent owns one attempt state for each logical model request, spanning outbound calls and control-flow continuations until a usable response is accepted, a terminal failure is finalized, or cancellation occurs. Agent-level `max_retries` counts additional attempts (default four) and `retry_delay` defaults to two seconds with exponential delays; provider/model retry controls are not used. A recognized transient failure retries the same request form. Native calling remains first; only a local, pre-network unsupported-native result may convert to same-model JSON without consuming an outbound attempt. Unexpected or permanent failures do not trigger protocol switching or automatic retry. Empty responses and other permitted pre-acceptance control-flow loops retain the state. Explicit checkpoint recovery restores the saved state and budget; it does not refill the budget or replay completed tools. Partial output or side effects prohibit automatic retry of that request.

## Consequences

- Native-first dispatch and the existing native/JSON interfaces remain, while transient provider availability errors no longer silently become JSON fallback.
- A single agent-owned budget makes retry behavior consistent across native, JSON, empty-response, and human-wait paths; local mode conversion is not counted as network I/O.
- Retry exhaustion ends the run without taking the bot offline. Completed tool turns remain recorded and are never replayed automatically.
- Checkpoint state grows to preserve the pending attempt mode, count, and remaining budget; explicit operator recovery remains distinct from automatic retries.
