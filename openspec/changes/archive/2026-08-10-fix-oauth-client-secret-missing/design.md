## Context

The MCP client (`mcp_oauth.py`) implements OAuth 2.0 for HTTP-transport MCP servers via the MCP SDK's `OAuthClientProvider`. For providers that don't support Dynamic Client Registration (DCR) — like Google — the `FileTokenStorage.get_client_info()` method pre-seeds an `OAuthClientInformationFull` from config so the SDK skips DCR entirely.

The bug: the pre-seed omits `token_endpoint_auth_method`, so the SDK's `prepare_token_auth()` reads `None` and sends no `client_secret` during token exchange. Google rejects with `400 client_secret is missing`.

The root cause is a two-object drift: `build()` sets `token_endpoint_auth_method="client_secret_basic"` on `client_metadata`, but `prepare_token_auth()` reads from `client_info.token_endpoint_auth_method` — a different object that never received the value.

```
┌─────────────────────────────────────────────────────────────┐
│                    Current (broken) flow                     │
└─────────────────────────────────────────────────────────────┘

  OAuthProviderFactory.build()
  │
  ├─ client_metadata = OAuthClientMetadata(
  │     token_endpoint_auth_method="client_secret_basic"  ✓ set
  │     scope=...
  │  )
  │
  └─ storage = FileTokenStorage(client_id, client_secret)
       │
       └─ get_client_info() → OAuthClientInformationFull(
              client_id=...,
              client_secret=...,
              redirect_uris=None,
              # token_endpoint_auth_method NOT set → None  ✗
          )

  MCP SDK async_auth_flow:
  │
  ├─ Step 4: if not client_info → FALSE (pre-seed is truthy) → skip DCR
  │
  └─ Step 5: _exchange_token_authorization_code()
       │
       └─ prepare_token_auth(data, headers)
            │
            ├─ auth_method = client_info.token_endpoint_auth_method  → None
            ├─ if "client_secret_basic" → FALSE
            ├─ elif "client_secret_post" → FALSE
            └─ # "none" → no client_secret sent
               │
               └─ Google: 400 "client_secret is missing"
```

## Goals / Non-Goals

**Goals:**
- Ensure `client_secret` is sent in the token exchange for `client_secret_basic` providers (Google/Gmail).
- Zero blast radius: all existing `FileTokenStorage` construction sites compile without modification; only the OAuth-flow sites gain the corrected auth header.
- Minimum reachable surface: change only the code path that actually executes in production, so no inert branch is added for a future reader to reason about.

