"""
agent_runtime.py
----------------
Construction boundary for agent executions (ADR-0007).

This module introduces the vocabulary and construction boundary for agent
executions (ADR-0007):

- :class:`RuntimeProfile` — *construction policy* for a sub-agent execution
  kind. ``MAIN`` construction is intentionally out of scope; the main agent is
  wired directly in ``main.py``.
- :class:`RuntimeOptions` — per-execution construction knobs currently
  duplicated across ``main.py``'s ``sub_agent_factory``, ``AgentController``,
  ``SubAgentRunner``, ``BuiltinExecutor.spawn_agent``, and ``PlanExecutor``.
- :class:`AgentRuntime` — the construction boundary for sub-agent products. It
  builds :class:`SubAgentRunner` instances for the four supported profiles and
  provides the per-run :class:`ReactContext` assembler used by
  :class:`AgentController`.

Design invariant: ``RuntimeProfile`` (construction policy) is deliberately kept
separate from ``SubAgentRecord.source`` (operator visibility / capacity policy,
see ADR-0006). The mapping between them is explicit — never identity — so
construction policy cannot leak into visibility or capacity semantics.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional

from confirmation import ConfirmationManager
from context_monitor import ContextMonitor
from config_schema import AgentConfig, ExecutorPaths
from embedding_client import EmbeddingClient
from model_registry import CatalogView, RunModel
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

@dataclass
class ControllerDeps:
    """Runtime dependencies shared between ``AgentController`` and ``ReactContext``.

    A small, typed transfer object. ``AgentController`` constructs one on init
    from its constructor parameters; ``AgentRuntime.build_react_context`` copies
    it into a ``ReactContext`` wholesale.  Adding a new runtime dependency now
    only requires updates in two places: this dataclass and ``ReactContext``.
    """

    llm: object
    tool_index: object
    memory: object
    builtin_executor: object
    mcp_manager: object
    skill_registry: object
    max_iterations: int = 8
    top_tools: int = 3
    ctx_max_tokens: int = 90_000
    tmp_dir: str = "/tmp/agent"
    downloads_dir: str = "downloads"
    workspace_dir: str = "~/Documents"
    log_file: str = "agent.log"
    log_backup_count: int = 30
    depth: int = 0
    label: str = "main"
    short_term: object = None
    working: object = None
    results: object = None
    cancel_event: Optional[threading.Event] = None
    owns_cancel_event: bool = True
    on_step: Optional[Callable[[int], None]] = None
    on_tool_trace: Optional[Callable] = None
    graph_memory: Optional[object] = None
    graph_memory_writer: Optional[object] = None
    graph_memory_max_entries: int = 10
    strategy_memory: Optional[object] = None
    max_subagents: int = 6
    creativity_mode: str = "default"
    plan_max_iterations: int = 50
    inactivity_warn_minutes: int = 15
    confirmation: ConfirmationManager = field(default_factory=ConfirmationManager)
    checkpoint_store: Optional[object] = None
    checkpoint_enabled: bool = True
    retry_timeout_seconds: int = 120
    max_retries: int = 4
    retry_delay: float = 2.0
    context_monitor: ContextMonitor | None = None
    step_notify_interval: int = 0
    # Presentation-only catalog view; never the authoritative run pin.
    model_catalog_view: CatalogView | None = None


class RuntimeProfile(Enum):
    """Construction policy for a sub-agent execution.

    ``MAIN`` is intentionally absent: the main agent is wired directly in
    ``main.py`` and is not built through this boundary. Each sub-agent profile
    defines construction defaults (depth, prompt variant, memory layering,
    trace/cancel behavior, and model configuration). Profiles are independent
    from :data:`sub_agent_registry.VISIBLE_SOURCES`; see :func:`profile_to_source`.
    """

    ON_DEMAND_SUBAGENT = "on-demand-subagent"
    SCHEDULED_AGENT = "scheduled-agent"
    PLAN_STEP_AGENT = "plan-step-agent"
    DIAGNOSTIC_AGENT = "diagnostic-agent"


# Explicit profile -> registry source mapping.
#
# This is intentionally NOT identity: the profile names differ from the source
# category strings so construction policy stays decoupled from visibility/capacity
# policy. There is no MAIN profile; main runs are not registered as sub-agents.
_PROFILE_SOURCE: dict[RuntimeProfile, Optional[str]] = {
    RuntimeProfile.ON_DEMAND_SUBAGENT: SOURCE_ON_DEMAND,
    RuntimeProfile.SCHEDULED_AGENT: SOURCE_SCHEDULED,
    RuntimeProfile.PLAN_STEP_AGENT: SOURCE_PLAN_STEP,
    RuntimeProfile.DIAGNOSTIC_AGENT: SOURCE_DIAGNOSTIC,
}


class _ScheduledRunMemory:
    """Private, non-persistent event memory for one scheduled run.

    Satisfies the ReAct loop's ``memory`` collaborator surface
    (``record_event`` / ``as_prompt_text``) without exposing or writing the
    operator's ``MemoryStore``. Lifetime is one runner, i.e. one scheduled run.
    """

    def record_event(self, event: str) -> None:
        """Discard *event*: scheduled runs never write the operator event log."""
        del event

    def as_prompt_text(self) -> str:
        """Scheduled runs expose no persistent memory entries to the prompt."""
        return "No persistent memory entries."


def profile_to_source(profile: RuntimeProfile) -> Optional[str]:
    """Return the registry visibility source for a construction *profile*.

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
    profile / configured default".
    """

    run_model: Optional[RunModel] = None
    max_iterations: Optional[int] = None
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
    """Construction boundary for sub-agent executions.

    Holds the shared, run-independent collaborators needed to build sub-agent
    products (LLM configuration, tool system, memory layers, notification, and
    filesystem/limit defaults). :meth:`create` builds :class:`SubAgentRunner`
    products for the four supported profiles; :meth:`build_react_context`
    assembles the per-run :class:`ReactContext` used by :class:`AgentController`.

    ``MAIN`` construction is intentionally out of scope; the main agent is wired
    directly in ``main.py``.
    """

    def __init__(
        self,
        *,
        agent_cfg: AgentConfig | None = None,
        embedding_factory: Callable[[threading.Event], EmbeddingClient] | None = None,
        paths: ExecutorPaths | None = None,
        tool_index: object = None,
        base_memory: object = None,
        builtin_executor: object = None,
        skill_registry: object = None,
        mcp_manager: object = None,
        results: object = None,
        usage_registry: object = None,
        notify_fn: object = None,
        data_dir: str = "data",
        scheduled_max_iterations: int = 100,
    ) -> None:
        # Typed agent config, embedding dependency + runtime path bundle;
        # validated by create() before any allocation.
        self._agent_cfg = agent_cfg
        self._embedding_factory = embedding_factory
        self._paths = paths or ExecutorPaths()
        # Shared tool + memory collaborators.
        self._tool_index = tool_index
        self._base_memory = base_memory
        self._builtin_executor = builtin_executor
        self._skill_registry = skill_registry
        self._mcp_manager = mcp_manager
        self._results = results
        self._usage_registry = usage_registry
        self._notify_fn = notify_fn
        # Filesystem + limit defaults.
        self._data_dir = data_dir
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
    def build_react_context(
        controller: "AgentController",
        trace_id: str,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> ReactContext:
        """Assemble the per-run ``ReactContext`` for a controller execution.

        This is the runtime-owned per-run context builder. It is deliberately
        separate from create-time construction: ``AgentController.run`` (and the
        sub-agent path via ``SubAgentRunner.run`` -> ``AgentController.run``) mints
        the per-run trace id and sets the LLM trace id, then delegates the
        dataclass assembly here. The LLM client remains pinned for the run.

        The controller exposes its runtime dependencies through
        ``controller.runtime_deps`` (a :class:`ControllerDeps` dataclass).  The
        runtime copies every shared field into ``ReactContext`` automatically, so
        adding a dependency only requires changing the two dataclass definitions.
        Per-run additions (trace id, parent context payload/prompt variant) are
        still layered on top here.

        ``cancel_event``, when given, overrides ``deps.cancel_event`` for this one
        run. ``AgentController.run`` passes a freshly minted, run-private event
        here for controllers that own their cancellation (see
        ``AgentController._cancel_registry``): each concurrent run then has its
        own event, so no run's start-of-loop ``clear()`` can race with a sibling
        run sharing the same object, and ``cancel()`` fans out to every
        currently-registered run instead of relying on one shared flag. Callers
        that omit it (direct ``build_react_context`` calls, tests, the
        externally-supplied-event path used by sub-agents) get
        ``deps.cancel_event`` unchanged, exactly as before.

        ``_on_step`` ordering is preserved because ``ControllerDeps`` is read from
        the controller at run start, after registry helpers have wired callbacks.
        """
        deps: ControllerDeps = controller.runtime_deps
        shared = {f.name: getattr(deps, f.name) for f in fields(ControllerDeps)}
        if cancel_event is not None:
            shared["cancel_event"] = cancel_event

        ctx = ReactContext(trace_id=trace_id, **shared)

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
        """Build a ``SubAgentRunner`` for *profile* using *options*.

        All supported profiles (``ON_DEMAND_SUBAGENT``, ``SCHEDULED_AGENT``,
        ``PLAN_STEP_AGENT``, ``DIAGNOSTIC_AGENT``) build a ``SubAgentRunner``
        with the runner-shaped product surface consumed by ``SubAgentSupervisor``,
        ``PlanExecutor``, and the registry helpers. All sub-agent profiles run at
        depth 1; the visibility source is assigned later by ``register_run``
        (see :func:`profile_to_source`). ``SCHEDULED_AGENT`` differs only in its
        memory wiring: it receives a private per-run memory and no operator
        ``ResultsMemory`` so a scheduled run sees job-owned context only; the
        other profiles share the operator memory and result recall.

        ``MAIN`` is not a supported profile; the main agent is wired directly in
        ``main.py``. Passing an unknown profile raises ``ValueError``.

        ``notify_fn`` and ``on_tool_trace`` are supervisor/collection wiring (not
        ``RuntimeOptions`` construction knobs) and are threaded through to the
        runner exactly as the legacy ``sub_agent_factory`` did.
        """
        if profile not in _PROFILE_SOURCE:
            raise ValueError(f"Unknown runtime profile: {profile!r}")
        if not isinstance(options.run_model, RunModel):
            raise ValueError(
                "RuntimeOptions.run_model must be an admitted RunModel snapshot."
            )
        if not isinstance(self._agent_cfg, AgentConfig):
            raise ValueError("AgentRuntime requires typed AgentConfig.")
        if not callable(self._embedding_factory):
            raise ValueError("AgentRuntime requires a callable embedding_factory.")

        if profile is RuntimeProfile.SCHEDULED_AGENT:
            # Job-owned context only (isolate-job-execution, design decision 2):
            # a private per-run memory and no operator result recall. Working
            # memory, graph memory, and job history stay runner-local/None.
            # Tool dispatch carries the immutable scheduled policy explicitly.
            from builtin_executor import ScheduledToolDispatch

            scheduled_tools = (
                ScheduledToolDispatch(self._builtin_executor)
                if self._builtin_executor is not None else None
            )
            return self._create_sub_agent(
                options, notify_fn=notify_fn, on_tool_trace=on_tool_trace,
                base_memory=_ScheduledRunMemory(), results=None,
                builtin_executor=scheduled_tools,
            )
        return self._create_sub_agent(
            options, notify_fn=notify_fn, on_tool_trace=on_tool_trace,
            base_memory=self._base_memory, results=self._results,
            builtin_executor=self._builtin_executor,
        )

    def _create_sub_agent(
        self,
        options: RuntimeOptions,
        *,
        notify_fn: object = None,
        on_tool_trace: object = None,
        base_memory: object = None,
        results: object = None,
        builtin_executor: object = None,
    ) -> "SubAgentRunner":
        """Build a ``SubAgentRunner`` mirroring the legacy ``sub_agent_factory``.

        The caller supplies an already admitted ``options.run_model`` (validated
        by :meth:`create`); no model resolution happens here. Preserves the
        scheduled ``max_iterations`` default, context preloading, depth=1, and
        the runner-shaped product surface. ``base_memory``, ``results``, and
        ``builtin_executor`` are the profile-selected collaborators.
        """
        # Local imports avoid import cycles at module load (agent_controller and
        # builtin_executor both sit above this construction boundary).
        from agent_controller import SubAgentRunner
        from builtin_tools.context_io import _load_context

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
            run_model=options.run_model,
            agent_cfg=replace(
                self._agent_cfg,
                max_iterations=effective_max_iter,
            ),
            embedding_factory=self._embedding_factory,
            paths=self._paths,
            tool_index=self._tool_index,
            base_memory=base_memory,
            builtin_executor=builtin_executor,
            skill_registry=self._skill_registry,
            mcp_manager=self._mcp_manager,
            results=results,
            short_term=pre_loaded_ctx,
            notify_fn=notify_fn or self._notify_fn,
            context_key=options.context_key,
            label=options.label if options.label is not None else "on-demand",
            usage_registry=self._usage_registry,
            depth=1,
            on_tool_trace=on_tool_trace,
            cancel_event=options.cancel_event,
            trace_id=options.trace_id,
            context_payload=options.context_payload,
            prompt_variant=options.prompt_variant,
        )
        return runner
