# Review Log — mcp-oauth-auth-logging

## all Round 1 — 2026-08-07

Full review of proposal.md, design.md, and tasks.md (none frozen; no specs/ or explore-brief.md present). Verified against `mcp_oauth.py`, `mcp_client.py`, `config_schema.py`, `main.py`, `agent_logging.py`.

### 🔴 Serious (blockers — fix before freezing)

- **S1 — `McpOAuthConfig` does not exist; the real class is `OAuthConfig`.** Wrong name in all three artifacts (`proposal.md:26`, `design.md:37-38` D2, `tasks.md:3-7` T1). The actual dataclass is `OAuthConfig`, built by `_parse_oauth` at `config_schema.py:642`, stored as `MCPServerConfig.oauth` (line 679). Rename to `OAuthConfig` everywhere.

- **S2 — The `trace` flag's typed-config change (T1) is disconnected from the runtime read path, and T1 is incomplete.** (1) Runtime reads the raw dict, not the typed config: `main.py:464` passes raw TOML dicts into `MCPManager`; `OAuthProviderFactory.build()` reads `oauth_cfg.get("trace", False)` off the raw `server_cfg["oauth"]` dict. The typed `OAuthConfig` field is never consulted. (2) T1 as written never populates the field: `_parse_oauth` (config_schema.py:627-653) builds `OAuthConfig(...)` from a fixed field list and does not parse `trace`. Adding the dataclass field without adding `trace=_parse_bool(...)` to `_parse_oauth` means the typed field is always `False`. An implementer wiring `build()` to the typed field will get a flag that is permanently `False` — trace mode silently never activates. Fix: pick one path and state it explicitly. Either (a) drop T1's dataclass change and document `trace` as a raw oauth TOML key, or (b) add `trace` to both `OAuthConfig` and `_parse_oauth`, and note runtime still reads the raw dict. Revisit T7 — vulture will flag the unused field, which is the symptom of this defect, not something to whitelist away.

- **S3 — Structural contradiction: a new capability is declared, but there is no spec delta.** `proposal.md:16` declares `mcp-oauth-auth-trace`; `proposal.md:20` claims "no spec-level behaviour changes"; no `specs/` directory exists. `openspec validate --strict` will reject a change that declares a capability with zero deltas. Resolve: (a) drop the "New Capabilities" entry and describe `trace` as a config-flag behavior (cheapest), or (b) keep it and add `specs/mcp-oauth-auth-trace/spec.md` with `## ADDED Requirements` + a scenario.

### 🟡 Minor (should fix)

- **M1 — T5c "augment the existing `logger.error`" is factually wrong.** The `call_tool` timeout handler (`mcp_client.py:242-244`) only sets `self.last_error` and returns; it emits no log record. The implementer must add a log, not augment one. Also the exception caught is `concurrent.futures.TimeoutError`, not builtin `TimeoutError` — T5c:88 should name it precisely. `proposal.md:10` and `design.md:9/22` repeat the false premise.

- **M2 — D4's second clause is unimplementable as stated.** `design.md:48` says `_prepare_oauth_provider()` "reads the already-computed info" from `get_tokens()`, but `get_tokens()` returns an `OAuthToken` carrying no `issued_at` or computed `remaining` — no channel to pass the value out. T5a sensibly logs only "stored token found," so tasks are fine; delete the misleading D4 sentence.

- **M3 — `state_match=<T/F>` is undefined when no expected_state is set (T3b).** `_handle` only validates state when `self.expected_state is not None` (mcp_oauth.py:260). T3b:45 should specify the value in that case (e.g., `state_match=n/a`).

- **M4 — Log-prefix convention deviates for `CallbackServer`.** T3a/T3b use `MCP OAuth callback …` with no server name, while all other new logs use `MCP [<server>] …`. `CallbackServer.__init__` receives only port/bind/cert/key/loop, not the server name — the design should note this constraint or thread `server_name` in.

- **M5 — `proposal.md:24` lists `make_callback_handler` in Impact, but no task touches it.** Trim it to avoid implying a change that isn't specified.

### ✅ Strengths

