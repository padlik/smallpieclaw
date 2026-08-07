## Context

The interactive OAuth flow (`_run_oauth_flow` in `mcp_client.py:907`) connects an MCP session with `auth=provider`, where `provider` is an `OAuthClientProvider` from the MCP SDK. The SDK's `OAuthClientProvider` is an `httpx.Auth` subclass whose `async_auth_flow` generator is lazy: it only enters the OAuth branch when the HTTP response status is 401. If the server returns 200 on `initialize` and `tools/list` (as Google's gmail bridge does), the OAuth handshake never fires, `redirect_handler` is never called, and no authorization URL reaches Telegram.

The current flow:

```
_run_oauth_flow
  ├── build provider + callback server
  ├── streamablehttp_client(auth=provider)
  ├── ClientSession.initialize()  ← 200, no 401, OAuth skipped
  ├── list_tools()                ← 200, no 401, OAuth skipped
  └── ready_future.set_result()   ← "ready" but no token
```

The SDK's `async_auth_flow` (in `mcp/client/auth/oauth2.py:491-605`) drives the full handshake as a side effect of a 401 response: PRM discovery → OASM discovery → client registration → `_perform_authorization()` (which calls `redirect_handler` then `callback_handler`) → token exchange → `storage.set_tokens()` → retry with Bearer header.

The key insight: `OAuthClientProvider` is an `httpx.Auth`, so **any** httpx request with `auth=provider` will trigger `async_auth_flow` on a 401 — not just MCP transport requests. We can make a standalone HTTP probe to the server URL before connecting the session, let the 401 trigger the full OAuth flow, and then connect the session with a valid token already in storage.

## Goals / Non-Goals

**Goals:**
- Ensure `redirect_handler` fires for OAuth-protected MCP servers that allow unauthenticated discovery (return 200 on `initialize`/`tools/list`).
- Reuse the SDK's full OAuth machinery (discovery, registration, PKCE, token exchange, storage) without replicating any of it.
- Preserve existing cancellation, timeout, and callback server behavior.
- Suppress the misleading "no token file found" warning when the probe confirms the server doesn't require OAuth.

**Non-Goals:**
- Runtime detection of whether a plain HTTP server (no `oauth` config) requires OAuth — the config-driven distinction is sufficient.
- Changes to token storage, callback server, redirect handler, or config schema.
- Customizing the SDK's discovery or registration steps.
- Supporting non-standard OAuth flows outside the SDK's auth-code + PKCE pattern.

## Decisions

### D1: Standalone httpx probe before session connection

**Decision:** Insert a standalone `httpx.AsyncClient(auth=provider)` GET request to the server URL between provider construction and `streamablehttp_client` connection.

**Rationale:** The provider is an `httpx.Auth` subclass. Any httpx request with `auth=provider` triggers `async_auth_flow` on a 401. This reuses 100% of the SDK's discovery, registration, PKCE, state validation, token exchange, and storage logic. The alternative — calling the SDK's internal `_perform_authorization()` directly — would require replicating the discovery chain (PRM + OASM + client registration) first, reaching into `_`-prefixed internals, and duplicating ~80 lines of security-critical code.

**Alternatives considered:**
- *Probe via a `tools/call` to a minimal tool* — requires knowing a tool name, which depends on `list_tools` (which may return an empty or partial list when unauthenticated). Fragile and server-specific.
- *Replicate the SDK's discovery chain manually* (Shape B) — reaches deep into SDK internals, duplicates discovery logic, fragile across SDK versions.
- *Do nothing, document the limitation* — leaves the gmail use case broken, which is the primary OAuth MCP server operators configure.

### D2: GET as the probe method

**Decision:** Use `GET` for the probe request.

**Rationale:** The MCP streamable_http transport uses POST for `initialize`, but the probe's purpose is simply to trigger a 401, not to perform an MCP handshake. GET is the simplest HTTP method and sufficient to trigger the 401 response that fires `async_auth_flow`. Some servers might behave differently for GET vs POST on the MCP endpoint, but a 401 on GET is the expected behavior for an OAuth-protected resource regardless of method.

### D3: Hardcode `MCP-Protocol-Version: 2025-11-25` on the probe

**Decision:** The probe request carries the header `MCP-Protocol-Version: 2025-11-25`.

