## 1. Config Schema and Secret Resolution

- [x] 1.1 Add typed provider-level configuration dataclasses and parser support for `[providers.<name>]` entries.
- [x] 1.2 Add explicit file-backed secret fields for provider API keys, model API keys, embeddings API keys, and Telegram bot token.
- [x] 1.3 Implement startup-time secret file reading with clear `ConfigError` messages for missing paths, unreadable files, and empty files.
- [x] 1.4 Define and test precedence: explicit model or embeddings field, then matching provider field, then existing built-in fallback behavior.
- [x] 1.5 Preserve existing `env:VAR` and explicit per-model configuration behavior without requiring provider sections.

## 2. Runtime Integration

- [x] 2.1 Ensure resolved model configs expose the same `api_key`, `base_url`, timeout, and retry values expected by `llm_client.py`.
- [x] 2.2 Ensure embeddings inherit matching provider credentials and base URL before falling back to the active model key.
- [x] 2.3 Ensure Telegram startup consumes a resolved bot token regardless of whether it came from `bot_token` or `bot_token_file`.
- [x] 2.4 Avoid logging or displaying newly introduced file-backed secret values in config or status output.

## 3. Documentation and Examples

- [x] 3.1 Update `config.toml.example` with provider-level defaults and file-backed secret examples.
- [x] 3.2 Add or update production `systemd --user` deployment documentation using `LoadCredential=`, `%d`, and `WorkingDirectory=`.
- [x] 3.3 Document `EnvironmentFile=` and secret-manager env injection as compatibility fallbacks with subprocess-inheritance caveats.

## 4. Tests and Validation

- [x] 4.1 Add config parser tests for provider inheritance, model override precedence, embeddings inheritance, and backward compatibility.
- [x] 4.2 Add config parser tests for file-backed secret success, missing file, empty file, and environment-supplied file path cases.
- [x] 4.3 Add documentation/example validation where practical to keep examples parseable.
- [x] 4.4 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80` after code changes.
- [x] 4.5 Run relevant pytest coverage for config parsing and runtime initialization.
- [x] 4.6 Run `openspec validate support-systemd-credential-secrets --type change --strict` before archive.
