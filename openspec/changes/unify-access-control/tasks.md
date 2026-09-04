# Tasks — unify-access-control

## 1. PathPolicy core (additive — old paths stay live)

- [x] 1.1 Create `path_policy.py`: `PathPolicy` frozen dataclass with Tier 0 hardcoded dict (superset of `_BLOCKED_SYSTEM_PREFIXES` incl. `/root`, `/run`, `/lib64`, plus credential homes with reason strings), Tier 1 entries (workspace, downloads, `/tmp/<agent>` rw; skills r; results rw), and `classify(realpath, operation) -> PROHIBITED|ALLOWED(r|rw)|UNRECOGNISED` using separator-boundary normcase-aware containment (reuse `_is_contained` logic)
- [x] 1.2 Add startup derivation: XDG data/state/config homes, vault file, agent dir as Tier 0; inode-alias set (`(st_dev, st_ino)`) for prohibited files; config `[security] prohibited_dirs` (append-only) and `allowed_dirs` (rw-only) ingestion with `~`-expansion and realpath
- [x] 1.3 Add one-shot conflict validation at construction: allowed dir containing/underlying a prohibited path → single warning; nonexistent config entries → warning, not failure; invalid config structure → startup error (fail closed)
- [x] 1.4 Write `tests/test_path_policy.py`: tier classification matrix, sibling-prefix bypass, symlink realpath resolution, inode-alias denial, `allowed_dirs=["/"]` recklessness immunity, frozen-immutability, conflict warnings, and the containment test asserting Tier 0 ⊇ legacy `_BLOCKED_SYSTEM_PREFIXES` ∪ `_SENSITIVE_USER_PREFIXES` ∪ `_RW_BLOCKED_USER_PREFIXES`

## 2. GrantLedger + PendingConfirmations (additive)

- [x] 2.1 Add `GrantLedger` to `confirmation.py` (owned by depth-0 `ConfirmationManager`): grant type `(tool, dir, lifetime, scope_owner)`, recursive dir matching, `check(tool, dir)`, `add(...)`, `clear_prompt_scope(sub_agent_id=None)`, `clear_all()`; `may_hold_grant(tool) -> False` for `shell` and `secret_get` enforced inside `check`/`add` (sink-side veto)
- [x] 2.2 Wire lifetimes: prompt-lifetime clearing at `react_loop()` entry (replacing `GrantTracker.reset()` boundary); session-lifetime clearing via `/reset` (extend `cmd_reset` → `agent_reset_fn` chain to clear both lifetimes)
- [x] 2.3 Add sub-agent run scoping: prompt grants from sub-agent confirmations carry the run identity; expire on run completion (hook `SubAgentSupervisor`/registry completion callbacks); single depth-0-owned ledger, `_coordinator` delegates
- [x] 2.4 Extract `PendingConfirmations` in `builtin_executor.py`: `_pending`/`_zone_paths`/`_zone_trackers` behind `stage()/take()/discard()/reset()` with one `threading.Lock`; run-scoped `reset()` in depth-0 `_run` finally
- [x] 2.5 Write tests: grant hit/miss per tool and dir (recursive, sibling exclusion), lifetime expiry boundaries (prompt cycle, /reset), shell/secret_get veto incl. crafted-callback-style direct ledger calls, prohibited immunity via ordering (classification before ledger), sub-agent scope expiry, staged-token lock atomicity, orphan cleanup on abnormal run end

## 3. Config schema + startup wiring

- [x] 3.1 Add `[security]` section to `config_schema.py` (`prohibited_dirs: list[str]`, `allowed_dirs: list[str]`, typed dataclass fields) with validation; update `config.toml.example`
- [x] 3.2 Construct PathPolicy in `main.py` after XDG/vault resolution; pass to `BuiltinExecutor`/`AgentController`/nsjail builder; startup fails on construction error (delete "checker unwired" fallbacks in `files.py`)
- [ ] 3.3 Update README security section (new config keys, removal of trusted dirs, new confirmation semantics)

## 4. File tools rewire

