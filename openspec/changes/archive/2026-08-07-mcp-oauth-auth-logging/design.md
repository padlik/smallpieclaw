## Context

The MCP OAuth flow spans two modules (`mcp_oauth.py`, `mcp_client.py`) and a background asyncio event loop isolated from the Telegram event loop. The current implementation has zero observability between "auth started" and "auth succeeded/failed":

- `FileTokenStorage` reads/writes tokens silently
- `redirect_handler` only logs on failure (missing tg_iface/chat_id); success is invisible
- `session.initialize()` completion is invisible — no way to tell if it was 200 (no auth needed) or 401→OAuth→retry
- `_run_oauth_flow` returns `{"success": True}` when the session becomes ready, but never verifies whether a token was actually stored — creating a false-positive on servers that allow unauthenticated discovery
- Tool call timeouts set `self.last_error` but emit no log record at all — the timeout value and session state are invisible

Investigation of the Gmail MCP failure revealed: the server returns 200 for `initialize()` and `list_tools()` (unauthenticated), so `async_auth_flow` never fires, no token is stored, and `/mcp auth` falsely reports success. Tool calls then stall because the server requires auth for tool execution but the session holds no bearer token. None of this was visible in logs.

The MCP SDK's `OAuthClientProvider` is purely reactive — it triggers OAuth only on HTTP 401. This is correct SDK behaviour; the logging gap is in our wrapper layer.

## Goals / Non-Goals

**Goals:**
- Log every observable lifecycle event in the OAuth auth flow at DEBUG by default
- Promote key events to INFO when `oauth.trace = true` is set for a server (scoped verbosity, no global log flooding)
- Log the full authorization URL only when `trace = true` (it contains client_id and scope — sensitive enough to gate)
- Add post-flow token verification: warn when `_run_oauth_flow` returns success but the token file is absent
- Add a timeout log in `call_tool` with the configured timeout value and `connected` state (currently no log is emitted on timeout)

**Non-Goals:**
- Logging the access_token or client_secret at any level
- Modifying the OAuth flow itself (no behaviour changes)
- Global httpx/SDK debug logging (too noisy; use Python log-level config externally if needed)
- Changing the threading or event-loop architecture

## Decisions

### D1 — Stdlib logging, not structlog directly

`mcp_oauth.py` and `mcp_client.py` already use `logging.getLogger(__name__)`. The project's `setup_logging()` wires stdlib logging into structlog's pipeline, so these loggers reach both sinks (agent.jsonl + agent.log) without any change. Consistent with existing pattern; no new dependency.

### D2 — `trace` flag lives in `OAuthConfig`, parsed by `_parse_oauth`, read at runtime from the raw dict

`config_schema.py` has an `OAuthConfig` dataclass built by `_parse_oauth` (config_schema.py:627-653). Adding `trace: bool = False` to both the dataclass and `_parse_oauth`'s field list is the natural location for config validation and documentation. However, the runtime read path is different: `main.py:464` passes raw TOML dicts (not typed `MCPServerConfig` objects) into `MCPManager`, so `OAuthProviderFactory.build()` reads `oauth_cfg.get("trace", False)` off the **raw** `server_cfg["oauth"]` dict. The typed `OAuthConfig.trace` field is therefore for config validation and documentation only — it is not the runtime read path. Both paths must be kept in sync: the dataclass field + `_parse_oauth` parse for validation, and the raw-dict `.get("trace", False)` in `build()` for runtime.

Alternative considered: a global `mcp_auth_trace = true` top-level config. Rejected — per-server granularity is more useful (you want trace on `gmail` only, not every MCP server).

### D3 — Post-flow token verification is a WARNING, not an error

`_run_oauth_flow` succeeding without a token file is anomalous but not necessarily fatal — a future server might legitimately not require token storage (e.g., session-only tokens). The right signal is a WARNING so operators notice it without the flow being marked as failure. If a server consistently shows this warning, it is a trigger to investigate the server's auth model.

### D4 — Token expiry computed and logged in `get_tokens()`, not in `_prepare_oauth_provider()`

`get_tokens()` already reads the raw token dict (which holds `issued_at`, written at mcp_oauth.py:112). Computing `remaining_seconds = int(issued_at + expires_in - time.time())` there keeps expiry awareness in the storage layer and makes it reusable. `_prepare_oauth_provider()` uses the returned `OAuthToken | None` only to decide `needs_auth`; it logs "stored token found" / "no stored token" without needing the computed remaining seconds (the `OAuthToken` dataclass carries no `remaining` field, so the value is not passed through).

### D5 — Authorization URL logged at INFO only when `trace = true`

The auth URL contains `client_id`, `scope`, `state`, and `redirect_uri`. Logging it at DEBUG unconditionally would expose `client_id` to anyone reading debug logs. Gating on `trace = true` makes the exposure explicit and opt-in, consistent with how other security-sensitive fields are handled in the project.

**Scope of the gate (boundary condition).** The `trace` gate applies to the *new* INFO log line added by this change, on the interactive path. It does **not** apply to the pre-existing WARNING in `make_redirect_handler` that fires when `tg_iface` or `chat_id` is absent (the non-interactive reconnect path, e.g. a stored refresh token rejected during `_prepare_oauth_provider`). That WARNING logs the full URL regardless of `trace`, and is left unchanged deliberately: with no Telegram chat in context, the log is the operator's only channel to complete authorization, so suppressing the URL there would break re-auth recovery in order to satisfy a logging preference. Operators should therefore treat `trace = false` as "no *routine* URL logging", not as a guarantee the URL never reaches the logs. The URL in that WARNING also carries `state` and `code_challenge` in addition to `client_id`/`scope`.

## Risks / Trade-offs

- **Log verbosity at DEBUG level** — The new DEBUG events (session.initialize start/done, list_tools, call_tool entry) will appear in agent.jsonl when the log level is configured to DEBUG. Acceptable: DEBUG is not the default and is already used for other verbose events. Mitigation: ensure messages are concise and carry server name as prefix.

- **`trace = true` exposes auth URL** — Intentional; document it as a diagnostic-only flag not for production use. Mitigation: note in config_schema.py docstring and design.

- **Cross-loop Telegram `send_message` in redirect_handler** — `redirect_handler` runs on the MCP event loop; `bot.send_message()` uses the PTB httpx client bound to the Telegram event loop. This can silently fail. The new INFO log for redirect_handler entry and the existing WARNING for Telegram failure together make this visible. The fix (routing the call back to the Telegram loop) is a separate concern; this change only makes the failure observable.

- **`CallbackServer` logs omit server name** — `CallbackServer.__init__` (mcp_oauth.py:168-183) receives only port/bind/cert/key/loop, not the server name. Its log messages therefore use the prefix `MCP OAuth callback …` instead of `MCP [<server>] …`. This is an intentional constraint of the current constructor, not an oversight. Threading `server_name` into `CallbackServer` would be a separate refactor.

## Migration Plan

Pure additive logging change. No migration needed. Deploy by merging the branch; no config changes required for existing deployments. Operators who want verbose auth tracing add `trace = true` to an `[[mcp_servers]]` oauth block.

## Open Questions

- None currently. The cross-loop Telegram `send_message` issue identified during exploration is observable after this change and warrants a follow-up fix change once confirmed.
