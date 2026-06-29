## 1. Config Schema and Secret Resolution

- [ ] 1.1 Add typed provider-level configuration dataclasses and parser support for `[providers.<name>]` entries.
- [ ] 1.2 Add explicit file-backed secret fields for provider API keys, model API keys, embeddings API keys, and Telegram bot token.
- [ ] 1.3 Implement startup-time secret file reading with clear `ConfigError` messages for missing paths, unreadable files, and empty files.
- [ ] 1.4 Define and test precedence: explicit model or embeddings field, then matching provider field, then existing built-in fallback behavior.
- [ ] 1.5 Preserve existing `env:VAR` and explicit per-model configuration behavior without requiring provider sections.

## 2. Runtime Integration

- [ ] 2.1 Ensure resolved model configs expose the same `api_key`, `base_url`, timeout, and retry values expected by `llm_client.py`.
- [ ] 2.2 Ensure embeddings inherit matching provider credentials and base URL before falling back to the active model key.
- [ ] 2.3 Ensure Telegram startup consumes a resolved bot token regardless of whether it came from `bot_token` or `bot_token_file`.
- [ ] 2.4 Avoid logging or displaying newly introduced file-backed secret values in config or status output.

## 3. Documentation and Examples

- [ ] 3.1 Update `config.toml.example` with provider-level defaults and file-backed secret examples.
- [ ] 3.2 Add or update production `systemd --user` deployment documentation using `LoadCredential=`, `%d`, and `WorkingDirectory=`.
- [ ] 3.3 Document `EnvironmentFile=` and secret-manager env injection as compatibility fallbacks with subprocess-inheritance caveats.

## 4. Tests and Validation

- [ ] 4.1 Add config parser tests for provider inheritance, model override precedence, embeddings inheritance, and backward compatibility.
- [ ] 4.2 Add config parser tests for file-backed secret success, missing file, empty file, and environment-supplied file path cases.
- [ ] 4.3 Add documentation/example validation where practical to keep examples parseable.
- [ ] 4.4 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80` after code changes.
- [ ] 4.5 Run relevant pytest coverage for config parsing and runtime initialization.
- [ ] 4.6 Run `openspec validate support-systemd-credential-secrets --type change --strict` before archive.
