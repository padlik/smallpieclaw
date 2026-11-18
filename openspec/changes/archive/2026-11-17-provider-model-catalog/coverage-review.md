# Task 11.1 coverage review: provider-model-catalog

Status: a coverage-review deliverable written for task 11.1 on 2026-10-31. It records how the frozen requirements and scenarios map to tests. It does not mark 11.1 complete: the parent owns the 11.1 checkbox and the 11.2–11.4 gates.

## Scope and limitations

**Sources reviewed**
- This change's frozen proposal, design, adr.md (including the 2026-10-25 graph lifecycle clarification), and tasks.md.
- All nine delta specs. Scenario counts: provider-model-catalog 59, scheduled-job-execution 77, per-model-context-window 44, native-tool-calling 21, llm-error-recovery 21, telegram-command-surface 10, provider-credential-inheritance 7, xdg-path-resolution 6, agent-runtime-construction 3.
- Accepted ADR-0037, ADR-0038 and ADR-0039.

**Existing coverage maps reused**
- `.slim/deepwork/resume-20261021/G-tests-report-20261026.md` (12.25 legacy helper-test migration).
- The 12.19 notes in `tests/test_model_switch_budget_guard.py:4-43`.
- The boundary docstring in `tests/test_root_composition_amended.py:1-51`.

**Method**
- Test inventory came from AST walks plus targeted reads of test bodies.
- Not every cited test body was read end to end.
- No CodeGraph index exists for this worktree.

**Evidence status: claims, not new execution**
- The focused-run counts below were reported by the implementer. They were not re-executed for this document:
  - F1–F3 retry suites: 190 passed;
  - F4–F15 gap suites: 860 passed;
  - after the `/stop` separation, stop/controller/scheduler suites: 632 passed;
  - `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80`: pass.
- **The full suite (`make check`) has not been run.** This deliverable is not execution evidence.

**Not claimed**
- Cancelling an independent scheduled run while it is still in admission or credential resolution is **not supported and not claimed**. See the F6 scope ruling.
- No accepted ADR, frozen artifact or task checkbox was changed for this review.

## Proof layers

| Label | Meaning |
|---|---|
| **P** | Production-root proof through the real `main._run` / `RootHarness`. Covers 7.1, 12.11, 12.20, 12.24 and the root portions of 3.6, 7.2 and 7.3. |
| **X** | Coordinated graph cutover and shutdown: 12.25, 12.26. |
| **K** | Component preparation: 12.16, 9.1, 12.17, 12.18. |
| **I** | C1/C2 injected adapter. A real registry and runtime sit behind an injected strict factory (12.12–12.14, C1 characterization). **Never cited as root proof.** |
| **U** | Focused unit test. |

## 1. Findings reconciliation

| ID | Finding | Resolution | Evidence |
|---|---|---|---|
| F1 | `CallAttemptState.mark_partial_output` and `mark_effect` have no production callers | **Rebutted as an implementation gap.** A failing call cannot emit partial output or cause an effect in this architecture. Reasons: no streaming (`llm_client.py:594-622`, Ollama `stream=False`); adapters drop `progress_cb`; tools dispatch only after `_request_turn` accepts. The veto stays in place for any future streaming mode. | `tests/test_react_loop_bounded_retry.py::test_failing_real_client_call_emits_no_partial_output_or_effect` (4 HTTP adapters × {503 with body, truncated 200}); `::test_real_client_failed_call_body_never_reaches_progress_or_history`; `tests/test_call_attempt_state.py::test_partial_output_or_effect_vetoes_automatic_retry` |
| F2 | Retry after a completed tool turn, without replay | **Resolved** | `tests/test_react_loop_bounded_retry.py::test_transient_failure_after_completed_tool_turn_retries_without_replay` |
| F3 | Attempt state kept across a parse re-prompt and a confirmation wait | **Resolved** | `::test_parse_reprompt_retains_request_state_and_budget`; `::test_confirmation_wait_keeps_request_state_until_action_is_accepted` |
| F4 | Required cap with C−I≤0 | **Resolved** | `tests/test_model_request_budget.py::test_required_cap_without_positive_remaining_combined_capacity_fails`; `tests/test_run_model_client.py::test_required_cap_without_remaining_combined_capacity_fails_before_send` |
| F5 | `/context` in the menu and in help | **Resolved** | `tests/test_telegram_command_surface.py::TestBotCommandDiscovery::test_context_in_bot_commands`; `::TestCmdHelpOutput::test_help_includes_context` |
| F6 | Scheduled cancellation before or during admission | **Scope ruling by the user, not a waiver.** See §1.1. | `tests/test_scheduled_admission_injected.py::test_agents_cancel_managed_landing_as_admitted_run_registers_cancels_before_any_request`; `::test_shutdown_during_admission_is_a_recorded_launch_failure`; `::test_running_scheduled_run_cancelled_during_inflight_request` |
| F7 | Pagination failing partway through | **Resolved** | `tests/test_model_discovery.py::test_second_page_failure_keeps_last_good_inventory_stale_without_partial_merge` (anthropic/google/openrouter × http-503/malformed) |
| F8 | Checkpoint deleted on success | **Already covered** | `tests/test_react_loop_error_recovery.py::TestReactLoopErrorRecovery::test_retry_then_success` (asserts the checkpoint path no longer exists) |
| F9 | Exhausted retry ends the run, not the bot | **Resolved (P)** | `tests/test_main.py::TestRootStartupComposition::test_exhausted_request_keeps_commands_and_next_task_available`. Limit: Telegram is a harness boundary. |
| F10 | Pinned scheduled model hits a capability or context failure | **Resolved (P)** | `tests/test_scheduler_failure_classification.py::test_root_scheduled_pinned_capability_or_context_failure_has_no_fallback` (capability-vision-unverified, context-exceeds-local-budget, context-rejected-by-provider) |
| F11 | Other paths cannot reach graph resolution | **Accepted as a structural argument.** `resolve_graph_model` has one production caller (`graph_memory.py:1552`), whose callers are `main._build_graph_memory` and the backfill CLI. The ordinary and scheduled factories use only `admit` / `admit_scheduled`. | `tests/test_graph_model_resolution.py::test_graph_resolution_does_not_open_ordinary_admission`; `tests/test_graph_memory_integration.py::test_prerequisites_do_not_open_ordinary_admission`; root admission spies |
| F12 | Legacy `context_window`/`max_tokens` compaction branch | **Kept as raw-dict compatibility only; never cited as scenario coverage.** It is unreachable in production. | `tests/test_p2_compaction.py::TestResolveCompactionThreshold::test_pinned_client_config_never_reaches_the_legacy_raw_dict_branch` |
| F13 | `config_schema.resolve_model_id` is dead code | **Recorded as dead and unrelated.** Its tests are not selector evidence. The strict path is proven. | `tests/test_model_registry.py::test_ordinary_selection_and_admission_use_exact_canonical_ids_only` (display-name, case-variant, upstream-only, padded) |
| F14 | README overstated Main inheritance | **Resolved** by rewording in the README (sub-agent / scheduled model sentence). | Behavior shown by the root explicit-selector tests in §2 |
| F15 | Retired embedding tests | **Partly restored; the serial fallback stays retired** (replaced by explicit unavailability) | `tests/test_embedding_client.py::test_empty_batch_returns_empty_without_request`, `::test_partial_cache_hit_requests_only_missing_inputs_in_order`; `::test_unavailable_configuration_is_safe_and_fails_actionably` |
| — | Removing a TOML job when the legacy file is unreadable or malformed | **Already covered** | `tests/test_scheduler_legacy_readonly.py::TestTomlRemovalShadowPreflight::test_removal_refused_by_current_legacy_file` (`malformed_json`, `unreadable_directory`) |
| — | Agentic legacy `run_now` | **Resolved** | `tests/test_scheduled_admission_injected.py::test_run_now_of_agentic_legacy_json_job_uses_normal_admission` |

