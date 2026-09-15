## MODIFIED Requirements

### Requirement: File-backed token storage

The system SHALL store OAuth tokens per-server in JSON files resolved via `xdg_paths(agent_name).mcp_tokens_dir` (the `$XDG_STATE_HOME/<agent_name>/mcp_tokens/` directory), with `0600` file permissions. The storage SHALL implement the MCP SDK's `TokenStorage` protocol.

When persisting a token response that omits `refresh_token` (as refresh-grant responses do), `FileTokenStorage.set_tokens` SHALL retain the previously stored `refresh_token` (merge-preserve). An incoming `refresh_token` SHALL always overwrite the stored one, so full re-authorization and real token rotation still replace the stored value. Only `refresh_token` is merge-preserved; all other fields replace as before.

The `FileTokenStorage` SHALL accept a `token_endpoint_auth_method` parameter (defaulting to `"client_secret_basic"`) so the pre-seeded `OAuthClientInformationFull` carries the auth method the SDK's `prepare_token_auth()` expects.

The `get_client_info()` method SHALL set `token_endpoint_auth_method` on its pre-seed return path: the constructed `OAuthClientInformationFull` SHALL include `token_endpoint_auth_method` set to the storage's configured value. When `get_client_info()` returns a persisted block — which it does only when that block's `client_secret` matches the configured one — it SHALL return it unmodified; on a `client_secret` mismatch the configured credentials take precedence and the pre-seed is returned instead, so a secret rotation takes effect.

When a persisted `client_info` block cannot be parsed, `get_client_info()` SHALL log a warning and fall back to the pre-seed rather than raising, so a corrupt or schema-drifted block does not prevent connecting to the server.

