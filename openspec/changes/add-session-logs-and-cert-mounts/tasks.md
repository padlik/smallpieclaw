## 1. conversation_id lifecycle and persistence

- [x] 1.1 Add `conversation_id` attribute to `BuiltinExecutor.__init__` (default `""`, like `_shell_env`). Add a property or simple attribute access for shell tools to read it via `self._owner.conversation_id`.
- [x] 1.2 Add a helper function in `main.py` to read/generate the `conversation_id` from `~/.local/state/<agent>/conversation_id`. Generate `uuid4().hex[:12]` if the file is missing or corrupted. Write the file atomically (temp file + `os.replace`).
- [x] 1.3 In `main.py` startup: call the helper to get `conversation_id`, create `~/.local/state/<agent>/conversations/` directory, load `conversations/<id>.json` into `ShortTermMemory` if it exists (reuse `context_io._load_context` pattern, which handles corrupted JSON gracefully — logs a warning and returns fresh `ShortTermMemory`). Pass `short_term=loaded` to `AgentController`, and set `builtin.conversation_id = conversation_id`.
- [x] 1.4 In `main.py` shutdown `finally:` block: save `agent.short_term` to `conversations/<conversation_id>.json` using the atomic-write pattern (temp file + `os.replace`). Guard with `if conversation_id:` and `try/except` so a save failure doesn't crash shutdown.
- [x] 1.5 In `agent_controller.reset_task()`: before clearing `short_term`, save it to `conversations/<old_id>.json` when `save=True`. Then generate a new `conversation_id`, write the `conversation_id` file atomically (temp file + `os.replace`), and update `builtin_executor.conversation_id`. When `save=False` (`/reset discard`), skip the save but still rotate the id. Clear `short_term` and `working` as today.

## 2. session_logs path change (shell_logs → session_logs)

- [x] 2.1 In `builtin_tools/shell.py` `_open_shell_log`: change `log_dir` from `os.path.join(self._owner._data_dir, "shell_logs")` to `os.path.join(xdg_state_home, agent_name, "session_logs", self._owner.conversation_id)`. Compute `xdg_state_home` and `agent_name` from existing config or pass them through `BuiltinExecutor`. Create the directory with mode 0700, files with 0600 (unchanged).
- [x] 2.2 Update the tool-output notice in `_run_shell_subprocess` and `_run_shell_pty` (lines ~431, ~618): the path in `[full output saved to: <path>]` is now the XDG-state-based path. No change to the notice format — just the path value changes because `log_dir` changed.
- [x] 2.3 Update `tests/test_builtin_executor.py`: change the `shell_logs` dir assertion (line ~802) from `tmp_path / "shell_logs"` to the new XDG-state-based path. Update the `_open_shell_log` monkeypatch test (line ~1144) to use the new path computation.
- [x] 2.4 Update `config.toml.example`: change the reference from `<data_dir>/shell_logs/` to `~/.local/state/<agent>/session_logs/<conversation_id>/`. Add a comment documenting the `session_logs_retention_days` option (default 7). Add a note under the `allow_net` option that when `allow_net = true`, the system CA certificate store is mounted read-only and `SSL_CERT_FILE` / `SSL_CERT_DIR` env vars are injected for TLS.

## 3. session_logs read-only mount inside nsjail

- [x] 3.1 In `nsjail_config.py` `NsjailConfigBuilder.build()`: add a `session_logs_dir: str = ""` kwarg. When non-empty and `os.path.isdir(session_logs_dir)`, add a mount entry: `mount: { src: <dir> dst: <dir> is_bind: true rw: false mandatory: false }`. Place it after the session mounts block.
- [x] 3.2 In `builtin_tools/shell.py` `_run_shell_nsjail`: compute `session_logs_dir` from `self._owner.conversation_id` and the XDG state path, pass it to `builder.build(command, timeout, shell_env=env_snapshot, session_logs_dir=session_logs_dir)`.
- [x] 3.3 In `tests/nsjail/test_nsjail_mounts.py`: update `test_shell_logs_not_mounted` (line ~218) — rename to `test_session_logs_mounted_readonly` and assert the session_logs folder IS mounted read-only at its host path when `session_logs_dir` is provided. Add a second test asserting the mount is skipped when `session_logs_dir` is empty.
- [x] 3.4 In `tests/test_nsjail_config.py`: add a test asserting `session_logs_dir` kwarg produces a read-only mount entry with `src == dst`, `rw: false`, `mandatory: false`.

