## 1. Code changes in `mcp_oauth.py`

- [ ] 1.1 Add `token_endpoint_auth_method: str = "client_secret_basic"` parameter to `FileTokenStorage.__init__` and store it as `self.token_endpoint_auth_method`
- [ ] 1.2 In `OAuthProviderFactory.build()`, pass `token_endpoint_auth_method=client_metadata.token_endpoint_auth_method` to the `FileTokenStorage` constructor (line ~392)
- [ ] 1.3 In `get_client_info()` pre-seed path (line ~185), add `token_endpoint_auth_method=self.token_endpoint_auth_method` to the `OAuthClientInformationFull` constructor
- [ ] 1.4 In `get_client_info()` cached path, inside the `if cached.client_secret == self.client_secret:` block, immediately before `return cached`: fill-if-None — if `cached.token_endpoint_auth_method is None`, replace with `cached = cached.model_copy(update={"token_endpoint_auth_method": self.token_endpoint_auth_method})`

## 2. Regression tests in `tests/test_mcp_oauth.py`

- [ ] 2.1 Add test: pre-seed path returns `token_endpoint_auth_method == "client_secret_basic"` when no token file exists
- [ ] 2.2 Add test: cached path with `token_endpoint_auth_method=None` is repaired to the storage's configured value
- [ ] 2.3 Add test: cached path with `token_endpoint_auth_method="client_secret_post"` (non-None) is preserved unchanged
- [ ] 2.4 Add test: `FileTokenStorage` constructed without the new param defaults to `"client_secret_basic"` (backward compat)

## 3. Verification

- [ ] 3.1 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80 --exclude interfaces.py` — no new violations
- [ ] 3.2 Run `pytest tests/test_mcp_oauth.py -v` — all tests pass including new regression tests
- [ ] 3.3 Run `make check` — full suite passes (baseline: 1627 passed, 1 skipped)
- [ ] 3.4 Run `openspec validate fix-oauth-client-secret-missing --type change --strict` — validation passes