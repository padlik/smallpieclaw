## Context

The MCP OAuth subsystem (`mcp_oauth.py` provider/storage, `mcp_client.py` manager/wrappers, `react_loop.py` failure translation) has five verified defects (evidence in `explore-brief.md`; production logs Sept 10–12):

- **A — disk wipe**: `FileTokenStorage.set_tokens` (`mcp_oauth.py:137-147`) replaces the whole token file with `model_dump(exclude_none=True)`; refresh-grant responses (Google omits `refresh_token`) erase the stored `refresh_token`.
- **B — memory one-shot**: the `mcp` SDK (1.29.1, `oauth2.py:465`) replaces `context.current_tokens` wholesale after a refresh grant, so `can_refresh_token()` is false after the first refresh; the next expiry falls back to a full authorization flow.
- **C — background buttons**: `_run_oauth_flow` (`mcp_client.py:1845-1915`) registers the long-lived wrapper with the *interactive* provider (chat-bound, no `on_non_interactive`), so background-job 401s post Telegram auth buttons into a stale chat.
- **D — zombie handshake**: auth-required failures surface as `"MCP server 'X' not connected"` (`mcp_client.py:420-423`) or `"non-interactive OAuth redirect — needs manual authorization"` (`mcp_oauth.py:589-592`); neither matches `_is_mcp_auth_failure` indicators (`react_loop.py:121`), so the server never gets `needs_auth` + recovery hint.
- **E — registration leak**: `_register_wrapper` / `_connect_server` (`mcp_client.py:888-899`, `:1810-1824`) never purge the server's old `_tool_to_server` entries and `_connect_server` doesn't close the replaced wrapper — ~14 duplicate "tool name conflict" warnings per re-auth, leaked session tasks.

Two provider construction sites exist today: interactive (`_run_oauth_flow`, chat-bound) and non-interactive (`_prepare_oauth_provider`, `chat_id=None` + `on_non_interactive` → degrades correctly, verified by the Sept 10 14:07 startup log). The defect is that the *long-lived* wrapper keeps the interactive one.

Diagram conventions: ASCII, lightweight C4-inspired (component + dynamic views only).

```
+---------------------------+ smallpieclaw process (single daemon) ---------------------------+
|                                                                                                 |
|  react_loop.py                    mcp_client.py                    mcp_oauth.py               |
|  +----------------+              +---------------------+          +----------------------+   |
|  | _is_mcp_auth_   |  tool error  | MCPManager          | provider | OAuthProviderFactory |   |
|  | failure + hint  |<-------------| _wrappers[name] -->_|<-------->|  builds providers   |   |
|  +----------------+   outcome    |  _SdkClientWrapper  |          | FileTokenStorage    |   |
|                                 |   call_tool / queue  |          |  set_tokens/get_*   |   |
|   telegram_interface.py         +----------+----------+          +----------+-----------+   |
|   send_oauth_prompt (buttons)              | auth=provider                  |               |
|                                            v                                v               |
|                            mcp SDK OAuthClientProvider    $XDG_STATE_HOME/<agent>/mcp_tokens/ |
|                            (async_auth_flow, refresh)                     <server>.json      |
+-------------------------------------------------+-----------------------------------------------+
                                                  |
                                                  v HTTP (streamable-http) + OAuth 2.0
                                        google-workspace MCP + accounts.google.com
```

## Goals / Non-Goals

**Goals:**
- Refresh grants never lose the `refresh_token` — on disk or in memory.
- Background/scheduled tool calls never trigger interactive authorization (no buttons); they degrade to `needs_auth` with an actionable `/mcp auth` error.
- The interactive `/mcp auth` flow is unchanged for the operator (same buttons, cancel, probe).
- Server re-registration is atomic: no duplicate tool-map warnings, no leaked wrappers.
- All fixes in our layer (`mcp_oauth.py`, `mcp_client.py`, `react_loop.py`); no SDK patching, no config changes.

