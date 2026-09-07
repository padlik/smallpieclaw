# Task 9.3 Smoke Evidence — Linux + nsjail (automated)

- Date: 2026-09-07
- Vehicle: Lima VM `nsjail-test` (Ubuntu aarch64, kernel 7.0.0-30-generic, nsjail 3.6 at `/usr/local/bin/nsjail`)
- Method: automated e2e tests in `tests/nsjail/test_nsjail_config_e2e.py` — builder-generated configs (`NsjailConfigBuilder.build()`, the agent's real runtime code path) executed against the real nsjail binary inside the VM. No manual steps remain.

## Item-by-item mapping

| 9.3 item | Evidence |
|---|---|
| Prohibited path hard error | Unit: `tests/test_file_tools_zone.py` — `test_file_write_prohibited_hard_error`, `test_file_read_prohibited_hard_error`, `test_file_patch_prohibited_hard_error`, `test_file_diff_prohibited_on_either_hard_error`, `test_file_send_prohibited_hard_error`; `tests/test_path_policy.py` (tier matrix, credential homes, inode alias) |
| 3-button grant flow incl. `/reset` clearing | Unit (interactive Telegram UX cannot be driven headless in a VM): `tests/test_subagent_confirm_grants.py` — `test_file_tools_render_till_reset_button`, `test_subconfirm_yes_creates_prompt_grant_with_scope`, `test_subconfirm_till_reset_creates_session_grant`; `tests/test_grant_ledger.py` — `test_clear_all_clears_both_lifetimes`; `tests/test_pending_confirmations.py` — `test_reset_task_calls_clear_all` |
| Shell 2-button prompt | Unit: `tests/test_subagent_confirm_grants.py::test_shell_does_not_render_till_reset_button` + sink-veto defense `test_crafted_callback_for_shell_denied_at_sink` / `test_direct_ledger_add_for_shell_refused`; `tests/test_grant_ledger_veto.py` (injected-grant rejection at `check`, manager-delegate refusal) |
| Results artifact round-trip (shell write → file_read) | VM e2e: `tests/nsjail/test_nsjail_config_e2e.py::test_builder_results_artifact_round_trip` (write inside jail → read at host path) and `test_builder_mounts_skills_readonly_and_results_rw` (results `rw: true` mount asserted in config + functional write/cat round-trip) |
| Jail mount table matches config | VM e2e: `test_builder_mounts_skills_readonly_and_results_rw` (skills `rw: false`, results `rw: true`), `test_builder_prohibited_overlap_dir_not_mounted` (conflicting allowed dir not mounted; other tier-1 entries mounted; file-plane classification ALLOWED/PROHIBITED split verified), `test_builder_session_logs_absent_from_config` (no session-logs/state-home mount), plus the nine pre-existing builder e2e tests |

## Results

- `pytest tests/nsjail -q` → **56 passed in 14.12s** (includes the 4 new smoke e2e tests above, `test_shell_backend_parity.py`, and the 9 original builder tests)
- `make check` (full lint + suite) → **2097 passed, 1 skipped** (the documented macOS `normcase` platform skip)

## Finding surfaced by the automation (fixed)

The new skills/results mounts exposed a real ordering bug in `nsjail_config.py`: tier-derived bind mounts were emitted **before** the `session_tmpdir → /tmp` scratch remount, so any tier entry whose destination lives under `/tmp` was shadowed and nsjail died with `remountPt(): statvfs ENOENT`. Fix: session mounts (scratch `/tmp` + tmp_dir) are now emitted first, tier mounts after — verified in the VM (failing config reordered → rc=0), and the full VM suite passes against the fixed builder. This is exactly the class of config-generation bug the e2e suite exists to catch.

## Boundary statement

Interactive Telegram flows (3-button/2-button rendering, button-press grant creation, `/reset` clearing) require a live bot and cannot run headless in a VM; they are covered by the unit tests cited above. Every jail-side 9.3 item is automated. Task 9.3 is satisfied by automated VM tests + existing unit coverage; no manual steps remain.