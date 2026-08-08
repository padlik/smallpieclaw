## 1. Probe Method Change

- [x] 1.1 Add `import json` to `mcp_client.py` imports (if not already present)
- [x] 1.2 Remove `_PROBE_TOOL_NAME` constant from `mcp_client.py`. Keep `_PROBE_POST_HEADERS` (reused for both `tools/list` and `tools/call` POSTs).
- [x] 1.3 Modify `_probe_oauth_challenge` in `mcp_client.py`: when GET returns 200 or 405 without an auth challenge (`not probe_saw_auth_challenge and final_status in (200, 405)`), replace the single dummy-tool POST with a two-step discovery: (a) POST `tools/list` JSON-RPC body (`jsonrpc: "2.0"`, `id: str(uuid.uuid4())`, `method: "tools/list"`, `params: {}`) with `_PROBE_POST_HEADERS`, (b) parse the response to extract the first tool name from `result.tools[0].name`. Handle both `application/json` (call `response.json()`) and `text/event-stream` (concatenate `data:` frames, parse JSON) response framings.
- [x] 1.4 After extracting the first tool name, send POST `tools/call` JSON-RPC body (`jsonrpc: "2.0"`, `id: str(uuid.uuid4())`, `method: "tools/call"`, `params: {"name": <first_tool_name>, "arguments": {}}`) with `_PROBE_POST_HEADERS`. Update `final_status` to the `tools/call` response status. The POST uses the same `httpx.AsyncClient` with `auth=provider` and `event_hooks={"response": [_on_response]}` so the event hook fires on the 401.
- [x] 1.5 If `tools/list` returns an empty tool list (no tools in `result.tools`), log a WARNING and skip the `tools/call` POST — fall through to the existing completion logging + session fallback path. Do not send `tools/call` when there are no tools.
- [x] 1.6 If `tools/list` returns 401 (event hook sets `probe_saw_auth_challenge=True`), do not send `tools/call` — the OAuth flow already fired on the `tools/list` 401. Fall through to the existing completion logging (which logs "probe triggered OAuth handshake").
- [x] 1.7 Ensure the INFO/WARNING logging block at the end of `_probe_oauth_challenge` reflects the **final** status — the `tools/call` result when a `tools/call` was issued, the `tools/list` result when `tools/list` returned 401 or empty, not the intermediate GET 200/405. The `final_status` variable must be updated to the last response status before the logging block runs.
- [x] 1.8 Reset `final_status = None` before each POST request (tools/list and tools/call) so a POST exception reports None, not a stale prior status.

## 2. Tests — Two-Step Discovery Probe

- [x] 2.1 Add test: GET returns 200 → POST `tools/list` returns 200 with tools → POST `tools/call` with first tool name returns 401 → `probe_saw_auth_challenge=True`, `final_status=401`, event hook fired. Use `httpx.MockTransport` with a handler that returns 200 on GET, 200 on POST `tools/list` (with tool names in response), and 401 on POST `tools/call`. Verify the INFO log says "probe triggered OAuth handshake (status=401)". Verify the POST `tools/call` body contains the first tool name from the `tools/list` response.
- [x] 2.2 Add test: GET returns 200 → POST `tools/list` returns 200 with tools → POST `tools/call` returns 200 → `probe_saw_auth_challenge=False`, `final_status=200`. Verify the INFO log says "probe returned 200 — server did not require OAuth".
- [x] 2.3 Add test: GET returns 200 → POST `tools/list` returns 200 with empty tool list → WARNING, no `tools/call` sent. Verify `probe_saw_auth_challenge=False`, `final_status=200` (the `tools/list` status). Verify no "server did not require OAuth" INFO (200 is from tools/list, not tools/call — but the server didn't challenge, so the existing completion log fires). Verify only 2 requests made (GET + tools/list, no tools/call).
- [x] 2.4 Add test: GET returns 200 → POST `tools/list` returns 401 → `probe_saw_auth_challenge=True`, OAuth fires on tools/list. Verify no `tools/call` sent (only GET + tools/list). Verify INFO log says "probe triggered OAuth handshake".
- [x] 2.5 Add test: GET returns 401 → no POST issued → existing behavior unchanged. Verify `probe_saw_auth_challenge=True`, `final_status=401`. Verify only 1 request made (GET only).
- [x] 2.6 Add test: GET returns 405 → POST `tools/list` returns 200 with tools → POST `tools/call` with first tool name returns 401 → OAuth fires. Verify the log reflects the final `tools/call` status (401), not the intermediate GET 405. Mirror of test 2.1 but with GET returning 405 instead of 200.
- [x] 2.7 Add test: GET returns 200 → POST `tools/list` returns SSE-framed response (`text/event-stream` with `data:` frames containing JSON-RPC tool list) → probe parses SSE, extracts first tool name → POST `tools/call` returns 401 → OAuth fires. Verify the handler returns `Content-Type: text/event-stream` and the body contains `data: {"jsonrpc":"2.0",...}` frames.
- [x] 2.8 Add test: GET returns 200 → POST `tools/list` returns 500 (non-200, non-401) → WARNING, no `tools/call` sent. Verify `probe_saw_auth_challenge=False`, `final_status=500` (the tools/list status). Verify only 2 requests made (GET + tools/list, no tools/call). Verify WARNING logged.
- [x] 2.9 Add test: GET returns 200 → POST `tools/list` raises `httpx.ConnectError` → `final_status=None`, `error` is not None, WARNING logged. Verify `probe_saw_auth_challenge=False`.
- [x] 2.10 Add test: GET returns 200 → POST `tools/list` returns 200 → POST `tools/call` raises `httpx.ConnectError` → `final_status=None`, `error` is not None, WARNING logged. Verify `probe_saw_auth_challenge=False`.

## 3. Tests — Full Flow Integration

- [x] 3.1 Add test in `test_mcp_oauth_probe.py`: GET 200 → tools/list 200 → tools/call 401 → OAuth fires → token file created → session connects → `{"success": True}`. Patch `_probe_oauth_challenge` to return `(True, 401, None)` and `_session_runner` to write a token file.
- [x] 3.2 Add test: GET 200 → tools/list 200 → tools/call 200 → no OAuth → no token file → session connects → INFO "server did not require OAuth" → no WARNING. Patch `_probe_oauth_challenge` to return `(False, 200, None)`.
- [x] 3.3 Add test: GET 200 → tools/list returns empty → WARNING for no tools → session fallback. Patch `_probe_oauth_challenge` to return `(False, 200, None)`.

## 4. Lint and Validate

- [x] 4.1 Run `ruff check .` and fix any issues in modified files.
- [x] 4.2 Run `vulture . vulture_whitelist.py --min-confidence 80` and update `vulture_whitelist.py` if new public API symbols are flagged.
- [x] 4.3 Run `pytest tests/test_mcp_oauth_probe.py tests/test_mcp_oauth_logging.py -v` and ensure all tests pass.
- [x] 4.4 Run `openspec validate oauth-probe-real-tool --type change --strict` to verify the change artifacts are valid.