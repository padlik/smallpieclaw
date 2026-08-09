## MODIFIED Requirements

### Requirement: File-backed token storage

The system SHALL store OAuth tokens per-server in JSON files resolved via `xdg_paths(agent_name).mcp_tokens_dir` (the `$XDG_STATE_HOME/<agent_name>/mcp_tokens/` directory), with `0600` file permissions. The storage SHALL implement the MCP SDK's `TokenStorage` protocol.

The `FileTokenStorage` SHALL accept a `token_endpoint_auth_method` parameter (defaulting to `"client_secret_basic"`) so the pre-seeded `OAuthClientInformationFull` carries the auth method the SDK's `prepare_token_auth()` expects.

The `get_client_info()` method SHALL set `token_endpoint_auth_method` on its pre-seed return path: the constructed `OAuthClientInformationFull` SHALL include `token_endpoint_auth_method` set to the storage's configured value. When `get_client_info()` returns a persisted block — which it does only when that block's `client_secret` matches the configured one — it SHALL return it unmodified; on a `client_secret` mismatch the configured credentials take precedence and the pre-seed is returned instead, so a secret rotation takes effect.

When a persisted `client_info` block cannot be parsed, `get_client_info()` SHALL log a warning and fall back to the pre-seed rather than raising, so a corrupt or schema-drifted block does not prevent connecting to the server.

#### Scenario: Token storage path resolution
- **GIVEN** the agent is running with `--agent-name <name>`
- **WHEN** the `FileTokenStorage` resolves the token directory
- **THEN** the directory is `$XDG_STATE_HOME/<name>/mcp_tokens/` as resolved by `xdg_paths()`, not a hardcoded relative path

#### Scenario: Token file created with restrictive permissions
- **GIVEN** an OAuth flow completes successfully for server `gmail`
- **WHEN** the tokens are stored
- **THEN** a file `$XDG_STATE_HOME/<name>/mcp_tokens/gmail.json` is created with `0600` permissions containing a `token` object carrying the fields the provider returned (`access_token` and `token_type`, plus `refresh_token`, `scope`, and `expires_in` when granted) and an `issued_at` stamp
- **AND** a `client_info` block is present only if one was already persisted, since a normal flow never writes one

#### Scenario: Token storage directory creation
- **GIVEN** the `mcp_tokens_dir` does not exist on first boot
- **WHEN** the agent starts and `_create_xdg_dirs` runs in `main.py`
- **THEN** the `mcp_tokens` directory is created under `$XDG_STATE_HOME/<name>/` alongside other state directories

#### Scenario: Pre-seeded client info from config includes token_endpoint_auth_method
- **GIVEN** an MCP server is configured with `oauth.client_id` and `oauth.client_secret` and has no stored token file
- **WHEN** `FileTokenStorage.get_client_info()` is called
- **THEN** it returns an `OAuthClientInformationFull` constructed from the config values (`client_id`, `client_secret`) with `token_endpoint_auth_method` set to the storage's configured value (defaulting to `"client_secret_basic"`)
- **AND** the SDK skips Dynamic Client Registration
- **AND** the SDK's `prepare_token_auth()` sends `client_secret` via HTTP Basic auth header in the token exchange request

#### Scenario: Malformed cached client info falls back to the pre-seed
- **GIVEN** an MCP server has a stored token file whose `client_info` block cannot be parsed into an `OAuthClientInformationFull` (e.g. hand-edited, or written by an older schema, so a required field such as `redirect_uris` is absent)
- **WHEN** `FileTokenStorage.get_client_info()` is called
- **THEN** a warning naming the server is logged and no exception propagates
- **AND** the returned `OAuthClientInformationFull` is the pre-seed built from config, carrying `token_endpoint_auth_method`
- **AND** the connection attempt proceeds instead of failing

#### Scenario: Token revocation
- **GIVEN** a server `gmail` has stored tokens
- **WHEN** the operator runs `/mcp auth revoke gmail`
- **THEN** the token file `$XDG_STATE_HOME/<name>/mcp_tokens/gmail.json` is deleted, the server's status changes to `needs_auth`, and its tools are unregistered