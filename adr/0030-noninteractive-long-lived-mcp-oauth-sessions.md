# 0030-noninteractive-long-lived-mcp-oauth-sessions

## Status

Accepted

## Date

2026-09-14

## Supersedes

None

## Context

`MCPManager._run_oauth_flow` registered the post-flow long-lived wrapper with the *interactive* provider — permanently bound to the Telegram chat that ran `/mcp auth`, with no `on_non_interactive` callback. Background/scheduled tool calls through that wrapper therefore triggered full OAuth flows on token expiry, posting authorization buttons into a stale chat (the only button-delivery path is `send_oauth_prompt`, gated on a non-None `chat_id`). A headless aliveness job's pass/fail then depended on a human tapping within the 30-second tool-call timeout; when nobody did, the 300-second flow timeout killed the session ("unhandled errors in a TaskGroup") and left a zombie server whose `not connected` error matched none of the react loop's auth-failure indicators.

The startup path (`_prepare_oauth_provider`) already had the correct shape: `chat_id=None` + `on_non_interactive` → clean `needs_auth` degradation, no buttons.

## Considered Options

- **Non-interactive provider on the long-lived wrapper (chosen)**: after a successful interactive flow, register the long-lived wrapper with a provider built like the startup one (`chat_id=None`, `on_non_interactive` marking `needs_auth`), aligned with react-loop auth-failure detection.
- **Clear the chat binding on the live provider post-flow**: rejected — mutates a live SDK object mid-lifecycle; `auth=provider` is baked into the httpx transport at construction, so the binding cannot be reliably cleared anyway.
- **Keep background buttons, make the flow managed/cancellable**: rejected — preserves the human-in-the-loop dependency for headless jobs; a scheduled job must never depend on a Telegram tap.

## Decision Outcome

Interactive authorization is ephemeral; long-lived sessions are always non-interactive:

- The in-flight `/mcp auth` flow keeps today's chat-bound provider (buttons, cancel, probe) and is torn down after success.
- The registered long-lived wrapper runs a non-interactive provider; a background tool call that hits an auth-required condition degrades to `needs_auth` — no Telegram buttons, no callback-server wait — with error texts aligned to the react loop's MCP auth-failure indicators so the server reports 🔐 (`needs_auth`) and the job sees a "Run `/mcp auth <name>`" recovery hint.
- Cost accepted: one extra session bring-up per interactive authorization (ordinary initialize + tools/list with the now-valid stored token; no handshake).

## Consequences

- **Good**: background jobs degrade predictably (needs_auth + actionable hint) instead of zombie-ing; sessions survive auth failures; orphan button prompts are eliminated.
- **Good**: the 30s-tool-timeout vs 300s-flow-wait mismatch becomes moot for background contexts because background flows can no longer start.
- **Bad**: background jobs can never self-heal a genuinely dead refresh token (Google's ~6-month expiry, revocation) — a human must run `/mcp auth`. Accepted for a single-user personal agent.
- **Neutral**: the interactive `/mcp auth` UX is unchanged for the operator.