## 1. Vault Core and Config Schema

- [ ] 1.1 Add `VaultConfig` dataclass to `config_schema.py` with `type: str` field.
- [ ] 1.2 Add `agent_name` (default `"piclaw"`) and `agent_home` (default `""`) fields to `AgentConfig` in `config_schema.py`.
- [ ] 1.3 Add `vault` field to `AppConfig` in `config_schema.py`.
- [ ] 1.4 Implement `_load_vault(path: str) -> dict` JSON loader with validation and `ConfigError` on missing/invalid files.
- [ ] 1.5 Extend `expand_env()` in `config_schema.py` to support `sec:` prefix resolution against the vault.
- [ ] 1.6 Wire vault loading into `parse_config()` so `sec:` references are resolved before validation.
- [ ] 1.7 Remove `_resolve_file_secret()` function and all `*_file` fields from `config_schema.py`.
- [ ] 1.8 (Keep) `ProviderConfig` dataclass, `providers` field, `_parse_providers()`, and `_normalize_models()` provider inheritance remain in `config_schema.py` — they are not removed.
- [ ] 1.9 Remove `bot_token_file` resolution from `_parse_telegram()` in `config_schema.py`.
- [ ] 1.10 Remove `api_key_file` resolution from `_normalize_embeddings()` in `config_schema.py`.
- [ ] 1.11 Add tests in `tests/test_config_schema.py` for `sec:` resolution (found key, missing key, mixed with `env:`).
- [ ] 1.12 Add tests for vault file loading (valid JSON, missing file, invalid JSON).
- [ ] 1.13 Add tests for `agent_name`/`agent_home` defaults and custom values.
- [ ] 1.14 Remove all `*_file` provider/model/telegram tests from `tests/test_config_schema.py`. Keep provider inheritance tests.
- [ ] 1.15 Add tests for `sec:` resolution in provider fields (provider inherits `sec:` resolved value, model inherits from provider).
- [ ] 1.16 Run `pytest tests/test_config_schema.py -v` and fix failures.
- [ ] 1.17 Run `ruff check .` and fix style issues.
- [ ] 1.18 Run `vulture . vulture_whitelist.py --min-confidence 80` and update whitelist if needed.

## 2. Built-in Tool: `secret_get`

- [ ] 2.1 Add `secret_get` entry to `BUILTIN_TOOLS` dict in `builtin_executor.py`.
- [ ] 2.2 Implement `_exec_secret_get()` in `builtin_executor.py` with `_requires_confirmation()` gating.
- [ ] 2.3 Implement `_run_secret_get()` in `builtin_executor.py` that reads from the vault file and returns the value.
- [ ] 2.4 Wire vault path into `BuiltinExecutor` (pass from `main.py` or load from config).
- [ ] 2.5 Add tests for `secret_get` success, missing key, and user denial.
- [ ] 2.6 Run `pytest tests/test_builtin_executor.py -v` and fix failures.

## 3. System Prompt and Skills

- [ ] 3.1 Add `VAULT RULES` section to `SYSTEM_PROMPT_TEMPLATE` in `prompt_builder.py` instructing the LLM to use `secret_get` for unbound variables in skills.
- [ ] 3.2 Add `secret_get` to the built-in tools list in the system prompt.
- [ ] 3.3 Run `pytest tests/test_prompt_builder.py -v` and fix failures.

## 4. Main Wiring

- [ ] 4.1 In `main.py`, compute `agent_name` and `agent_home` from config.
- [ ] 4.2 In `main.py`, compute vault path from `$SPC_VAULT_FILE` or the default `~/.local/share/<agent_name>/secrets.json`.
- [ ] 4.3 Pass vault path to `BuiltinExecutor` and `AgentController`.
- [ ] 4.4 Run `pytest tests/test_main.py` (or relevant startup tests) and fix failures.

## 5. Documentation and Examples

- [ ] 5.1 Update `config.toml.example`: remove `*_file` examples; keep `[providers.*]` examples and add `sec:` usage within them; add `[vault]` section.
- [ ] 5.2 Update `README.md`: replace file-backed/provider secret guidance with vault instructions; document `sec:` usage and `secret_get` tool.
- [ ] 5.3 Update `README.md` with a clear reference table of all supported `[providers.<name>]` fields (`api_key`, `base_url`, `request_timeout`, `max_retries`, `retry_delay`) and which ones models inherit.
- [ ] 5.4 Update `README.md` and `config.toml.example` with OpenAI-compatible provider instructions (e.g. xAI Grok: `provider = "openai"`, `base_url = "https://api.x.ai/v1"`, `api_key = "sec:XAI_API_KEY"`).

## 6. Validation

- [ ] 6.1 Run full test suite: `pytest tests/ -v --tb=short`.
- [ ] 6.2 Run `ruff check .`.
- [ ] 6.3 Run `vulture . vulture_whitelist.py --min-confidence 80`.
- [ ] 6.4 Run `openspec validate vault-secret-manager --type change --strict`.
- [ ] 6.5 Mark remaining task checkboxes complete as work is done.