### 1.1 F6 scope ruling (user decision, 2026-10-31)

- Frozen scenario: `specs/scheduled-job-execution/spec.md`, "Cancellation around admission".
- **The ruling.** The user chose to preserve the existing cancellation paths. The scenario's "existing cancellation semantics" means the existing operator and shutdown cancellation paths.
  - Those paths reach a scheduled run once it is registered. Registration happens after the synchronous `admit_scheduled` returns (`sub_agent_supervisor.py:352-381`). This order is unchanged since the proposal commit.
  - Shutdown during admission is a recorded launch failure.
- **Proven:**
  - cancel at the first point the run can be addressed: a cancelled record under the run ID, tracking released, clients closed, no provider request;
  - shutdown during admission: a failed record, an alert even with `notify=false`, tracking released;
  - in-flight cancel.
- **Out of scope.** A new pending-admission or pre-registration cancel handle is **outside provider-model-catalog** and is neither implemented nor claimed. This is a scope ruling. It does not waive or reinterpret any supported behavior.

### 1.2 `/stop` scope separation

- During review an ownership-scoped `/stop` was briefly implemented. It conflicted with live main specs (telegram-command-surface "/stop cascades to all sub-agents"; scheduled-job-execution, sub-agent-supervision and sub-agent-council-control `/stop` clauses) and with ADR-0033 lines 38, 42 and 54.
- The user ruled it a **separate future change**. It was selectively reverse-applied from this worktree. `cmd_stop` again calls `cancel_all_managed()` with its original reply, and the README `/stop` row is restored.
- **The conflict is therefore closed for this change.** No live spec or accepted ADR is contradicted, and none was edited.
- `tests/test_scheduled_admission_injected.py::test_agents_cancel_managed_landing_as_admitted_run_registers_cancels_before_any_request` keeps its accurate name. It exercises the `cancel_all_managed` sweep shared by `/agents cancel managed` and the current `/stop`.

### 1.3 Observations (not frozen-spec violations; left for triage)

- O1: when the budget is exhausted at a parse re-prompt, the operator sees a misleading "unexpected provider failure" card.
- O2: the legacy provider retry helpers in `providers/_utils.py` are dead code.
- O3: when submit rolls back during shutdown, the constructed runner's clients are not closed.
- O4: a 503 is classified as `unknown`.

## 2. Requirement/scenario → test map

Tests are cited by file, then node. Layer labels follow the table above; anything unlabelled is U.

### 2.1 provider-model-catalog

