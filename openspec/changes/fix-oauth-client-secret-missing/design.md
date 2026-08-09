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
- Normalize both return paths in `get_client_info()` (pre-seed and cached) so the fix is robust against stale persisted state.
- Zero blast radius: all existing `FileTokenStorage` construction sites compile without modification; only the OAuth-flow sites gain the corrected auth header.
- Single source of truth: the auth method value flows from `client_metadata` to `storage` to `client_info`, preventing future drift.

**Non-Goals:**
- Making `token_endpoint_auth_method` configurable via `OAuthConfig` (future enhancement for `client_secret_post`-only providers; not needed for the current fix since `build()` already hardcodes `"client_secret_basic"`).
- Changing the `redirect_uris=None` in the pre-seed (harmless — token exchange reads from `client_metadata.redirect_uris`, not `client_info`'s).
- Modifying the MCP SDK itself.

## Decisions

### Decision 1: Defaulted constructor param on `FileTokenStorage`

Add `token_endpoint_auth_method: str = "client_secret_basic"` to `FileTokenStorage.__init__`.

**Rationale:** The default matches `build()`'s hardcoded value, so all 3 non-test construction sites (`mcp_oauth.py:392`, `mcp_client.py:411`, `mcp_client.py:613`) and 8 test constructions compile and behave unchanged. A required param would break all of them.

**Alternatives considered:**
- Hardcode in `get_client_info()` pre-seed only (Option A): leaves the cached return path latent-broken.
- Config field on `OAuthConfig` (Option B): scope creep — `OAuthConfig` has no such field, and the value already exists on `client_metadata`.

### Decision 2: Source from `client_metadata` in `build()`

In `OAuthProviderFactory.build()`, pass `token_endpoint_auth_method=client_metadata.token_endpoint_auth_method` to the `FileTokenStorage` constructor.

**Rationale:** This closes the two-object drift gap directly — storage and metadata can never diverge. No config schema changes needed.

### Decision 3: Fill-if-None on the cached path

In `get_client_info()`, when a cached `client_info` block exists and the secret matches (line 176), fill `token_endpoint_auth_method` if it's `None` via `model_copy(update={"token_endpoint_auth_method": self.token_endpoint_auth_method})`.

**Rationale:** A cached block persisted with `exclude_none=True` reloads `None` fields as `None`. Force-overriding would clobber a legitimate DCR-provided method for non-Google providers. Fill-if-None repairs stale state while preserving provider-chosen methods.

### Decision 4: Pre-seed path includes the auth method

In `get_client_info()`, the pre-seed return (line 185) includes `token_endpoint_auth_method=self.token_endpoint_auth_method` in the constructed `OAuthClientInformationFull`.

**Rationale:** This is the primary reproduced-bug fix — the pre-seed is the path that fires for all new OAuth flows with providers that don't support DCR (Google). Without this, `prepare_token_auth()` reads `None` and sends no `client_secret`.

```
┌─────────────────────────────────────────────────────────────┐
│                    Fixed flow                                │
└─────────────────────────────────────────────────────────────┘

  OAuthProviderFactory.build()
  │
  ├─ client_metadata = OAuthClientMetadata(
  │     token_endpoint_auth_method="client_secret_basic"
  │  )
  │
  └─ storage = FileTokenStorage(
       client_id=...,
       client_secret=...,
       token_endpoint_auth_method=client_metadata.token_endpoint_auth_method  ← NEW
     )
       │
       └─ get_client_info()
            │
            ├─ cached path: fill-if-None via model_copy  ← NEW
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

- **[Risk] Cached block with a non-None but wrong auth method** → Mitigation: fill-if-None only touches `None` values; a cached method set by a real DCR response is preserved. If a provider genuinely requires `client_secret_post` and the cached block says so, it stays.
- **[Risk] Default `"client_secret_basic"` doesn't match a future `build()` change** → Mitigation: `build()` passes `client_metadata.token_endpoint_auth_method` explicitly, so the default is only a fallback for construction sites that don't go through `build()` (the two `mcp_client.py` sites). Even if those sites reach `get_client_info()`, the default matches `build()`'s value, so behavior is unchanged.
- **[Risk] `model_copy` availability** → Mitigation: `OAuthClientInformationFull` is a Pydantic `BaseModel`; `model_copy` is available since Pydantic v2. The project uses Pydantic v2 (confirmed in `config_schema.py`).

## Migration Plan

No migration needed. The fix is backward-compatible:
- New token files get the correct `token_endpoint_auth_method` from the pre-seed.
- Existing token files with a cached `client_info` block missing the field are repaired on read via fill-if-None.
- Existing token files with a cached `client_info` block that has the field set are unaffected.

**Rollback:** Revert the three code changes in `mcp_oauth.py`. No data migration or file format changes.

## Open Questions

- None blocking. Future enhancement: make `token_endpoint_auth_method` configurable via `OAuthConfig` for `client_secret_post`-only providers. This would require adding a field to `OAuthConfig` in `config_schema.py` and passing it through `build()`. Not in scope for this fix.