## 4. CA certificate store mount when allow_net=true

- [x] 4.1 In `nsjail_config.py`: add a `_detect_ca_certs()` method to `NsjailConfigBuilder` that returns `(cafile, capath)` or `(None, None)`. Detection order: Debian (`/etc/ssl/certs` dir + `/etc/ssl/certs/ca-certificates.crt` file), Alpine (`/etc/ssl/cert.pem` file, no dir), Fedora (`/etc/pki/tls/certs` dir + `/etc/pki/tls/certs/ca-bundle.crt` file). Return `(None, None)` if none found.
- [x] 4.2 In `NsjailConfigBuilder.build()`: when `self.allow_net` is True, call `_detect_ca_certs()`. If a path is found, add a read-only mount (`mandatory: false`) of the directory or file. Add `SSL_CERT_FILE=<cafile>` and `SSL_CERT_DIR=<capath>` as `envar` lines in the config (only when the path exists). If no path found, skip both mount and env vars (graceful degradation).
- [x] 4.3 In `tests/test_nsjail_config.py`: add tests for CA cert detection and env var injection: (a) `allow_net=True` on Debian → mount + env vars present, (b) `allow_net=False` → no mount, no env vars, (c) `allow_net=True` but no CA path → no mount, no env vars, jail starts.
- [x] 4.4 In `tests/nsjail/test_nsjail_mounts.py`: add a test asserting `/etc/ssl/certs` is mounted read-only when `allow_net=True` (if the path exists on the test host). Add a negative test: no CA cert mount when `allow_net=False`.

## 5. Retention cleanup

- [x] 5.1 In `config_schema.py`: add `session_logs_retention_days: int = 7` to the agent config dataclass. Parse from `agent.session_logs_retention_days` in `config.toml` with `_parse_int` or equivalent.
- [x] 5.2 In `main.py` startup (after `conversation_id` is loaded, before agent construction): add a cleanup pass that scans `~/.local/state/<agent>/session_logs/` and deletes conversation folders whose newest file mtime is older than `session_logs_retention_days`. Also delete the corresponding `conversations/<old_id>.json`. Skip the active conversation's folder. If the `session_logs/` directory doesn't exist, no-op.
- [x] 5.3 Add a test for the retention cleanup: create old and new session_logs folders + conversation files, run the cleanup, assert old ones are deleted and new ones (including active) are preserved.

## 6. Prompt update

- [x] 6.1 In `prompts/system/03-capabilities.md`: add a note under the SHELL PERSISTENCE section that large shell outputs are saved to a path readable by both `file_read` and `shell` (the `[full output saved to: <path>]` notice gives the path). No jail-internal vs host-internal distinction needed.
- [x] 6.2 In `prompts/sub-agent/03-tools.md`: add the same note for sub-agents (they share the main conversation's session_logs folder).

## 7. vulture whitelist and lint

- [x] 7.1 Update `vulture_whitelist.py` if any new public symbols (e.g., `_detect_ca_certs`, `conversation_id` property) are flagged by vulture.
- [x] 7.2 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80` to verify no lint or dead-code issues.

## 8. Integration verification

- [x] 8.1 Run `make test` to verify all existing tests pass with the path changes.
- [x] 8.2 Run `openspec validate add-session-logs-and-cert-mounts --type change --strict` to verify spec format.
- [x] 8.3 Verify the full lifecycle manually (if possible on a Linux host with nsjail): start agent → run a large shell command → check session_logs folder is created under XDG state → restart agent → verify conversation history is loaded → run `/reset` → verify new conversation_id and old conversation file retained.