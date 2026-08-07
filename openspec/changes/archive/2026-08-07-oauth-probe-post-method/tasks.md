## 1. Probe Method Change

- [x] 1.1 Add `import uuid` to `mcp_client.py` imports (if not already present)
- [x] 1.2 Add module-level constants for the POST probe: `_PROBE_TOOL_NAME = "_oauth_probe"`, `_PROBE_JSONRPC_ID` generation helper (or inline `str(uuid.uuid4())`), and the POST probe headers dict (`Accept: application/json, text/event-stream`, `Content-Type: application/json`)
- [x] 1.3 Modify `_probe_oauth_challenge` in `mcp_client.py` (~line 913-995): after the GET request, if `response.status_code == 405`, issue a POST request to the same `server_url` with the JSON-RPC `tools/call` body (`jsonrpc: "2.0"`, `id: str(uuid.uuid4())`, `method: "tools/call"`, `params: {"name": "_oauth_probe", "arguments": {}}`) and the MCP transport headers (`Accept`, `Content-Type`, `MCP-Protocol-Version`). Update `final_status` to the POST response status. The POST request uses the same `httpx.AsyncClient` with `auth=provider` and `event_hooks={"response": [_on_response]}` so the event hook fires on the 401.
- [x] 1.4 Ensure the INFO/WARNING logging block at the end of `_probe_oauth_challenge` (lines 985-994) reflects the **final** status — the POST result when a POST retry occurred, not the intermediate GET 405. The `final_status` variable must be updated to the POST response status before the logging block runs.
- [x] 1.5 Verify the POST 405 case flows through the existing `_run_probe_step` WARNING path (lines 1082-1099) via the returned `final_status=405` — no new duplicate WARNING inside `_probe_oauth_challenge`.

## 2. Tests — POST Retry Path

- [x] 2.1 Add test: GET returns 405 → POST returns 401 → `probe_saw_auth_challenge=True`, `final_status=401`, event hook fired. Use `httpx.MockTransport` with a handler that returns 405 on GET and 401 on POST. Verify the INFO log says "probe triggered OAuth handshake (status=401)".
- [x] 2.2 Add test: GET returns 405 → POST returns 200 → `probe_saw_auth_challenge=False`, `final_status=200`. Verify the INFO log says "probe returned 200 — server did not require OAuth" (not 405).
- [x] 2.3 Add test: GET returns 405 → POST returns 405 → `probe_saw_auth_challenge=False`, `final_status=405`. Verify no "server did not require OAuth" INFO (405 is not 200); the WARNING for unexpected status is emitted by `_run_probe_step`, not duplicated in `_probe_oauth_challenge`.
- [x] 2.4 Add test: GET returns 200 → no POST issued → existing behavior unchanged. Verify the POST handler is never called (assert request count or mock).
- [x] 2.5 Add test: GET returns 401 → no POST issued → existing behavior unchanged. Verify `probe_saw_auth_challenge=True`, `final_status=401`.

## 3. Tests — Full Flow Integration

- [x] 3.1 Add test in `test_mcp_oauth_probe.py`: GET 405 → POST 401 → OAuth fires → token file created → session connects → `{"success": True}`. Patch `_probe_oauth_challenge` to return `(True, 401, None)` and `_session_runner` to write a token file.
- [x] 3.2 Add test: GET 405 → POST 200 → no OAuth → no token file → session connects → INFO "server did not require OAuth" → no WARNING. Patch `_probe_oauth_challenge` to return `(False, 200, None)`.
- [x] 3.3 Add test: GET 405 → POST 405 → WARNING for unexpected status → session fallback. Patch `_probe_oauth_challenge` to return `(False, 405, None)`.

## 4. Lint and Validate

- [x] 4.1 Run `ruff check .` and fix any issues in modified files.
- [x] 4.2 Run `vulture . vulture_whitelist.py --min-confidence 80` and update `vulture_whitelist.py` if new public API symbols are flagged.
- [x] 4.3 Run `pytest tests/test_mcp_oauth_probe.py tests/test_mcp_oauth_logging.py -v` and ensure all tests pass.
- [x] 4.4 Run `openspec validate oauth-probe-post-method --type change --strict` to verify the change artifacts are valid.