## 1. Recovery decision and capability

- [ ] 1.1 Test-first: add the default-false per-run interactive LLM recovery argument through controller invocation into `ReactContext`; prove callbacks/notifications cannot enable it and concurrent runs cannot share it.
- [ ] 1.2 Test-first: explicitly enable the capability on Telegram root request and resume wiring; keep all `SubAgentRunner` profiles headless and sensitive-tool confirmation behavior unchanged.
- [ ] 1.3 Test-first: gate `_handle_llm_error` before save/token/wait, with cancellation precedence and fixed category-selected `❌` guidance for headless/non-retryable failures; preserve automatic budgets, safe labels and truthful exhaustion wording.
- [ ] 1.4 Add a real `SubAgentRunner → AgentController → react_loop` regression with scripted transport and exact send counts; prove no headless retry wait/token/checkpoint even with checkpoint writing enabled and positive timeout, without replacing `request_retry` with an instant-return mock, and verify owned-client/lease/registration cleanup.

## 2. Resume policy and durable safety

- [ ] 2.1 Test-first: add one recovery-layer eligibility predicate/eligible-list view, preserving raw store enumeration and legacy default-true behavior; enforce controller rejection before authorization/send for explicit non-retryable checkpoints.
- [ ] 2.2 Test-first: use that view in Telegram `/resume` and startup notices; cover mixed A/B/C numbering, newest eligible selection, only-ineligible/no-checkpoint cases, malformed-file handling without touching ineligible files, unchanged legacy-file bytes, and the specified resuming/startup/busy strings.
- [ ] 2.3 Extend recovery regressions for eligible Retry/Cancel/timeout, checkpoint-disabled/save-failed in-memory retry and warning, deferred messages, full-context continuation, no budget refill/tool replay, pre-send failure/cancellation preservation, and ownership/retirement mismatch or permission failure.

## 3. Truthful caller outcomes

- [ ] 3.1 Test-first: normalize supervisor completion with cancellation first, explicit structured outcomes second, reserved plain `❌` failure third; verify failed parent retrieval and retained successful text/JSON output.
- [ ] 3.2 Test-first: normalize plan runner terminal failures to the existing `success=false, recoverable=false` outcome; prove no task retry or diagnostic replay, while preserving cancellation, explicit successful JSON with `❌` output, DAG dependency skipping and cleanup.
- [ ] 3.3 Add end-to-end caller regressions for headless LLM failure through supervisor/plan/scheduler boundaries; verify scheduler classification, next-cron behavior, recording and notification policy remain unchanged with no whole-run replay.

## 4. Documentation and verification

- [ ] 4.1 Update existing recovery documentation to describe immediate headless/non-retryable failure, retained interactive retry/resume, shared eligibility and untouched hidden legacy files; do not add config or cleanup interfaces.
- [ ] 4.2 Run `openspec validate eliminate-unusable-llm-recovery --type change --strict`, `make check`, `vulture . vulture_whitelist.py --min-confidence 80` and `git diff --check`; record commands, exits, test totals and skip reasons, fixing regressions without weakening rules.

Execution discipline: each checkbox is a bounded implementation/validation slice (maximum two hours). Use red-green TDD for behavior changes, focused tests for each slice, and required `ruff check .` plus `vulture . vulture_whitelist.py --min-confidence 80` after code changes. Run the complete applicable suite once after reconciliation. Apply only after proposal artifacts are committed on `main`, using an approved non-main implementation lane. No implementation, commit, merge or archive is authorized by this checklist.