| Requirement / scenario | Tests |
|---|---|
| Identity: upstream ID contains slashes | `test_model_records.py::test_canonical_identity_splits_only_first_slash`; `test_model_discovery.py::test_openai_exact_ids_and_unsupported_metadata_remain_unknown` |
| Unsupported per-model tuning rejected | `test_config_schema.py::TestProviderConnections::test_unsupported_provider_field_rejected_before_expansion`, `::test_provider_retry_keys_rejected_actionably`, `::test_legacy_model_entry_rejected`; `test_config_example.py::test_example_config_rejects_appended_legacy_keys` |
| Ollama: non-cloud endpoint rejected | `test_config_schema.py::TestProviderConnections::test_ollama_is_official_cloud_only`, `::test_ollama_malformed_urls_are_sanitized`; `test_model_discovery.py::test_ollama_rejects_non_official_origin_before_request`; `test_ollama_transport.py::test_ollama_custom_or_invalid_origin_is_rejected_before_client_construction` |
| Ollama: one origin, no `/api/api` | `test_model_discovery.py::test_ollama_cloud_path_auth_and_no_duplicate_api_segment`; `test_ollama_transport.py::test_installed_sdk_serializes_exact_pinned_cloud_request_and_counts_once`, `::test_ollama_redirect_is_not_followed` |
| Ollama: missing credential | `test_config_schema.py::TestProviderConnections::test_ollama_requires_credential_source`, `::test_ollama_normalizes_api_suffix_and_requires_key`; `test_model_discovery.py::test_cloud_ollama_empty_api_key_requires_authentication_before_any_request` |
| Refresh: complete pagination replaces inventory | `test_model_discovery.py::test_anthropic_cursor_pages_and_nullable_limits`, `::test_google_pages_use_exact_base_model_id_not_resource_name`, `::test_openrouter_uses_same_origin_next_link_and_false_capability`; `test_model_registry.py::test_refresh_replaces_complete_inventory_and_keeps_supplements_separate`; P `test_main.py::TestRootStartupComposition::test_startup_refreshes_every_provider_through_all_pages_then_restores_main` |
| Refresh: failed refresh preserves stale inventory | `test_model_discovery.py::test_second_page_failure_keeps_last_good_inventory_stale_without_partial_merge`, `::test_pagination_cycle_fails_without_partial_inventory`, `::test_foreign_pagination_is_rejected_before_credentialed_request`; `test_model_registry.py::test_failed_refresh_preserves_last_good_models_and_marks_provider_stale`, `::test_malformed_duplicate_or_unsourced_inventory_is_rejected_without_partial_merge`; P `test_main.py::TestRootGraphStartupContainment::test_graph_resolves_stale_last_good_entry_after_failed_provider_refresh` |
| Refresh: valid empty inventory | `test_model_discovery.py::test_empty_inventory_is_valid_and_invalid_response_is_safe`; `test_model_registry.py::test_valid_empty_inventory_deactivates_but_remembers_selection_and_rediscovery_does_not_restore` |
| Refresh: late refresh cannot restore a deleted provider | `test_model_registry.py::test_changed_or_deleted_connection_discards_inflight_refresh_and_purges_inventory`, `::test_newer_completed_refresh_discards_older_late_result` |
| Supplement fills only unknown fields; distinct limit types | `test_model_discovery.py::test_models_dev_exact_match_fills_unknown_fields_only_and_is_credential_free`, `::test_supplement_failure_does_not_invalidate_provider_inventory`, plus the Anthropic, Google, OpenRouter and OpenAI adapter tests above |
| Privacy: no private data sent to models.dev | `test_model_discovery.py::test_models_dev_exact_match_fills_unknown_fields_only_and_is_credential_free` |
| Settings: saved model absent; no saved selection; corrupt settings preserved | `test_model_registry.py::test_unavailable_saved_id_is_remembered_but_not_restored_or_admitted`, `::test_missing_settings_do_not_infer_selection_and_explicit_admission_is_gated`, `::test_corrupt_managed_state_is_preserved_and_registry_remains_inactive`; `test_model_settings.py::test_unavailable_remembered_model_id_is_preserved`, `::test_corrupt_input_is_preserved_exactly_before_any_write`, `::test_preservation_failure_prevents_overwrite`; P `test_main.py::TestRootManagedSettingsStartup::test_missing_or_corrupt_settings_keep_commands_and_gate_model_work`, `TestRootStartupComposition::test_missing_main_keeps_commands_available_without_default_or_model_work` |
| Settings: selection write fails | `test_model_registry.py::test_selection_persists_before_active_publication_and_write_failure_keeps_prior_choice`; `test_telegram_models.py::TestModelSelectionCallback::test_select_callback_reports_failure_and_leaves_prior_selection_unchanged_when_persistence_fails` |
| Serialization: concurrent refresh and selection; expired callback | `test_model_registry.py::test_refresh_merges_against_selection_made_while_fetch_is_blocked`, `::test_refresh_persists_candidate_before_publishing_it`; `test_telegram_models.py::TestModelSelectionCallback::test_select_callback_rejects_stale_or_unknown_tokens`, `::test_select_callback_reports_reopen_when_target_removed_before_redemption` |
| Shared registry: current-run pin visible; metadata state without callability promise | `test_model_catalog_prompt_view.py::test_removed_pin_never_shares_a_line_with_the_active_model`, `::test_removed_pin_is_flagged_as_missing_from_current_inventory`, `::test_section_disclaims_any_promise_that_a_listed_model_is_callable`, `::test_capability_true_is_supported_false_is_unsupported_and_none_is_unknown`, `::test_field_sources_follow_each_models_actual_provenance`, `::test_no_connection_value_or_provider_error_text_reaches_any_consumer`; P `test_main.py::TestRootStartupComposition::test_main_request_presents_active_main_distinct_from_admitted_pin` |
| Admission: inactive Main blocks explicit job override; scheduled job fails when Main invalid | `test_model_registry.py::test_scheduled_admission_requires_active_main_before_classifying_or_resolving`, `::test_scheduled_admission_without_saved_catalog_fails_closed_without_resolution`; I `test_scheduled_admission_injected.py::test_selector_does_not_bypass_missing_main`; P `test_scheduler_failure_classification.py::test_root_scheduled_job_without_valid_main_fails_before_model_work` |
| Admission: unresolvable scheduled selector inherits a valid Main | `test_model_registry.py::test_scheduled_admission_pins_saved_selector_or_falls_back_to_main_with_one_resolution`; P `test_scheduled_model_admission.py::test_root_scheduled_selector_matrix`, `::test_root_scheduled_unknown_selector_warning_is_bounded_and_redacted` |
| Admission: ordinary explicit selectors stay strict | `test_model_registry.py::test_ordinary_selection_and_admission_use_exact_canonical_ids_only`; I `test_profile_admission_matrix.py::test_factory_explicit_b_without_main_is_refused`, `::test_spawn_explicit_b_without_main_is_refused_before_allocation`, `::test_plan_step_without_main_fails_step_before_allocation`; P `test_run_model_admission.py::test_root_factory_refuses_explicit_selector_without_active_main`, `test_main.py::TestRootDiagnosticMainGate::test_diagnostic_profile_refuses_valid_explicit_selector_without_active_main` |
| Admission: each run admitted exactly once | I `test_admission_factory_wiring.py::test_profile_branch_admits_once_and_pins_runner`, `test_agent_runtime_characterization.py::TestRuntimeFactoryAdapterAdmission::test_each_call_admits_fresh_pin_on_profile_branch`; P `test_root_composition_amended.py` (all six tests), `test_agent_runtime_characterization.py::TestRootSubAgentFactoryWiring::test_builtin_installs_exact_helper_factory_and_forwards_one_pin` |
| Admission: removed model does not change a running task; rediscovery does not reactivate | `test_model_registry.py::test_pinned_run_survives_selection_and_inventory_removal_and_view_hides_connection_secrets`, `::test_valid_empty_inventory_deactivates_but_remembers_selection_and_rediscovery_does_not_restore`; `test_run_model_admission.py::test_selection_and_connection_refresh_never_retarget_existing_runners`; P `::test_root_pins_survive_later_main_selection_and_real_refresh` |
| Admission: obsolete agent selector rejected | `test_config_schema.py::TestProviderConnections::test_obsolete_agent_selectors_rejected`; `test_fallback_deprecation.py::test_agent_fallback_models_present_is_rejected` |
| Admission: on-demand and plan failures contained | I `test_profile_admission_matrix.py::test_spawn_explicit_b_without_main_is_refused_before_allocation`, `::test_plan_step_without_main_fails_step_before_allocation` |
| Pinned metadata reaches the budgeting owner | `test_run_model_client.py::test_preview_uses_final_wire_estimate_without_sending_or_counting`; `test_request_preparation.py::TestOverflowAttributes::test_preview_exposes_budget_and_metadata_is_typed`; `test_p2_compaction.py::TestResolveCompactionThreshold::test_catalog_metadata_uses_hard_budget_reserve` |
| Embeddings missing: bot keeps running, indexing degraded | `test_embeddings.py::test_parsed_unavailable_embeddings_degrade_client_and_tool_index`; P `test_main.py::TestRootStartupGraphMemory::test_graph_disabled_when_embeddings_unavailable`, `::test_graph_disabled_when_embedding_configuration_unavailable` |
| Graph prerequisites invalid: graph disabled only | K `test_graph_memory_integration.py::test_unusable_graph_selector_fails_with_registry_code_and_guidance`, `::test_unavailable_embeddings_fail_actionably_and_close_client`; P `test_main.py::TestRootStartupGraphMemory::test_graph_disabled_when_selector_unresolvable`, `TestRootGraphStartupContainment::test_invalid_scheduler_and_graph_selector_fail_independently_while_commands_work` |
| Live backfill fails on invalid prerequisites | K `test_graph_memory_backfill.py::TestPrepareBackfillPrerequisites::*`; X `TestBackfillMainLiveAndDryRun::test_live_path_prepare_failure_exits_before_db_http_and_state_writes`, `::test_live_path_embedding_unavailable_exits_before_graph_store` |
| Graph allowance/budget scenarios (exceeds allowance, O<1024, unknown capacity, temperature strip, Google fields) | `test_graph_model_budget.py::test_graph_client_prefit_overflow_raises_before_any_send`, `::test_factory_checks_known_output_threshold_before_any_io`, `::test_provider_rejecting_fixed_cap_with_unknown_output_fails_after_one_send`, `::test_graph_client_sends_fixed_policy_on_each_adapter_and_counts_usage_once`, `::test_discovered_google_methods_infer_no_limits_and_rejection_stays_single_send`; X `test_graph_memory_backfill.py::TestBackfillExtractionWireIsolation::*`; P `test_main.py::TestRootStartupGraphMemory::test_graph_disabled_when_extraction_budget_too_small` |
| Graph resolution: works without Main; exact selector; rechecks only inside resolution | `test_graph_model_resolution.py::test_graph_selector_resolves_exact_record_regardless_of_main_state`, `::test_graph_selector_not_exactly_in_catalog_is_model_not_found`, `::test_graph_selector_of_wrong_type_is_invalid_request`, `::test_graph_target_change_during_credential_resolution_fails_connection_changed`, `::test_graph_target_aba_restoration_still_fails_generation_recheck`, `::test_unrelated_change_during_credential_resolution_keeps_graph_pin_with_current_provenance`, `::test_graph_pin_is_not_retargeted_by_later_main_change_or_target_removal`; P `test_main.py::TestRootStartupGraphMemory::test_graph_enabled_works_without_active_main` |
| Graph: one pin, per-call owned clients | K `test_graph_memory_integration.py::TestGraphExtractionClientBoundary::*`; X `test_graph_memory_backfill.py::TestBuildExtractionLLMCall::*`, `TestBackfillMainLiveAndDryRun::test_live_path_prepares_then_runs_extraction_and_closes_clients`; P `test_main.py::TestRootGraphShutdown::test_completed_drain_closes_dependencies_only_after_worker_termination` |
| Graph: other paths cannot reach resolution | Structural argument (F11) plus the reverse-direction tests |
| Graph startup: resolves after refresh; saved catalog only; missing/corrupt/unknown catalog fails before DB; dry run touches nothing; budget check first; explicit embeddings | P `test_main.py::TestRootGraphStartupContainment::test_graph_resolves_stale_last_good_entry_after_failed_provider_refresh`; K `test_graph_memory_backfill.py::TestPrepareBackfillPrerequisites::test_saved_catalog_pins_graph_model_independent_of_main`, `::test_missing_catalog_fails_with_refresh_guidance`, `::test_corrupt_catalog_fails_without_backup_or_contents`, `::test_unknown_selector_keeps_model_not_found`; X `TestBackfillMainLiveAndDryRun::test_live_path_corrupt_catalog_or_unknown_selector_fails_before_side_effects`, `::test_dry_run_counts_despite_missing_telegram_secret_and_invalid_selector`, `::test_cold_start_dry_run_never_imports_ladybug`, `::test_dry_run_preview_limit_force_checksum_and_no_writes`; `test_graph_model_budget.py::test_kernel_rejects_prerequisites_at_zero_input`; K `test_graph_memory_integration.py::test_graph_budget_failure_uses_kernel_code_before_any_graph_client`, `::test_embedding_credential_comes_only_from_embedding_config` |
| Shutdown: completed drain precedes teardown | X `test_graph_memory.py::TestGraphMemoryWriterBoundedStop::test_finite_stop_drains_buffered_pending_batch_before_returning`, `test_graph_memory_integration.py::test_graph_writer_finite_stop_drains_buffered_batch_on_real_worker_thread`; P `test_main.py::TestRootGraphShutdown::test_completed_drain_closes_dependencies_only_after_worker_termination` |
| Shutdown: stop timeout retains dependencies | X `test_graph_memory.py::TestGraphMemoryWriterBoundedStop::test_blocked_worker_stop_fails_explicitly_until_released`; P `test_main.py::TestRootGraphShutdown::test_blocked_extraction_fails_shutdown_and_retains_live_worker_dependencies`, `::test_blocked_embedding_after_extraction_retains_root_embedding_and_store`, `::test_unexpected_stop_failure_is_wrapped_and_retains_dependencies` |
| Migration: preview, ambiguity, operator edits, backups, default seeding, local Ollama, unrelated records, idempotency, lock | `test_migrate_models.py::test_preview_leaves_original_files_unchanged`, `::test_default_matching_different_accounts_stops_without_seed`, `::test_operator_edit_during_failure_window_is_not_rolled_back`, `::test_recover_leaves_operator_edit_untouched`, `::test_apply_backs_up_exact_originals_before_target_replace`, `::test_tuning_and_retry_fields_are_discarded_and_reported`, `::test_first_entry_seeds_when_no_default_configured`, `::test_stopped_first_entry_infers_no_default`, `::test_local_ollama_entry_stops_for_explicit_cloud_choice`, `::test_apply_does_not_touch_unrelated_records`, `::test_repeating_successful_apply_is_idempotent`, `::test_live_daemon_lock_refuses_apply`; `test_process_lock.py::*`; P `test_main.py::TestStartupLockOrdering::test_lock_precedes_config_read_and_holds_through_run` |