**Non-Goals:**
- Making `token_endpoint_auth_method` configurable via `OAuthConfig` (deferred to its own proposal; see Risks for the trade-off this leaves in place).
- Passing the value from `client_metadata` into `FileTokenStorage` in `build()`. `build()` hardcodes the metadata value and `oauth_cfg` has no key for it, so the round-trip is a tautology equal to the constructor default — it cannot prevent drift, because there is no independent second source to drift from.
- Normalizing the cached return path of `get_client_info()`. That path is unreachable in the shipped flow (see Risks).
- Fixing `set_client_info`'s lossy `exclude_none=True` persistence. Pre-existing, orthogonal to this bug, and only observable on the unreachable cached path.
- Changing the `redirect_uris=None` in the pre-seed (harmless — token exchange reads from `client_metadata.redirect_uris`, not `client_info`'s).
- Modifying the MCP SDK itself.

## Decisions

### Decision 1: Defaulted constructor param on `FileTokenStorage`

Add `token_endpoint_auth_method: str = "client_secret_basic"` to `FileTokenStorage.__init__`.

**Rationale:** The default is the sole source of the value, so all 3 non-test construction sites (`OAuthProviderFactory.build` in `mcp_oauth.py`, `_prepare_oauth_provider` and `get_token_info` in `mcp_client.py`) and every test construction compile and behave unchanged. A required param would break all of them. The param also gives the deferred `OAuthConfig` knob a seam to plug into without touching `get_client_info()` again.

**Alternatives considered:**
- Config field on `OAuthConfig` (Option B): scope creep for a bug fix. Deferred to its own proposal.
- Hardcoding the literal directly in the pre-seed with no parameter: marginally smaller, but leaves no seam for the config knob and makes the value untestable independently of the default.

### Decision 2: Pre-seed path includes the auth method

In `get_client_info()`, the final pre-seed `return OAuthClientInformationFull(...)` includes `token_endpoint_auth_method=self.token_endpoint_auth_method` in the constructed `OAuthClientInformationFull`.

**Rationale:** This is the primary reproduced-bug fix — the pre-seed is the path that fires for all new OAuth flows with providers that don't support DCR (Google). Without this, `prepare_token_auth()` reads `None` and sends no `client_secret`.

```
┌─────────────────────────────────────────────────────────────┐
│                    Fixed flow                                │
└─────────────────────────────────────────────────────────────┘

  OAuthProviderFactory.build()          ← UNCHANGED
  │
  ├─ client_metadata = OAuthClientMetadata(
  │     token_endpoint_auth_method="client_secret_basic"   (read by the SDK for DCR,
  │  )                                                      not by prepare_token_auth)
  │
  └─ storage = FileTokenStorage(client_id=..., client_secret=...)
       │                         └─ token_endpoint_auth_method defaults to
       │                            "client_secret_basic"  ← NEW (ctor default)
       │
       └─ get_client_info()
            │
            ├─ cached path: unchanged (unreachable — nothing writes client_info)
            └─ pre-seed path: include token_endpoint_auth_method  ← NEW

  MCP SDK prepare_token_auth():
  │
  ├─ auth_method = client_info.token_endpoint_auth_method  → "client_secret_basic"
  ├─ if "client_secret_basic" → TRUE
  └─ sends Authorization: Basic base64(client_id:client_secret)
     │
     └─ Google: 200 OK ✓
```

## Risks / Trade-offs

- **[Accepted trade-off] `client_secret_basic` is asserted for every OAuth MCP server, not just Google.** Because there is no config knob, a provider whose client is registered as public/PKCE-only regresses from working (`prepare_token_auth` matched no branch, so no client authentication was sent — which such a provider accepts) to `401/400 invalid_client` once a `Basic` header is attached. `OAuthConfig` requires a `client_secret` key, so such an operator would have supplied a placeholder. The regression is narrower than that implies: `_require` in `config_schema.py` only checks key *presence*, and the SDK's basic branch additionally requires a *truthy* `client_secret` (`oauth2.py:200`), so `client_secret = ""` still sends no client authentication and does not regress. Only a **non-empty** placeholder does. No such provider is configured or exercised today, and the alternative — shipping the knob — is scope creep on a bug fix. Mitigation is the deferred proposal below; until then this is a known limitation, not an oversight.
- **[Risk] The cached return path is unreachable, so a stale cached `token_endpoint_auth_method` is not repaired.** `get_client_info()` never returns `None`, so both of the SDK's `set_client_info` calls — the CIMD path and the DCR path, each inside the `if not self.context.client_info:` block at mcp/client/auth/oauth2.py:572 — never run, and no `client_info` block is ever written. Confirmed by repo-wide grep: the SDK holds the only calls. A stale block can therefore only arrive from a hand-edited or legacy token file. → Mitigation: if such a block fails to parse, the existing `except (TypeError, ValueError)` handler logs and degrades to the pre-seed, which now carries the correct auth method. That fallback is covered by a regression test.
- **[Risk] The new parameter is typed `str`, while the SDK field is `Literal["none", "client_secret_post", "client_secret_basic", "private_key_jwt"] | None`.** An out-of-`Literal` value would raise `ValidationError` out of `get_client_info()` rather than a `ConfigError`. → Mitigation: unreachable today — the only value supplied is the constructor default. This becomes live the moment the value is config-driven, so the deferred proposal must narrow the annotation and validate at config-parse time.

## Migration Plan

No migration needed. The fix is backward-compatible:
- New OAuth flows get the correct `token_endpoint_auth_method` from the pre-seed.
- The agent never writes a `client_info` block, so there is no persisted state to migrate.
- A hand-edited or legacy `client_info` block that parses is returned as-is; one that fails to parse degrades to the pre-seed with a warning.

**Rollback:** Revert the three added lines in `mcp_oauth.py`. No data migration or file format changes.

## Open Questions

- None blocking.

**Deferred to its own proposal:** make `token_endpoint_auth_method` configurable via `OAuthConfig` for `client_secret_post`-only and public/PKCE providers. That change must add a field to `OAuthConfig` in `config_schema.py`, thread it through `build()` into `FileTokenStorage` (the seam Decision 1 leaves in place), narrow the parameter annotation from `str` to the SDK's `Literal` union, and validate the value at config-parse time so a typo fails at startup rather than mid-OAuth-flow. It resolves the accepted trade-off above.