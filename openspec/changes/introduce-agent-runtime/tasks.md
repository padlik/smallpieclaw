## 1. Characterize Current Construction Behavior

- [x] 1.1 Add golden-equivalence tests for current `AgentController.run` `ReactContext` assembly, including graph memory, strategy memory, confirmation, trace, cancel ownership, memory fields, and iteration limits.
- [x] 1.2 Add golden-equivalence tests for current `SubAgentRunner` construction, including isolated LLM client provisioning, short-term memory source, working memory, prompt variant, context payload, and runner-shaped product surface.
- [x] 1.3 Add tests for `fallback_models` trichotomy (`None`, `[]`, explicit list), per-call LLM overrides, usage registry propagation, caller tag preservation, and model active-index restoration.
- [x] 1.4 Add tests that registry-installed `_on_step` callbacks are not clobbered after construction.

## 2. Introduce Runtime Types and Builder Skeleton

- [x] 2.1 Add `RuntimeProfile` with `MAIN`, `ON_DEMAND_SUBAGENT`, `SCHEDULED_AGENT`, `PLAN_STEP_AGENT`, and `DIAGNOSTIC_AGENT`.
- [x] 2.2 Add `RuntimeOptions` covering model, fallback models, max iterations, max tokens, temperature, top-p, context key, context payload, prompt variant, trace id, cancel event, and label.
- [x] 2.3 Add an `AgentRuntime` construction boundary without moving call sites yet.
- [x] 2.4 Add profile-to-source mapping tests proving profiles remain separate from `SubAgentRecord.source` visibility/capacity categories.

## 3. Centralize Sub-Agent Construction

- [x] 3.1 Move `main.py` `sub_agent_factory` construction logic behind `AgentRuntime.create(...)` while preserving the factory entry point for existing callers.
- [x] 3.2 Route `spawn_agent` and scheduler-created runners through the runtime builder without changing model-facing tool arguments, supervision options, or result shapes.
- [x] 3.3 Route plan-step and diagnostic runner construction through the runtime builder without changing `PlanExecutor` DAG, retry, timeout, diagnostic, registry, or aggregation semantics.
- [x] 3.4 Preserve runner-shaped product compatibility: `.run()`, `.agent_id`, `._model_id`, `._cancel_event`, `._llm`, `._agent`, `._short_term`, `.close()`, and `.notify_fn`.

## 4. Centralize ReactContext Assembly

- [x] 4.1 Extract `ReactContext` assembly behind the runtime builder while preserving `AgentController` and `SubAgentRunner` as thin frontends.
- [x] 4.2 Preserve graph-memory and strategy-memory post-init wiring in runtime-built contexts.
- [x] 4.3 Preserve trace propagation and ADR-0004 thread/executor trace-identity guarantees.
- [x] 4.4 Preserve cancel-event ownership semantics: owned events may be cleared at run start, forwarded/shared events must not be cleared.
- [x] 4.5 Preserve confirmation manager wiring and existing interactive controller behavior.
- [x] 4.6 Preserve `_on_step` ordering: runtime construction must not set or overwrite the registry-installed step callback after registration helpers wire it.

## 5. Update Call Sites and Documentation Surfaces

- [x] 5.1 Update `BuiltinExecutor` spawn construction comments/tests to reference runtime construction where appropriate.
- [x] 5.2 Update `PlanExecutor` construction comments/tests to clarify runtime construction vs plan orchestration ownership.
- [x] 5.3 Update any developer-facing notes needed to clarify that `RuntimeProfile` is construction policy and `SubAgentRecord.source` is visibility/capacity policy.

## 6. Verification

- [x] 6.1 Run focused tests for runtime/profile construction, agent controller context assembly, sub-agent runner construction, spawn/scheduler paths, plan execution, diagnostics, trace/cancel behavior, and registry iteration callbacks. (`190 passed`)
- [x] 6.2 Run `openspec validate introduce-agent-runtime --type change --strict`. (valid)
- [x] 6.3 Run `ruff check .`. (all checks passed)
- [x] 6.4 Run `vulture . vulture_whitelist.py --min-confidence 80 --exclude interfaces.py,.venv,venv`. (no findings)
- [x] 6.5 Run `make check`. (`1069 passed, 1 skipped`)
- [x] 6.6 Perform scripted or manual burn-in for main, on-demand, scheduled, plan-step, and diagnostic profiles to confirm construction equivalence and no supervision/visibility regressions. (Scripted burn-in via characterization/runtime/profile/spawn/scheduler/plan/running-agent suites; live-daemon manual run was not performed in this environment.)
