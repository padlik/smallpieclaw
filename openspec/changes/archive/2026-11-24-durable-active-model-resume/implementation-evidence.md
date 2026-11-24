# Implementation evidence — durable-active-model-resume

Environment: project `.venv` (`source .venv/bin/activate`), branch `feature/durable-active-model-resume`.
Static checks after each slice: `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80`.

## Group 1 (tasks 1.1–1.6)

- 1.1: `git branch --show-current` → `feature/durable-active-model-resume`; tree clean at start; proposal commit fe15782 on main.
- 1.2 red: after renaming the test-side eligibility name to `checkpoint_recovery_eligible` and adding durable-only tests,
  `pytest tests/test_llm_recovery_capability.py` → `26 failed` (`ReactContext` had no such field; old wait path blocked).
- 1.3/1.4 green: field renamed across `react_loop.py`, `agent_controller.py`, `main.py`; `_handle_llm_error` replaced with
  save-and-return (`_save_recovery_checkpoint`), retry timeout removed from `ReactContext`/`ControllerDeps`/controller/main wiring.
  `pytest tests/test_react_loop_error_recovery.py tests/test_llm_recovery_capability.py tests/test_interactive_llm_recovery_regression.py`
  → `60 passed`.
- 1.5: new cases in `tests/test_interactive_llm_recovery_regression.py` (verified save, save False, `OSError`, failed
  `recovery_available`, disabled writes, no memory-only continuation).
- 1.6: card-dependent expectations migrated in `tests/test_react_loop_error_recovery.py`,
  `tests/test_headless_llm_recovery_real_runner.py` (16 passed), plus fixtures in `tests/test_main.py`,
  `tests/test_react_loop_bounded_retry.py`, `tests/test_ordinary_run_owners_10_1.py`,
  `tests/test_scheduled_model_admission.py`, `tests/test_p3_execution_harness.py`, `tests/test_canonical_native_probe.py`.
- Design note: when a resumed owner still holds an unretired snapshot (no request sent), `_save_recovery_checkpoint`
  reports availability via `recovery_available` and does not rewrite the file (design decision 2, pre-send preservation).
- `ruff check .` → `All checks passed!`; vulture → exit 0.

## Group 2 (tasks 2.1–2.5)

- 2.1 red: `pytest tests/test_main_run_admission.py` → `3 failed, 32 passed` (changed-model, removed-historical-connection and
  history/counter restoration were refused with "Cannot resume checkpoint"); `tests/test_agent_controller_resume.py` → `4 failed, 34 passed`.
- 2.2 green: controller no longer compares `checkpoint["model_id"]` with the admitted model; it still requires a non-empty string
  historical model, an admitted `llm_cfg["id"]` string, and every other envelope/history/eligibility/attempt validation.
  `pytest tests/test_run_recovery_contract_4_4.py tests/test_main_run_admission.py tests/test_agent_controller_resume.py
  tests/test_active_model_resume_transport.py` → `99 passed`.
- 2.3: `_announce_model_handoff` (agent_controller.py) emits the design's notice only when canonical IDs differ, after validation +
  admission and immediately before `react_loop`; IDs outside `[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}` render as "an unrecognized model";
  delivery failure is logged and ignored. Telegram: `_ProgressPanel.classify` shows lines starting `Resuming with ` untruncated
  (otherwise the 80-char fallback would cut the provider-handoff sentence); red `1 failed` then `113 passed` in `tests/test_tool_brief_panel.py`.
  (Flag for parent: this is a one-line rendering rule, not new visual design.)
- 2.4/2.5: new `tests/test_active_model_resume_transport.py` (`8 passed`): openai, openrouter, google, ollama native resume,
  anthropic local native→JSON conversion (no extra outbound attempt, mode json), local preparation failure (exact original bytes, zero
  sends, lease released), post-send 409 (sanitized failure, new checkpoint records the active model and counters 3/1/1, one request,
  no fallback host), post-send context failure (no new checkpoint, retired snapshot not resurrected). Red proof: temporarily restoring
  the equality check made 7 of the 8 fail (`7 failed, 1 passed`); check removed again.
- `ruff check .` → `All checks passed!`; vulture → exit 0.

## Group 3 (tasks 3.1–3.3)

These are regression guards over behavior the active-model change must not weaken; the existing implementation already satisfied
them, so they passed on first run (no production change in this group).
- 3.1: `tests/test_react_loop_bounded_retry.py` (+2: snapshot retired at the transport send, not at serialization;
  repeated explicit resumes never refill the automatic budget) and `tests/test_run_recovery_contract_4_4.py` (cross-model resume:
  snapshot on disk at admission, gone at the send, counters 3/1/1 continued). `32 passed` / `18 passed`.
