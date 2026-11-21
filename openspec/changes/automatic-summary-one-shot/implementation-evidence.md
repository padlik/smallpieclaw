# Implementation Evidence — automatic-summary-one-shot

Branch `fix/automatic-summary-one-shot` (from `main` at a319c66, clean baseline). Tasks 1.1–3.1 done by the implementation lane; 3.2/3.3 left for the parent's independent validation. No commits made.

## Red (before production edit)

Command: `source .venv/bin/activate; python -m pytest tests/test_compaction_partition_6_4.py tests/test_compaction_failure_6_6.py -q --tb=line` → `3 failed, 58 passed`.

| Test | Failure | Cause |
|---|---|---|
| `test_compaction_partition_6_4.py::...::test_one_shot_success_is_isolated_from_main_request_state` | `assert (1 == 1 and 2 == 0)` | summary attempt state had `remaining_retries == 2` (inherited main budget) |
| `test_compaction_partition_6_4.py::...::test_transient_summary_error_is_one_send_and_leaves_main_state_alone` | `assert 3 == 1` | three summary sends with `max_retries=2` |
| `test_compaction_failure_6_6.py::TestSummaryIsOneShotWhileMainKeepsItsRetries::test_transient_summary_failure_makes_one_send_no_wait_main_retries_twice` | `assert 3 == 1` (summary bodies) | old retry loop; log showed 3 summary 503s, then 503, 503, 200 for the main request |

The pre-send cancellation test (`test_cancel_set_before_the_summary_send_makes_no_provider_call`) passed pre-change (characterizes retained behavior).

## Coverage (task 1.2–1.4)

- 6.4: success test now one send / one binding / zero retries / non-manual / main state and checkpoint unchanged; transient-failure test one send under `max_retries=2`, main attempt untouched, run disabled.
- 6.6 new regression (real `LLMClient` over `httpx.MockTransport`, recording `retry_wait`, `max_retries=2`, `retry_delay=0.5`): one summary send; main sends 503, 503, success, finish all with original history/goal (4 sends); exactly 2 waits, each after a main send; one `summary_failed` warning; later steps skip compaction.
- 6.6 reused as sufficient: provider error, empty provider reply, whitespace, blank-string callback (non-string `None`/`42` added in the addendum below), invalid candidate, sanitized warnings, run disablement/new-run reset, programming-error propagation, cancellation during send (incl. provider-error race, both client-sees/doesn't-see cancel), after reply, during validation, cooperative blocking cancel, no main send after cancel. Added: cancellation before send (no provider I/O).
- `tests/test_request_preparation.py::TestSummaryCall` (required): renamed `test_summary_is_one_shot_and_main_state_untouched`, now expects one call on 200 (old 503-then-retry expectation removed).

## Production (task 2.1)

`react_loop.py`: `_summarize_on_pin` now uses `CallAttemptState(max_retries=0, retry_delay=0)`, `bind_attempt(attempt, manual=False)`, one `chat`, pre-call / provider-error-race / post-reply cancellation checks, narrow supported-error family; loop, wait/backoff and `_is_transient_llm_error` removed (no remaining callers outside `.slim/` recovery snapshots). `/simplify` command is not available to this subagent; the narrow cleanup was done directly. `_prepare_request`, `context_manager.py`, config, ADRs, main retries untouched.

## Green / static (tasks 2.2, 3.1)

- `python -m pytest tests/test_compaction_partition_6_4.py tests/test_compaction_publish_6_5.py tests/test_compaction_failure_6_6.py tests/test_request_preparation.py -q --tb=short` → exit 0, `78 passed`, 0 skipped.
- `ruff check .` → exit 0, `All checks passed!`
- `vulture . vulture_whitelist.py --min-confidence 80` → exit 0, no output.
- README.md compaction paragraph (line ~631) updated: one-shot independent accounting, no summary retries/waits, safe failure with unchanged history, run-local disablement.

## Left for parent

3.2 (`make check`, focused run recorded by parent, unexcluded vulture) and 3.3 (`openspec validate ... --strict`, `git diff --check`, final review). No skips introduced by this lane.

## Coverage addendum (oracle-confirmed gaps; coverage-only characterizations, no red phase)

The earlier reuse claim that non-string results were already covered was inaccurate: only blank *strings* were parametrized (via `maybe_compact`). Added, test-only:

- `test_compaction_partition_6_4.py::...::test_one_shot_success_is_isolated_from_main_request_state` now returns a `usage` payload (7 prompt / 3 completion / 10 total) and asserts `client.get_today_usage()` equals exactly 7/3/10 after the single summary send.
- `test_compaction_failure_6_6.py::TestFailurePreservesMainState::test_non_string_automatic_callback_result_is_an_empty_summary_failure[none|int]`: real `_prepare_request` + `maybe_compact` failure handling with only the summary callback returning `None`/`42`; asserts original messages object/content and goal index unchanged (what the main request sends), run disabled, second call makes no attempt (no provider I/O), one sanitized `empty_summary` warning, main attempt state and checkpoint untouched.

Focused command: see "Addendum run" below.

### Addendum run

- `python -m pytest "tests/test_compaction_partition_6_4.py::TestSummaryUsesPinnedClientWithOwnAttemptState" "tests/test_compaction_failure_6_6.py::TestFailurePreservesMainState" -q --tb=short` → exit 0, `6 passed`, 0 skipped.
- `ruff check .` → exit 0. `vulture . vulture_whitelist.py --min-confidence 80` → exit 0.
- Tasks 3.2/3.3 remain open for the parent.

## Final validation and review — 2026-11-21

All final commands used the project `.venv/bin` on `PATH`, after the coverage additions:

| Command | Exit | Result |
| --- | --- | --- |
| `pytest tests/test_compaction_partition_6_4.py tests/test_compaction_publish_6_5.py tests/test_compaction_failure_6_6.py tests/test_request_preparation.py -v --tb=short` | 0 | 80 passed, no skips |
| `make check` | 0 | Ruff/vulture clean; 6335 passed, 2 skipped |
| `vulture . vulture_whitelist.py --min-confidence 80` | 0 | Clean, no output |
| `openspec validate automatic-summary-one-shot --type change --strict` | 0 | Change valid |
| `git diff --check` | 0 | No whitespace errors |

The two skips are the existing intentional `test_vision_query_scope_line` placeholder and macOS `test_normcase_case_insensitive`; no new skips.

Full logs were written under the machine-local temporary diagnostic location `/private/var/folders/6s/p9xmtzd53wv7jx8zq89wqzmr0000gn/T/opencode/` (nonportable, not durable evidence attachments; the summaries recorded in this file are the durable evidence):
- `final-3.2-3.3-pytest-143814.log`
- `final-3.2-3.3-makecheck-143816.log`
- `final-3.2-3.3-vulture-144016.log`
- `final-3.2-3.3-openspec-gitdiff-144016.log`

Independent oracle review found no production defects. Its two coverage findings (automatic-summary usage totals and non-string callback results) were addressed and re-reviewed with no residual findings. Existing real-loop regressions cover the shared original-request continuation path.

This completes the apply-stage evidence only. No commit, merge, formal verify stage, or archive was performed.
