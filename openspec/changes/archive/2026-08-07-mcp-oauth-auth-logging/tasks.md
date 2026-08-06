# Tasks

- [x] T1 — Add `trace` field to `OAuthConfig` in `config_schema.py`

## T1 — Add `trace` field to `OAuthConfig` in `config_schema.py`

Add `trace: bool = False` to the `OAuthConfig` dataclass (built by `_parse_oauth` at config_schema.py:627-653). Add `trace=_parse_bool(oauth.get("trace", False), f"{section}.trace")` to the `_parse_oauth` field list so the typed field is actually populated from TOML. Add a docstring note that this is a diagnostic-only flag that promotes auth events to INFO and logs the full authorization URL; not for production use.

**Note:** At runtime, `OAuthProviderFactory.build()` reads `trace` from the **raw** `server_cfg["oauth"]` dict via `oauth_cfg.get("trace", False)`, not from the typed `OAuthConfig` field (because `main.py:464` passes raw TOML dicts into `MCPManager`). The typed field is for config validation and documentation only. Both paths must stay in sync.

Files: `config_schema.py`

---

- [x] T2 — Add logging to `FileTokenStorage` in `mcp_oauth.py`

## T2 — Add logging to `FileTokenStorage` in `mcp_oauth.py`

### T2a — `get_tokens()`
After successfully loading a token, log at DEBUG:
```
MCP [<server>] token: found (scope=<scope>, has_refresh=<T/F>, remaining=<N>s)
```
Compute `remaining` as `int(issued_at + expires_in - time.time())` when both fields are present; otherwise log `remaining=unknown`. If no file exists, log at DEBUG: `MCP [<server>] token: no file on disk`.

### T2b — `set_tokens()`
After `_atomic_write`, log at INFO:
```
MCP [<server>] tokens written to disk (scope=<scope>, has_refresh=<T/F>)
```

### T2c — `get_client_info()`
Log at DEBUG when returning cached client_info: `MCP [<server>] client_info: using cached`.
Log at DEBUG when returning pre-seeded: `MCP [<server>] client_info: using pre-seeded credentials`.

Files: `mcp_oauth.py`

---

- [x] T3 — Add logging to `CallbackServer` in `mcp_oauth.py`

## T3 — Add logging to `CallbackServer` in `mcp_oauth.py`

### T3a — `start()`
After server starts (i.e., after `asyncio.start_server` succeeds), log at INFO:
```
MCP OAuth callback server started on <bind>:<port>
```

### T3b — `_handle()`
After successfully setting the future result (code received, state validated), log at INFO:
```
MCP OAuth callback received (code=yes, state_match=<T/F/n/a>)
```
Use `state_match=n/a` when `self.expected_state is None` (no validation was performed). Log at DEBUG (not WARNING) for malformed requests and state mismatches (they already return 400; DEBUG is enough).

Files: `mcp_oauth.py`

---

- [x] T4 — Add logging to `make_redirect_handler` in `mcp_oauth.py`

## T4 — Add logging to `make_redirect_handler` in `mcp_oauth.py`

At the entry of `_handler`, before `cb_server.start()`:
- Log at INFO: `MCP [<server>] redirect_handler called (state=<first 8 chars>...)`
- If `trace` is True (pass `trace` parameter into `make_redirect_handler`), additionally log at INFO: `MCP [<server>] auth URL: <full_url>`

After `bot.send_message()` succeeds, log at INFO:
```
MCP [<server>] auth URL sent via Telegram (chat=<chat_id>)
```

Update `make_redirect_handler` signature to accept `trace: bool = False`. Update `OAuthProviderFactory.build()` to pass `oauth_cfg.get("trace", False)` to `make_redirect_handler`.

Files: `mcp_oauth.py`

---

- [x] T5 — Add session lifecycle logging to `mcp_client.py`

## T5 — Add session lifecycle logging to `mcp_client.py`

### T5a — `_prepare_oauth_provider()`
After reading existing token:
- If token found: `logger.info("MCP [%s] stored token found; connecting with existing token", self.name)`
- If no token: `logger.info("MCP [%s] no stored token; marking needs_auth", self.name)`

### T5b — `_session_runner()`
Add at DEBUG:
- Before `initialize()`: `MCP [<name>] session.initialize() start`
- After `initialize()` returns: `MCP [<name>] session.initialize() done`
- After `list_tools()` loop completes: `MCP [<name>] list_tools: <N> tools discovered`

### T5c — `call_tool()`
At DEBUG before enqueuing the request:
```
MCP [<name>] calling tool '<tool_name>'
```
In the `TimeoutError` handler (the exception caught is `concurrent.futures.TimeoutError` at mcp_client.py:242, which is aliased as builtin `TimeoutError` in Python 3.11+), **add** a `logger.error` (there is currently no log emitted on timeout — the handler only sets `self.last_error` and returns):
```
MCP [<name>] tool '<tool_name>' timed out after <timeout>s (connected=<T/F>)
```

Files: `mcp_client.py`

---

- [x] T6 — Add OAuth flow lifecycle logging and post-flow token verification to `MCPManager._run_oauth_flow`

## T6 — Add OAuth flow lifecycle logging and post-flow token verification to `MCPManager._run_oauth_flow`

### T6a — Flow start
At the top of `_run_oauth_flow`, log at INFO:
```
MCP [<name>] interactive OAuth flow starting
```

### T6b — Session ready
After `ready_task` completes without exception, before checking `wrapper.needs_auth`, log at INFO:
```
MCP [<name>] session ready; verifying token storage
```

### T6c — Post-flow token check
After `ready_task` succeeds and `wrapper.needs_auth` is False, check whether the token file exists:
```python
token_file = self._mcp_tokens_dir / f"{name}.json"
if not token_file.exists():
    logger.warning(
        "MCP [%s] OAuth flow returned success but no token file found — "
        "redirect_handler may not have fired (server may allow unauthenticated discovery)",
        name,
    )
```
This does NOT change the return value — the flow still returns `{"success": True}` — but makes the false-positive visible.

Files: `mcp_client.py`

---

- [x] T7 — Update `vulture_whitelist.py` if needed

## T7 — Update `vulture_whitelist.py` if needed

After implementation, run `make lint`. If vulture flags `trace` as unused (e.g., in `OAuthConfig`), add it to `vulture_whitelist.py`.

Files: `vulture_whitelist.py` (conditional)

---

- [x] T8 — Run `make check` and add targeted tests

## T8 — Run `make check` and add targeted tests

### T8a — Add two targeted tests

Despite the change being primarily additive logging, two new branches introduce observable behavior worth locking in:

1. **False-positive WARNING test**: Verify that when `_run_oauth_flow` returns `{"success": True}` but no token file exists, the WARNING log is emitted. Use `caplog` to assert the warning message contains "no token file found".

2. **Trace-gated auth URL test**: Verify that the full authorization URL is logged at INFO only when `trace = true`, and is NOT logged when `trace = false`. Use `caplog` with two runs (trace on/off) and assert the auth URL line appears in one but not the other.

Files: `tests/test_mcp_oauth_logging.py` (new file)

### T8b — Run `make check`

Run `make check` (ruff + vulture + pytest). Confirm:
- No new ruff violations
- No new vulture false positives (or whitelist added in T7)
- All existing tests pass
- The two new tests pass
