"""
agent_runtime.py
----------------
Construction boundary for agent executions (ADR-0007).

This module introduces the vocabulary and skeleton for centralizing agent
construction:

- :class:`RuntimeProfile` — *construction policy* for a given execution kind.
- :class:`RuntimeOptions` — per-execution construction knobs currently
  duplicated across ``main.py``'s ``sub_agent_factory``, ``AgentController``,
  ``SubAgentRunner``, ``BuiltinExecutor.spawn_agent``, and ``PlanExecutor``.
- :class:`AgentRuntime` — the construction boundary that will (in later phases)
  build controller / sub-agent-runner / ``ReactContext`` products.

Scope note (Phase 2): this is a **skeleton only**. It establishes the intended
API and holds construction dependencies, but production call sites are NOT yet
routed through it. :meth:`AgentRuntime.create` is intentionally unimplemented.

Design invariant: ``RuntimeProfile`` (construction policy) is deliberately kept
separate from ``SubAgentRecord.source`` (operator visibility / capacity policy,
see ADR-0006). The mapping between them is explicit — never identity — because
``MAIN`` has no source and construction policy must not leak into visibility or
capacity semantics.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional

from react_loop import ReactContext
from sub_agent_registry import (
    SOURCE_DIAGNOSTIC,
    SOURCE_ON_DEMAND,
    SOURCE_PLAN_STEP,
    SOURCE_SCHEDULED,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent_controller import AgentController, SubAgentRunner


# ---------------------------------------------------------------------------
# Runtime profile — construction policy
# ---------------------------------------------------------------------------

class RuntimeProfile(Enum):
    """Construction policy for an agent execution.

    Each profile defines construction defaults (depth, prompt variant, memory
    layering, trace/cancel behavior, and model configuration). Profiles are
    independent from :data:`sub_agent_registry.VISIBLE_SOURCES`; see
    :func:`profile_to_source`.
    """

    MAIN = "main"
    ON_DEMAND_SUBAGENT = "on-demand-subagent"
    SCHEDULED_AGENT = "scheduled-agent"
    PLAN_STEP_AGENT = "plan-step-agent"
    DIAGNOSTIC_AGENT = "diagnostic-agent"


# Explicit profile -> registry source mapping.
#
# This is intentionally NOT identity: MAIN has no visibility source, and the
# sub-agent profile names differ from the source category strings so that
# construction policy stays decoupled from visibility/capacity policy.
_PROFILE_SOURCE: dict[RuntimeProfile, Optional[str]] = {
    RuntimeProfile.MAIN: None,
    RuntimeProfile.ON_DEMAND_SUBAGENT: SOURCE_ON_DEMAND,
    RuntimeProfile.SCHEDULED_AGENT: SOURCE_SCHEDULED,
    RuntimeProfile.PLAN_STEP_AGENT: SOURCE_PLAN_STEP,
    RuntimeProfile.DIAGNOSTIC_AGENT: SOURCE_DIAGNOSTIC,
}


def profile_to_source(profile: RuntimeProfile) -> Optional[str]:
    """Return the registry visibility source for a construction *profile*.

    ``MAIN`` returns ``None`` (main runs are not registered as sub-agents).
    Sub-agent profiles return their corresponding
    :data:`sub_agent_registry.VISIBLE_SOURCES` category. The mapping is explicit
    so profiles remain construction policy while sources remain visibility and
    capacity policy.
    """
    return _PROFILE_SOURCE[profile]


# ---------------------------------------------------------------------------
# Runtime options — per-execution construction knobs
# ---------------------------------------------------------------------------

@dataclass
class RuntimeOptions:
    """Per-execution construction knobs.

    These mirror the values currently threaded through ``sub_agent_factory`` and
    the controller/runner constructors. ``None`` defaults mean "inherit the
    profile / configured default"; in particular ``fallback_models`` preserves
    the existing trichotomy:

    - ``None``  — inherit configured fallback models.
    - ``[]``    — disable fallback inheritance.
    - ``[...]`` — use this explicit fallback chain.
    """

    model: Optional[str] = None
    fallback_models: Optional[list[str]] = None
    max_iterations: Optional[int] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    context_key: Optional[str] = None
    context_payload: Optional[dict] = None
    prompt_variant: Optional[str] = None
    trace_id: Optional[str] = None
    cancel_event: Optional[threading.Event] = None
    label: Optional[str] = None


# ---------------------------------------------------------------------------
# AgentRuntime — construction boundary (skeleton)
# ---------------------------------------------------------------------------

class AgentRuntime:
    """Construction boundary for agent executions (skeleton).

    Holds the shared, run-independent collaborators needed to build agent
    products (LLM configuration, tool system, memory layers, notification, and
    filesystem/limit defaults). In later phases :meth:`create` will build the
    controller / sub-agent-runner / ``ReactContext`` products currently produced
    by ``main.py``'s ``sub_agent_factory`` and the controller/runner
    constructors.

    Phase 2 scope: skeleton only. No production call site is routed through this
    class yet, and :meth:`create` is not implemented.
    """

    def __init__(
        self,
        *,
        config: Optional[dict] = None,
        all_models: Optional[list[dict]] = None,
        background_model_cfg: Optional[dict] = None,
        tool_index: object = None,
        executor: object = None,
        creator: object = None,
        base_memory: object = None,
        builtin_executor: object = None,
        skill_registry: object = None,
        mcp_manager: object = None,
        results: object = None,
        usage_registry: object = None,
        notify_fn: object = None,
        data_dir: str = "data",
        tmp_dir: str = "/tmp/agent",
        downloads_dir: str = "downloads",
        top_tools: int = 3,
        ctx_max_tokens: int = 90_000,
        scheduled_max_iterations: int = 100,
    ) -> None:
        # Shared model configuration.
        self._config = config
        self._all_models = all_models if all_models is not None else []
        self._background_model_cfg = background_model_cfg or {}
        # Shared tool + memory collaborators.
        self._tool_index = tool_index
        self._executor = executor
        self._creator = creator
        self._base_memory = base_memory
        self._builtin_executor = builtin_executor
        self._skill_registry = skill_registry
        self._mcp_manager = mcp_manager
        self._results = results
        self._usage_registry = usage_registry
        self._notify_fn = notify_fn
        # Filesystem + limit defaults.
        self._data_dir = data_dir
        self._tmp_dir = tmp_dir
        self._downloads_dir = downloads_dir
        self._top_tools = top_tools
        self._ctx_max_tokens = ctx_max_tokens
        self._scheduled_max_iterations = scheduled_max_iterations

    @staticmethod
    def source_for_profile(profile: RuntimeProfile) -> Optional[str]:
        """Return the registry visibility source for *profile*.

        Thin wrapper over :func:`profile_to_source` so callers can reach the
        mapping through the runtime object without importing the module-level
        helper. Kept separate from construction so visibility/capacity policy
        stays owned by the registry.
        """
        return profile_to_source(profile)

    @staticmethod
    def build_react_context(controller: "AgentController", trace_id: str) -> ReactContext:
        """Assemble the per-run ``ReactContext`` for a controller execution.

        This is the runtime-owned per-run context builder. It is deliberately
        separate from create-time construction: ``AgentController.run`` (and the
        sub-agent path via ``SubAgentRunner.run`` -> ``AgentController.run``) mints
        the per-run trace id, saves/restores the model ``_active_idx``, and sets
        the LLM trace id, then delegates the dataclass assembly here.

        The controller remains the state holder; the runtime owns the assembly so
        graph/strategy post-init wiring, cancel-event ownership, confirmation,
        callbacks (including a registry-installed ``_on_step``), context payload,
        and prompt variant are read from the controller at run start exactly as
        the legacy inline assembly did — preserving ``_on_step`` ordering because
        the value is read after registry helpers wire it.
        """
        ctx = ReactContext(
            llm=controller.llm,
            tool_index=controller.tool_index,
            executor=controller.executor,
            creator=controller.creator,
            memory=controller.memory,
            builtin_executor=controller.builtin_executor,
            mcp_manager=controller.mcp_manager,
            skill_registry=controller.skill_registry,
            max_iterations=controller.max_iterations,
            top_tools=controller.top_tools,
            ctx_max_tokens=controller.ctx_max_tokens,
            tmp_dir=controller.tmp_dir,
            downloads_dir=controller.downloads_dir,
            log_file=controller.log_file,
            log_backup_count=controller.log_backup_count,
            depth=controller._depth,
            label=controller.label,
            trace_id=trace_id,
            short_term=controller.short_term,
            working=controller.working,
            results=controller.results,
            cancel_event=controller._cancel_event,
            owns_cancel_event=controller._owns_cancel_event,
            on_step=controller._on_step,
            on_tool_trace=controller._on_tool_trace,
            job_history_fn=controller._job_history_fn,
            graph_memory=controller._graph_memory,
            graph_memory_writer=controller._graph_memory_writer,
            graph_memory_max_entries=controller._graph_memory_max_entries,
            strategy_memory=getattr(controller, "strategy_memory", None),
            max_subagents=getattr(controller.builtin_executor, "_max_subagents", 6),
            creativity_mode=getattr(controller, "creativity_mode", "default"),
            plan_max_iterations=getattr(controller, "plan_max_iterations", 50),
            inactivity_warn_minutes=getattr(controller, "inactivity_warn_minutes", 15),
            confirmation=controller._confirmation,
        )
        # Sub-agent context sharing: propagate the parent context payload and
        # prompt variant (set by SubAgentRunner) so react_loop can inject the
        # PARENT CONTEXT section and select the sub-agent prompt variant.
        ctx._context_payload = controller._context_payload
        ctx._prompt_variant = controller._prompt_variant
        return ctx

    def create(
        self,
        profile: RuntimeProfile,
        options: RuntimeOptions,
        *,
        notify_fn: object = None,
        on_tool_trace: object = None,
    ) -> "SubAgentRunner":
        """Build an agent execution product for *profile* using *options*.

        Sub-agent profiles (``ON_DEMAND_SUBAGENT``, ``SCHEDULED_AGENT``,
        ``PLAN_STEP_AGENT``, ``DIAGNOSTIC_AGENT``) build a ``SubAgentRunner``
        with the runner-shaped product surface consumed by ``SubAgentSupervisor``,
        ``PlanExecutor``, and the registry helpers. Construction is uniform across
        sub-agent profiles: they all run at depth 1 and differ only in the
        visibility source assigned later by ``register_run`` (see
        :func:`profile_to_source`), never in the built product itself.

        ``MAIN`` construction is intentionally deferred to a later phase; the main
        controller/client/memory objects are still built directly in ``main.py``.

        ``notify_fn`` and ``on_tool_trace`` are supervisor/collection wiring (not
        ``RuntimeOptions`` construction knobs) and are threaded through to the
        runner exactly as the legacy ``sub_agent_factory`` did.
        """
        if profile is RuntimeProfile.MAIN:
            raise NotImplementedError(
                "AgentRuntime.create(MAIN) is deferred; main construction remains "
                "in main.py for this change."
            )
        if profile not in _PROFILE_SOURCE:
            raise ValueError(f"Unknown runtime profile: {profile!r}")

        return self._create_sub_agent(
            options, notify_fn=notify_fn, on_tool_trace=on_tool_trace,
        )

    def _create_sub_agent(
        self,
        options: RuntimeOptions,
        *,
        notify_fn: object = None,
        on_tool_trace: object = None,
    ) -> "SubAgentRunner":
        """Build a ``SubAgentRunner`` mirroring the legacy ``sub_agent_factory``.

        Preserves model resolution, per-call LLM override shallow-copy, the
        ``fallback_models`` trichotomy, scheduled ``max_iterations`` default,
        context preloading, depth=1, and the runner-shaped product surface.
        """
        # Local imports avoid import cycles at module load (agent_controller and
        # builtin_executor both sit above this construction boundary).
        from agent_controller import SubAgentRunner
        from builtin_executor import _load_context
        from config_schema import resolve_model_id

        if self._config is None:
            raise ValueError(
                "AgentRuntime requires 'config' to build sub-agent products."
            )

        # Resolve model config — accept full model ID, model name, or alias.
        if options.model:
            resolved = resolve_model_id(options.model, self._all_models)
            model_cfg = next(
                (m for m in self._all_models if m.get("model") == resolved), None
            )
            if model_cfg is None:
                raise ValueError(
                    f"Model '{options.model}' not found in [[models]]. "
                    f"Available: {[m.get('model') for m in self._all_models]}"
                )
        else:
            model_cfg = self._background_model_cfg

        # Per-call LLM parameter overrides — always shallow-copy to protect the
        # shared config from mutation.
        llm_overrides: dict = {}
        if options.max_tokens is not None:
            llm_overrides["max_tokens"] = options.max_tokens
        if options.temperature is not None:
            llm_overrides["temperature"] = options.temperature
        if options.top_p is not None:
            llm_overrides["top_p"] = options.top_p
        model_cfg = {**model_cfg, **llm_overrides}

        # max_iterations: explicit override > scheduled default.
        effective_max_iter = (
            options.max_iterations
            if options.max_iterations is not None
            else self._scheduled_max_iterations
        )

        pre_loaded_ctx = None
        if options.context_key:
            pre_loaded_ctx = _load_context(
                options.context_key, self._data_dir, max_turns=50,
            )

        runner = SubAgentRunner(
            model_cfg=model_cfg,
            config=self._config,
            tool_index=self._tool_index,
            executor=self._executor,
            creator=self._creator,
            base_memory=self._base_memory,
            builtin_executor=self._builtin_executor,
            skill_registry=self._skill_registry,
            mcp_manager=self._mcp_manager,
            results=self._results,
            short_term=pre_loaded_ctx,
            notify_fn=notify_fn or self._notify_fn,
            context_key=options.context_key,
            label=options.label if options.label is not None else "on-demand",
            max_iterations=effective_max_iter,
            top_tools=self._top_tools,
            ctx_max_tokens=self._ctx_max_tokens,
            tmp_dir=self._tmp_dir,
            downloads_dir=self._downloads_dir,
            usage_registry=self._usage_registry,
            depth=1,
            fallback_models=options.fallback_models,
            on_tool_trace=on_tool_trace,
            cancel_event=options.cancel_event,
            trace_id=options.trace_id,
            context_payload=options.context_payload,
            prompt_variant=options.prompt_variant,
        )
        return runner
