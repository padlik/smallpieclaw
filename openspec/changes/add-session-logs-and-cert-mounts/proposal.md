## Why

Shell command output logs (`shell_logs/`) currently live inside the agent's own `data/` directory, accumulate forever with no cleanup, and are invisible to sandboxed shell commands. Meanwhile, when `allow_net=true` is enabled, TLS-dependent tools (`curl`, `git`, Python `ssl`) fail inside the jail because `/etc/ssl/certs` is not mounted — the CA bundle is unreachable. This change moves shell output logs to XDG state home under a per-conversation folder, makes them read-only accessible inside the nsjail jail at the same host path, persists the main agent's chat history across restarts so conversations survive reboots, and mounts the system CA certificate store read-only when networking is enabled.

## What Changes

- **Move `shell_logs/` to `~/.local/state/<agent>/session_logs/<conversation_id>/`** — relocated from `data/` to XDG state home, grouped by conversation. The directory is created owner-only (0700), files owner-only (0600), same as today.
- **Rename `shell_logs` → `session_logs`** — the directory name and all references change. Old `data/shell_logs/` is left in place (unattributable to any conversation); new logs start fresh in the new location.
- **Mount `session_logs/<conversation_id>/` read-only inside the nsjail jail at the same host path** — agent writes outside the jail; sandboxed shell commands read inside the jail via an identical absolute path. Reverses the archived "shell_logs not mounted" decision, but read-only (not read-write), so the attack surface is read-only.
- **Persist the main agent's `short_term` (chat history) across restarts** — saved to `~/.local/state/<agent>/conversations/<conversation_id>.json` on shutdown and `/reset` (save), loaded on startup. `/reset discard` rotates the id without saving. Reuses the proven `context_io._save_context` / `_load_context` atomic-write pattern already used by sub-agents. Working memory (task state) is NOT persisted — restart always starts a fresh task.
- **Introduce a `conversation_id`** — a 12-char hex label persisted to `~/.local/state/<agent>/conversation_id`. Generated on first startup, survives restarts, rotated on `/reset` (both save and discard variants). Ties together the conversation file and the session_logs folder. Sub-agents inherit the main conversation's `conversation_id` and share its `session_logs` folder — no separate sub-agent session_logs.
- **Mount `/etc/ssl/certs` (or distro equivalent) read-only and non-mandatory inside the jail when `allow_net=true`** — system mount (bypasses the trusted-dir blocklist by construction, like `/dev/null`). `mandatory: false` so the jail still starts if the path is absent. Distro-aware: detects Debian (`/etc/ssl/certs`), Alpine (`/etc/ssl/cert.pem`), Fedora/RHEL (`/etc/pki/tls/certs`).
- **Inject `SSL_CERT_FILE` and `SSL_CERT_DIR` env vars when `allow_net=true`** — added to the nsjail config `envar` lines (not per-call `-E` flags). Bypasses the `/usr/lib/ssl/cert.pem` broken-symlink problem. Honored by Python `ssl`, OpenSSL, `curl`, `git`, `httpx`. `certifi` ignores them (uses its own bundle under `/usr`, already mounted) — so `requests`/`httpx` already work.
- **Update the system prompt** — `03-capabilities.md` tells the LLM that large shell outputs are saved to a path readable by both `file_read` and `shell`. The tool-output notice (`[full output saved to: <path>]`) already gives the path; no jail-internal vs host-internal distinction is needed since the path is identical inside and outside the jail.
- **Add a startup cleanup pass for old session_logs folders** — configurable retention (default: delete folders older than 7 days). The current code never cleans up `shell_logs`; any policy is an improvement.

## Capabilities

### New Capabilities
- `conversation-persistence`: Main agent chat history (`short_term`) survives process restarts via a `conversation_id` persisted to XDG state home. Loaded on startup, saved on shutdown and `/reset`. Working memory is not persisted.
- `session-log-management`: Shell output artifact logs stored under XDG state home in per-conversation folders (`~/.local/state/<agent>/session_logs/<conversation_id>/`), replacing the old `data/shell_logs/` location. The `_open_shell_log` and `_finalize_shell_log` helpers compute the path from the `conversation_id` held by `BuiltinExecutor`. Mounted read-only inside the nsjail jail at the same host path, with age-based retention cleanup.

### Modified Capabilities
- `nsjail-shell-sandboxing`: Adds a read-only mount of the active conversation's `session_logs` folder at its host path inside the jail. Adds a read-only, non-mandatory mount of the system CA certificate store (distro-aware) and injection of `SSL_CERT_FILE` / `SSL_CERT_DIR` env vars when `allow_net=true`.

## Impact

- **`builtin_tools/shell.py`** — `_open_shell_log` log_dir computation changes from `data_dir/shell_logs` to XDG-state-based `session_logs/<conversation_id>/`. Needs access to `conversation_id` (via `self._owner.conversation_id`).
- **`nsjail_config.py`** — `NsjailConfigBuilder.build()` gains a `session_logs_dir` kwarg (per-call, stateless) and an `allow_net`-gated CA cert mount + env var injection. New `_detect_ca_certs()` helper for distro-aware path detection.
- **`builtin_executor.py`** — gains a `conversation_id` attribute (like `_shell_env`), updated on `/reset`.
- **`main.py`** — startup: read/generate `conversation_id`, load `conversations/<id>.json` into `ShortTermMemory`, pass to `AgentController`. Shutdown `finally:` block: save `short_term` to `conversations/<id>.json`. Startup: run session_logs retention cleanup.
- **`agent_controller.py`** — `reset_task()` rotates `conversation_id` (generate new, write file, update `builtin_executor.conversation_id`) and saves current `short_term` before clearing.
- **`prompts/system/03-capabilities.md`** — add a note that large shell outputs are saved to a path readable by both `file_read` and `shell`.
- **`config_schema.py`** — new optional config field `session_logs_retention_days` (default 7).
- **Tests** — `test_builtin_executor.py` (shell log path change), `tests/nsjail/test_nsjail_mounts.py` (session_logs RO mount, CA cert mount when allow_net), `test_nsjail_config.py` (CA cert env vars, distro detection), new tests for conversation persistence lifecycle.
- **`vulture_whitelist.py`** — update if new public symbols are flagged.
- **Archived decision reversal** — `openspec/changes/archive/2026-07-28-add-shell-isolation-improvements/design.md` Decision 3 ("shell_logs not mounted") is reversed for `session_logs`, but read-only instead of read-write.