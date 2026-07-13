## 1. Characterize Current Construction Behavior

- [ ] 1.1 Add golden-equivalence tests for current `AgentController.run` `ReactContext` assembly, including graph memory, strategy memory, confirmation, trace, cancel ownership, memory fields, and iteration limits.
- [ ] 1.2 Add golden-equivalence tests for current `SubAgentRunner` construction, including isolated LLM client provisioning, short-term memory source, working memory, prompt variant, context payload, and runner-shaped product surface.
- [ ] 1.3 Add tests for `fallback_models` trichotomy (`None`, `[]`, explicit list), per-call LLM overrides, usage registry propagation, caller tag preservation, and model active-index restoration.
- [ ] 1.4 Add tests that registry-installed `_on_step` callbacks are not clobbered after construction.

## 2. Introduce Runtime Types and Builder Skeleton

- [ ] 2.1 Add `RuntimeProfile` with `MAIN`, `ON_DEMAND_SUBAGENT`, `SCHEDULED_AGENT`, `PLAN_STEP_AGENT`, and `DIAGNOSTIC_AGENT`.
- [ ] 2.2 Add `RuntimeOptions` covering model, fallback models, max iterations, max tokens, temperature, top-p, context key, context payload, prompt variant, trace id, cancel event, and label.
- [ ] 2.3 Add an `AgentRuntime` construction boundary without moving call sites yet.
- [ ] 2.4 Add profile-to-source mapping tests proving profiles remain separate from `SubAgentRecord.source` visibility/capacity categories.

## 3. Centralize Sub-Agent Construction

- [ ] 3.1 Move `main.py` `sub_agent_factory` construction logic behind `AgentRuntime.create(...)` while preserving the factory entry point for existing callers.
- [ ] 3.2 Route `spawn_agent` and scheduler-created runners through the runtime builder without changing model-facing tool arguments, supervision options, or result shapes.
- [ ] 3.3 Route plan-step and diagnostic runner construction through the runtime builder without changing `PlanExecutor` DAG, retry, timeout, diagnostic, registry, or aggregation semantics.
- [ ] 3.4 Preserve runner-shaped product compatibility: `.run()`, `.agent_id`, `._model_id`, `._cancel_event`, `._llm`, `._agent`, `._short_term`, `.close()`, and `.notify_fn`.

## 4. Centralize ReactContext Assembly

- [ ] 4.1 Extract `ReactContext` assembly behind the runtime builder while preserving `AgentController` and `SubAgentRunner` as thin frontends.
- [ ] 4.2 Preserve graph-memory and strategy-memory post-init wiring in runtime-built contexts.
- [ ] 4.3 Preserve trace propagation and ADR-0004 thread/executor trace-identity guarantees.
- [ ] 4.4 Preserve cancel-event ownership semantics: owned events may be cleared at run start, forwarded/shared events must not be cleared.
- [ ] 4.5 Preserve confirmation manager wiring and existing interactive controller behavior.
- [ ] 4.6 Preserve `_on_step` ordering: runtime construction must not set or overwrite the registry-installed step callback after registration helpers wire it.

## 5. Update Call Sites and Documentation Surfaces

- [ ] 5.1 Update `BuiltinExecutor` spawn construction comments/tests to reference runtime construction where appropriate.
- [ ] 5.2 Update `PlanExecutor` construction comments/tests to clarify runtime construction vs plan orchestration ownership.
- [ ] 5.3 Update any developer-facing notes needed to clarify that `RuntimeProfile` is construction policy and `SubAgentRecord.source` is visibility/capacity policy.

## 6. Verification

- [ ] 6.1 Run focused tests for runtime/profile construction, agent controller context assembly, sub-agent runner construction, spawn/scheduler paths, plan execution, diagnostics, trace/cancel behavior, and registry iteration callbacks.
- [ ] 6.2 Run `openspec validate introduce-agent-runtime --type change --strict`.
- [ ] 6.3 Run `ruff check .`.
- [ ] 6.4 Run `vulture . vulture_whitelist.py --min-confidence 80 --exclude interfaces.py,.venv,venv`.
- [ ] 6.5 Run `make check`.
- [ ] 6.6 Perform scripted or manual burn-in for main, on-demand, scheduled, plan-step, and diagnostic profiles to confirm construction equivalence and no supervision/visibility regressions.