**Rationale:** The SDK reads this header at `oauth2.py:498` to set `context.protocol_version`, which `should_include_resource_param()` (oauth2.py:161-178) uses to decide whether to include the RFC 8707 `resource` parameter in the authorization URL. Without this header, `should_include_resource_param()` returns False (no PRM yet, no protocol version), and the `resource` parameter is omitted — which some servers may require. The value `2025-11-25` matches the latest protocol version the server negotiated in production logs. The probe happens before any MCP negotiation, so we can't discover the version dynamically; hardcoding the latest known version is the pragmatic choice. If a future SDK version changes the protocol version, this constant needs updating — but it's a single value in one place.

### D4: Probe timeout = overall flow timeout

**Decision:** The `httpx.AsyncClient` for the probe uses `timeout=oauth_cfg.get("timeout", 300)` — the same overall flow timeout.

**Rationale:** The httpx client timeout bounds individual socket operations (connect/read/write), not wall-clock across the entire flow. The SDK's `async_auth_flow` makes several HTTP requests during discovery and registration, each needing a reasonable timeout. The operator's browser-authorization wait happens inside `callback_handler` between HTTP requests — there is no open socket during that wait, so the httpx timeout does not govern it. The operator window is enforced separately by `wait_for_callback(timeout=300)` in `CallbackServer` (`mcp_oauth.py:329-331`), which wraps an `asyncio.wait_for` on the callback future. Using the overall flow timeout (300s) for the httpx client ensures the discovery/registration/token-exchange requests don't time out prematurely, while the callback wait has its own timeout.

### D5: Cancellation via `_watch_cancel` race

**Decision:** The probe step runs concurrently with the existing `_watch_cancel` coroutine. If the operator taps Cancel, `_oauth_cancel_requested` is set, `_watch_cancel` completes, and `asyncio.wait` returns — the probe's `httpx.AsyncClient` context is exited via task cancellation.

**Rationale:** The current `_run_oauth_flow` already races `ready_future` against `_watch_cancel`. The probe step replaces the "session becomes ready" signal with "probe completes" (which includes the full OAuth flow). The same `asyncio.wait({probe_task, cancel_task}, return_when=FIRST_COMPLETED)` pattern applies. When the cancel task wins, the probe task is cancelled, the `httpx.AsyncClient` context manager exits cleanly (httpx handles `CancelledError`), and the flow returns the cancellation error.

The operator most often taps Cancel while `callback_handler` is blocked on `wait_for_callback()` (which is `asyncio.wait_for(self._future, timeout=300)` at `mcp_oauth.py:331`). `asyncio.wait_for` is cancellable — `CancelledError` propagates through the `await`, up through `callback_handler`, up through the SDK's `async_auth_flow` generator, and out of the probe task. The `httpx.AsyncClient` context manager then exits via the cancellation. No changes to `CallbackServer` or `wait_for_callback` are needed; the existing `asyncio.Future` + `asyncio.wait_for` pattern is already cancellation-safe.

### D6: Suppress "no token file" warning when server didn't require auth

**Decision:** Use an httpx `event_hooks={"response": [...]}` callback on the probe's `AsyncClient` to record whether **any** authorization-triggering status (401 or 403) was seen during the auth flow. The SDK's `async_auth_flow` enters the OAuth branch on 401 (oauth2.py:514) and the insufficient-scope branch on 403 (oauth2.py:606) — both call `_perform_authorization`, which fires `redirect_handler`. After the session is ready, use this flag (not the final response status) to decide whether to emit the "no token file found" warning:

- **Auth challenge seen (401 or 403) + no token file** → emit WARNING (OAuth flow fired but `redirect_handler` failed or callback timed out — genuine error).
- **No auth challenge seen + no token file** → log at INFO: "server did not require OAuth on probe; connecting without token" (server allowed unauthenticated access — no token expected).

**Rationale:** `OAuthClientProvider` is an `httpx.Auth`. When `client.get()` runs with `auth=provider`, httpx drives `async_auth_flow` internally. The intermediate 401 is consumed *inside* the auth-flow generator; the returned `response` object is the **final** response after the auth retry (with Bearer token), so its status is not 401. httpx `.history` covers redirects, not auth retries. Therefore `response.status_code == 401` is always `False` — the returned status is the post-retry result.

An httpx response event hook fires on **every** response that passes through the client, including the intermediate 401 or 403 that triggers the auth flow. The hook can record `probe_saw_auth_challenge = True` when it observes a 401 or 403, giving us an observable signal that the auth flow was attempted. This requires no changes to `redirect_handler`, `callback_handler`, or `FileTokenStorage` — it's a pure client-side observation.

