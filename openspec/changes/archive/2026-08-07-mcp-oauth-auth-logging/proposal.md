## Why

MCP OAuth authentication failures are invisible in logs — the current flow has no instrumentation between "flow started" and "flow succeeded/failed", making it impossible to distinguish a genuine token exchange from a false-positive where a non-compliant server accepted unauthenticated discovery. A per-server `trace` flag is needed to dump the full auth sequence on demand for diagnosis.

## What Changes

- Add structured INFO/DEBUG log events throughout the OAuth flow in `mcp_oauth.py` and `mcp_client.py`, covering token storage reads/writes, redirect and callback handler invocations, session initialization, and tool calls
- Add a post-flow token file verification check in `_run_oauth_flow` that warns when the flow reports success but no token was stored (false-positive detection)
- Add an optional `oauth.trace` boolean field per MCP server in `config_schema.py` (`OAuthConfig` dataclass, parsed by `_parse_oauth`); when `true`, key auth events are promoted to INFO level and the full authorization URL is logged (scoped debug without global verbosity). At runtime, `OAuthProviderFactory.build()` reads `trace` from the raw `server_cfg["oauth"]` dict — the typed field is for config validation and documentation, not the runtime read path
- Add a timeout log in `call_tool` (currently none exists) that includes the configured timeout value and current `connected` state for faster triage

## Capabilities

### New Capabilities

_(none — this change adds observability/logging only; no new spec-level capability is introduced. The `oauth.trace` flag is a config-level diagnostic toggle, not a user-facing capability.)_

### Modified Capabilities

_(none — existing OAuth connect/auth/call behaviour is unchanged; this change only adds log emission and a non-fatal post-flow warning)_

## Impact

- `mcp_oauth.py`: `FileTokenStorage.get_tokens`, `set_tokens`, `get_client_info`; `CallbackServer.start`, `_handle`; `make_redirect_handler`
- `mcp_client.py`: `_SdkClientWrapper._prepare_oauth_provider`, `_session_runner`, `call_tool`; `MCPManager._run_oauth_flow`
- `config_schema.py`: `OAuthConfig` dataclass (parsed by `_parse_oauth`) — add `trace: bool = False` field and parse it in `_parse_oauth`
- No public API changes; no new dependencies; no breaking changes
