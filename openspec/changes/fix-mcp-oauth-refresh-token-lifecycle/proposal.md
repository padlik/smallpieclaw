## Why

The google-workspace MCP server's OAuth refresh token is being silently destroyed. Two compounding defects — verified in code and in production logs (Sept 10–12, `agent_logs.sqlite`) — turn every silent token refresh into a total loss of the refresh token: on disk, `FileTokenStorage.set_tokens` replaces the whole token file with `exclude_none=True`, dropping the absent `refresh_token` (Google's refresh-grant responses never include one); in memory, the MCP SDK replaces `current_tokens` wholesale after a refresh, so the provider can refresh exactly once before falling back to a full authorization flow. The consequence observed in production: the refresh token alternates present/absent on consecutive 6-hour scheduled job runs, and the *second* expiry posts Telegram auth buttons **from a background job** into a stale chat. The scheduled aliveness check then depends on a human tapping "Authorize" within the 30-second tool-call timeout; when nobody does, the OAuth flow times out at 300s, the session dies ("unhandled errors in a TaskGroup"), and the server zombies in `not connected` — an error text that matches none of the react loop's auth-failure indicators — until a human runs `/mcp auth` manually. Three manual re-auths were required in 48 hours. The same auth churn exposes a co-located lifecycle leak: every re-auth re-registers the server without purging its previous tool-name map entries or closing the replaced wrapper, logging ~14 duplicate "tool name conflict" warnings per re-auth and leaking the old session task (with its chat-bound provider) for the process lifetime.

## What Changes

- **Merge-preserve `refresh_token` in token storage.** `FileTokenStorage.set_tokens` SHALL retain the previously stored `refresh_token` when the incoming token response omits one (as refresh-grant responses do); an incoming `refresh_token` SHALL still overwrite the stored one (real rotation preserved). Stops the on-disk wipe.
- **Keep `refresh_token` usable in memory across refresh grants.** A `OAuthClientProvider` subclass SHALL re-attach the pre-refresh `refresh_token` to the post-refresh token set before persisting, so the provider can refresh indefinitely instead of exactly once. Stops the in-memory one-shot.
- **No interactive authorization from background context.** The long-lived wrapper registered after a successful `/mcp auth` SHALL run with a non-interactive provider (`chat_id=None`, `on_non_interactive` set): a background tool call that hits an auth-required condition degrades to `needs_auth` instead of posting Telegram auth buttons. The interactive `/mcp auth` flow keeps today's button UX unchanged.
- **Auth-failure error handshake.** Auth-required failures surfaced from background tool calls — including the `not connected` fast-fail and the non-interactive abort error — SHALL be recognized by the react loop's MCP auth-failure detection, SHALL mark the server `needs_auth`, and SHALL return an actionable "Run `/mcp auth <name>`" error, so the server reports 🔐 instead of zombie-ing with no recovery hint.
- **Registration hygiene.** Re-registering a server (after re-auth or `/mcp on` reconnect) SHALL atomically replace its previous registration: stale tool-name map entries are purged before the new tools are registered, and the replaced wrapper is closed. Eliminates the ~14 "tool name conflict: claimed by both" warnings observed per re-auth and stops leaking old wrappers (with their session tasks and providers).

No configuration changes; no `mcp` SDK modification or pinning. Existing stored tokens remain readable. A token file already wiped of its `refresh_token` by the old behavior requires one manual re-auth after this change — no data migration is planned.

## Capabilities

### New Capabilities

(none — all changes modify existing capabilities)

### Modified Capabilities

- `mcp-oauth-flow`: Token persistence SHALL survive refresh grants (merge-preserve `refresh_token`); the SDK-driven automatic refresh SHALL keep the refresh token usable across successive refreshes (in-memory re-attach); auth-required conditions reached from non-interactive context SHALL never deliver authorization buttons and SHALL surface a `/mcp auth` recovery hint with `needs_auth` status.
- `mcp-transport`: Server (re-)registration SHALL atomically replace the previous registration (no duplicate tool-name map entries, replaced wrapper closed), and auth-related session death SHALL leave the server in `needs_auth` rather than a silent `not connected` zombie.

## Impact

- `mcp_oauth.py` — `FileTokenStorage.set_tokens` (merge-preserve semantics), new `OAuthClientProvider` subclass (refresh re-attach), `OAuthProviderFactory.build` wiring.
- `mcp_client.py` — `_run_oauth_flow` (register the long-lived wrapper with a non-interactive provider), `_register_wrapper` / `_connect_server` (tool-map purge + old-wrapper close), `call_tool` fast-fail and non-interactive abort error texts.
- `react_loop.py` — `_is_mcp_auth_failure` indicator alignment so background auth failures are detected and translated.
- Tests — `tests/test_mcp_oauth.py`, `tests/test_mcp_client.py`, `tests/test_mcp_oauth_probe.py`, `tests/test_react_loop.py`.
- Out of scope (recorded for follow-up): stale-contextvar OAuth log context (misleading trace/prompt IDs spanning 12h+ in production logs); any change to the interactive `/mcp auth` button UX; any `mcp` SDK patching or version pinning.