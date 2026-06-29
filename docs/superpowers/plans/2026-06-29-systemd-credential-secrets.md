# Systemd Credential Secrets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add provider-level model/embedding defaults and file-backed secret resolution suitable for `systemd --user` credentials.

**Architecture:** Keep runtime consumers simple by resolving inheritance and file-backed secrets during `parse_config()`. Typed dataclasses and `app_cfg._raw` should both contain runtime-ready secret values and inherited defaults so `LLMClient`, Telegram startup, and existing callers continue to consume plain resolved config.

**Tech Stack:** Python dataclasses, TOML config parsing, pytest, ruff, vulture, OpenSpec.

## Global Constraints

- Follow PEP 8 style guidelines.
- Include type hints for function parameters and return types.
- Write docstrings for all public modules, classes, functions, and methods.
- After every code change, run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80`.
- Use TDD: write failing tests before production changes.
- Preserve existing `env:VAR` and explicit per-model config behavior.
- Do not implement direct cloud secret-manager SDK integration.
- Do not rotate secrets at runtime or re-read files per request.
- Do not redesign subprocess environment inheritance.

---

## File Structure

- Modify `config_schema.py`: provider config dataclass, file-secret resolver, parser inheritance logic, raw dict normalization.
- Modify `tests/test_config_schema.py`: config parser tests for provider inheritance, file-backed secrets, precedence, backward compatibility, and raw dict behavior.
- Modify `config.toml.example`: provider defaults, file-backed secret examples, and fallback guidance.
- Modify `README.md`: `systemd --user` production service guidance using `LoadCredential=`, `%d`, and `WorkingDirectory=`.
- Modify `openspec/changes/support-systemd-credential-secrets/tasks.md`: mark completed OpenSpec tasks.

## Phase 1: Parser Tests and Implementation

### Required Contract Notes

- Normalize `app_cfg._raw` as the primary runtime contract; runtime consumers read raw dict values, not typed dataclasses.
- Resolve provider defaults and file-backed secrets into `_raw["telegram"]`, `_raw["models"]`, and `_raw["embeddings"]` before runtime use.
- Reject same-level ambiguous secret sources: `api_key` plus `api_key_file`, and `bot_token` plus `bot_token_file`.
- Apply cross-level secret precedence by logical secret source: model or embeddings value/file source wins over provider value/file source.
- Strip at most one trailing newline sequence from secret files; do not call `.strip()` for stored secret values.
- Reject zero-byte and newline-only secret files with field-specific `ConfigError` messages that do not include secret contents.
- Preserve legacy `_raw` unchanged when no provider section and no `*_file` fields are present.
- If `vulture` flags new public config symbols, update `vulture_whitelist.py`.

### Task 1: Provider Defaults and Backward Compatibility

**Files:**
- Modify: `tests/test_config_schema.py`
- Modify: `config_schema.py`

**Interfaces:**
- Produces: `ProviderConfig` dataclass with `name: str`, `api_key: str`, `base_url: str`, `request_timeout: int | None`, `max_retries: int | None`, `retry_delay: int | None`.
- Produces: `AppConfig.providers: dict[str, ProviderConfig]`.
- Consumes: existing `parse_config(raw: dict) -> AppConfig`.

- [ ] Step 1: Add failing tests for provider inheritance and explicit model backward compatibility.
- [ ] Step 2: Run focused pytest and verify the provider tests fail because `providers` is unsupported.
- [ ] Step 3: Implement `ProviderConfig`, provider parser, model inheritance precedence, and raw dict normalization.
- [ ] Step 4: Run focused pytest and verify these tests pass.

### Task 2: File-Backed Secret Resolution

**Files:**
- Modify: `tests/test_config_schema.py`
- Modify: `config_schema.py`

**Interfaces:**
- Produces: internal helper `_resolve_secret_file(path: str, field_path: str) -> str`.
- Produces: support for `api_key_file` and `bot_token_file` on relevant config sections.

- [ ] Step 1: Add failing tests for successful file-backed provider key, env-supplied file path, missing file, empty file, newline handling, ambiguous value/file combinations, and Telegram `bot_token_file`.
- [ ] Step 2: Run focused pytest and verify the new file-secret tests fail for missing implementation.
- [ ] Step 3: Implement explicit file-backed secret fields, one-trailing-newline stripping, empty-secret rejection, and field-specific `ConfigError` messages.
- [ ] Step 4: Run focused pytest and verify file-secret tests pass.

### Task 3: Embeddings Inheritance and Runtime Compatibility

**Files:**
- Modify: `tests/test_config_schema.py`
- Modify: `config_schema.py`

**Interfaces:**
- Consumes: provider parser from Task 1 and file resolver from Task 2.
- Produces: normalized `app_cfg._raw["embeddings"]` with provider-inherited `api_key` and `base_url` when omitted.

- [ ] Step 1: Add failing tests for embeddings inheriting provider credentials/base URL and preserving active-model fallback when no provider credential exists.
- [ ] Step 2: Run focused pytest and verify expected failures.
- [ ] Step 3: Implement embeddings inheritance before existing `LLMClient` fallback.
- [ ] Step 4: Run focused pytest and verify all config schema tests pass.

## Phase 2: Documentation and Examples

### Task 4: Config Example Update

**Files:**
- Modify: `config.toml.example`

**Interfaces:**
- Consumes: parser syntax implemented in Phase 1.
- Produces: documented `[providers.<name>]`, `api_key_file`, `bot_token_file`, and legacy env fallback examples.

- [ ] Step 1: Update the config example to show provider defaults and file-backed secret patterns.
- [ ] Step 2: Verify examples match implemented field names exactly.

### Task 5: README systemd --user Guidance

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: deployment pattern from `design.md`.
- Produces: user-service example with `LoadCredential=`, `%d`, `WorkingDirectory=`, and fallback caveats.

- [ ] Step 1: Replace or supplement system-service guidance with a `systemd --user` production example.
- [ ] Step 2: Include caveats for `EnvironmentFile=`, secret-manager wrappers, and subprocess inheritance.

## Phase 3: OpenSpec Task Checkboxes and Validation

### Task 6: Mark Tasks and Validate

**Files:**
- Modify: `openspec/changes/support-systemd-credential-secrets/tasks.md`

**Interfaces:**
- Consumes: completed implementation from Phases 1-2.
- Produces: OpenSpec tasks marked complete.

- [ ] Step 1: Mark tasks 1.1 through 4.3 complete after matching work is done.
- [ ] Step 2: Run `pytest tests/test_config_schema.py -v`.
- [ ] Step 3: Run `openspec validate support-systemd-credential-secrets --type change --strict`.
- [ ] Step 4: Run `ruff check .`.
- [ ] Step 5: Run `vulture . vulture_whitelist.py --min-confidence 80`.
- [ ] Step 6: Run broader tests if focused tests and lint are clean.
- [ ] Step 7: Mark validation tasks 4.4 through 4.6 complete only after commands pass.

## Self-Review

- Spec coverage: provider model inheritance, embeddings inheritance, explicit overrides, file-backed secrets, systemd guidance, fallback caveats, and strict validation are covered.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: `ProviderConfig`, `api_key_file`, `bot_token_file`, and `providers` names are used consistently.
