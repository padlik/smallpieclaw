# Implementation ledger — simplify-model-runtime

This is the recovery ledger for the apply phase. It is not the frozen review log and it does not change any frozen planning decision. The checkboxes in `tasks.md` belong to the parent coordinator.

## Identity and authority

| Item | Value |
|---|---|
| Base commit | `2c00e608beecc315d60f18ae01eff8932f77854d` (Ollama>=0.6.2 fix; `main` is an ancestor) |
| Planning-publication proof (task 1.1) | `ecffe23` on `main` publishes blobs identical to the feature merge `1a6…`. There was no frozen-planning diff before apply. |
| Worktree | `/Users/paulpronko/.local/share/opencode/worktree/ff9414/apply-simplify-model-runtime` (detached, externally managed) |
| Python | `/Users/paulpronko/Documents/develop/smallpieclaw/.venv/bin` (Python 3.14, ollama 0.6.2). Prepend it and Homebrew to `PATH`. |
| Baseline evidence | `/private/var/folders/6s/p9xmtzd53wv7jx8zq89wqzmr0000gn/T/opencode/simplify-apply-baseline/` (`make-check.log`, `ruff.log`, `vulture.log`, `collect.txt`, `openspec-*.json`) |
| Baseline result | `make check`: 5397 passed, 2 skipped (`test_path_policy.py::TestIsContained::test_normcase_case_insensitive`, `test_consent_scope_wording.py::test_vision_query_scope_line`), 2 warnings. Ruff and Vulture exact pass. 5399 nodes collected. |
| Authorization | Apply was approved explicitly. **No** commit, branch, merge, push, archive, sync or install is authorized. No commit per task is planned. |

## Continuing obligations (tasks 1.2, still binding after setup)

- For each code behavior, use test-driven development:
  1. Write one focused failing test.
  2. Confirm the reason it fails.
  3. Make the smallest fix.
  4. Run that test and its owner suite.
