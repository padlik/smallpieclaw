## Context

The current secret management uses `api_key_file`/`bot_token_file` fields with a file-reading resolver (`_resolve_file_secret`). This was designed for systemd `LoadCredential=` deployments but is inflexible, requires systemd knowledge, and does not support non-secret configuration values (e.g. base URLs, subdomain names). It also leaves provider credentials scattered across model entries.

There is no centralized place to store arbitrary key-value pairs (keys, tokens, URLs, bearer headers) that both config files and the running agent can reference.

## Goals / Non-Goals

**Goals:**

- Replace `api_key_file`/`bot_token_file` with a unified `sec:` prefix that resolves against a per-agent vault.
- Preserve `[providers.*]` provider-level sections and credential inheritance; provider fields now support `sec:` references like any other config value.
- Provide a file-backed vault (`~/.local/share/<agent_name>/secrets.json`) with a simple JSON format.
- Allow `sec:` references in any config string value (models, MCP env, provider fields, etc.).
- Provide a `secret_get` built-in tool gated by user confirmation so the LLM can retrieve vault entries at runtime.
- Add `agent_name` (default `"piclaw"`) and `agent_home` (default `"~/<agent_name>"`) to `[agent]` config for shared state paths.
- Instruct the LLM in the system prompt to look up unbound variables in the vault when executing skill instructions.

**Non-Goals:**

- Do not implement encrypted vault backends or cloud providers in this change; the `[vault]` section will have `type = "file"` only.
- Do not implement vault plugins in this change.
- Do not implement vault write/modify operations via the agent; the vault is read-only at runtime.
- Do not cache vault values in `os.environ` to avoid leaking to subprocesses.

## Decisions

### 1. Vault backend: simple JSON file

A single JSON file per agent:

```json
{
  "OPENAI_API_KEY": "sk-...",
  "JIRA_DOMAIN": "mycompany",
  "JIRA_API_KEY_CLOUD": "xyz",
  "OLLAMA_HOST": "http://localhost:11434"
}
```

Path: `~/.local/share/<agent_name>/secrets.json` (default).
Override via environment variable `$SPC_VAULT_FILE`.

The `[vault]` config section:

```toml
[vault]
type = "file"
```

Future types (encrypted-file, cloud) can be added without changing the lookup interface.

**Why not TOML?** JSON is simpler for a flat key-value store and avoids quoting ambiguity.

### 2. `sec:` prefix resolver

Extend `expand_env()` in `config_schema.py` to recognize a second prefix:

```python
_SEC_PREFIX = "sec:"
```

When a string value is exactly `sec:VAR_NAME`, the resolver:
1. Loads the vault file (lazy — only when a `sec:` reference is encountered).
2. Looks up `VAR_NAME` in the vault dict.
3. Returns the string value.
4. Raises `ConfigError` if the key is missing.

The vault is loaded once per `parse_config()` call and cached for the duration of resolution.

```python
def _resolve_sec(key: str, path: str, vault: dict) -> str:
    value = vault.get(key)
    if value is None:
        raise ConfigError(f"Vault key '{key}' referenced in config{path} is not found. Add it to the vault.")
    return str(value)
```

### 3. `secret_get` built-in tool

Add a new built-in tool `secret_get` that retrieves a value from the vault at runtime.

```python
def _exec_secret_get(self, args: dict, caller_tag: str = "") -> dict:
    key = args.get("key", "")
    if not key:
        return {"success": False, "error": "secret_get: 'key' is required."}
    # Requires user confirmation BEFORE consulting the vault.
    # This prevents the agent from probing for existing keys.
    return self._requires_confirmation("secret_get", args, f"Look up vault key '{key}'", caller_depth=0, caller_tag=caller_tag)
```

In the Telegram UI, this renders as:
> 🔑 **Secret Lookup Requested**
> Agent wants to look up a secret named `OLLAMA_HOST` in the vault. Approve?
> [Approve] [Deny]

**Important**: The vault is consulted ONLY after user approval. If the user approves but the key does not exist, the tool returns an error indicating the key was not found. If the user denies, the tool returns an error and the agent must handle it (e.g. abort the task).

**Future**: This per-key approval will be replaced by a single "Open vault" request that grants the agent temporary access to read any key.

### 4. System prompt instruction for vault-aware skills