### 2.2 scheduled-job-execution

| Requirement / scenario | Tests |
|---|---|
| Startup containment (unreadable/unparseable, missing, operator notice, failed reload, casts, state/registration) | `test_scheduler_load_containment.py::test_unparseable_file_bytes_survive_startup`, `::test_missing_file_loads_empty_job_map_without_error`, `::test_missing_file_first_add_creates_file`, `::test_bad_file_sends_one_operator_notice`, `::test_bad_file_notice_visible_in_tool_list`, `::test_malformed_local_setting_does_not_escape_construction`, `::test_malformed_state_file_does_not_escape_construction`, `::test_registration_failure_does_not_escape_start`; `test_scheduler_atomic_reload.py::TestFailedReloadPreservesLatch::test_latch_and_last_good_state_unchanged`; `test_telegram_scheduler_load_notice.py::*`; P `test_main.py::TestRootGraphStartupContainment::test_invalid_scheduler_and_graph_selector_fail_independently_while_commands_work` |
| Job containment (bad scheduling field, fire-time bad field, non-string task, siblings, invalid overlap policy) | `test_scheduler_load_containment.py::test_bad_scheduling_field_shows_config_error`, `::test_bad_scheduling_field_leaves_job_unscheduled`, `::test_bad_scheduling_field_run_now_returns_config_error`, `::test_bad_job_leaves_healthy_sibling_registered`; `test_scheduler_fire_dispositions.py::test_bad_field_fire_is_recorded_launch_failure`, `::test_run_now_with_non_string_task_is_recorded_launch_failure`, `::test_blank_string_task_takes_notify_gated_reminder`, `::test_active_job_with_invalid_policy_is_skipped`, `::test_idle_job_with_invalid_policy_persists_last_run` |
| Raw selectors stored raw until execution | `test_scheduled_model_admission.py::test_core_add_without_model_inherits_silently`, `::test_core_modify_without_model_keeps_current_model`, `::test_core_add_stores_falsy_model`, `::test_core_add_stores_truthy_raw_model`, `::test_tool_add_stores_raw_model`; `test_scheduler_toml_encoder.py::TestExplicitPatchPreservesDocument::test_add_without_model_omits_model_key`, `::test_modify_with_none_model_keeps_stored_model`; `test_scheduler_load_containment.py::test_raw_model_value_loads_without_config_error` |
| Selector evidence bounded and redacted; pinned ID after admission | `test_scheduled_selector_logging.py::test_selector_evidence_non_string_is_label_only`, `::test_selector_evidence_long_string_bounded_with_indicator`, `::test_registry_unknown_secret_selector_redacted_and_bounded`; `test_scheduler_failure_classification.py::test_pre_pin_launch_record_stores_label_for_non_string_model`, `::test_pre_pin_launch_record_bounds_and_redacts_string_model`; P `test_root_composition_amended.py::test_scheduled_owner_notice_contains_admitted_pin` |
| Admission matrix (absent/None/blank/non-string/unknown/known/stale) | `test_model_registry.py::test_scheduled_admission_pins_saved_selector_or_falls_back_to_main_with_one_resolution`; P `test_scheduled_model_admission.py::test_root_scheduled_selector_matrix`, `::test_root_scheduled_stale_known_selector_is_pinned_without_warning`; `test_trusted_scheduled_spawn.py::test_scheduler_fire_empty_model_inherits` |
| Main gate for scheduled runs | See 2.1 admission rows |
| No fallback after pin (credential, capability, context, outage) | P `test_scheduler_failure_classification.py::test_root_scheduled_credential_failure_stays_on_effective_model`, `::test_root_scheduled_pinned_capability_or_context_failure_has_no_fallback`, `::test_root_scheduled_provider_failure_is_one_attempt_without_fallback` |
| Legacy per-job `fallback_models` | `test_fallback_deprecation.py::test_job_fallback_models_load_and_reload_are_silent`, `::test_job_fallback_models_fire_warns_once_and_spawns_without_key`; `test_scheduler_fire_dispositions.py::test_fire_warns_once_about_fallback_models`, `::test_fire_never_reveals_fallback_models_value`, `::test_fire_leaves_fallback_models_file_bytes_unchanged` |
| Trusted source and one root admission | `test_trusted_scheduled_spawn.py::test_default_spawn_refuses_bad_selector_shape`, `::test_obsolete_key_refusal_is_identical_for_scheduled_source`, `::test_scheduler_fire_forwards_raw_job_model_to_factory`; `test_admission_factory_wiring.py::test_scheduled_raw_selector_forwarded_by_identity`; I `test_scheduled_admission_injected.py::test_cron_fire_admits_once_and_completes_on_pinned_model`; P `test_root_composition_amended.py::test_scheduled_explicit_selector_preserves_profile_lifecycle` |
| Admission races (refresh, Main change, same-revision document, redacted warnings) | `test_model_registry.py::test_scheduled_admission_rejects_state_changed_during_credential_resolution` (`select-other`, `reselect-main`), `::test_scheduled_admission_rejects_same_revision_removal_only_document_during_resolution`; I `test_scheduled_admission_injected.py::test_same_entry_refresh_during_admission_credentials_fails_launch`; `test_scheduled_selector_logging.py::test_registry_unknown_short_selector_warns_before_resolve` |
| Lifecycle: admission failure is a recorded launch failure; no invented pre-spawn record | P `test_scheduler_failure_classification.py::test_root_scheduled_job_without_valid_main_fails_before_model_work`; `::test_failed_spawn_alert_is_direct_with_notify_false`; `test_scheduler_fire_dispositions.py::test_blank_string_task_takes_notify_gated_reminder` |
| Lifecycle: cancellation around admission | Existing paths only, per the F6 ruling (§1.1): I `test_scheduled_admission_injected.py::test_agents_cancel_managed_landing_as_admitted_run_registers_cancels_before_any_request`, `::test_shutdown_during_admission_is_a_recorded_launch_failure`, `::test_running_scheduled_run_cancelled_during_inflight_request` |
| Explicit patch, encoder, load never writes | `test_scheduler_toml_encoder.py::TestEncoderRoundtrip::*`, `TestExplicitPatchPreservesDocument::*`, `::test_encoder_refuses_unrepresentable_value`, `::test_add_refuses_unrepresentable_model_before_attempt`; `test_scheduler_load_containment.py::test_legacy_schedule_load_leaves_file_untouched`. The "comments and layout not promised" scenario is a non-guarantee and needs no test. |
| Mutation protocol (prepare, freshness, external edit, concurrency, write failure, lock order) | `test_scheduler_atomic_mutations.py::TestTriggerPreparationFault::*`, `::test_sequential_own_writes_all_succeed`, `TestExternalEditRefused::*`, `TestRecordedMissingFile::*`, `::test_independent_updates_from_same_base_both_persist`, `TestWriteFailure::*`, `TestNoDeadlockWithRunHistory::*`, `TestNoDeadlockWithStateSave::*`, `TestNoDeadlockWithReload::*`; `test_scheduler_atomic_publication.py::*` |
| Publication failure latch | `test_scheduler_publication_failure.py::TestPostCommitFault::*`, `::test_latched_scheduler_refuses_every_public_mutation`, `::test_reload_after_fault_clears_publishes_complete_unit_and_clears_latch` |
| Reload of own candidate | `test_scheduler_atomic_reload.py::test_operator_edit_reload_publishes_own_bytes_without_rewrite`, `TestMutationDuringReloadPreparation::*`, `TestLegacyFingerprintRace::*`, `TestFailedReloadPreservesLatch::*`, `::test_successful_reload_clears_set_latch`; `test_scheduler_reload_atomicity.py::TestReloadAtomicPublish::test_concurrent_reader_never_sees_empty_map` |
| Refusal before or after attempt | `test_scheduler_load_containment.py::test_known_bad_file_refuses_add_before_attempt`; `test_scheduler_atomic_mutations.py::TestExternalEditRefused::test_mismatch_after_attempt_calls_hook_exactly_once`; `test_scheduler_publication_failure.py::test_builtin_postcommit_failure_is_executed` |
| Legacy JSON read-only and shadow removal | `test_scheduler_legacy_readonly.py::TestNonDestructiveLoad::*`, `::test_json_only_job_schedule_prose_carries_marker`, `TestJsonOnlyMutationRefused::*`, `TestTomlRemovalShadowPreflight::*`, `::test_legacy_change_after_preflight_fails_removal`, `::test_shadowing_toml_job_change_proceeds`, `::test_bad_legacy_file_sends_startup_notice`, `::test_malformed_legacy_reload_keeps_last_good_legacy_jobs`, `::test_operator_cleanup_reload_drops_legacy_job`; I `test_scheduled_admission_injected.py::test_run_now_of_agentic_legacy_json_job_uses_normal_admission` |
| State projection | `test_scheduler_state_projection.py::test_saved_state_excludes_raw_model_selector`, `::test_state_write_fault_is_contained` |

