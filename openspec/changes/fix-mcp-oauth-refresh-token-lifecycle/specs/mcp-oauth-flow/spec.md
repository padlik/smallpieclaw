## MODIFIED Requirements

### Requirement: File-backed token storage

The system SHALL store OAuth tokens per-server in JSON files resolved via `xdg_paths(agent_name).mcp_tokens_dir` (the `$XDG_STATE_HOME/<agent_name>/mcp_tokens/` directory), with `0600` file permissions. The storage SHALL implement the MCP SDK's `TokenStorage` protocol.

When persisting a token response that omits `refresh_token` (as refresh-grant responses do), `FileTokenStorage.set_tokens` SHALL retain the previously stored `refresh_token` (merge-preserve). An incoming `refresh_token` SHALL always overwrite the stored one, so full re-authorization and real token rotation still replace the stored value. Only `refresh_token` is merge-preserved; all other fields replace as before.

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

#### Scenario: Refresh-grant response preserves the stored refresh token

- **GIVEN** server `gmail` has a stored token file containing `refresh_token = R1` and a valid `access_token`
- **WHEN** a silent token refresh persists a new token set whose `refresh_token` is absent (a refresh-grant response)
- **THEN** the stored file's `token` object retains `refresh_token = R1`
- **AND** the new `access_token`, `expires_in`, `scope` (when present), and `issued_at` replace the previous values

#### Scenario: Incoming refresh token overwrites the stored one

- **GIVEN** server `gmail` has a stored token file containing `refresh_token = R1`
- **WHEN** `set_tokens` persists a token set carrying `refresh_token = R2` (a full re-authorization or provider-initiated rotation)
- **THEN** the stored file's `token` object contains `refresh_token = R2` — re-authentication still replaces existing tokens wholesale

#### Scenario: No stored refresh token and none granted

- **GIVEN** server `gmail` has a stored token file with no `refresh_token` (e.g. previously wiped or never granted)
- **WHEN** `set_tokens` persists a token set that also omits `refresh_token`
- **THEN** the stored file contains no `refresh_token` — merge-preserve never invents one

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

### Requirement: Automatic token refresh

The system SHALL rely on the MCP SDK's `OAuthClientProvider` to automatically refresh expired access tokens using the stored refresh token when the MCP server returns 401. The provider SHALL retain the refresh token across refresh grants in memory (re-attaching the pre-refresh `refresh_token` after a successful refresh whose response omits one, only when the refresh succeeded), so that successive access-token expirations are all refreshable and never fall back to a full authorization flow while a valid refresh token exists. On disk, persistence SHALL follow the merge-preserve semantics of the file-backed token storage requirement.

#### Scenario: Silent token refresh on 401

- **GIVEN** a connected MCP server has an expired access token but a valid refresh token
- **WHEN** a tool call results in a 401 response
- **THEN** the SDK automatically refreshes the token via an outbound POST to the provider's token endpoint, stores the new token, and retries the original request without user interaction
- **AND** the provider's in-memory token set retains the refresh token after the refresh, so a subsequent access-token expiration also refreshes silently instead of triggering a full authorization flow

#### Scenario: Successive silent refreshes never lose the refresh token

- **GIVEN** a connected MCP server with an OAuth-configured provider has a stored token containing a valid refresh token
- **WHEN** the access token expires and is silently refreshed, and then expires again and is refreshed a second time (e.g. consecutive scheduled-job runs on a 6-hour cadence with 1-hour Google access tokens)
- **THEN** both refreshes succeed silently, the token file retains the refresh token after each write, and no authorization URL is delivered to Telegram at any point

#### Scenario: Failed refresh does not resurrect a dead refresh token

- **GIVEN** a connected MCP server's refresh token has been revoked or expired at the provider
- **WHEN** a token refresh is attempted and the provider rejects it (non-200 response)
- **THEN** the refresh-token re-attachment does not occur — the cleared token state is preserved, the provider reports it cannot refresh, and the failure degrades to the needs-auth path rather than re-arming a doomed refresh loop

#### Scenario: Token refresh failure surfaces to operator

- **GIVEN** a connected MCP server has an expired access token and the refresh token is revoked or expired
- **WHEN** a tool call results in a 401 response and the SDK's refresh attempt fails
- **THEN** the tool call fails with an error message indicating the token expired and the operator should run `/mcp auth <name>`, and the server's status changes to `needs_auth`
- **AND** when the tool call originates from a non-interactive context (background or scheduled job), no authorization URL or auth button is delivered to Telegram for this failure

## ADDED Requirements

### Requirement: Background authentication degradation

The system SHALL ensure that authorization-required conditions reached from non-interactive context (background agents, scheduled jobs) never trigger interactive authorization. The long-lived wrapper registered after a successful interactive OAuth flow SHALL run with a non-interactive provider (no chat binding, with a non-interactive redirect callback that marks the server `needs_auth`), while the interactive flow's chat-bound provider is used only for the in-flight `/mcp auth` flow and is discarded when the flow completes.

Auth-required failures surfaced from background tool calls SHALL be recognized by the react loop's MCP auth-failure detection (for OAuth-configured servers), SHALL mark the owning server `needs_auth`, and SHALL return an actionable error directing the operator to run `/mcp auth <name>`. This detection and translation is owned by this capability; `mcp-transport` covers only the resulting wrapper status.

#### Scenario: Long-lived session runs a non-interactive provider

- **GIVEN** the operator completes a successful `/mcp auth <name>` flow
- **WHEN** the manager registers the long-lived wrapper for the server
- **THEN** the registered wrapper's OAuth provider has no Telegram chat binding and carries the non-interactive redirect callback, so no future tool call through this wrapper can deliver an authorization prompt
- **AND** the interactive flow's session is closed (its session task cancelled and its provider discarded), and the registered session is brought up with the newly stored token so no authorization handshake fires during registration

#### Scenario: Background tool call never delivers auth buttons

- **GIVEN** a server's long-lived wrapper holds an access token whose refresh token is dead (revoked or expired at the provider)
- **WHEN** a scheduled background job calls a tool on that server and the SDK's authorization flow fires
- **THEN** no authorization prompt or button is sent to any Telegram chat
- **AND** the non-interactive redirect callback marks the server `needs_auth`, stops the OAuth callback server, and aborts the SDK flow
- **AND** the tool call fails with an error that the react loop recognizes as an MCP authentication failure for this OAuth-configured server
- **AND** the react loop translates the failure to a message directing the operator to run `/mcp auth <name>` and marks the server `needs_auth`

#### Scenario: needs_auth fast-fail for tool calls

- **GIVEN** a server's wrapper is marked `needs_auth`
- **WHEN** any component calls a tool on that server
- **THEN** the call fails fast with an error containing "needs authorization" and directing the operator to run `/mcp auth <name>`
- **AND** for an OAuth-configured server, the react loop's auth-failure detection recognizes the error and preserves the `needs_auth` status

#### Scenario: Interactive flow keeps the operator experience unchanged

- **GIVEN** the operator runs `/mcp auth <name>` in Telegram
- **WHEN** the interactive OAuth flow runs
- **THEN** the authorization URL is still delivered to the operator's chat as an inline button with a working Cancel button, the proactive probe still fires the handshake, and success still registers the server's tools
- **AND** after success, the long-lived wrapper is registered with the non-interactive provider per the degradation requirement