Token-file existence alone cannot distinguish the three outcomes:
- 200, no auth needed → no token file (correct, no warning)
- 401/403, OAuth succeeded → token file exists (no warning needed)
- 401/403, OAuth flow fired but failed → no token file (warning appropriate)

The event-hook flag separates case 1 from case 3; token-file existence separates case 2 from cases 1+3. Together they fully discriminate.

### D7: Probe runs inside `_run_oauth_flow`, not in `_session_runner`

**Decision:** The probe is added to `_run_oauth_flow` (the interactive path), not to `_session_runner` (the connection path used by both interactive and non-interactive flows).

**Rationale:** `_session_runner` is used by both the non-interactive startup path (where `_prepare_oauth_provider` checks for stored tokens and marks `needs_auth` if none exist) and the interactive path (where `_run_oauth_flow` sets `_interactive=True` and skips `_prepare_oauth_provider`). The probe is only needed for the interactive flow — the non-interactive path correctly marks `needs_auth` and waits for the operator to trigger `/mcp auth`. Adding the probe to `_session_runner` would make every connection attempt (including startup with a valid token) do an unnecessary unauthenticated request.

## Risks / Trade-offs

- **[Server returns non-401/non-200 on probe]** → The SDK's `async_auth_flow` only enters the OAuth branch on exactly 401. A 403 triggers the insufficient-scope branch (oauth2.py:606-627), which also calls `_perform_authorization`. Other status codes (5xx, 302) are not handled by the auth flow and would surface as httpx errors. Additionally, a GET against an MCP streamable-http endpoint (which expects POST) may return 405/406 **after** a successful authenticated retry — the final response status is not a reliable signal. Mitigation: use the event-hook flag (D6) to determine whether the auth flow was attempted, not the final response status. If the event hook saw a 401 or 403, the OAuth flow fired regardless of the final status. If no auth challenge was seen and the final status is not 200, log the status at WARNING and proceed to session connection (the session's own `initialize` may trigger a 401 as a fallback).

- **[Server doesn't 401 on GET but does on POST]** → Some servers might only protect POST requests (the MCP transport method). A GET probe would get 200, no OAuth fires, and the session would fail on `initialize` with a 401 that the SDK's auth flow would then handle — but by that point we've already passed the probe step and the `redirect_handler` might fire during `session.initialize()` instead. This is actually acceptable: the OAuth flow would fire during the session connection, the link would be sent, and the callback would complete. The probe is a best-effort trigger; if it doesn't work, the session connection provides a second chance. Mitigation: none needed — the session connection is the fallback.

- **[Protocol version header mismatch]** → If the server expects a different protocol version than `2025-11-25`, the `resource` parameter inclusion might be wrong. Mitigation: the `resource` parameter is a best-effort RFC 8707 enhancement; its absence doesn't break the flow, it just means the server might not include resource indicators in the token. The hardcoded value can be updated when the protocol version changes.

- **[Probe adds latency to the interactive flow]** → The probe is one extra HTTP round-trip before the session connects. For a server that returns 401, this adds ~100-500ms. For a server that returns 200, the probe is wasted but fast. Mitigation: acceptable for an interactive flow that already takes seconds.

- **[httpx.AsyncClient created outside the SDK's transport]** → The probe uses a separate `httpx.AsyncClient` from the one the MCP transport uses. The SDK's `async_auth_flow` runs within this client's auth flow, so discovery and registration requests go through the probe client, not the transport. This is fine — the auth flow only needs an HTTP client to make requests; it doesn't depend on the MCP transport. The tokens are persisted to `storage` regardless of which client triggered the flow.

## Migration Plan

No migration needed. The change is purely additive to the interactive OAuth flow. Existing stored tokens continue to work (the non-interactive path is unchanged). Operators who previously saw the "no token file found" warning will now see the authorization link sent to Telegram.

**Rollback:** Revert the probe step in `_run_oauth_flow`. The flow returns to the previous behavior (relying on 401 during `session.initialize()`), which works for servers that challenge on `initialize` but not for servers that allow unauthenticated discovery.

## Open Questions

None. All open questions from the explore-brief are resolved by the decisions above:
- Q1 (protocol version) → D3: hardcode `2025-11-25`
- Q2 (probe method) → D2: GET
- Q3 (probe timeout) → D4: overall flow timeout
- Q4 (warning suppression) → D6: suppress when no auth challenge seen, warn when auth challenge seen but no token