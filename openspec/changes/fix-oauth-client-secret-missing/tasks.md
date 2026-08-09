## 1. Code changes in `mcp_oauth.py`

- [x] 1.1 Add `token_endpoint_auth_method: str = "client_secret_basic"` parameter to `FileTokenStorage.__init__` and store it as `self.token_endpoint_auth_method`
- [x] 1.2 In the `get_client_info()` pre-seed path (the final `return OAuthClientInformationFull(...)`), add `token_endpoint_auth_method=self.token_endpoint_auth_method` to the `OAuthClientInformationFull` constructor

## 2. Regression tests in `tests/test_mcp_oauth.py`

- [x] 2.1 Add test: pre-seed path returns the storage's configured `token_endpoint_auth_method`. Configure a value that is **not** the constructor default (`"client_secret_post"`), so the test fails if the parameter is ignored
- [x] 2.2 Add test: `FileTokenStorage` constructed without the new param defaults to `"client_secret_basic"` (backward compat)
- [x] 2.3 Add test: a `client_info` block that cannot be parsed logs a warning and falls back to the pre-seed instead of raising. Write the malformed block directly to the token file rather than round-tripping it through `set_client_info`, so the test does not depend on how client_info is persisted
- [x] 2.4 Fix `test_token_storage_preserves_client_info`, which asserted only facts that were also true of the pre-seed fallback and so never exercised the cached path it names: give the cached block a `client_id` distinct from the configured one, and a non-None `redirect_uris` so it survives the `exclude_none=True` round-trip and re-parses
- [x] 2.5 In `test_build_returns_provider`, assert the auth method reaches the storage the provider is built with (`provider.context.storage.token_endpoint_auth_method`). This pins the constructor default arriving via `build()`; it does not pin a kwarg, since `build()` passes none
- [x] 2.6 Add test: the written token file nests the grant under a `token` key with an `issued_at` stamp and no `client_info` block. Assert on the raw JSON, since `get_tokens()` reads `data.get("token") or data` and so cannot distinguish the nested layout from a flat one. Pins the corrected "Token file created with restrictive permissions" spec scenario

## 3. Verification

- [x] 3.1 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80 --exclude interfaces.py` — no new violations
- [x] 3.2 Run `pytest tests/test_mcp_oauth.py -v` — all tests pass including new regression tests
- [x] 3.3 Mutation-check the new tests: making `__init__` ignore the parameter, dropping the pre-seed kwarg, and narrowing the malformed-block handler to `except TypeError` must each fail at least one test
- [x] 3.4 Run `make check` — full suite passes (baseline: 1627 passed, 1 skipped)
- [x] 3.5 Run `openspec validate fix-oauth-client-secret-missing --type change --strict` — validation passes

## 4. Post-review revisions (2026-08-09)

A high-effort code review after implementation proved two planned mechanisms inert.
Both were removed; see `review-log.md` and the `explore-brief.md` addendum.

- [x] 4.1 Remove the `token_endpoint_auth_method=` kwarg from the `FileTokenStorage` construction in `OAuthProviderFactory.build()` — a tautology equal to the constructor default, confirmed by mutation to be unpinned by any test
- [x] 4.2 Remove the fill-if-None `model_copy` repair from the `get_client_info()` cached path — that path is unreachable, since nothing ever writes a `client_info` block
- [x] 4.3 Delete the tests that pinned the removed behavior: the two cached-path auth-method tests and the `build()` constructor-spy test
- [x] 4.4 Update `proposal.md`, `design.md`, `specs/mcp-oauth-flow/spec.md`, and this file to match; record the accepted `client_secret_basic`-for-all-providers trade-off and the deferred `OAuthConfig` knob
- [x] 4.5 Re-run lint, mutation checks, `make check`, and `openspec validate --strict` after the removals
