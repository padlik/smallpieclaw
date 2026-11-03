# 0043-use-input-counts-and-success-only-summary-compaction

## Status

accepted, supersedes ADR-0020, ADR-0022, ADR-0034 (partial; see Supersedes)

## Date

2026-11-01

## Supersedes

ADR-0020 (partial: replace effective per-model window and 85%-minus-output compaction formula and any still-live all-model vision routing; retain single-model client/no cross-model fallback). ADR-0022 (partial: replace completion reserve, real headroom/danger, effective-window diagnostics and window-derived trigger; retain MAIN-only per-turn push/reference swap, identity/liveness and per-server tool counts). ADR-0034 (partial: remove `C`/`T`/`O` limits/assumed window, required-output-limit metadata budgeting/rejection and verified-vision catalog gate; use a simple configured mandatory cap only where the adapter requires an output parameter; retain no scan/switch, omission of optional output caps and ordinary `temperature`/`top_p`, and the fixed graph exception). The ADR-0034 all-model scan reversal already superseded ADR-0020 in that scope; it is not restored here.

## Context

ADR-0020 and ADR-0022 still carry window-derived compaction, reserves and danger/headroom calculations, and ADR-0034 added discovered limit and capability policy. Once inventory is advisory these are not reliable local request gates. The existing summary flow also needs to preserve original evidence when summarization fails. Future model adapters should need only informational input counts and a narrow mandatory output parameter, not invented capacity math.

## Considered Options

- Keep per-model fit preflight, reserves, fallback truncation and overflow repair: can avoid some provider errors but reintroduces metadata authority and may silently destroy history.
- Use one configured input threshold and success-only publication (chosen): simpler and loss-avoiding, though providers may still reject oversized requests.
- Disable compaction entirely: avoids summary failure but discards a useful history-management mechanism.

## Decision Outcome

Estimate input tokens for system prompt, chat history and complete current tool definitions, including native schemas and MCP server grouping. The model-independent positive-integer `agent.compaction_threshold_tokens` defaults to `60000`; counts and threshold are informational in `/context` and `context_profile` (with existing pinned identity, turn and liveness). Preserve MAIN-only snapshot publication and reference swap; remove completion reserve, effective window, headroom, danger and capacity percentages. Neither counts nor discovered metadata block, shrink or switch a request. `agent.required_output_cap_tokens` is a separate positive integer defaulting to `1024`, emitted only where the adapter API requires an output parameter (currently Anthropic). Other ordinary adapters omit optional caps, `temperature` and `top_p`; graph's fixed 1024/0.1 exception remains independent.

Before an eligible over-threshold send, automatically attempt compaction once using existing span partition and summary prompt; retain goal and last two messages expanded to whole native call/result groups, summarize only a nonempty middle. Build a copy, validate native history and a nonempty stripped summary, and publish history plus goal index together only on success. Do not cap retained text, recur in the same step, repair overflow, or reject the still-large send. Later eligible steps may compact again after success, including a previously published summary in the eligible middle. Failure, empty/invalid summary warns, sends original history unchanged and disables further automatic attempts for that run only; cancellation propagates without failure classification. A provider context rejection follows existing error/checkpoint handling with no forced retry/repair. Native pairing, argument and wire schema validation remain; summary calls use the run's pin but their own retry state.

The hidden manual `/compress` is separate: under idle-command-lock exclusion it uses its existing conversation-summary prompt, independently admitted owned client (closed on every path), and one-entry successful summary shape without changing working memory. Only success-safe publication/failure preservation is shared with the automatic policy; failure never disables a future run's automatic compaction. Never borrow a closed run client for idle compression.

## Consequences

- Good: diagnostic counts are useful without pretending to know provider capacity; malformed native history and failed summaries cannot silently corrupt the next send.
- Bad: a long uncompactable request can be rejected by a provider; the operator must address it manually, and fixed numeric defaults will not suit every model.
- Follow-up: preserve native schema/pairing and manual-client lifetime regression coverage while replacing fit/overflow policy in a separately approved apply.
