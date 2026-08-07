## 1. Probe step in `_run_oauth_flow`

- [ ] 1.1 Add a `_probe_oauth_challenge` async helper method to `MCPManager` (or `_SdkClientWrapper`) that makes a standalone `httpx.AsyncClient(auth=provider, timeout=oauth_cfg.get("timeout", 300))` GET request to the server URL with `MCP-Protocol-Version: 2025-11-25` header, using an httpx `event_hooks={"response": [...]}` callback to record whether any 401 or 403 was observed (`probe_saw_auth_challenge: bool`). Return the flag and the final response status. The timeout covers discovery/registration/token-exchange socket operations (D4); the operator's browser-authorization wait is bounded separately by `wait_for_callback(timeout=300)`.
- [ ] 1.2 In `_run_oauth_flow` (`mcp_client.py:907`), insert the probe step between `OAuthProviderFactory.build()` (line 926) and `wrapper._session_runner()` (line 943). Confirm the callback server is already listening (`cb_server.start()` at line 924) before the probe fires — the probe triggers `redirect_handler` → `callback_handler` which blocks on `wait_for_callback()`, requiring a live listener. Wrap the probe in `asyncio.create_task` and race it against `_watch_cancel` using `asyncio.wait({probe_task, cancel_task}, return_when=FIRST_COMPLETED)`. On cancel, cancel the probe task and return `{"success": False, "error": "Cancelled by operator"}`. On probe exception (non-401/200/403 status surfacing as httpx error, or callback timeout), log the error at WARNING and proceed to session connection as a fallback (the session's own `initialize` may trigger a 401 — design Risk D2/fallback).
- [ ] 1.3 After the probe completes, log the outcome at INFO: if `probe_saw_auth_challenge` is True, log "MCP [<name>] probe triggered OAuth handshake (status=<final_status>)"; if False, log "MCP [<name>] probe returned <status> — server did not require OAuth".
- [ ] 1.4 Store `probe_saw_auth_challenge` in a local variable accessible after the session becomes ready (for the warning-conditioning logic in task 2.2).

## 2. Warning conditioning and session connection

- [ ] 2.1 After the session is ready (`ready_task in done`, `mcp_client.py:968`), check `token_file.exists()` as before, but condition the warning on `probe_saw_auth_challenge`: if the probe saw an auth challenge (401/403) but no token file exists, emit the existing WARNING ("OAuth flow returned success but no token file found — redirect_handler may not have fired"); if the probe did not see an auth challenge, log at INFO ("MCP [<name>] server did not require OAuth on probe; connecting without token") instead of warning.
- [ ] 2.2 Verify the session connects normally after the probe — the provider now has a valid token (if 401 → full flow → `set_tokens`) or no token (if 200 → no flow). The `streamablehttp_client(auth=provider)` in `_session_runner` should add the Bearer header if a token exists, or proceed without it if not.

## 3. Logging

- [ ] 3.1 Add INFO log at probe start: "MCP [<name>] proactive OAuth probe starting (url=<server_url>)".
- [ ] 3.2 Add INFO log at probe completion with the event-hook flag and final status (covered by task 1.3).
- [ ] 3.3 Ensure the existing `redirect_handler` INFO log ("MCP [<name>] redirect_handler called") still fires when the SDK calls it during the probe — no changes needed to `mcp_oauth.py`, just verify it appears in logs when the probe triggers a 401.

## 4. Tests

- [ ] 4.1 Add test: probe triggers 401 → `redirect_handler` is called → `callback_handler` completes → token file created → session connects. Use `ScriptedLLM`-style mocking or `unittest.mock` to mock the httpx response chain (401 → redirect → callback → 200 with token). Verify the auth URL is sent via Telegram.
- [ ] 4.2 Add test: probe returns 200 → `redirect_handler` is NOT called → no token file → session connects → INFO log "server did not require OAuth" → no WARNING emitted.
- [ ] 4.3 Add test: probe triggers 401 but callback times out / `redirect_handler` fails → no token file → WARNING "no token file found" is emitted (probe saw auth challenge). Also add a parametrized variant or separate test for 403 insufficient-scope: probe triggers 403 → `probe_saw_auth_challenge` is True → same warning-conditioning behavior as 401.
- [ ] 4.4 Add test: probe returns a non-401/200/403 status (e.g. 500) → no auth challenge seen → WARNING logged for the unexpected status → session connection still attempted as fallback (design Risk mitigation).
- [ ] 4.5 Add test: operator cancels during probe (while `callback_handler` is blocked) → `_oauth_cancel_requested` set → probe task cancelled → flow returns `{"success": False, "error": "Cancelled by operator"}` → callback server closed → port released.
- [ ] 4.6 Update existing `tests/test_mcp_oauth_logging.py`: the warning assertions that currently expect "no token file found" unconditionally must be updated to reflect the probe-conditioned warning behavior. Add a mock probe that returns 200 (no auth challenge) and verify no warning is emitted in that case.
- [ ] 4.7 Run `make check` (ruff + vulture + pytest) and fix any issues.

## 5. Validation

- [ ] 5.1 Run `openspec validate proactive-oauth-401-probe --type change --strict` to verify the change artifacts are well-formed.
- [ ] 5.2 Run `make check` (lint + test) and ensure all pass.