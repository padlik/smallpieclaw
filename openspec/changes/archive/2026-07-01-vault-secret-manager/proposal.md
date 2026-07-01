## Why

Agent configurations currently scatter provider credentials, MCP tokens, and endpoint URLs across model entries and MCP server blocks. The existing `api_key_file`/`bot_token_file` mechanism depends on systemd `LoadCredential=`, is inflexible, and does not support non-secret config values (e.g. API base URLs). A single, agent-scoped secret vault lets operators store keys, tokens, and arbitrary strings in one place, reference them uniformly with a `sec:` prefix, and lets the LLM retrieve them programmatically with user approval.

## What Changes

- **Remove** `api_key_file`, `bot_token_file`, and `_resolve_file_secret` from config parsing and the entire codebase.
- **Add** `[vault]` config section with a `type` field (initially `file` only).
- **Add** `agent_name` (default `"piclaw"`) and `agent_home` (default `"~/<agent_name>"`) to `[agent]` config.
- **Add** `sec:` prefix resolver to `expand_env()` and config-time value substitution (`sec:VAR_NAME` looks up key in vault).
- **Add** a `secret_get` built-in tool (requires user confirmation before returning the value) so skills and the LLM can retrieve vault entries at runtime.
- **Add** vault file path: `~/.local/share/<agent_name>/secrets.json` (override via `$SPC_VAULT_FILE`).
- **Update** system prompt to instruct the LLM to look up unbound variables in the vault when executing skill instructions. Not all variables but only for Keys and API endpoints.
- **BREAKING**: Configs using `api_key_file` or `bot_token_file` will fail to parse; operators must migrate those secrets into the vault and replace with `sec:KEY_NAME`.
- **Keep** `[providers.*]` provider-level configuration; provider fields now support `sec:` references like any other config value.

## Capabilities

### New Capabilities
- `vault-secret-manager`: Centralized key-value secret store per agent, with a file-backed default backend. Supports arbitrary string values (keys, tokens, URLs, bearer headers).
- `vault-config-resolution`: Config values prefixed with `sec:` are resolved against the vault at startup during `expand_env()`.
- `vault-runtime-lookup`: Agent tools can read vault entries at runtime via `secret_get`, gated by user confirmation.
- `agent-scoped-directories`: Agent runtime uses `agent_name` and `agent_home` for shared state paths (vault, logs, etc.).

### Modified Capabilities
- Leave empty.

## Impact

- `config_schema.py`: Remove provider sections and `*_file` fields; add `[vault]`, `agent_name`, `agent_home`; extend `expand_env` with `sec:` prefix.
- `builtin_executor.py`: Add `secret_get` tool with confirmation flow.
- `prompt_builder.py`: Add instructions for vault-aware skill execution.
- `main.py`: Wire vault initialization into startup.
- `tests/test_config_schema.py`: Remove `*_file` tests; add `sec:` expansion tests.
- `config.toml.example`: Remove provider/file-backed examples; add `[vault]` and `sec:` examples.
- `README.md`: Update secret management section.
- `vulture_whitelist.py`: May need updates for removed symbols.