- 3.2: `tests/test_checkpoint_store.py` (+3: unlink permission failure blocks and stays recoverable; save never overwrites a
  changed pending snapshot; second owner refused) and `tests/test_agent_controller_resume.py` (+6 cross-model: changed snapshot,
  missing snapshot, permission-denied retirement — zero sends and bytes preserved; concurrent resume refused before a second
  admission; cancel during admission and setup failure preserve bytes, close the owned client and release the lease). `74 passed`.
- 3.3: `tests/test_resume_eligibility.py` (+3: disabled new writes still resume and retire before send; abandoned checkpoint stays
  listed/notified after the failed run without expiry or new controls; `/resume` offers it with no reply markup). `21 passed`.
  Filtered newest-first `/resume N`, busy, corrupted/non-retryable preservation, startup notices stay covered by the retained tests.
- `ruff check .` → `All checks passed!`; vulture → exit 0.

## Group 4.1–4.3 (retry surface removal)

- 4.1 red: new `tests/test_telegram_error_card.py` → `3 failed, 6 passed` (card sender still existed; stale Retry/Cancel callback still
  signalled the agent and answered "Retrying…"/"Cancelled").
- 4.2: removed `ConfirmationManager.request_retry/signal_retry`, the retry event/result maps, `RETRY_PREFIX`,
  `AgentController.resume_llm_error`, and the corresponding `vulture_whitelist.py` entries. `tests/test_confirmation_retry.py` kept as a
  retirement guard plus a surviving tool-confirmation/extension check (every old case was retry-only). Migrated fixtures in
  `test_compaction_failure_6_6.py`, `test_react_loop.py`, `test_p3_trace_ids.py`, `test_react_loop_bounded_retry.py`,
  `test_llm_recovery_capability.py`, `test_agent_controller_resume.py`, `test_doom_loop.py`, `test_p3_execution_harness.py`;
  the retry-card deferred-message test became a general running-task deferred-message test (same assertions).
- 4.3: removed `_send_llm_error_prompt`, `_handle_llm_error_progress` and the `__LLM_ERROR__:` panel dispatch; `cb_llm_retry` is now an
  authenticated inert handler (`_require_cb_auth`) that acknowledges and edits to "⌛ These LLM error buttons have expired. Use /resume
  to continue any saved progress." with no agent/checkpoint access. `tests/test_telegram_error_card.py` → `9 passed`.
- Broad guard: `pytest tests -k "telegram or confirm or callback or grant or headless or doom or checkpoint or resume"` →
  `912 passed`. `ruff check .` → `All checks passed!`; vulture → exit 0.

## Group 4.4–4.7 and 5.1

- 4.4 red: `pytest tests/test_config_llm_error.py` → `11 failed, 3 passed`; green after removing `retry_timeout_seconds` from
  `LLMErrorHandlingConfig` and rejecting the key (any value) in `_reject_removed_fields`; `checkpoint_enabled` default true kept
  (existing `bool()` coercion unchanged). `config.toml.example` updated; `tests/test_config_example.py` +2. `23 passed`.
- 4.5/4.6: fixtures migrated while removing the retry API (`test_react_loop`, `test_doom_loop`, `test_p3_execution_harness`,
  `test_main_run_admission`, `test_scheduled_model_admission` → `234 passed`; `test_ordinary_run_owners_10_1`,
  `test_compaction_failure_6_6`, `test_canonical_native_probe` → `60 passed`). Tracked-file search for retry-only symbols leaves only
  negative guards, the removal-note in config/README, and the historical `docs/model-runtime-council-findings.md` (left unchanged).
  `.slim/recovery` scratch copies and virtualenvs untouched.
- 4.7: README (recovery, `/resume`, active-model handoff, failed-save loss, conversion limits, config, operator-managed abandoned
  checkpoints), `config.toml.example`, `ARCHITECTURE.md` (callback list), `AGENTS.md` (developer note).
- 5.1 final cumulative run (project .venv): `ruff check .` → exit 0 `All checks passed!`;
  `vulture . vulture_whitelist.py --min-confidence 80` → exit 0 (no `--exclude`);
  `pytest tests -k "telegram or confirm or callback or grant or headless or doom or checkpoint or resume or config or recovery or
  admission or react_loop or retry or p3 or compaction or native or owners or main" --ignore=tests/nsjail` → `2742 passed`.
  Full `make check` (5.2) and `openspec validate` (5.3) deliberately NOT run; left for parent.

## Review fixes (handoff identity rendering, retention docs)

- Red (`pytest tests/test_agent_controller_resume.py`): `12 failed, 44 passed`. Direct repro on the old code:
  `_announce_model_handoff(out.append, 'https://user:pass@host', 'b/model')` printed the URL verbatim (P1), while
  `a/model(2026)` and a 205-char ID rendered as "an unrecognized model" (P2).
