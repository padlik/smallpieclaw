## MODIFIED Requirements

### Requirement: File-backed token storage

The system SHALL store OAuth tokens per-server in JSON files resolved via `xdg_paths(agent_name).mcp_tokens_dir` (the `$XDG_STATE_HOME/<agent_name>/mcp_tokens/` directory), with `0600` file permissions. The storage SHALL implement the MCP SDK's `TokenStorage` protocol.

The `FileTokenStorage` SHALL accept a `token_endpoint_auth_method` parameter (defaulting to `"client_secret_basic"`) so the pre-seeded `OAuthClientInformationFull` carries the auth method the SDK's `prepare_token_auth()` expects. The `OAuthProviderFactory.build()` SHALL source this parameter from `client_metadata.token_endpoint_auth_method` to prevent drift between the two objects.

The `get_client_info()` method SHALL normalize `token_endpoint_auth_method` on both return paths:
- **Pre-seed path**: the constructed `OAuthClientInformationFull` SHALL include `token_endpoint_auth_method` set to the storage's configured value.
- **Cached path**: when a cached `client_info` block exists and the configured `client_secret` matches, if the cached block's `token_endpoint_auth_method` is `None`, it SHALL be filled with the storage's configured value. A non-`None` cached value SHALL be preserved (e.g. a method set by a real Dynamic Client Registration response for a non-Google provider).

#### Scenario: Token storage path resolution
- **GIVEN** the agent is running with `--agent-name <name>`
- **WHEN** the `FileTokenStorage` resolves the token directory
- **THEN** the directory is `$XDG_STATE_HOME/<name>/mcp_tokens/` as resolved by `xdg_paths()`, not a hardcoded relative path

#### Scenario: Token file created with restrictive permissions
- **GIVEN** an OAuth flow completes successfully for server `gmail`
- **WHEN** the tokens are stored
- **THEN** a file `$XDG_STATE_HOME/<name>/mcp_tokens/gmail.json` is created with `0600` permissions containing `access_token`, `refresh_token`, `expires_at`, `scope`, and `client_info`

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

#### Scenario: Cached client info with missing token_endpoint_auth_method is repaired
- **GIVEN** an MCP server has a stored token file with a cached `client_info` block whose `token_endpoint_auth_method` is `None` (e.g. persisted with `exclude_none=True`)
- **AND** the cached `client_secret` matches the currently configured `client_secret`
- **WHEN** `FileTokenStorage.get_client_info()` is called
- **THEN** the returned `OAuthClientInformationFull` has `token_endpoint_auth_method` filled with the storage's configured value
- **AND** the SDK's `prepare_token_auth()` sends `client_secret` in the token exchange request

#### Scenario: Cached client info with DCR-provided auth method is preserved
- **GIVEN** an MCP server has a stored token file with a cached `client_info` block whose `token_endpoint_auth_method` is `"client_secret_post"` (set by a real Dynamic Client Registration response for a non-Google provider)
- **AND** the cached `client_secret` matches the currently configured `client_secret`
- **WHEN** `FileTokenStorage.get_client_info()` is called
- **THEN** the returned `OAuthClientInformationFull` retains `token_endpoint_auth_method="client_secret_post"` unchanged
- **AND** the SDK's `prepare_token_auth()` sends `client_secret` in the request body

#### Scenario: Token revocation
- **GIVEN** a server `gmail` has stored tokens
- **WHEN** the operator runs `/mcp auth revoke gmail`
- **THEN** the token file `$XDG_STATE_HOME/<name>/mcp_tokens/gmail.json` is deleted, the server's status changes to `needs_auth`, and its tools are unregistered