### 2.3 per-model-context-window

| Scenario | Tests |
|---|---|
| Budget matrix: C+T+O, neither known (local), C+O reserve, Gemini T-only, known optional O≥C | `test_model_request_budget.py::test_optional_cap_budget_matrix`; `test_p2_compaction.py::TestResolveCompactionThreshold::test_catalog_metadata_uses_hard_budget_reserve`, `::test_catalog_input_only_limit_is_not_reduced_by_output_or_local_context`; `test_request_preparation.py::TestRepairInputCeiling::*` |
| Effective limit taken from the pin each turn | `test_p2_compaction.py::TestResolveCompactionThreshold::test_supplied_request_budget_threshold_is_hard_input_based_and_metadata_immutable`, `::test_request_budget_overrides_catalog_metadata_threshold_for_compaction`, `::test_pinned_client_config_never_reaches_the_legacy_raw_dict_branch` |
| Tool-definition tokens (included; default 0) | `test_p2_compaction.py::TestToolDefsTokensCompaction::*`, `TestBackwardCompatibility::test_call_without_tool_defs_tokens_unchanged`; `test_p3_token_estimation.py::TestCompleteRequestPayload::test_counts_system_messages_tool_definitions_and_native_call_arguments` |
| Required cap: unknown O; bounded by C−I; C−I≤0; input-only | `test_model_request_budget.py::test_required_cap_rejects_missing_combined_output_metadata`, `::test_required_cap_uses_remaining_combined_capacity_as_reserve`, `::test_required_cap_without_positive_remaining_combined_capacity_fails`, `::test_required_cap_for_input_only_model_does_not_reduce_input_budget`; `test_run_model_client.py::test_required_cap_without_remaining_combined_capacity_fails_before_send` |
| Unknown optional O; floor does not override hard fit | `test_model_request_budget.py::test_unknown_optional_output_uses_assumed_local_reserve_only_with_combined_capacity`, `::test_input_over_hard_budget_is_rejected_even_when_floor_would_apply`; `test_request_preparation.py::TestRealReactPreparation::test_sub_256_budget_gets_forced_repair_or_final_reject` |
| Strict limits (malformed rejected; absent means unknown) | `test_graph_model_budget.py::test_kernel_rejects_invalid_raw_metadata_limits` (shared `_positive_optional`); `test_model_request_budget.py::test_invalid_counts_and_limits_fail_closed`, `::test_optional_cap_budget_matrix` |
| Obsolete fallback settings; single-model client | `test_fallback_deprecation.py::*`; `test_p2_vision_fallback.py::test_text_only_provider_failure_does_not_retry_or_retarget`; `test_native_tool_contract.py::test_compatibility_alias_propagates_failure_from_one_pinned_send` |
| Fixed-output graph budget (7 scenarios) | `test_graph_model_budget.py::test_kernel_fixed_cap_report_and_exact_fit_boundary`, `::test_kernel_rejects_prerequisites_at_zero_input`, `::test_kernel_accepts_output_threshold_and_unknown_output`, `::test_factory_checks_known_output_threshold_before_any_io`, `::test_graph_client_without_headroom_fails_permanently_before_io`, `::test_graph_client_prefit_overflow_raises_before_any_send`, `::test_ordinary_required_cap_still_needs_known_output_while_graph_does_not`; `test_model_request_budget.py::test_graph_fixed_cap_is_not_lowered_and_can_be_used_without_output_metadata` |
| Pinned vision | `test_pinned_vision_model.py::*`; `test_p2_vision_fallback.py::*`; `test_run_model_client.py::test_unknown_vision_and_unsupported_native_are_rejected_before_send` |
| R6 manual-switch guard (9 scenarios) | The 12.19 evidence map in `tests/test_model_switch_budget_guard.py:4-43` is kept unchanged (`TestGuardedRejection`, `TestGuardedFit`, `TestTransitions::test_a_b_c_compares_against_b_and_does_not_rearm_a`, `TestDerivedBudgetComparison`, `TestUnknownHistoryProtection`, `TestHistoryReplacementInvalidatesTickets`, `TestGraphPreparation`, `TestRealGraphContract`, `TestExplicitAdmissionDoesNotBorrowExemption`) |
| REMOVED requirements: vision routing; fallback deprecation | Retired. Replaced by the pinned vision and obsolete fallback rows above. |

