## 1. Recovery decision and capability

- [x] 1.1 Test-first: add the default-false per-run interactive LLM recovery argument through controller invocation into `ReactContext`; prove callbacks/notifications cannot enable it and concurrent runs cannot share it.
- [x] 1.2 Test-first: explicitly enable the capability on Telegram root request and resume wiring; keep all `SubAgentRunner` profiles headless and sensitive-tool confirmation behavior unchanged.
- [x] 1.3 Test-first: gate `_handle_llm_error` before save/token/wait, with cancellation precedence and fixed category-selected `❌` guidance for headless/non-retryable failures; preserve automatic budgets, safe labels and truthful exhaustion wording.
- [x] 1.4 Add a real `SubAgentRunner → AgentController → react_loop` regression with scripted transport and exact send counts; prove no headless retry wait/token/checkpoint even with checkpoint writing enabled and positive timeout, without replacing `request_retry` with an instant-return mock, and verify owned-client/lease/registration cleanup.

## 2. Resume policy and durable safety

- [x] 2.1 Test-first: add one recovery-layer eligibility predicate/eligible-list view, preserving raw store enumeration and legacy default-true behavior; enforce controller rejection before authorization/send for explicit non-retryable checkpoints.
- [x] 2.2 Test-first: use that view in Telegram `/resume` and startup notices; cover mixed A/B/C numbering, newest eligible selection, only-ineligible/no-checkpoint cases, malformed-file handling without touching ineligible files, unchanged legacy-file bytes, and the specified resuming/startup/busy strings.
- [x] 2.3 Extend recovery regressions for eligible Retry/Cancel/timeout, checkpoint-disabled/save-failed in-memory retry and warning, deferred messages, full-context continuation, no budget refill/tool replay, pre-send failure/cancellation preservation, and ownership/retirement mismatch or permission failure.

## 3. Truthful caller outcomes

- [x] 3.1 Test-first: normalize supervisor completion with cancellation first, explicit structured outcomes second, reserved plain `❌` failure third; verify failed parent retrieval and retained successful text/JSON output.
- [x] 3.2 Test-first: normalize plan runner terminal failures to the existing `success=false, recoverable=false` outcome; prove no task retry or diagnostic replay, while preserving cancellation, explicit successful JSON with `❌` output, DAG dependency skipping and cleanup.
- [x] 3.3 Add end-to-end caller regressions for headless LLM failure through supervisor/plan/scheduler boundaries; verify scheduler classification, next-cron behavior, recording and notification policy remain unchanged with no whole-run replay.

## 4. Documentation and verification

- [x] 4.1 Update existing recovery documentation to describe immediate headless/non-retryable failure, retained interactive retry/resume, shared eligibility and untouched hidden legacy files; do not add config or cleanup interfaces.
- [x] 4.2 Run `openspec validate eliminate-unusable-llm-recovery --type change --strict`, `make check`, `vulture . vulture_whitelist.py --min-confidence 80` and `git diff --check`; record commands, exits, test totals and skip reasons, fixing regressions without weakening rules.

Execution discipline: each checkbox is a bounded implementation/validation slice (maximum two hours). Use red-green TDD for behavior changes, focused tests for each slice, and required `ruff check .` plus `vulture . vulture_whitelist.py --min-confidence 80` after code changes. Run the complete applicable suite once after reconciliation. Apply only after proposal artifacts are committed on `main`, using an approved non-main implementation lane. No implementation, commit, merge or archive is authorized by this checklist.

### Apply validation — 2026-11-20

Environment: `export PATH="$PWD/.venv/bin:$PATH"`; Python 3.14.6, project-venv pytest and Ollama 0.6.2. Earlier global-Python validation was invalid; the authoritative reconciled run is below.

| Command | Exit | Evidence |
| --- | --- | --- |
| `make check` | 0 | Ruff and configured vulture passed; **6322 passed, 2 skipped**, no failures. |
| `vulture . vulture_whitelist.py --min-confidence 80` | 0 | Unexcluded check passed. |
| `openspec validate eliminate-unusable-llm-recovery --type change --strict` | 0 | Change is valid. |
| `git diff --check` | 0 | No whitespace errors. |

The two unchanged skips are `test_path_policy.py::TestIsContained::test_normcase_case_insensitive` (macOS `normcase` is a no-op) and `test_consent_scope_wording.py::test_vision_query_scope_line` (intentional placeholder). No new skips. Focused review findings were corrected and re-review passed. Logs: session temporary directory `eliminate-unusable-llm-recovery-validation/final-*.log`.

### Verification-warning fixes — 2026-11-20

Retry cards now render current/max steps, with step-only fallback for older payloads. The user approved correcting the corrupted-checkpoint scenario to existing non-destructive skip/refusal behavior; no deletion logic was added. The delta uses an ADDED replacement `/resume` requirement and REMOVED predecessor so the corrected scenario is represented explicitly.

Artifact re-review passed for specs, ADR and tasks. Oracle re-verification confirmed 7/7 ADDED/MODIFIED requirements, 39/39 scenarios and both removals, with no unresolved findings.

Final project-venv checks after these fixes:
- `make check`: exit 0, Ruff/configured Vulture passed, **6331 passed, 2 unchanged baseline skips**.
- `vulture . vulture_whitelist.py --min-confidence 80`: exit 0, no findings.
- `openspec validate eliminate-unusable-llm-recovery --type change --strict`: exit 0, valid without warnings.
- `git diff --check`: exit 0.

Logs: session temporary directory `verify-warning-fixes/final-*.log`. The two skip reasons are unchanged from the earlier record above.