- [ ] 4.1 Rewire `_exec_file_read`/`_exec_file_diff`/`_exec_file_write`/`_exec_file_patch`/`_exec_file_send` in `builtin_tools/files.py`: classify via PathPolicy; PROHIBITED → hard error (`error_type: "prohibited_path"`, reason); ALLOWED → execute; UNRECOGNISED → ledger check → miss stages 3-button prompt; preserve `file_patch` confirm-before-read (description from args only)
- [ ] 4.2 Remove the `_is_sensitive_path` overlay from all `files.py` gates and delete `_SENSITIVE_PATH_PATTERNS` from `builtin_tools/patterns.py` (keep `_DANGEROUS_SHELL_PATTERNS` untouched — execution plane)
- [ ] 4.3 Update file-tool tests: replace zone/sensitive suites with tier-matrix + ledger-driven cases; prohibited hard-error cases; `file_diff` dual-path (prohibited either side fails, unrecognised either side prompts)

## 5. nsjail builder rewire

- [x] 5.1 Change `NsjailConfigBuilder` to consume the frozen PathPolicy: mount table = Tier 1 + Tier 2 minus Tier 0 overlap; delete the three blocklist tuples and `_load_trusted_mounts`; keep code-owned structural mounts (`/usr`, `/dev/null`, resolv.conf, CA store, session tmpdir) and env/DNS/limits logic unchanged
- [x] 5.2 Build the nsjail config once per session (cache at first shell call; per-call command/`-E`/`time_limit` only); remove session-logs mount; add `~/.<agent>/results` rw mount and repoint skills mount to `~/.<agent>/skills` (r)
- [x] 5.3 Update `tests/test_nsjail_config.py`: mount table from tiers, once-per-session caching, no-partial-mount rule (allowed dir containing prohibited path skipped with startup warning), results/skills mounts, session-logs absence

## 6. Agent dot-home + results dir

- [x] 6.1 Add dot-home bootstrap in `main.py`: create `~/.<agent>/skills/` and `~/.<agent>/results/`; one-time idempotent copy from legacy `$XDG_STATE_HOME/<agent>/skills/` (copy, legacy untouched)
- [x] 6.2 Relocate oversized shell artifacts: `_finalize_shell_log` writes to `~/.<agent>/results/<trace-id>/` (trace ID from `trace_context`), keep `_vault_secrets` redaction, report artifact path in tool result
- [x] 6.3 Write tests: dot-home creation, skills one-time copy idempotency, artifact relocation + redaction + path reporting, manual-clean retention (no automatic deletion anywhere)

## 7. Telegram UX

- [ ] 7.1 Replace confirmation prompt rows in `telegram_interface.py`: file ops → `[✅ Confirm]` `[✅✅ Till /reset]` `[❌ Deny]` (callback data carries grant semantics); `shell`/`secret_get` prompts render Confirm/Deny only; delete zone buttons (zone_allow/zone_trusted rows) and the main-agent "Approve all" button
- [ ] 7.2 Rewire `telegram_callbacks.py`: Confirm → resume + `ledger.add((tool, dir), prompt)`; Till /reset → resume + session grant; Deny → refusal; delete `cb_zone_*`, `confirm_all:`/`subconfirm_all:` approve-all handlers and `_ALLOWED_APPROVE_ALL_TOOLS`; headless sub-agent bridge passes scope annotation
- [ ] 7.3 Remove `/dir` command handlers and registration (`telegram_commands.py`); update `/help` text
- [ ] 7.4 Update Telegram tests: 3-button rendering, shell 2-button rendering, grant creation on button press, crafted-callback rejection at the ledger sink (port `test_subagent_approve_all.py` defense), `/reset` clears ledger

## 8. Deletions + cleanup

- [ ] 8.1 Delete `builtin_tools/access_control.py` (`TrustedZoneChecker`, `GrantTracker`, trusted_dirs persistence) and all references; delete `auto_approve_tools` from `confirmation.py`; remove `/dir` from `AgentRuntime`/executor wiring
- [ ] 8.2 Update `vulture_whitelist.py` for removed/new public symbols; run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80` clean
- [ ] 8.3 Update AGENTS.md module table (`access_control.py` → `path_policy.py`, confirmation/grant ledger roles) and Conventions gotchas

## 9. Final verification

- [ ] 9.1 Run full `make check` (lint + complete test suite) — fix all failures
- [ ] 9.2 Run `openspec validate unify-access-control --type change --strict` — fix all validation errors
- [ ] 9.3 Manual smoke (Linux + nsjail): prohibited path hard error, 3-button grant flow incl. `/reset` clearing, shell 2-button prompt, results artifact round-trip (shell write → file_read), jail mount table matches config