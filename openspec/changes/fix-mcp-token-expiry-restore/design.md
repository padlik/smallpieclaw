## Context

The MCP OAuth subsystem still dies ~1 hour after every interactive authorization. Root cause (verified in the installed mcp 1.29.1 SDK source, production logs Sept 15, and upstream repository state — see `explore-brief.md`): the SDK's `_initialize()` (`oauth2.py:484-492`) loads `current_tokens` from storage into a reconstructed provider but restores **neither** `token_expiry_time` nor `oauth_metadata`:

- **Gap A — perpetual validity**: `is_token_valid()` (`oauth2.py:129-134`) short-circuits on `not self.token_expiry_time`, so a loaded token passes forever and the proactive-refresh branch (`if not is_token_valid() and can_refresh_token()`, `oauth2.py:500`) is unreachable. The first 401 goes straight to a full authorization flow, which the long-lived non-interactive session (ADR-0030) correctly aborts → `needs_auth`.
- **Gap B — wrong refresh endpoint**: `_refresh_token()` (`oauth2.py:433-439`) reads `context.oauth_metadata.token_endpoint`; with metadata unrestored it falls back to `urljoin(base_url(server_url), "/token")` = `http://localhost:8000/token` (the local MCP proxy, not Google).

Trigger: ADR-0030's D3 handoff closes the interactive provider (which held expiry in memory) and registers a fresh non-interactive wrapper whose provider reconstructs from disk — arming both gaps on every `/mcp auth`. Production evidence Sept 15: token written 20:10:41 (`has_refresh=True`, `expires_in=3600`), expired 21:10:41, first call 21:26:47 → 401 → "token expired, needs manual authorization". Zero `"Token refresh failed"` log rows prove no refresh grant ever reached Google. Upstream fix PRs #1784 (expiry restore), #2492 (metadata persistence), #2875 (401-branch refresh) are all open; both gaps verified present in 1.30.0, 2.2.0, and `main`.

In-force ADR constraints: ADR-0029 (RT merge-preserve on disk + re-attach in memory — the refresh branch this change makes *reachable*), ADR-0030 (long-lived sessions non-interactive — degradation semantics untouched by this change), ADR-0019 (XDG token-file layout — same path/permissions/atomic write).

Diagram conventions: ASCII, lightweight C4-inspired (component + dynamic views only), per the sibling change.

```
+---------------------------+ smallpieclaw process (single daemon) ---------------------------+
|                                                                                                 |
|  mcp_client.py                                  mcp_oauth.py                                   |
|  +---------------------+                       +-----------------------------------------+   |
|  | MCPManager          |  both build sites ->   | OAuthProviderFactory.build              |   |
|  |  _prepare_oauth_    |------factory---------->|   FileTokenStorage                      |   |
|  |  provider (:552)    |                       |     get/set_tokens  (merge-preserve RT) |   |
|  |  _run_oauth_flow    |                       |     get/set_client_info                 |   |
|  |  (:1871, interactive)                       |     get/set_oauth_metadata   [NEW]      |   |
|  |  _SdkClientWrapper  |   auth=provider       |     read_issued_at           [NEW]      |   |
|  |   call_tool         |<----------------------|  RefreshPreservingOAuthProvider         |   |
|  +---------------------+                       |     _handle_refresh_response  (ADR-0029) |   |
|                                                |     _initialize               [NEW]     |   |
|                                                |     _handle_token_response    [NEW]     |   |
|                                                +-------------------+---------------------+   |
|                                                                    |                           |
|                                          $XDG_STATE_HOME/<agent>/mcp_tokens/<server>.json     |
|                                          { token{...,issued_at}, client_info?,                 |
|                                            oauth_metadata?  [NEW] }                             |
+------------------------------------------------+------------------------------------------------+
                                                 |
                                                 v  OAuth 2.0 (refresh grant)
                             google-workspace MCP + accounts.google.com / oauth2.googleapis.com
```

## Goals / Non-Goals