The token file MAY carry an optional top-level `oauth_metadata` block: the authorization-server metadata (per RFC 8414, the SDK's `OAuthMetadata` model) discovered during an authorization flow, persisted only after the token exchange completes. `FileTokenStorage` SHALL expose `get_oauth_metadata()` / `set_oauth_metadata()` for this block: `set_oauth_metadata` SHALL merge the block into the existing file via the same atomic write as all storage writes, and `get_oauth_metadata()` SHALL log a warning and return `None` on a malformed block rather than raising, mirroring the `client_info` fallback. When a file has a `token` block but no `oauth_metadata` block, the storage SHALL NOT invent one.

`set_tokens` SHALL carry the stored `oauth_metadata` block forward across writes alongside `client_info`: a block present on disk before the write is preserved after it, so silent refreshes never wipe the persisted metadata; the block is lost only by rollback to code that predates it. An incoming authorization flow that discovers fresh metadata replaces the stored block via `set_oauth_metadata`.

The storage SHALL expose `read_issued_at()`, a synchronous read returning the stored `token.issued_at` (or `None` when the file, the `token` block, or the `issued_at` key is absent), so a provider reconstructed from storage can restore the stored token's absolute expiry.

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

#### Scenario: OAuth metadata persisted after a completed authorization

- **GIVEN** an interactive authorization flow for server `gmail` discovers authorization-server metadata (token endpoint `https://oauth2.googleapis.com/token`) during the flow and completes the token exchange successfully
- **WHEN** the tokens are persisted
- **THEN** the token file carries an `oauth_metadata` block containing the discovered authorization-server metadata (the SDK `OAuthMetadata` model), including the `token_endpoint`, persisted only after the exchange completed
- **AND** a flow that fails before the token exchange completes leaves no `oauth_metadata` block behind

#### Scenario: Silent refresh preserves the stored oauth metadata

- **GIVEN** server `gmail` has a stored token file containing an `oauth_metadata` block
- **WHEN** a silent token refresh persists a new token set via `set_tokens`
- **THEN** the stored file's `oauth_metadata` block is preserved unchanged — present on disk before the write, present after it
- **AND** the block is replaced only when a subsequent authorization flow discovers fresh metadata

#### Scenario: Malformed oauth metadata block falls back to None

- **GIVEN** an MCP server has a stored token file whose `oauth_metadata` block cannot be parsed into the SDK's `OAuthMetadata` model (e.g. hand-edited or schema-drifted)
- **WHEN** `FileTokenStorage.get_oauth_metadata()` is called
- **THEN** a warning naming the server is logged and no exception propagates
- **AND** `None` is returned so the provider starts without restored metadata and the connection attempt proceeds

#### Scenario: Token revocation
- **GIVEN** a server `gmail` has stored tokens
- **WHEN** the operator runs `/mcp auth revoke gmail`
- **THEN** the token file `$XDG_STATE_HOME/<name>/mcp_tokens/gmail.json` is deleted, the server's status changes to `needs_auth`, and its tools are unregistered

#### Scenario: issued_at readable for expiry restoration

- **GIVEN** server `gmail` has a stored token file whose `token` block carries `issued_at = 1757985041.5`
- **WHEN** `read_issued_at()` is called on the storage
- **THEN** it returns `1757985041.5` without requiring an event loop
- **AND** for a file without the `token` block, without the `issued_at` key, or no file at all, it returns `None`

### Requirement: Automatic token refresh

The system SHALL rely on the MCP SDK's `OAuthClientProvider` to automatically refresh expired access tokens using the stored refresh token. A provider reconstructed from persisted tokens SHALL know the stored token's absolute expiry and the authorization server's token endpoint: on initialization it SHALL restore the SDK context's token-expiry clock from the stored `issued_at + expires_in` when both are present, and SHALL restore the persisted `oauth_metadata` block when present, so the silent refresh is attempted proactively — before any request carries a stale access token — and refresh grants are sent to the provider's real token endpoint rather than the SDK's `<server-base>/token` fallback. When the stored file lacks `issued_at` or the `oauth_metadata` block (a pre-migration file), the corresponding restore is skipped and behavior degrades exactly as before this change. The provider SHALL retain the refresh token across refresh grants in memory (re-attaching the pre-refresh `refresh_token` after a successful refresh whose response omits one, only when the refresh succeeded), so that successive access-token expirations are all refreshable and never fall back to a full authorization flow while a valid refresh token exists. On disk, persistence SHALL follow the merge-preserve semantics of the file-backed token storage requirement.

#### Scenario: Silent token refresh on restored expiry

- **GIVEN** a connected MCP server's long-lived provider was reconstructed from a stored token file whose `issued_at + expires_in` lies in the past, with a valid `refresh_token` and an `oauth_metadata` block naming the provider's token endpoint
- **WHEN** a tool call is made
- **THEN** the provider detects the expired token before the request is sent and silently refreshes it via an outbound POST to the restored token endpoint, stores the new token, and executes the tool call with the new Bearer token — the server never receives the stale access token and no user interaction occurs
- **AND** the provider's in-memory token set retains the refresh token after the refresh, so a subsequent access-token expiration also refreshes silently

#### Scenario: Silent token refresh on 401

- **GIVEN** a connected MCP server holds an access token that is still unexpired per the provider's restored expiry clock, but the server rejects it with 401 anyway (e.g. server-side invalidation before expiry)
- **WHEN** a tool call receives the 401 response
- **THEN** the SDK's 401 branch does not attempt a refresh grant — it runs the full authorization flow, which the long-lived non-interactive session aborts, so the failure degrades to the needs-auth path with the "run `/mcp auth <name>`" error
- **AND** the on-disk refresh token is not destroyed by the degradation

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

#### Scenario: Reconstructed provider restores the token endpoint

- **GIVEN** server `gmail` has a stored token file whose `oauth_metadata` block carries `token_endpoint = https://oauth2.googleapis.com/token`, and the MCP server URL is `http://localhost:8000/mcp`
- **WHEN** the provider is reconstructed from storage and its access token has expired with a valid refresh token
- **THEN** the silent-refresh POST goes to `https://oauth2.googleapis.com/token`
- **AND** no refresh grant is sent to `http://localhost:8000/token`

#### Scenario: Pre-migration token file degrades to today's behavior

- **GIVEN** server `gmail` has a stored token file written before this change (no `oauth_metadata` block), with a valid refresh token
- **WHEN** the provider is reconstructed from storage and its access token has expired
- **THEN** the silent-refresh POST goes to the SDK's fallback `<server-base>/token` URL, the refresh fails, and the failure degrades to the needs-auth path with the "run `/mcp auth`" error — no crash, and the on-disk refresh token is not destroyed
- **AND** after the operator runs `/mcp auth gmail` once, the file gains the `oauth_metadata` block and silent refreshes sustain thereafter

#### Scenario: Token file without issued_at degrades to today's behavior

- **GIVEN** server `gmail` has a stored token file whose `token` block lacks an `issued_at` stamp (e.g. hand-edited or written by tooling that predates the stamp), with a valid refresh token
- **WHEN** the provider is reconstructed from storage
- **THEN** the expiry restore is skipped: the SDK context's `token_expiry_time` remains unset, the loaded token passes validity checks, and the first request with the stale token draws a 401 that degrades to the needs-auth path exactly as before this change — no crash
- **AND** after the operator runs `/mcp auth gmail` once, the file is rewritten with a fresh `issued_at` and silent refreshes sustain thereafter