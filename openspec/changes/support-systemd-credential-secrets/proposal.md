## Why

Production deployments currently require repeating API key references across every model entry, and `systemd --user` deployments push operators toward environment variables that expose long-lived secrets to the agent process and its subprocesses. This change reduces model configuration duplication while enabling safer secret delivery through systemd credential files or other file-based secret providers.

## What Changes

- Add provider-level credential configuration so models can inherit `api_key`, `api_key_file`, `base_url`, retry defaults, and similar provider-scoped values without repeating them per model.
- Add file-based secret references for string secrets, allowing config to point to a protected file path directly or through an environment variable that contains only the file path.
- Preserve existing model-level fields as overrides so current configurations continue to work.
- Document a `systemd --user` production pattern using `LoadCredential=` and file-path environment variables rather than secret-value environment variables.
- Ensure secret resolution fails clearly on missing files, missing env vars, or invalid combinations.
- Avoid changing runtime model selection semantics beyond credential/default inheritance.

## Capabilities

### New Capabilities
- `secure-secret-resolution`: Resolves sensitive string configuration values from protected files or environment-provided file paths, suitable for systemd credential directories and cloud secret-manager wrappers.
- `provider-credential-inheritance`: Allows model and embedding configurations to inherit provider-level credentials and defaults while still supporting explicit per-model overrides.

### Modified Capabilities

## Impact

- Affected code: `config_schema.py`, `llm_client.py`, configuration loading tests, config examples, and deployment documentation.
- Affected config surface: optional provider-level sections and file-based secret references; existing `env:VAR` references remain supported.
- Affected deployments: `systemd --user` services can use `LoadCredential=` plus `*_FILE` environment variables to avoid storing API key values in the process environment.
- Security impact: reduces secret exposure in environment variables, but shell/MCP/tool subprocess inheritance remains relevant for any secrets that are still injected as env values.