### 2.4 native-tool-calling

| Scenario | Tests |
|---|---|
| Native succeeds; text response; native unsupported; transient; permanent and unexpected; linearization | `test_native_tool_contract.py::test_native_consumer_dispatches_only_the_first_structured_call`; `test_react_loop.py::*::test_tool_call_then_finish_preserves_wire_shape`, `::test_native_prose_without_tool_call_is_treated_as_finish`, `::test_native_failure_is_terminal_without_json_protocol_switch`, `::test_linearize_flattens_native_wire_shape`; `test_react_loop_bounded_retry.py::test_local_native_unsupported_converts_to_json_without_count_or_wait`, `::test_native_timeout_retries_native_without_json_switch`, `::test_unknown_permanent_and_context_errors_do_not_auto_retry` |
| Definition assembly, caching, budget counting | `test_native_tool_contract.py::test_assembled_definitions_are_exactly_builtins_plan_and_mcp_inputs`, `::test_real_loop_reuses_cached_full_definitions_on_every_native_request`; `test_schemas_sync.py`; `test_p3_token_estimation.py::TestCompleteRequestPayload::test_counts_system_messages_tool_definitions_and_native_call_arguments` |
| Protocol and provider payloads | `test_native_tool_contract.py::test_llm_provider_chat_signatures_are_retained`, `::test_openai_compatible_native_round_trip_keeps_full_definitions`, `::test_ollama_sdk_send_keeps_full_definitions_without_tool_choice`; `test_request_parameters.py::test_public_llm_provider_protocol_signatures_are_unchanged`; `test_ollama_transport.py::test_native_tool_wire_preserves_full_schema_and_snapshots_caller_tools` |

