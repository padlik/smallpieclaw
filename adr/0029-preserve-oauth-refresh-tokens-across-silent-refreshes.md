# 0029-preserve-oauth-refresh-tokens-across-silent-refreshes

## Status

Accepted

## Date

2026-09-14

## Supersedes

None

## Context

Google's OAuth token endpoint omits `refresh_token` from refresh-grant responses (it is only issued at consent). Two compounding behaviors destroyed stored refresh tokens on every silent refresh:

- The MCP SDK (`mcp` 1.29.1, `client/auth/oauth2.py`) replaces `context.current_tokens` wholesale with the refresh response, so `can_refresh_token()` is false after the first refresh — the next expiry falls back to a full authorization flow.
- `FileTokenStorage.set_tokens` persisted the incoming token with `model_dump(exclude_none=True)` as a full file replacement, erasing the previously stored `refresh_token` from disk.

Production logs (Sept 10–12, `agent_logs.sqlite`) show the stored refresh token alternating present/absent on consecutive 6-hour scheduled-job runs, forcing manual `/mcp auth` roughly every 12 hours.

## Considered Options

- **Storage merge-preserve + provider subclass (chosen)**: keep the stored `refresh_token` when the incoming token omits one (in `FileTokenStorage.set_tokens`), and subclass `OAuthClientProvider` to re-attach the pre-refresh `refresh_token` in memory after a successful refresh.
- **Storage-side fix only**: insufficient — the SDK reads `current_tokens` from its own context, not from storage, so disk-only fixes cannot keep `can_refresh_token()` true; the button-firing full-flow cycle would persist.
- **Patch/fork the SDK**: rejected — couples us to SDK internals and forgoes upstream fixes.
- **Full-field union merge in storage**: rejected — risks resurrecting stale `access_token`/`scope` after a legitimate re-auth.

## Decision Outcome

OAuth refresh tokens SHALL survive silent refresh grants both on disk and in memory:

- `FileTokenStorage.set_tokens` merge-preserves: when the incoming token omits `refresh_token` and the stored file has one, the stored value is retained; an incoming `refresh_token` always overwrites (real rotation preserved). Only `refresh_token` is merge-preserved.
- A `RefreshPreservingOAuthProvider` subclass overrides `_handle_refresh_response`: capture the pre-refresh `refresh_token`, delegate to `super()`, and only when `super()` returned `True` and the new `current_tokens` lacks a `refresh_token`, re-attach the captured one. The failure path is a no-op so a revoked refresh token stays dead.

Both provider construction sites (interactive `/mcp auth` flow and non-interactive startup) use the subclass via `OAuthProviderFactory.build`.

## Consequences

- **Good**: silent refreshes continue indefinitely; the disruptive full-auth cycle (Telegram buttons from background jobs, 12-hour manual re-auth cadence) is eliminated at its root.
- **Good**: fix lives entirely in our layer; no SDK fork; if the SDK later fixes its replace semantics natively, the override degrades to a passthrough.
- **Bad**: the override depends on a semi-internal SDK hook (`_handle_refresh_response`) — SDK upgrades need a contract test asserting `can_refresh_token()` stays true after refresh.
- **Neutral**: token files already wiped by the old behavior require one manual re-auth; no migration.