- After every code change, run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80`. Never weaken the rules.
- Introduce no new skips or xfails. The goal is not a smaller test count.
- Before deleting or removing any test node, apply `test-preservation-ledger.md`. If a slice finds an assertion the ledger missed, add it to the ledger first.
- For the final gate (task 11.2), run all of these:
  - the full `make check`;
  - exact `ruff` and `vulture`;
  - real `httpx.MockTransport` and root-composition tests;
  - graph e2e;
  - the nsjail suite (it skips visibly if Lima is absent).
- `openspec validate simplify-model-runtime --type change --strict` reports 4 informational notices about predecessor dependencies. They are expected and do not grant permission to archive.
- Evidence path for per-slice test runs and the final full suite: store each log under the baseline evidence directory as `slice-<task>.log` and `final-make-check.log`.

## Progress

| Task | State | Evidence |
|---|---|---|
| 1.1 | checked by parent | publication proof above |
| 1.2 | checked by parent | isolated worktree, status inspected; obligations above continue |
| 2.1 | complete; scoped re-review PASS for spec compliance and artifact quality; B1–B5 resolved | `test-preservation-ledger.md` / `.json`; audit `live=5399 ledger=5399 problems=[]`, body-level scan 2584 hit nodes; reviewer `ses_e5c308060ffeRxVb49jzG2bHmH` verified mappings and totals |
| 2.2 | complete; scoped review PASS for spec compliance and task quality; final validation passed | 17 nodes relocated, 4 ordinary owner nodes added, 1 existing node cited (see `test-preservation-ledger.md` § "Task 2.2 ordinary owner proofs"). Evidence: `/private/var/folders/6s/p9xmtzd53wv7jx8zq89wqzmr0000gn/T/opencode/simplify-task-2.2/`, `correction/` and `final-validation/`. Reviewer `ses_e573a36ffffeK6U2zHzuD2sgci` approved the corrections. Audit `live=5403 ledger=5403 problems=[]`; exact ruff/vulture passed; full pytest suite: 5401 passed, 2 baseline skips, 2 warnings. Validation invoked the checks separately rather than the `make check` wrapper. Strict OpenSpec validation passed with four expected predecessor-ordering notices. |
| 2.3 | complete; review PASS for spec compliance and task quality; full validation passed | 175 nodes relocated (157 `test_p2_compaction.py` keep → new `tests/test_context_manager.py`; 1 summary-cancellation node from `test_request_preparation.py` → same file; 17 `test_p2_graph_memory_admission.py` → `tests/test_graph_memory.py`, emptied source deleted). The 6 rewrite and 24 retire nodes stay in `test_p2_compaction.py`, running. See `test-preservation-ledger.md` § "Task 2.3 relocation record". Evidence: `/private/var/folders/6s/p9xmtzd53wv7jx8zq89wqzmr0000gn/T/opencode/simplify-task-2.3/` (`REPORT.md`, `task-2.3.diff`, AST proof, copied-tree mutation logs and `final-validation/`). Reviewer `ses_e525cf067ffeyTSlTbAHxv7X9R` verified the mapping and assertions. Full `make check`: 5401 passed, 2 baseline skips, 2 warnings; exact ruff/vulture clean; ledger `live=5403 ledger=5403 problems=[]`; strict OpenSpec validation passed with four expected predecessor notices. No production change. |
| 2.4–11.3 | not started | — |

Paused after task 2.3 as explicitly requested. No commit was created.

### User-approved scope exception (task 2.2)

The user explicitly approved one bounded production change. It is an exception to the "no production changes" rule for this task only and does not alter any frozen plan.

- **Change.** The ordinary MCP schema-build fallback warning (`react_loop.py:1083–1086`) now logs a fixed message plus the exception type name. It no longer logs the exception text, which may carry an arbitrary non-vault credential and reached raw stdlib log records.
- **Unchanged.** The strict path still re-raises. The fallback rebuild, the redaction chain, error classification, checkpoints and retries are untouched.
- **Proof.** The regression node `tests/test_native_tool_contract.py::test_ordinary_mcp_schema_failure_falls_back_without_leaking_exception_text` failed before the change and passes after it. Reverting the change in a copied tree makes it fail again.

### Open for 6.7 (recorded, no change authorized now)

On the ordinary path, a malformed resumed native group already fails before provider I/O, with history unrepaired and ownership released. The classification maps the fixed internal `_MALFORMED_HISTORY` error to the generic "Provider rejected the request" text. A message-only mapping can be examined in 6.7. The earlier review demand for unchanged checkpoint bytes was withdrawn as guard-only.

## 2.1 author findings after B1–B5 correction

- `tests/test_p2_graph_memory_admission.py` is about graph *fact* admission (observed/confirmed), not model admission. All 17 nodes are kept and move to `test_graph_memory.py`.
- The body-level second pass found 2,584 hit nodes. Shared and module-local helpers (not only `minimal_config`) carry removed agent keys, combined catalog fixtures, old snapshot fields and preview-fit hooks. Ledger F1–F4 tags name the owning slices and affected keep-in-place consumers; rewrite those helpers and rerun consumers.
- The word "budget" in `test_call_attempt_state.py`, `test_react_loop_bounded_retry.py` and `test_text_utils.py` refers to the retry-attempt budget or the redaction decode budget. These tests are kept and are out of scope for budget removal.
- O1–O5 are implementation follow-ups within frozen scope, not new user decisions: keep legacy no-registry `/models` conservatively until 9.x; migration-only symlink refusal retires without inventing a new preference check; summary-input rendering remains separate from published-history truncation; the context owner-file name is structural; update ledger before any newly discovered assertion is removed.

## Preflight consistency (tasks 1.1–11.3)

This table is a compact self-consistency check. It is not a new plan.

| Tasks | Self-consistent with frozen artifacts | Shared files / interfaces (ordering pair) |
|---|---|---|
| 2.1–2.4 | yes; moves happen before deletion | ledger/doc files only here; later test moves must precede 10.1–10.3 |
| 3.1–3.7 | yes (ADR-0040) | `model_settings.py`/`model_registry.py`/`xdg.py`/`main.py`; 3.2 → 3.3/3.4 → 3.5/3.6 |
| 4.1–4.4 | yes (ADR-0041) | `model_registry.py` (shared with 3.x), `agent_runtime.py`/`main.py`/supervisor; 4.1 → 4.2/8.1 |
| 5.1–5.4 | yes (ADR-0042) | `config_schema.py` (shared with 3.1/6.1/10.2), `main.py`, `graph_memory.py`, `backfill_graph_memory.py` |
| 6.1–6.7 | yes (ADR-0043) | `config_schema.py`, `context_manager.py`/`react_loop.py`/`context_monitor.py`, `llm_client.py`/`providers/`; 6.1 rewrites fixture F1 |
| 7.1–7.3 | yes; manual is separate from automatic | `agent_controller.py`, `telegram_commands.py` |
| 8.1–8.5 | yes (ADR-0041/0044) | `scheduler.py`, `main.py`, `builtin_tools/schedule.py`; 8.1 depends on 4.1 parsing |
| 9.1–9.4 | yes | `telegram_commands.py`/`telegram_formatter.py`/`telegram_model_selection.py`, context tools; depends on 3.x and 6.2 |
| 10.1–10.3 | yes; only after 2.x relocations are green | `model_request_budget.py`, `model_selection_guard.py`, `migrate_models.py` |
| 11.1–11.3 | yes; 11.3 needs separate approval | README, `config.toml.example`, full gates |

No contradiction with frozen requirements was found. No decision needs unfreezing.
