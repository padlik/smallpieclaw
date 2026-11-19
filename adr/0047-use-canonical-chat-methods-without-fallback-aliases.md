# 0047-use-canonical-chat-methods-without-fallback-aliases

## Status

accepted, supersedes ADR-0009 (partial; see Supersedes)

## Date

2026-11-19

## Supersedes

ADR-0009 (partial: only the compatibility clause keeping `chat_with_fallback()` and `chat_with_tools_fallback()` as `LLMProvider` methods). Native-first dispatch with the text JSON path as bounded by ADR-0035, native tool payloads, `parse_json` handling, and unchanged `chat()` / `chat_with_tools()` signatures remain in force.

## Context

ADR-0020 and ADR-0035 removed model fallback and moved retry ownership to the agent, so the `*_fallback` methods only forward to single-model calls. The ReAct loop and native capability probe still call these misleading names, and ADR-0009 required them for backward compatibility.

## Considered Options

- Keep the aliases as deprecated wrappers: no breakage, but keeps misleading names across clients, Protocol and test doubles.
- Remove the aliases and migrate all callers to canonical methods (chosen).

## Decision Outcome

The chat interface is `chat()` and `chat_with_tools()` only; the fallback-named aliases are removed everywhere with no wrappers (accepted breaking change). Calls stay single pinned-model, errors propagate to the shared retry owner, and cancellation is unchanged. The native capability probe resolves the canonical `chat_with_tools` callable and never invokes the provider. Embedding methods and the pre-send native-unsupported handling are unchanged.

## Consequences

- Good: names match behavior; one call path per capability.
- Bad: external callers of the aliases break.
- Follow-up: migrate ReAct, the probe, Protocol and test doubles in the authorized apply. ADR-0042, ADR-0022 and ADR-0043 are not affected.
