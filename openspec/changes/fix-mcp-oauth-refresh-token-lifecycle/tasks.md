## 1. Token storage merge-preserve (design D1 / ADR-0029)

- [x] 1.1 Add failing tests in `tests/test_mcp_oauth.py` (TestFileTokenStorage area): persist a token WITH `refresh_token=R1`, then `set_tokens` with a token whose `refresh_token` is None (refresh-grant shape) → file retains `refresh_token=R1` while `access_token`/`issued_at`/`scope` update; persist `refresh_token=R2` → file shows `R2` (rotation/re-auth overwrite); no stored RT + incoming None → file still has none (never invented)
- [x] 1.2 Implement merge-preserve in `FileTokenStorage.set_tokens` (`mcp_oauth.py:137-147`): when incoming `refresh_token` is None and the existing file has one, carry the stored value into the new `token` payload; incoming `refresh_token` always wins; `client_info` preservation and atomic write unchanged
- [x] 1.3 Run `pytest tests/test_mcp_oauth.py -v` — new + existing storage tests pass; `ruff check .` and `vulture` clean

## 2. Refresh-preserving provider subclass (design D2 / ADR-0029)

- [x] 2.1 Add failing contract tests in `tests/test_mcp_oauth.py` (mock/fake the refresh response): successful refresh with an RT-less response → `can_refresh_token()` stays True and `current_tokens.refresh_token` re-attached; failed refresh (super returns False, tokens cleared) → no re-attach, cleared state preserved, no exception, return value passed through; and a two-cycle successive-refresh test (the defect-B regression mirroring the delta scenario's 6h-cadence case): two consecutive silent refreshes → the RT survives on disk AND in memory after BOTH (relies on group 1's merge for the disk copy)
- [x] 2.2 Implement `RefreshPreservingOAuthProvider(OAuthClientProvider)` in `mcp_oauth.py` overriding `_handle_refresh_response` with the success-only gate: capture pre-refresh `current_tokens.refresh_token` (guard None) → `result = await super()._handle_refresh_response(response)` → re-attach the captured RT (model copy) ONLY when `result is True` AND `current_tokens is not None` AND its `refresh_token is None` → return `result` unchanged; failure path is a strict no-op
- [x] 2.3 Wire `OAuthProviderFactory.build` (`mcp_oauth.py:409-479`) to construct the subclass so both interactive and non-interactive sites get it; run `pytest tests/test_mcp_oauth.py -v` — contract tests from 2.1 pass

## 3. Non-interactive long-lived session (design D3 / ADR-0030)

- [x] 3.1 Add failing tests in `tests/test_mcp_client.py` (extending the `start_oauth_flow`/probe harness): after a simulated successful flow, the REGISTERED wrapper's provider has no chat binding and carries the non-interactive callback; the interactive flow wrapper is closed (its session task cancelled); the registered wrapper's session came up with the stored token (no handshake fired); the `finally` block does not cancel the live non-interactive runner
- [x] 3.2 Implement in `_run_oauth_flow` (`mcp_client.py:1826-1935`): build the non-interactive provider (same shape as `_prepare_oauth_provider`: `chat_id=None`, `on_non_interactive` → `needs_auth` + ready-future resolution) up front; after `_verify_token_persisted` passes, close the interactive flow wrapper, construct a fresh wrapper with the non-interactive provider, bring up its session (second bring-up), hand off ITS task (`session_task = None`), register via `_register_wrapper`; restructure the `finally` to track the two session tasks so only abandoned ones are cancelled and `cb_server.stop()` still runs
- [x] 3.3 Run `pytest tests/test_mcp_client.py tests/test_mcp_oauth_probe.py -v` — new lifecycle tests pass; existing probe/flow/cancel tests unchanged (probe suppression, cancel, single-flow lock intact)

## 4. Background auth-failure handshake (design D4)

- [x] 4.1 Add failing tests: a `needs_auth` wrapper's `call_tool` fast-fails with "needs authorization. Run /mcp auth <name>." BEFORE the generic not-connected error (`tests/test_mcp_client.py`); `_is_mcp_auth_failure` matches "needs authorization" and the reworded non-interactive abort text for an OAuth-configured server, and the translated outcome marks `needs_auth` + returns the recovery hint (`tests/test_react_loop.py`, extend TestAuthFailure area)
- [x] 4.2 Implement: `needs_auth` check before the `connected` check in `call_tool` (`mcp_client.py:418-423`); reword the non-interactive abort `RuntimeError` (`mcp_oauth.py:589-592`) to "non-interactive OAuth redirect — token expired, needs manual authorization"; add `"needs authorization"` to the indicator list in `_is_mcp_auth_failure` (`react_loop.py:121`)
- [x] 4.3 Add the end-to-end background dead-RT integration test (spans groups 3–4; delta scenarios "Background tool call never delivers auth buttons" and "Auth-related session death leaves the server needs_auth"): with a registered non-interactive wrapper (group 3) and a dead refresh token, a background tool call fires the SDK flow → assert NO `send_oauth_prompt` call (mock Telegram iface), the wrapper ends `needs_auth`/`connected=False`, status queries report `needs_auth`, and a subsequent call fast-fails with the recovery hint
- [x] 4.4 Run `pytest tests/test_react_loop.py tests/test_mcp_client.py -v` — new + existing auth-failure tests pass

## 5. Atomic re-registration (design D5)

- [x] 5.1 Add failing tests in `tests/test_mcp_client.py`: `_register_wrapper` with pre-existing same-server map entries produces NO conflict warnings and no duplicate entries (simulate the 14-tool re-auth); `_connect_server` closes the replaced wrapper; a tool name claimed by a DIFFERENT server still logs a conflict warning
- [x] 5.2 Implement: purge the server's `_tool_to_server` entries under the lock before inserting new tools in `_register_wrapper` (`mcp_client.py:1810-1824`, mirror `revoke_server`'s cleanup at `:822-823`); close the replaced wrapper in `_connect_server` (`mcp_client.py:888-899`) as `_register_wrapper` already does
- [x] 5.3 Run `pytest tests/test_mcp_client.py -v`

## 6. End-to-end verification

- [x] 6.1 Run `make check` (ruff + vulture + full suite) — all pass; no new skips vs the baseline (2097 passed, 1 platform-specific skip); verify regression scenarios from the production incident: consecutive silent refreshes keep the RT on disk and in memory, background dead-RT flow yields needs_auth + hint (covered by groups 1–5 tests)
- [x] 6.2 Run `openspec validate fix-mcp-oauth-refresh-token-lifecycle --type change --strict` — passes