- Fix (`agent_controller.py` `_safe_model_label` / `_announce_model_handoff`, `AgentController._selector_redactor`, `main.py`
  wiring): reuse the established canonical-selector validation `model_settings._preference_selector` (rejects `://`, endpoints,
  credential-shaped text, `env:`/`sec:`, control characters; requires a valid provider part; keeps punctuation) and the established
  `model_registry.bounded_selector_evidence` (256-char bound with `...`, vault redaction via the root's `selector_redactor`, the same
  >=6-char needle policy as scheduled selectors). The custom regex was removed. Admission rules are untouched.
- New cases: plain URL, credential URL, URL nested after a provider part, `www.`, `env:`/`sec:`, `api_key=` text and newline
  (each separately) → "an unrecognized model"; vault-secret inside an allowed-charset ID → `***REDACTED***`; punctuated/long IDs kept;
  oversized ID bounded; controller wires its redactor.
- Docs: README (/resume section) and `config.toml.example` now state that no NEW expiry/discard control exists and that the existing
  cap (20 supported checkpoints, pruned on save; other-format, unreadable and active-owner files never pruned) is unchanged.
  Mechanical re-scan of every doc I wrote found no other "nothing is deleted" promise.
- Green: `pytest` on 11 files (resume, transport, admission, main, config, model_settings/registry, panel, eligibility, store) —
   `599 passed`; `ruff check .` exit 0; `vulture . vulture_whitelist.py --min-confidence 80` exit 0.

## Final command validation — 2026-11-24

After all runtime/test edits and review fixes were reconciled, the independent
test-execution specialist ran these commands in the project `.venv`:

| Command | Exit | Evidence |
| --- | --- | --- |
| `make check` (once, including nsjail) | 0 | 6517 passed, 2 skipped in 112.38s; lint passed |
| `ruff check .` | 0 | All checks passed! |
| `vulture . vulture_whitelist.py --min-confidence 80` | 0 | No findings, no exclusions |
| `openspec validate durable-active-model-resume --type change --strict` | 0 | Change 'durable-active-model-resume' is valid |
| `git diff --check` | 0 | No findings |

Both skips are existing: `test_consent_scope_wording.py::test_vision_query_scope_line`
is the intentional heavy-mock placeholder, and
`test_path_policy.py::TestIsContained::test_normcase_case_insensitive` is the
macOS `normcase` platform skip. Zero new skips; the nsjail/Lima tests ran.
Full logs are in
`/private/var/folders/6s/p9xmtzd53wv7jx8zq89wqzmr0000gn/T/opencode/final-validation-logs/`
(`make_check.log`, `ruff.log`, `vulture.log`, `openspec.log`, `git_diff_check.log`).
The orchestrator inspected the pytest summary and static/OpenSpec logs.

## Apply alignment and review closure

Oracle code/safety review and designer Telegram rendering review are complete.
All three code-review findings were fixed and independently closed; the follow-up
ran 17 handoff tests successfully. Existing selector validation, bounded evidence
and vault redaction replace the custom display regex. Admission is unchanged.
Retention documentation describes the existing cap/pruning rather than promising
that no automatic deletion occurs.

| Exploration decision | Implementation / test evidence |
| --- | --- |
| Cancellation wins | `_handle_llm_error`; capability and controller cancellation tests |
| Context/permanent failure | Fixed terminal guidance; error-recovery and transport context-rejection tests |
| Eligible retryable failure with verified save | `_save_recovery_checkpoint`; owner-backed save/availability tests in `test_interactive_llm_recovery_regression.py` |
| Disabled/unsuccessful save | Disabled-write, false-return and `OSError` tests in that file; no memory-only continuation |
| Retryable headless failure | Default-false eligibility; `test_headless_llm_recovery_real_runner.py` |
| Active-model resume | Single immutable admission in `test_main_run_admission.py`; transport history/counter/no-replay tests |
| No admissible active model | Admission-failure tests: no send, unchanged bytes and cleanup |
| Preparation failure/cancellation | Controller/transport exact-byte preservation; bounded-retry send-gate tests |
| Target rejects saved input | Transport sanitized rejection, no fallback/replay, new-model retryable checkpoint and no context-failure resurrection |

Retired/replacement requirements are accounted for: inline retry APIs/maps/card
publication were removed in favor of save-and-return; durable storage retains
atomic writes, verification and exact-snapshot retirement; obsolete timeout
configuration is rejected while existing stored recovery works with disabled
writes; explicit checkpoint eligibility replaces interactive eligibility without
extending headless recovery; active-model admission replaces historical-model
equality without changing ownership or budgets. Safe handoff and authenticated
inert stale callbacks have dedicated regression coverage.

This completes apply alignment only. Formal OpenSpec verify remains a separately
requested stage; no archive-readiness claim, merge, archive or commit was made.