**Goals:**
- A provider reconstructed from storage knows the stored token's absolute expiry and the AS token endpoint, so the SDK's proactive silent-refresh branch is reachable and aims at the right URL.
- Silent refresh sustains across expiries indefinitely (with ADR-0029) — no operator-visible change until the token is genuinely dead.
- All changes confined to `mcp_oauth.py` (`FileTokenStorage` + `RefreshPreservingOAuthProvider`); `mcp_client.py` wiring untouched — both provider build sites (`_prepare_oauth_provider` `mcp_client.py:552`, interactive `_run_oauth_flow` `:1871`) construct via `OAuthProviderFactory.build`, so both automatically get the new overrides. (Verify must assert this.)
- Drift-guarded: an SDK upgrade that changes the pinned couplings fails tests loudly (b9b3a24 pattern).
- Pre-existing token files degrade exactly as today (no `issued_at` → no expiry restore; no `oauth_metadata` → no metadata restore); one manual `/mcp auth` per server completes migration.

**Non-Goals:**
- 401-branch refresh interposition (upstream #2875 pattern) — rejected in the proposal; revisit only if upstream lands it.
- SDK upgrade (1.30.0 / 2.x verified unfixed; 2.x costs an `httpx2` migration and breaks the `_handle_refresh_response` override surface).
- Timer-driven background refresh (refresh still happens on next request, not on a schedule).
- Any change to the degradation path (ADR-0030 `needs_auth` semantics, react-loop detection) or the `/mcp auth status` surface (`read_status` untouched).
- Fixing stale-contextvar OAuth logging (carried over from the previous change's out-of-scope list).

## Decisions

### D1 — `_initialize` override restores `token_expiry_time` (fixes Gap A)

`RefreshPreservingOAuthProvider` gains:

```
async def _initialize(self) -> None:
    await super()._initialize()                 # loads current_tokens + client_info (SDK contract)
    tokens = self.context.current_tokens
    if tokens is None or tokens.expires_in is None:
        return                                   # nothing to restore
    read_issued_at = getattr(self.context.storage, "read_issued_at", None)
    if read_issued_at is None:
        return                                   # foreign storage (duck-typed guard)
    issued_at = read_issued_at()
    if issued_at is None:
        return                                   # pre-existing file without issued_at -> today's behavior
    self.context.token_expiry_time = issued_at + tokens.expires_in
```

The math mirrors the SDK's `calculate_token_expiry(expires_in) = time.time() + expires_in` (`auth_utils.py:68-80`) with `issued_at` as the base instead of now. `issued_at` is stamped by our own `set_tokens` (`mcp_oauth.py:155`) on the same host, so no clock-skew handling is needed. An expiry computed in the past (stale file, quiet server) makes `is_token_valid()` false on the next request → the proactive branch fires → refresh — the fix working, not a failure mode.

*Alternative rejected*: persisting an absolute `expires_at` instead — equivalent information, but it duplicates `issued_at + expires_in` already in the file (single source of truth) and changes the `token` block schema shared with the SDK's model dump. Rejected.

*Alternative rejected*: restoring inside `get_tokens()` — the SDK model `OAuthToken` has no expiry-stamp field; smuggling one through would corrupt the SDK contract.

### D2 — Persist and restore the full `OAuthMetadata` block (fixes Gap B; resolves OQ1)

**Decision: full `OAuthMetadata` block, not a flat `token_endpoint` string.**

- Restore: the same `_initialize` override also does `self.context.oauth_metadata = await storage.get_oauth_metadata()` (guarded by `getattr` the same way; `None` → skip).
- Rationale: the block round-trips through the SDK's own `OAuthMetadata` model (`model_validate`) — validation for free, no hand-rolled schema; it feeds `_refresh_token`'s `token_endpoint` today and any future consumer (`authorization_endpoint`, `revocation_endpoint`, `issuer`) without re-discovery; and the method names `get_oauth_metadata`/`set_oauth_metadata` match upstream PR #2492's proposed `TokenStorage` protocol extension — if that lands, our storage already conforms and the SDK calls it directly.
- A flat string would be minimal but couples us to a custom schema and gets thrown away on #2492.

Malformed block → warn + `None` (the `get_client_info` fallback pattern, `mcp_oauth.py:216-242`); the refresh then degrades via the wrong-endpoint fallback exactly like a pre-migration file. The 401 full-flow path is unaffected: it re-discovers and overwrites `context.oauth_metadata` regardless.

### D3 — Persist at token-exchange completion; single ownership on the subclass

`_handle_token_response` override (new on the subclass): after `await super()._handle_token_response(response)` succeeds (200 → tokens set in context → `update_token_expiry` → `set_tokens` persisted with a fresh `issued_at`), persist the metadata when set:

```
async def _handle_token_response(self, response) -> None:
    await super()._handle_token_response(response)
    if self.context.oauth_metadata is not None:
        await self.context.storage.set_oauth_metadata(self.context.oauth_metadata)
```

The interactive flow's discovery (PRM → OASM) sets `context.oauth_metadata` before Step 5's exchange, so a successful `/mcp auth` always has it. Persisting only after a successful exchange keeps the block's meaning precise: "the AS metadata that issued the current stored token". Both build sites construct the subclass via the factory, so the interactive path persists and the non-interactive path restores — one class, no wiring changes.

*Alternative rejected*: persisting at discovery time — writes metadata for flows that may fail the exchange, decoupling the block from token validity for no benefit.

*Alternative rejected*: a standalone startup discovery pass — duplicates the SDK's discovery chain; the one-time `/mcp auth` re-run is the documented migration instead.

### D4 — Storage layer: three additions, one lesson applied

On `FileTokenStorage` (`mcp_oauth.py`):

1. `async get_oauth_metadata() -> OAuthMetadata | None` and `async set_oauth_metadata(metadata)` — `set` reads the existing file (or `{}`), sets the `oauth_metadata` key (`model_dump(mode="json", exclude_none=True)`), atomic-writes (existing `_atomic_write`, 0600, tmp+rename); `get` warns + returns `None` on a malformed block. The metadata-only-file state (`oauth_metadata` present, `token` absent) is unreachable: `set_oauth_metadata` is only called after `super()._handle_token_response` has run `set_tokens` in the same flow. A test pins the ordering.
2. **`set_tokens` carries `oauth_metadata` forward alongside `client_info`** (`mcp_oauth.py:156-160` today): the payload assembly reads the existing file's `oauth_metadata` block into the new payload when present. Without this, the *first silent refresh* would wipe the metadata block from disk — the exact class of loss ADR-0029 fixed for `refresh_token`, generalized to the new key.
3. `read_issued_at() -> float | None` (sync, internal): reads the file's `token.issued_at`; `None` on missing/corrupt. Feeds D1. Sync like `read_status` — a tiny file read; no event-loop contract.

Token-file schema after the change:

```json
{
  "token": {
    "access_token": "...", "token_type": "Bearer", "expires_in": 3600,
    "scope": "...", "refresh_token": "...", "issued_at": 1757985041.5
  },
  "client_info":     { "...": "unchanged, optional" },
  "oauth_metadata":  { "issuer": "https://accounts.google.com",
                       "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                       "token_endpoint": "https://oauth2.googleapis.com/token" }
}
```

### D5 — Verification: drift guards, restore matrix, end-to-end refresh

All in `tests/test_mcp_oauth.py`, extending the b9b3a24 executable-assertion pattern (`test_sdk_still_invokes_handle_refresh_response` reads SDK source and pins a call site):

- **Drift guards** (fail loudly on SDK bump, forcing re-verification; if upstream *fixes* the gaps, the guard fails and the override gets dropped — that is the documented exit):
  - `inspect.getsource(OAuthClientProvider._initialize)` contains no `token_expiry_time` write (upstream #1784 landing trips this → remove the D1 restore).
  - `inspect.getsource(OAuthClientProvider._refresh_token)` still reads `oauth_metadata.token_endpoint` with the `/token` fallback (upstream #2492/#2875 landing trips this → re-verify the metadata restore).
  - the flow still calls `await self._handle_token_response(` (orphan-detection for the D3 override, mirroring the existing `_handle_refresh_response` pin).
- **Unit**: `read_issued_at` (present / missing / no file); `set_tokens` carries `oauth_metadata` + `client_info` forward; `get`/`set_oauth_metadata` round-trip; malformed → warn + `None`; `_initialize` restore matrix — (issued_at + expires_in) → expiry set; issued_at missing → expiry None; expires_in missing → None; no token → no-op; metadata restored when present; malformed metadata → `context.oauth_metadata is None` (equals the fresh-context starting state — assert the value, not "unchanged").
- **End-to-end**: reconstructed-provider silent refresh — real `FileTokenStorage` in a tmp dir; write a token file with `issued_at` in the past, `expires_in=3600`, a `refresh_token`, and an `oauth_metadata` block whose `token_endpoint` points at a scripted local endpoint; drive `async_auth_flow` with a generator harness; assert (a) the first yielded request is a refresh POST to the *restored* endpoint (not `<server-base>/token`), (b) feeding a 200 refresh response (RT-less, Google shape) persists the new token with RT merge-preserved and metadata carried forward, (c) the retried request carries the new Bearer.

Dynamic view — long-lived session after migration:

```
 tool call (background or interactive)
   -> httpx transport (auth = RefreshPreservingOAuthProvider)
   -> async_auth_flow (first use per process):
        _initialize
          super(): current_tokens <- storage.get_tokens(); client_info <- storage.get_client_info()
          [D1] token_expiry_time <- storage.read_issued_at() + current_tokens.expires_in
          [D2] oauth_metadata    <- storage.get_oauth_metadata()
        is_token_valid()? --no (restored expiry in past)--> can_refresh_token()? --yes (ADR-0029)-->
          [D2] _refresh_token() POSTs oauth_metadata.token_endpoint  (Google, not localhost:8000/token)
          200 -> _handle_refresh_response (ADR-0029 re-attach) -> set_tokens
                 (merge-preserve RT [D4.2]; carry client_info + oauth_metadata) -> retry succeeds
        --yes--> request proceeds normally
 pre-migration file (no oauth_metadata): same path, but refresh POSTs to the wrong
 endpoint -> non-200 -> SDK clears tokens (disk untouched) -> 401 -> full flow ->
 non-interactive abort -> needs_auth  == exactly today's behavior, then one /mcp auth migrates
```

## Risks / Trade-offs

- [SDK upgrade fixes `_initialize`/refresh upstream] -> drift guards fail loudly at test time; remove the corresponding override (documented exit). The overrides are additive and call `super()`, so an upstream fix degrades to a passthrough, not a break.
- [Persisted metadata goes stale (Google rotates endpoints)] -> refresh grant fails non-200 → SDK clears in-memory tokens → full flow → interactive `/mcp auth` re-discovers and re-persists fresh metadata. Recovery path identical to today's.
- [Pre-migration files: restored expiry + missing metadata = refresh aimed at `http://localhost:8000/token`] -> one harmless local POST before the usual degradation; `clear_tokens()` is in-memory only, so the on-disk RT is never at risk. Window closes after one `/mcp auth`.
- [`read_issued_at`/`get_oauth_metadata` duck-typed guards hide a miswired storage] -> both provider build sites use the factory, which constructs `FileTokenStorage` in the same module; guards exist only for protocol-shaped foreign storages. Verify asserts factory wiring.
- [New public methods trip vulture] -> `vulture_whitelist.py` entries following the existing `mcp_oauth.py` block (`vulture_whitelist.py:63-72`).
- [Extra `_initialize` file read per provider construction] -> one tiny JSON read, once per provider lifetime; `get_tokens` already reads the same file.

## Migration Plan

1. Deploy with daemon restart; no config change, no schema rewrite — old files load unchanged (missing keys → restores skipped).
2. Run `/mcp auth <server>` once per OAuth server: the flow persists `oauth_metadata` and a fresh `issued_at`; silent refresh sustains thereafter.
3. Rollback: revert the commit. New-code token files carry an extra `oauth_metadata` key that the old code ignores on read (`get_tokens` reads only the `token` block) and drops on the next write (old `set_tokens` rebuilds the payload) — harmless either way.

## Open Questions

- None blocking. All brief OQs resolved: OQ1 → D2 (full block); OQ2 → restore path logs at DEBUG only, `read_status` untouched; OQ3 → tasks (vulture entries).
- ADR candidate for the adr step: the **storage restore contract** (reconstructed providers restore expiry + AS metadata from persisted state) is a durable architectural commitment sibling to ADR-0029 — to be recorded as a new ADR; no in-force ADR requires revisiting (ADR-0029/0030 are extended, not contradicted; ADR-0019's path/permissions/atomic-write unchanged).