### 2.5 llm-error-recovery

| Scenario | Tests |
|---|---|
| Typed categories; cancellation not classified | `test_react_loop_error_recovery.py::TestClassifyLLMError::*`; `test_client_error_contract.py::*`; `test_react_loop_bounded_retry.py::test_pre_send_cancel_retires_saved_checkpoint_regardless_of_write_permission`; `test_request_preparation.py::TestSummaryCall::test_cancellation_propagates_through_compaction` |
| Checkpoint written, atomic, non-fatal; deleted on success or cancel; survives timeout | `test_request_preparation.py::TestPreparationFailureState::test_timeout_saves_valid_zero_outbound_state`; `test_checkpoint_store.py::test_atomic_write_leaves_no_tmp`, `::test_save_oserror_is_non_fatal_and_logs_warning`; `test_react_loop_error_recovery.py::TestReactLoopErrorRecovery::test_retry_then_success`, `TestHandleLLMError::test_cancel_deletes_checkpoint`, `::test_timeout_preserves_checkpoint` |
| Retry defaults, zero retries, exhaustion, obsolete provider retry keys | `test_call_attempt_state.py::test_default_budget_allows_five_calls_with_exponential_delays`, `::test_zero_retry_budget_still_allows_one_initial_call`; `test_react_loop_bounded_retry.py::test_timeout_has_five_actual_calls_and_exponential_waits`, `::test_real_client_transport_counts_each_automatic_retry_once`; P `test_main.py::TestRootStartupComposition::test_exhausted_request_keeps_commands_and_next_task_available`; `test_config_schema.py::TestProviderConnections::test_provider_retry_keys_rejected_actionably` |
| One attempt state per logical request; new state per call; effects veto; no refill | `test_react_loop_bounded_retry.py::test_parse_reprompt_retains_request_state_and_budget`, `::test_confirmation_wait_keeps_request_state_until_action_is_accepted`, `::test_empty_response_reasks_share_the_attempt_budget`, `::test_transient_failure_after_completed_tool_turn_retries_without_replay`, `::test_failing_real_client_call_emits_no_partial_output_or_effect`; `test_agent_controller_resume.py::test_resume_authorizes_exactly_one_manual_continuation_without_refill` |

### 2.6 agent-runtime-construction · 2.7 provider-credential-inheritance · 2.8 telegram-command-surface · 2.9 xdg-path-resolution