Add a new rule in `SYSTEM_PROMPT_TEMPLATE`:

```
VAULT RULES:
- When a SKILL.md or task references an unbound key or API endpoint variable (e.g. "Set OLLAMA_HOST to your endpoint" or "use your API_KEY"), use the `secret_get` tool to retrieve it from the vault.
- Do NOT guess values. If a vault key is missing, report the error and stop.
- Vault keys are case-sensitive and match the names in the vault exactly.
```

### 5. Remove `api_key_file`, `bot_token_file`, keep provider sections

Delete from `config_schema.py`:
- `_resolve_file_secret()` function
- `api_key_file` resolution in `_normalize_models`, `_normalize_embeddings`, `_parse_telegram`
- `bot_token_file` resolution in `_parse_telegram`

**Keep** `ProviderConfig`, `providers` field, `_parse_providers()`, and `_normalize_models()` — they remain useful for provider-wide defaults.

Provider fields now support `sec:` references just like model fields:

```toml
[providers.openai]
api_key = "sec:OPENAI_API_KEY"
base_url = "https://api.openai.com/v1"

[[models]]
provider = "openai"
model = "gpt-4o-mini"
# Inherits api_key from provider above
```

Migration for existing `*_file` users: replace `api_key_file = "..."` with `api_key = "sec:KEY_NAME"` (or any other provider field that used `*_file`).

### 6. `agent_name` and `agent_home` in `[agent]`

Add two new fields to `AgentConfig`:

```python
@dataclass(frozen=True)
class AgentConfig:
    ...
    agent_name: str = "piclaw"
    agent_home: str = ""
```

Both default from `agent_name` when empty:

| Config | `agent_name` | `agent_home` | Vault path |
|--------|-------------|--------------|------------|
| Default | `"piclaw"` | `""` → `~/piclaw/` | `~/.local/share/piclaw/secrets.json` |
| Custom name | `"mybot"` | `""` → `~/mybot/` | `~/.local/share/mybot/secrets.json` |
| Custom home | `"openbot"` | `"/opt/openbot"` | `~/.local/share/openbot/secrets.json` |
| Custom everything | `"tel_bot"` | `"/opt/mybot"` | `$SPC_VAULT_FILE` → `/opt/agents/data/mybot/secrets.json` |

**Rule**: `agent_home` and vault path are independent. Each defaults from `agent_name` but can be overridden explicitly without affecting the other.

The vault path is computed as:
```python
vault_path = os.environ.get("SPC_VAULT_FILE") or os.path.expanduser(
    f"~/.local/share/{agent_name}/secrets.json"
)
```

## Risks / Trade-offs

- [Risk] Removing `api_key_file` breaks existing production configs using systemd `LoadCredential=`. → Mitigation: Provide clear migration path in README.
- [Risk] Vault file is unencrypted JSON. → Mitigation: Document that this is a deliberate first step; encrypted backends are a future `[vault]` type.
- [Risk] `secret_get` confirmation adds friction for skill execution. → Mitigation: Future "approve-all for vault lookups" option; for now explicit approval is correct.
- [Risk] `sec:` resolution at config parse time means the vault must exist before startup. → Mitigation: Fail fast with clear error message listing missing keys.
- [Risk] Vault values are strings only. → Mitigation: Document this; complex values can be JSON-stringified.

## Migration Plan

1. Implement vault loader and `sec:` resolver.
2. Add `secret_get` built-in tool with confirmation flow.
3. Remove `*_file` and provider sections from config parsing.
4. Add `agent_name`/`agent_home` to config.
5. Update config example and README.
6. Update tests.
7. Validate: existing configs using `env:` continue to work; configs using `*_file` fail with clear migration instructions.

## Open Questions (Resolved)

- ✅ Should the vault file be auto-created as `{}` if missing? **Answer: No** — fail fast so the operator knows something is wrong.
- ✅ Should `secret_get` return the raw value or wrap it? **Answer: Raw value** in `output`, like `memory_write get`.
- ✅ Should we support a `--init-vault` CLI flag? **Answer: No** — manual JSON editing is fine.
- ✅ Should `sec:` work inside MCP `[mcp_servers.env]` and `[mcp_servers.headers]`? **Answer: Yes** — `expand_env` runs on the entire raw dict before parsing.
