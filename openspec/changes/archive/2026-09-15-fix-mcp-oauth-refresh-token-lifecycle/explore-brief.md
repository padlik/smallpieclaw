# Explore Brief: fix-mcp-oauth-refresh-token-lifecycle

Investigation completed 2026-09-14. Root causes verified by code (file:line) and
production logs (`/tmp/agent_logs.sqlite`, Sept 10–12; `agent.log.2026-09-12.gz`).

## Verified defects

| ID | Defect | Evidence |
|---|---|---|
| A | `FileTokenStorage.set_tokens` full-replace + `exclude_none=True` wipes the stored `refresh_token` whenever the SDK persists a refresh-grant response (Google omits `refresh_token` there) | `mcp_oauth.py:137-147`; SDK `mcp/client/auth/oauth2.py:467`; log: `has_refresh=False` writes on the 6h job grid |
| B | SDK replaces `context.current_tokens` wholesale on refresh → `can_refresh_token()` false after first refresh → next expiry fires a full auth flow (buttons) even with a valid disk token | SDK `oauth2.py:465`, `:137-139`; log: 03:59 refresh → 10:00 full flow |
| C | Long-lived wrapper registered by `/mcp auth` keeps the interactive chat-bound provider → background-job 401s send Telegram auth buttons into a stale chat | `mcp_client.py:1845-1915`; `mcp_oauth.py:565`; `telegram_interface.py:571-576`; log: buttons under 12h-old trace/prompt context |
| D | Error-text handshake gap: `"not connected"` fast-fail and `"needs manual authorization"` abort match none of `_is_mcp_auth_failure` indicators → no `needs_auth` marking, zombie server after session death | `mcp_client.py:420-423`; `mcp_oauth.py:589-592`; `react_loop.py:121`; log: Sept 12 16:00 zombie |
| E | `_register_wrapper` / `_connect_server` never clean the server's old `_tool_to_server` entries (and `_connect_server` doesn't close the old wrapper) → 14 duplicate-registration warnings per re-auth, leaked wrappers | `mcp_client.py:888-899`, `:1810-1824`; log: 14 conflicts at 22:00:22 |

Production consequence: refresh tokens alternated True/False on consecutive job runs;
any full flow from a background call guarantees a 30s tool-timeout (vs 300s flow wait),
then session death ("unhandled errors in a TaskGroup"), then a zombie until a human
runs `/mcp auth`.

## Rejected alternatives

- **"Background auth URL lacks `prompt=consent`/`access_type=offline`" (H2)** — killed:
  logged background auth URLs contain both params; single daemon process held the config
  throughout.
- **"Background consent mints no refresh token"** — killed: Sept 11 09:59 background-triggered
  consent wrote `has_refresh=True`; the next silent refresh wiped it 6h later.
- **Patch/fork the `mcp` SDK** — rejected: keep the fix in our subclass/storage layer.
- **Disk merge alone (fix A only)** — insufficient: defect B still forces the full-flow
  button cycle on the second expiry.
- **Keep background-triggered interactive buttons as a feature** — rejected: a headless
  aliveness job must never depend on a human tapping Telegram within 30s; degrade to
  `needs_auth` instead.

## Final approach

| Fix | Change | Files |
|---|---|---|
| 1a | `set_tokens` merge-preserve: keep stored `refresh_token` when incoming token lacks one; incoming value wins when present (rotation preserved) | `mcp_oauth.py` |
| 1b | Subclass `OAuthClientProvider._handle_refresh_response` to re-attach the pre-refresh `refresh_token` in memory before persisting | `mcp_oauth.py` + build sites in `mcp_client.py` |
| 2 | After a successful interactive flow, register the long-lived wrapper with a non-interactive provider (`chat_id=None`, `on_non_interactive` set) | `mcp_client.py` `_run_oauth_flow` |
| 2b | Error-text handshake: background-path auth failures must surface text that `_is_mcp_auth_failure` recognizes and mark `needs_auth` so status shows 🔐 and the error says "Run `/mcp auth <name>`" | `mcp_client.py`, `react_loop.py` |
| 3 | Registration hygiene: purge the server's old `_tool_to_server` entries before re-registering; close the old wrapper in `_connect_server` | `mcp_client.py` |

## Cross-module data flows

- Interactive: `/mcp auth` (`telegram_mcp_commands.py:358`) → `start_oauth_flow`
  (`mcp_client.py:1226`) → `_run_oauth_flow` (`:1826`) → provider w/ chat_id (`:1845`) →
  probe (`:1857`) → wrapper registered (`:1915`). Post-fix the registered wrapper's
  provider is non-interactive; only the in-flight flow uses the chat-bound one.
- Background: scheduler job → react loop → `call_tool` → SDK transport
  (`auth=provider`) → `async_auth_flow`. Post-fix: refresh re-attaches RT (1b); a
  genuine full-auth need aborts via `on_non_interactive` → `needs_auth` → handshake
  error (2b) → react loop returns "Run `/mcp auth`".
- Persistence: SDK `set_tokens` → merge-preserving `FileTokenStorage.set_tokens`.

## Open questions

- OQ1: exact 2b mechanism (extend indicator list vs. reword abort/fast-fail errors vs.
  both) — decided in design.md.
- OQ2: merge preserves `refresh_token` only, not other fields — default adopted.
- OQ3: stale-contextvar OAuth logging (misleading trace/prompt IDs for 12h+) —
  observability-only; **out of scope**, follow-up change.