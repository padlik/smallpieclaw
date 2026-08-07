## proposal Round 1 — 2026-08-07
### 🔴 Fixed
(none)
### 🟡 Addressed
- Test-impact note added to proposal.md: existing test_mcp_oauth_logging.py warning assertions need updating for probe-conditioned warning
### 🔴 Outstanding
(none — batch frozen)

## design Round 1 — 2026-08-07
### 🔴 Fixed
- D6 discrimination mechanism was unimplementable: httpx auth flow consumes intermediate 401 internally, returned response status is never 401; token-file existence alone can't distinguish "200 no auth" from "401 flow failed". Fixed: D6 now uses httpx event_hooks response callback to record any intermediate 401/403, providing an observable signal that the auth flow was attempted.
### 🟡 Addressed
- D4 timeout rationale corrected: httpx timeout bounds socket operations, not wall-clock; operator window enforced by wait_for_callback(timeout=300) separately
- Risk section reconciled with D2 GET: non-401/200 final status now keys off event-hook flag, not final response status; handles 405/406 after successful auth retry
- D5 cancellability confirmed: wait_for_callback uses asyncio.wait_for on a Future, which is cancellable; CancelledError propagates through callback_handler → async_auth_flow → probe task
### 🔴 Outstanding
(none)

## design Round 2 — 2026-08-07
### 🔴 Fixed
(none)
### 🟡 Addressed
- D6 event-hook flag extended to track 403 (insufficient-scope) in addition to 401, since both trigger _perform_authorization and redirect_handler; flag renamed conceptually to "auth challenge" to match Risk section's own statement
### 🔴 Outstanding
(none — batch frozen)

## specs Round 1 — 2026-08-07
### 🔴 Fixed
- 401-vs-403 divergence between design D6 and spec: design was already updated to "401 or 403" before review; two residual 401-only references in Risk section and open-questions mapping fixed to "401 or 403"
### 🟡 Addressed
- Mechanism leakage: "detects auth challenge via event hook" scenario replaced with two observable-behavior scenarios ("OAuth flow fired but did not complete" → warning; "Proactive probe confirms server does not require OAuth" → no warning, INFO log)
- proposal.md:25 soft-amended: "received a 401" → "triggered an authorization challenge (401 or 403)"
- Duplicated no-warning scenario trimmed from spec (behavior already covered by probe-200 scenario)
### 🔴 Outstanding
(none — batch frozen)

## tasks Round 1 — 2026-08-07
### 🔴 Fixed
(none)
### 🟡 Addressed
- D4 timeout added to task 1.1: httpx.AsyncClient timeout=oauth_cfg.get("timeout", 300) with rationale note
- Probe error handling added to task 1.2: on probe exception (non-401/200/403 or callback timeout), log WARNING and proceed to session connection as fallback
- Callback-server-before-probe ordering confirmed in task 1.2: cb_server.start() at line 924 must be before the probe fires
- 403 test added to task 4.3: parametrized variant asserting probe_saw_auth_challenge=True on 403, same warning-conditioning as 401
- Non-200 fallback test added as task 4.4: probe returns 500 → no auth challenge → WARNING → session connection attempted
### 🔴 Outstanding
(none — batch frozen)