| Scenario | Tests |
|---|---|
| Runtime: model override preserved; generation overrides rejected; context options preserved | `test_run_model_admission.py::test_actual_admitted_pin_reaches_exact_provider_on_every_profile`, `::test_obsolete_generation_and_selector_options_are_rejected_not_ignored`, `::test_context_prompt_callbacks_and_profile_memory_permissions_preserved`; `test_admission_factory_wiring.py::test_obsolete_generation_keyword_rejected_before_admission`, `::test_supported_arguments_forwarded`; P `test_root_composition_amended.py::test_on_demand_explicit_selector_runs_supervised_on_its_pin` |
| Credentials: provider inheritance; per-model override rejected; migration preserves references; transport inheritance; legacy config routed to migration | `test_run_model_client.py::test_provider_request_uses_pinned_route_auth_and_one_count`; `test_config_schema.py::TestProviderConnections::test_legacy_model_entry_rejected`; `test_migrate_models.py::test_credential_references_remain_in_original_without_promotion`, `::test_unsupported_entry_stops_for_operator_resolution`; `test_model_secrets.py::test_resolved_snapshot_hides_api_key_and_provider_endpoint` |
| Embeddings: matching connection; never chat credentials | `test_embedding_client.py::test_matching_connection_supplies_endpoint_key_and_timeout`, `::test_missing_credential_does_not_borrow_unrelated_chat_connection`; P `test_main.py::TestRootStartupGraphMemory::test_graph_disabled_when_embeddings_unavailable` |
| Telegram: menu and help (health, compress, context, status, reset) | `test_telegram_command_surface.py::TestBotCommandDiscovery::*` (including `test_context_in_bot_commands`); `TestCmdHelpOutput::*` (including `test_help_includes_context`) |
| Telegram: `/models` (single command, concise list, stale refresh, opaque token, expiry, persistence failure) | `TestBotCommandDiscovery::test_models_is_the_only_catalog_command`; `test_telegram_models.py::TestRegistryBackedCatalog::*`, `TestRefreshCallback::*`, `TestModelSelectionCallback::*`, `TestCompleteEntriesUnderMessageLimit::*`; P `test_main.py::TestRootStartupComposition::test_models_selection_callback_recovers_ordinary_request_on_same_root_registry` |
| XDG: defaults, overrides, runtime directory, purity; settings in the protected config home | `test_xdg.py::TestXdgPathsDefaults::*`; `test_path_policy.py::*::test_config_home_reason_is_agent_internal` (structural: config_home is prohibited) |

## 3. Retired-test mapping (relative to the proposal commit `38aabb4`)

| Retired test | Replacement or reason |
|---|---|
| `test_config_schema.py::TestModelConfig::*`, `TestParseConfig::test_multiple_models`, `TestValidation::test_model_missing_*` / `test_no_models` | Per-model config removed. Replaced by `TestProviderConnections::test_legacy_model_entry_rejected` and `TestParseConfig::test_legacy_models_key_is_rejected_even_when_empty`. |
| `TestParseConfigWithEnvVars::test_raw_dict_contains_resolved_values` | `::test_raw_dict_preserves_provider_credential_reference` |
| `TestProviderCredentialInheritance::test_model_inherits_*`, `::test_provider_sec_value_inherited_by_model` | `test_run_model_client.py::test_provider_request_uses_pinned_route_auth_and_one_count` |
| `TestProviderCredentialInheritance::test_embeddings_inherit_provider_credentials_when_section_present` | `test_embedding_client.py::test_matching_connection_supplies_endpoint_key_and_timeout` |
| `TestProviderCredentialInheritance::test_omitted_embeddings_section_preserves_active_model_fallback` | The behavior is now intentionally prohibited. Replaced by `::test_omitted_embeddings_section_is_unavailable` and `test_embedding_client.py::test_missing_credential_does_not_borrow_unrelated_chat_connection`. |
| `TestProviderCredentialInheritance::test_legacy_config_raw_dict_remains_unchanged` | `::test_parse_raw_credentials_remain_references` |
| `test_agent_runtime_characterization.py::TestMainActiveIndexRestore::*`, `TestSubAgentModelConfiguration::test_active_index_restored_*` | `_active_idx` retired. Replaced by `::test_pin_is_immutable_across_successful_run` / `_run_exception` and `TestMainRunLocalPinIsolation::test_run_local_pin_and_trace_isolated`. |
| `TestSubAgentModelConfiguration::test_per_call_overrides_*` | `::test_removed_model_and_generation_kwargs_rejected_before_allocation` |
| `TestAgentControllerReactContextAssembly::test_cancel_ownership_owned_by_default` | `::test_controller_owns_private_event_without_loop_clearing` |
| `test_agent_controller_model_restore.py::test_restores_primary_model_on_*` | `::test_{success,exception,cancellation}_preserves_run_pin_and_main_selection` |
| `test_embeddings.py::TestLLMClientEmbedBatch::*`, provider batch tests | Embedding moved to `EmbeddingClient`. Replaced by `test_embedding_client.py::test_google_batch_order_and_openai_cache`, `::test_provider_errors_are_sanitized_and_bad_batch_cardinality_fails`, `::test_empty_batch_returns_empty_without_request`, `::test_partial_cache_hit_requests_only_missing_inputs_in_order`. |
| `test_embeddings.py::...::test_unsupported_provider_falls_back_to_serial` | Retired on purpose. Unsupported adapters are now unavailable (`test_embedding_client.py::test_unavailable_configuration_is_safe_and_fails_actionably`). |
| `test_providers.py::TestRunWithRetry::test_retries_and_calls_progress_cb` | Provider retry removed (ADR-0035). Replaced by `test_react_loop_bounded_retry.py::test_timeout_has_five_actual_calls_and_exponential_waits`, `::test_real_client_transport_counts_each_automatic_retry_once`. |
| `test_graph_memory_backfill.py::TestBuildExtractionLLMCall` (3 legacy tests) | Kept under the same names and migrated to the pin contract (X, 12.25; see the G-report), plus `::test_failed_chat_closes_owned_client_and_transports_once` |
| Raw-dict-only tests: `test_p2_compaction.py::TestPerModelContextWindow::*`, `TestResolveCompactionThreshold::test_with_context_window` and its siblings; `test_config_schema.py::TestResolveModelId::*` | Kept as legacy compatibility (F12, F13). **Not cited for any scenario.** |

## 4. Conclusion for 11.1

**Mapping status**
- Every requirement and scenario in the nine delta specs has a cited test, or one of the following explicitly recorded alternatives:
  - a structural argument (F11; XDG mount);
  - a non-guarantee (comments/layout);
  - the user's F6 scope ruling.
- Production-root, cutover/shutdown, component and injected evidence are kept separate. No injected double is cited as root proof.

**Remaining before completion**
- The parent's own check of this deliverable.
- The full-suite run (11.3).
- Strict validation (11.4).

**Spec-compliance issues**
- No new ones were found.
- The observations in §1.3 do not violate frozen text.
