## 1. Vault Core and Config Schema

- [x] 1.1 Add `VaultConfig` dataclass to `config_schema.py` with `type: str` field.
- [x] 1.2 Add `agent_name` (default `"piclaw"`) and `agent_home` (default `""`) fields to `AgentConfig` in `config_schema.py`.
- [x] 1.3 Add `vault` field to `AppConfig` in `config_schema.py`.
- [x] 1.4 Implement `_load_vault(path: str) -> dict` JSON loader with validation and `ConfigError` on missing/invalid files.
- [x] 1.5 Extend `expand_env()` in `config_schema.py` to support `sec:` prefix resolution against the vault.
- [x] 1.6 Wire vault loading into `parse_config()` so `sec:` references are resolved before validation.
- [x] 1.7 Remove `_resolve_file_secret()` function and all `*_file` fields from `config_schema.py`.
- [x] 1.8 (Keep) `ProviderConfig` dataclass, `providers` field, `_parse_providers()`, and `_normalize_models()` provider inheritance remain in `config_schema.py` — they are not removed.
- [x] 1.9 Remove `bot_token_file` resolution from `_parse_telegram()` in `config_schema.py`.
- [x] 1.10 Remove `api_key_file` resolution from `_normalize_embeddings()` in `config_schema.py`.
- [x] 1.11 Add tests in `tests/test_config_schema.py` for `sec:` resolution (found key, missing key, mixed with `env:`).
- [x] 1.12 Add tests for vault file loading (valid JSON, missing file, invalid JSON).
- [x] 1.13 Add tests for `agent_name`/`agent_home` defaults and custom values.
- [x] 1.14 Remove all `*_file` provider/model/telegram tests from `tests/test_config_schema.py`. Keep provider inheritance tests.
- [x] 1.15 Add tests for `sec:` resolution in provider fields (provider inherits `sec:` resolved value, model inherits from provider).
- [x] 1.16 Run `pytest tests/test_config_schema.py -v` and fix failures.
- [x] 1.17 Run `ruff check .` and fix style issues.
- [x] 1.18 Run `vulture . vulture_whitelist.py --min-confidence 80` and update whitelist if needed.

## 2. Built-in Tool: `secret_get`

- [x] 2.1 Add `secret_get` entry to `BUILTIN_TOOLS` dict in `builtin_executor.py`.
- [x] 2.2 Implement `_exec_secret_get()` in `builtin_executor.py` with `_requires_confirmation()` gating.
- [x] 2.3 Implement `_run_secret_get()` in `builtin_executor.py` that reads from the vault file and returns the value.
- [x] 2.4 Wire vault path into `BuiltinExecutor` (pass from `main.py` or load from config).
- [x] 2.5 Add tests for `secret_get` success, missing key, and user denial.
- [x] 2.6 Run `pytest tests/test_builtin_executor.py -v` and fix failures.

## 3. System Prompt and Skills

- [x] 3.1 Add `VAULT RULES` section to `SYSTEM_PROMPT_TEMPLATE` in `prompt_builder.py` instructing the LLM to use `secret_get` for unbound variables in skills.
- [x] 3.2 Add `secret_get` to the built-in tools list in the system prompt.
- [x] 3.3 Run prompt-related tests and fix failures.

## 4. Main Wiring

- [x] 4.1 In `main.py`, compute `agent_name` and `agent_home` from config.
- [x] 4.2 In `main.py`, compute vault path from `$SPC_VAULT_FILE` or the default `~/.local/share/<agent_name>/secrets.json`.
- [x] 4.3 Pass vault path to `BuiltinExecutor` and `AgentController`.
- [x] 4.4 Verified by full test suite — no `test_main.py` exists.

## 5. Documentation and Examples

- [x] 5.1 Update `config.toml.example`: remove `*_file` examples; add `sec:` and vault usage; add `[vault]` section.
- [x] 5.2 Update `README.md`: replace file-backed/provider secret guidance with vault instructions; document `sec:` usage and `secret_get` tool.
- [x] 5.3 Update `README.md` with provider field reference table (`api_key`, `base_url`, `request_timeout`, `max_retries`, `retry_delay`).
- [x] 5.4 Update `README.md` and `config.toml.example` with OpenAI-compatible provider instructions (xAI Grok example).

## 6. Validation

- [x] 6.1 Full test suite: `pytest tests/ -v --tb=short` — **840 passed, 1 skipped**.
- [x] 6.2 `ruff check .` — **All checks passed**.
- [x] 6.3 `vulture . vulture_whitelist.py --min-confidence 80` — **Clean (no project-level issues)**.
- [x] 6.4 `openspec validate vault-secret-manager --type change --strict` — **Change 'vault-secret-manager' is valid**.
- [x] 6.5 All task checkboxes marked complete.