**Non-Goals:**
- Stale-contextvar OAuth logging (misleading trace/prompt IDs spanning 12h+) — follow-up change.
- Interactive `/mcp auth` UX changes; token migration for already-wiped files (one manual re-auth suffices); Google RT long-expiry (~6 months) handling beyond clean degradation; the 30s-tool-timeout vs 300s-flow-wait mismatch (moot once background flows can't start).

## Decisions

### D1 — Merge-preserve `refresh_token` in `FileTokenStorage.set_tokens` (fix A)

`set_tokens` reads the existing file (it already does, for `client_info`); when the incoming `OAuthToken.refresh_token` is `None` and the stored file has one, the stored value is carried into the new payload. An incoming `refresh_token` always overwrites (real rotation — Google may re-issue, other providers rotate). Only `refresh_token` is merge-preserved; all other fields replace as today.

*Alternative rejected*: persisting the full union of old+new fields — risks resurrecting stale `access_token`/`scope` after a legit re-auth and complicates the file contract.

*Spec note (watch-item F3)*: this *refines* the main spec's "Re-authentication replaces existing tokens" scenario — full re-auth carries a fresh `refresh_token`, which still overwrites. The delta spec encodes "omitted → preserve / present → overwrite" explicitly.

### D2 — Subclass the SDK provider to re-attach `refresh_token` in memory (fix B)

New `RefreshPreservingOAuthProvider(OAuthClientProvider)` in `mcp_oauth.py` overrides `_handle_refresh_response`. The SDK method (`oauth2.py:454-473`) has two paths: failure (non-200 → `clear_tokens()`, `return False`) and success (`current_tokens = token_response`, `set_tokens`, `return True`). The override mirrors that contract exactly:

1. Capture `self.context.current_tokens.refresh_token` before delegating (if `current_tokens` is non-`None`).
2. `result = await super()._handle_refresh_response(response)` — unchanged behavior.
3. **Only when `result is True`** and `self.context.current_tokens is not None` with `refresh_token is None` and a captured token exists: re-attach the captured `refresh_token` (model copy) so `can_refresh_token()` stays true.
4. Return `result` unchanged.

On the failure path (`False`) the override does nothing — the SDK cleared tokens, which is the correct outcome for a revoked refresh token; re-attaching there would resurrect a revoked RT and re-arm a doomed refresh loop.

*Disk-write ordering*: on the current refresh cycle, `super()` persists the RT-less token via `set_tokens` (`oauth2.py:467`) *before* the override re-attaches in memory — disk correctness this cycle comes from D1's read-merge; the re-attached in-memory `refresh_token` only flows into a `set_tokens` write on the *next* refresh. Net effect: both copies converge, none silently lost.

*Alternative rejected*: wrapping/monkey-patching `TokenStorage.set_tokens` to fake a refresh token into the SDK's context — the SDK reads `current_tokens` from its context, not from storage, so storage-side fixes alone cannot keep `can_refresh_token()` true.

### D3 — Non-interactive provider on the long-lived wrapper (fix C)

`_run_oauth_flow` keeps using the chat-bound provider for the *in-flight* flow (probe + session bring-up + consent). But because the SDK binds `auth=provider` into the httpx transport at construction time (`mcp_client.py:499`, `:506`), the provider cannot be swapped on a live session — a **second session bring-up is required**, and that is new behavior relative to today's lifecycle:

- **Today** (`mcp_client.py:1871-1915`) there is exactly *one* session bring-up: the interactive wrapper (chat-bound provider, `set_oauth(provider, interactive=True)`) drives the 401 handshake *and* becomes the registered long-lived wrapper (`set_task` `:1913`, `_register_wrapper` `:1915`). There is no post-flow reconnect.
- **After the fix**: after the flow session becomes ready (token stored + verified), `_run_oauth_flow` constructs a **fresh wrapper with the non-interactive provider** (built identically to `_prepare_oauth_provider`: `chat_id=None`, `on_non_interactive` marking `needs_auth`) and performs a **second** `connect()`-style bring-up — with the now-valid stored token, so no handshake fires and the cost is one ordinary session initialize + tools/list.
- **Teardown**: the interactive flow wrapper is closed (its session task cancelled/closed as today's `finally` at `:1925-1931` does for abandoned tasks) instead of being handed off; the non-interactive wrapper's live session task is handed off (`session_task = None`) and registered via `_register_wrapper`. The `finally` block must distinguish the two tasks so the live non-interactive runner is never cancelled.

*Alternative rejected*: clearing the chat binding on the in-flight provider post-flow — mutates a live SDK object mid-lifecycle; fragile against SDK internals, and `auth=provider` is already baked into the running transport.

*Consequence*: after any successful `/mcp auth`, the first background 401 with a dead refresh token aborts via `on_non_interactive` (needs_auth, no buttons, no callback-server wait) instead of hanging 300s and killing the session.

### D4 — Auth-failure handshake (fix D; resolves OQ1)

Three aligned changes, defense-in-depth:

1. **needs_auth-aware fast-fail**: `call_tool` (`mcp_client.py:420-423`) checks `needs_auth` before the generic `connected` check and returns an auth-specific error: `"MCP server '<name>' needs authorization. Run /mcp auth <name>."` — the text contains `needs authorization`, matched by the new indicator in (3) below, and stays accurate for both the never-authorized and token-expired cases.
2. **Abort text**: the non-interactive abort `RuntimeError` (`mcp_oauth.py:589-592`) is reworded to include `token expired` ("non-interactive OAuth redirect — token expired, needs manual authorization") so any path surfacing the raw abort also matches detection.
3. **Indicator extension**: `_is_mcp_auth_failure` adds `needs authorization` to its indicator list (catches (1)'s text redundantly and any future wording drift).

With (1)–(3), a background auth failure always lands in `mark_needs_auth` + the "Run `/mcp auth`" translation — the server shows 🔐 (needs_auth) in `/mcp list`, never a silent zombie. *Homed in `mcp-oauth-flow`'s delta (watch-item F2); `mcp-transport`'s delta covers only the wrapper-state consequence.*

### D5 — Atomic re-registration (fix E)

`_register_wrapper` purges the server's existing `_tool_to_server` entries under the lock before inserting the new wrapper's tools (mirroring `revoke_server`'s cleanup, `mcp_client.py:822-823`); `_connect_server` closes the replaced wrapper the same way `_register_wrapper` already does. Cross-server conflicts (a tool name claimed by a *different* server) still warn as today.

Dynamic view — background tool call after the fix:

```
 scheduled job -> react_loop -> MCPManager.call_tool
   -> wrapper (non-interactive provider)
      -> token valid?  yes -> tool executes                         [steady state]
      -> expired, RT alive (re-attached via D2):
           SDK refresh grant -> set_tokens merge (D1) -> retry      [silent, forever]
      -> expired, RT dead (revoked/expired at Google):
           SDK full-flow attempt -> redirect handler sees
           chat_id=None -> on_non_interactive -> needs_auth=True,
           callback server stopped, RuntimeError (D4.2)
   -> call_tool fast-fail (D4.1) or error text (D4.2)
   -> react_loop _is_mcp_auth_failure (D4.3) -> mark_needs_auth
   -> job sees "Run /mcp auth <name>" error; /mcp list shows needs_auth
```

## Risks / Trade-offs

- [SDK upgrade changes `_handle_refresh_response` signature/behavior] -> The override is additive and calls `super()`; if the SDK later fixes the replace semantics natively, the override becomes a no-op passthrough. Test with a contract test asserting `can_refresh_token()` stays true after refresh; SDK pin review at upgrade time.
- [Merge-preserve resurrects a revoked refresh token] -> A revoked RT fails the refresh grant with 400; the SDK clears tokens (`oauth2.py:456-458`) and the flow degrades via D3/D4 — no worse than today, strictly better than losing the token silently.
- [D3 adds a second session bring-up after `/mcp auth`] -> New cost relative to today (which registers the flow's wrapper directly, no reconnect); bounded to one ordinary session initialize + tools/list with the now-valid stored token — no handshake, no probe re-run (probe suppression unchanged). The interactive flow session is torn down in the same `finally` structure that today guards abandoned tasks, so nothing leaks.
- [`needs authorization` indicator false-positives on unrelated errors] -> Gated by `server_has_oauth` (existing check in `_is_mcp_auth_failure`), so only OAuth-configured servers can match.
- [One-time manual re-auth for already-wiped token files] -> Documented in proposal; accepted.

## Migration Plan

1. Deploy with daemon restart (no config/schema changes; token file format unchanged — merge-preserve only adds a field back).
2. For any server whose stored token lacks `refresh_token` (already wiped), run `/mcp auth <name>` once.
3. Rollback: revert the commit; token files written by the new code remain valid (same schema).

## Open Questions

- None blocking. Out-of-scope follow-ups recorded in the proposal (stale-contextvar logging; 30s/300s mismatch mooted by D3).
- No in-force ADR requires revisiting: ADR-0019 (XDG token-file location) is untouched; the new provider subclass and merge semantics will be recorded as a new ADR in the adr step.