- Root-cause framing (Gmail false-positive) directly motivates T6c — well-earned.
- Security model is sound: `access_token`/`client_secret` never logged; auth URL gated behind `trace=true`; `state` truncated to 8 chars. Verified across all tasks.
- D1 (stdlib logging, not structlog) is correct — confirmed all modules use `logging.getLogger(__name__)`; `log_event()`/`LogEvent` is optional taxonomy sugar, not required.
- D3 (WARNING not error) well-argued; T6c correctly preserves `{"success": True}` return.
- Impact list mostly accurate — all named functions/methods verified to exist.
- Task granularity good — all well under 2h, concrete enough to implement.

### 🔴 Outstanding

- S1, S2, S3 must be resolved before any artifact can be frozen.
- M1, M2 should be addressed to avoid implementer confusion.

### 💡 Recommendations

1. Reuse existing expiry logic from `MCPManager.get_token_info` (mcp_client.py:538-539) instead of duplicating in T2a.
2. Add two targeted tests despite T8's "no tests needed": (a) false-positive WARNING fires when flow succeeds with no token file, (b) auth URL logged only when `trace=true`. Both are observable and cheap with `caplog`.
3. If S2 is fixed by dropping the unused typed field, T7's whitelist step becomes unnecessary; if kept, ensure `_parse_oauth` reads it so vulture sees a real consumer.

## all Round 2 — 2026-08-07

Re-review after fixes for S1–S3, M1–M5. Verified all three artifacts against codebase.

### 🔴 Fixed
- S1: `McpOAuthConfig` → `OAuthConfig` corrected in proposal:9,26, design D2:36-38, T1:3-5. One leftover in T7:132 fixed post-review (N1).
- S2: T1 now adds `trace` to both `OAuthConfig` dataclass AND `_parse_oauth` field list. D2 explicitly documents typed-config validation path vs runtime raw-dict read path. Consistent across all artifacts.
- S3: Dropped "New Capabilities" entry from proposal. `trace` reframed as config-level diagnostic toggle. "No spec changes" is now honest.
- M1: T5c now says "add a `logger.error`" (not "augment"). Names `concurrent.futures.TimeoutError`. Proposal and design corrected.
- M2: D4 rewritten — `_prepare_oauth_provider()` logs without computed remaining seconds (OAuthToken carries no `remaining` field).
- M3: T3b now specifies `state_match=n/a` when `self.expected_state is None`.
- M4: Design Risks section notes `CallbackServer` constructor constraint (no server name).
- M5: `make_callback_handler` removed from proposal Impact list.
- Bonus: T8a adds two targeted tests (false-positive WARNING + trace-gated auth URL).

### 🟡 Addressed
- N1: Stray `McpOAuthConfig` in T7:132 → corrected to `OAuthConfig` post-review.
- N2: `_parse_bool` error-label string in T1 changed from hardcoded `"oauth.trace"` to `f"{section}.trace"` for consistency with existing `_parse_oauth` convention.

### 🔴 Outstanding
_(none — all blockers and minor issues resolved)_

### ⚖️ Verdict
**Ready to freeze.** All three Round 1 blockers (S1, S2, S3) and all five minor issues (M1–M5) resolved. N1 and N2 corrected post-review. Artifacts tell a single consistent story with no contradictions. Change is clean, low-risk, and cleared for apply.

### Freeze Status
- `proposal.md` — **frozen**
- `design.md` — **frozen**
- `tasks.md` — **frozen**
## post-apply code review — 2026-08-07
### 🔴 Fixed
 - `get_tokens()` TTL diagnostic could discard a valid token: `int()` on a corrupt/non-finite `issued_at` raised outside the intended guard → separated token construction from diagnostics, widened to `(TypeError, ValueError, OverflowError)`, added parametrized regression test
 - trace=false test was vacuous (drove the `tg_iface=None` branch, which logs the URL via the pre-existing WARNING) → rewritten to use a mock Telegram interface + chat_id and assert the URL is absent from all records; mutation-verified
### 🟡 Addressed
 - unreachable `state_match=no` arm in `CallbackServer._handle` → replaced with a `validated_state` boolean plus explanatory comment
 - `OAuthConfig.trace` comment claimed it "promotes auth events to INFO" (not implemented; all new auth events are unconditionally INFO per tasks.md) → corrected to describe URL gating and the raw-dict runtime read path
### 🔴 Outstanding
 - None. D5's gate does not cover the non-interactive fallback WARNING; operator decision (2026-08-07) is to leave the URL in that log unchanged, since it is the only re-auth channel when no chat is in context. Documented as a boundary condition in design.md D5.
