"""Acceptance tests for presenting the sanitized catalog view in prompts (task 8.3, phase 1).

One real ``ModelRegistry`` view feeds every presentation path: the pure renderer
``prompt_builder.format_model_catalog_section``, ``format_models``, the direct,
Jinja, legacy-fallback and sub-agent prompts, and the native/text descriptions
of the built-in ``spawn_agent`` and ``schedule`` tools.

Renderer output contract exercised here: every model entry is a single line
containing its canonical id; integers render with optional thousands
separators; provider and model state use the words ``fresh``/``stale``,
``pinned``, ``active`` and ``supported``/``unsupported``/``unknown`` (no
per-field source labels: provider-reported values are advisory display data); hidden inventory is reported on a line that
contains the word ``omitted`` and the omitted count.

Only the external boundaries are faked: provider discovery and credential
resolution.

Phase 2 (runtime seam): the same view travels through ``ControllerDeps``,
``AgentController`` and ``ReactContext``; ``_assemble_system_prompt`` and
``_ensure_tool_defs`` consume the exact provided object, a replaced view
invalidates cached definitions and their token grouping, and a guarded first
request previews and dispatches one captured bundle.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

import prompt_builder
import prompt_loader
from agent_controller import AgentController
from agent_runtime import AgentRuntime, ControllerDeps
from builtin_tools.descriptors import BUILTIN_TOOLS
from builtin_tools.schemas import (
    BUILTIN_TOOL_SCHEMAS,
    PSEUDO_TOOL_SCHEMAS,
    build_tool_definitions,
    builtin_tool_names,
)
from config_schema import AgentConfig, ExecutorPaths, ProviderConfig, ResolvedProviderConnection
from context_monitor import ContextMonitor, group_tool_defs_by_server
from interfaces import ChatResponse
from model_discovery import ProviderInventory
from model_registry import (
    CatalogDocument,
    CatalogView,
    ModelMetadata,
    ModelRecord,
    ModelRegistry,
    ProviderState,
    RunModel,
)
from model_settings import ModelSettings
from react_loop import ReactContext, _assemble_system_prompt, _ensure_tool_defs, react_loop
from tool_registry import Tool

from tests.execution_harness import NativeScriptedLLM, RecordingExecutor, ScriptedLLM, build_context

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

ALPHA_REF = "env:UNIQUE_ALPHA_REF_7c1d9e"
ALPHA_URL = "https://alpha-endpoint-7c1d9e.invalid/v1"
BRAVO_REF = "env:UNIQUE_BRAVO_REF_7c1d9e"
BRAVO_URL = "https://bravo-endpoint-7c1d9e.invalid/v1"
RESOLVED_SECRET = "resolved-credential-7c1d9e-value"
DISCOVERY_ERROR_TEXT = "upstream-said-private-detail-7c1d9e"
RAW_VALUES = (ALPHA_REF, ALPHA_URL, BRAVO_REF, BRAVO_URL, RESOLVED_SECRET, DISCOVERY_ERROR_TEXT)

PROMPT_KINDS = ("direct", "jinja", "legacy", "sub-agent")


# ---------------------------------------------------------------------------
# Boundary fakes and real-registry fixtures
# ---------------------------------------------------------------------------

class _Discovery:
    """Fake external provider discovery boundary."""

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}

    def fetch(self, connection: ResolvedProviderConnection) -> ProviderInventory:
        result = self.responses[connection.provider_id]
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


def _resolve(config: ProviderConfig) -> ResolvedProviderConnection:
    return ResolvedProviderConnection(
        config.name, config.type, config.base_url, config.request_timeout, RESOLVED_SECRET
    )


def _model(
    upstream: str,
    provider: str = "alpha",
    *,
    context: int | None = None,
    input_tokens: int | None = None,
    output: int | None = None,
    vision: bool | None = None,
    tools: bool | None = None,
    reasoning: bool | None = None,
    display: str | None = None,
) -> ModelRecord:
    metadata = ModelMetadata(context, input_tokens, output, vision, tools, reasoning)
    return ModelRecord(f"{provider}/{upstream}", provider, upstream, display or upstream, metadata)


def _inventory(provider: str, adapter: str, models: tuple[ModelRecord, ...]) -> ProviderInventory:
    return ProviderInventory(provider, adapter, tuple(models), "2026-10-01T12:00:00Z")


@dataclass(frozen=True)
class _World:
    view: CatalogView
    pinned: RunModel
    removed: ModelRecord
    active: ModelRecord
    stale_provider_ids: tuple[str, ...]
    connections: dict[str, ProviderConfig]
    registry: ModelRegistry


def _build_world(
    tmp_path: Path,
    alpha_models: tuple[ModelRecord, ...],
    bravo_models: tuple[ModelRecord, ...] = (),
    *,
    fail_bravo: bool = False,
) -> _World:
    """Pin a model through real admission, select another, then refresh the pin away."""
    removed = _model("retired-run", context=4096)
    connections = {
        "alpha": ProviderConfig("alpha", "openai", ALPHA_REF, ALPHA_URL),
        "bravo": ProviderConfig("bravo", "openrouter", BRAVO_REF, BRAVO_URL),
    }
    states = tuple(
        ProviderState(name, cfg.type, "2026-09-01T00:00:00Z", "fresh") for name, cfg in connections.items()
    )
    seeded = tuple(sorted((removed, *alpha_models, *bravo_models), key=lambda item: item.id))
    settings = ModelSettings(tmp_path / "models-config.toml")
    settings.write(CatalogDocument(1, 2, None, states, seeded))
    discovery = _Discovery()
    registry = ModelRegistry(connections, settings, discovery, _resolve)

    registry.select(removed.id)
    pinned = registry.admit()
    active = alpha_models[-1]
    registry.select(active.id)

    discovery.responses["alpha"] = _inventory("alpha", "openai", alpha_models)
    discovery.responses["bravo"] = (
        RuntimeError(DISCOVERY_ERROR_TEXT) if fail_bravo else _inventory("bravo", "openrouter", bravo_models)
    )
    report = registry.refresh()

    world = _World(
        registry.view(pinned), pinned, removed, active, report.stale_provider_ids, connections, registry
    )
    _assert_world_setup(world, fail_bravo=fail_bravo)
    return world


def _assert_world_setup(world: _World, *, fail_bravo: bool) -> None:
    """Fail loudly on bad fixture state so a missing API cannot mask it."""
    view = world.view
    assert world.pinned.model == world.removed
    assert view.pinned_model == world.removed
    assert world.removed.id not in {item.id for item in view.models}
    assert view.active_model_id == world.active.id
    assert view.selection_status == "active"
    assert world.active.id in {item.id for item in view.models}
    statuses = {state.provider_id: state.status for state in view.providers}
    assert statuses == {"alpha": "fresh", "bravo": "stale" if fail_bravo else "fresh"}
    assert world.stale_provider_ids == (("bravo",) if fail_bravo else ())
    # Raw connection values exist outside the view, so absence checks are not vacuous.
    assert world.connections["alpha"].api_key == ALPHA_REF
    assert world.connections["alpha"].base_url == ALPHA_URL
    assert world.connections["bravo"].api_key == BRAVO_REF
    assert world.pinned.connection.api_key == RESOLVED_SECRET
    assert world.pinned.connection.base_url == ALPHA_URL
    for raw in RAW_VALUES:
        assert raw not in repr(view)


_RICH = _model(
    "rich",
    context=123456,
    output=7777,
    vision=True,
    tools=False,
    display="Rich Model",
)
_BLANK = _model("blank")
_LEGACY = _model("legacy", "bravo", context=32768, tools=True)


@pytest.fixture
def meta_world(tmp_path: Path) -> _World:
    """Mixed provenance and tri-state capabilities; provider bravo is stale after a failed refresh."""
    return _build_world(tmp_path, (_RICH, _BLANK), (_LEGACY,), fail_bravo=True)


@pytest.fixture
def large_world(tmp_path: Path) -> _World:
    """Twenty-five alpha inventory models; the active one sorts last."""
    models = tuple(_model(f"m{index:02d}", context=1000 + index) for index in range(25))
    return _build_world(tmp_path, models)


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

def _render(view: CatalogView | None, **kwargs: object) -> str:
    return prompt_builder.format_model_catalog_section(view, **kwargs)


def _joined(section: str, model_id: str) -> str:
    lines = [line for line in section.splitlines() if model_id in line]
    assert lines, f"{model_id} missing from section:\n{section}"
    return "\n".join(lines)


def _omitted_numbers(section: str) -> set[int]:
    numbers: set[int] = set()
    for line in section.splitlines():
        if "omitted" in line.lower():
            numbers.update(int(match) for match in re.findall(r"\d+", line))
    return numbers


class _ToolIndex:
    def search(self, _goal: str, top_k: int = 3) -> list[object]:
        return []


class _Memory:
    def as_prompt_text(self) -> str:
        return "No persistent memory entries."


class _LLM:
    _models: list = []
    llm_cfg: dict = {}


def _prompt(kind: str, **extra: object) -> str:
    common = dict(
        tool_index=_ToolIndex(),
        memory=_Memory(),
        results=None,
        skill_registry=None,
        llm=_LLM(),
        tmp_dir="/tmp/agent",
        downloads_dir="downloads",
        log_file="agent.log",
        log_backup_count=30,
        top_tools=3,
        user_goal="goal",
    )
    if kind == "direct":
        return prompt_builder.build_system_prompt(**common, **extra)[0]
    if kind == "jinja":
        return prompt_loader.build_system_prompt(**common, prompts_dir=str(_PROMPTS_DIR), **extra)[0]
    if kind == "legacy":
        return prompt_loader.build_system_prompt(
            **common, prompts_dir=str(_PROMPTS_DIR / "does-not-exist"), **extra
        )[0]
    assert kind == "sub-agent"
    return prompt_loader.build_system_prompt(
        **common, prompts_dir=str(_PROMPTS_DIR), mode="sub-agent", **extra
    )[0]


def _mcp_model_tool() -> Tool:
    return Tool(name="model", language="mcp", description="MCP model tool", is_mcp=True, server_name="srv")


def _native(view: CatalogView | None) -> dict[str, dict]:
    mcp = SimpleNamespace(
        get_tools=lambda: [
            SimpleNamespace(
                name="model",
                description="MCP model tool",
                input_schema={"type": "object", "properties": {}},
            )
        ]
    )
    definitions = build_tool_definitions(mcp, model_catalog_view=view)
    return {item["function"]["name"]: item["function"] for item in definitions}


def _consumer_text(kind: str, view: CatalogView) -> str:
    if kind == "renderer":
        return _render(view)
    if kind == "format_models":
        return prompt_builder.format_models(_LLM(), model_catalog_view=view)
    if kind in PROMPT_KINDS:
        return _prompt(kind, model_catalog_view=view)
    if kind == "native":
        return json.dumps(_native(view))
    assert kind == "text"
    tools = [BUILTIN_TOOLS["spawn_agent"], BUILTIN_TOOLS["schedule"], _mcp_model_tool()]
    return prompt_builder.format_tools(tools, model_catalog_view=view)


ALL_KINDS = ("renderer", "format_models", *PROMPT_KINDS, "native", "text")


# ---------------------------------------------------------------------------
# Renderer: pin, active selection, callability
# ---------------------------------------------------------------------------

def test_removed_pin_never_shares_a_line_with_the_active_model(meta_world: _World) -> None:
    section = _render(meta_world.view)

    pin_text = _joined(section, meta_world.removed.id)
    active_text = _joined(section, meta_world.active.id)

    assert "pinned" in pin_text.lower()
    assert "active" in active_text.lower()
    assert not any(
        meta_world.removed.id in line and meta_world.active.id in line for line in section.splitlines()
    )


def test_removed_pin_is_flagged_as_missing_from_current_inventory(meta_world: _World) -> None:
    pin_text = _joined(_render(meta_world.view), meta_world.removed.id).lower()

    assert re.search(r"no longer|not in|not present|removed|absent|missing", pin_text)


def test_section_disclaims_any_promise_that_a_listed_model_is_callable(meta_world: _World) -> None:
    section = _render(meta_world.view).lower()

    assert re.search(r"guarantee|not imply|does not mean|no promise|not a promise", section)


# ---------------------------------------------------------------------------
# Renderer: limits, capabilities, provenance, freshness
# ---------------------------------------------------------------------------

def test_known_limits_render_exact_values(meta_world: _World) -> None:
    text = _joined(_render(meta_world.view), _RICH.id)

    assert re.search(r"123[,_]?456", text)
    assert re.search(r"7[,_]?777", text)


def test_capability_true_is_supported_false_is_unsupported_and_none_is_unknown(meta_world: _World) -> None:
    text = _joined(_render(meta_world.view), _RICH.id).lower()

    assert re.search(r"(?<!un)supported", text)
    assert "unsupported" in text
    assert "unknown" in text


def test_unreported_fields_render_unknown_and_never_unsupported(meta_world: _World) -> None:
    text = _joined(_render(meta_world.view), _BLANK.id).lower()

    assert "unknown" in text
    assert "unsupported" not in text
    assert not re.search(r"(?<!un)supported", text)


def test_rendered_metadata_carries_no_provenance_labels(meta_world: _World) -> None:
    """Provider-reported values are display facts: no per-field source label is rendered."""
    section = _render(meta_world.view)
    for model_id in (_RICH.id, _BLANK.id, _LEGACY.id):
        facts = _joined(section, model_id).lower()
        assert "models.dev" not in facts and "[provider]" not in facts


def test_provider_freshness_lists_stale_and_fresh_providers(meta_world: _World) -> None:
    lines = [line.lower() for line in _render(meta_world.view).splitlines()]

    assert any("bravo" in line and "stale" in line and "alpha/" not in line for line in lines)
    assert any("alpha" in line and "fresh" in line and "alpha/" not in line for line in lines)


def test_section_invents_no_provider_budget_wording(meta_world: _World) -> None:
    section = _render(meta_world.view).lower()

    assert not re.search(r"budget|quota|rate limit|remaining", section)


# ---------------------------------------------------------------------------
# Renderer: inventory cap
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cap", [20, 5])
def test_inventory_cap_never_hides_pin_or_active_state(large_world: _World, cap: int) -> None:
    section = _render(large_world.view, max_models=cap)

    assert "pinned" in _joined(section, large_world.removed.id).lower()
    assert "active" in _joined(section, large_world.active.id).lower()


@pytest.mark.parametrize("cap", [20, 5])
def test_omitted_count_matches_hidden_inventory_models(large_world: _World, cap: int) -> None:
    assert len(large_world.view.models) == 25
    section = _render(large_world.view, max_models=cap)

    shown = len(set(re.findall(r"alpha/m\d\d", section)))

    assert 0 < shown < 25
    assert 25 - shown in _omitted_numbers(section)


# ---------------------------------------------------------------------------
# Prompt consumers share the same section once
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ("format_models", *PROMPT_KINDS))
def test_consumer_contains_the_shared_section_exactly_once(large_world: _World, kind: str) -> None:
    section = _render(large_world.view).strip()

    text = _consumer_text(kind, large_world.view)

    assert text.count(section) == 1


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ALL_KINDS)
def test_no_connection_value_or_provider_error_text_reaches_any_consumer(meta_world: _World, kind: str) -> None:
    text = _consumer_text(kind, meta_world.view)

    assert meta_world.removed.id in text
    for raw in RAW_VALUES:
        assert raw not in text


@pytest.mark.parametrize("kind", ("renderer", "jinja", "native", "text"))
def test_consumers_never_dump_catalog_dataclass_reprs(meta_world: _World, kind: str) -> None:
    text = _consumer_text(kind, meta_world.view)

    assert meta_world.removed.id in text
    for marker in ("CatalogView(", "ModelRecord(", "ModelMetadata(", "ProviderState(", "RunModel("):
        assert marker not in text


# ---------------------------------------------------------------------------
# Native tool definitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["spawn_agent", "schedule"])
def test_native_description_carries_pin_and_active_identity(meta_world: _World, name: str) -> None:
    description = _native(meta_world.view)[name]["description"]

    assert BUILTIN_TOOL_SCHEMAS[name]["description"] in description
    assert meta_world.removed.id in description
    assert meta_world.active.id in description


def test_native_compact_summary_keeps_pin_limits_and_states(meta_world: _World) -> None:
    description = _native(meta_world.view)["spawn_agent"]["description"].lower()

    assert "pinned" in description
    assert "active" in description
    assert re.search(r"4[,_]?096", description)
    assert "unknown" in description
    assert "fresh" in description


def test_native_build_leaves_static_schemas_and_descriptors_untouched(meta_world: _World) -> None:
    schema_snapshot = copy.deepcopy(BUILTIN_TOOL_SCHEMAS)
    pseudo_snapshot = copy.deepcopy(PSEUDO_TOOL_SCHEMAS)
    descriptor_snapshot = {name: tool.description for name, tool in BUILTIN_TOOLS.items()}

    definitions = _native(meta_world.view)

    assert BUILTIN_TOOL_SCHEMAS == schema_snapshot
    assert PSEUDO_TOOL_SCHEMAS == pseudo_snapshot
    assert {name: tool.description for name, tool in BUILTIN_TOOLS.items()} == descriptor_snapshot
    assert definitions["spawn_agent"]["parameters"] == schema_snapshot["spawn_agent"]["parameters"]
    assert definitions["schedule"]["parameters"] == schema_snapshot["schedule"]["parameters"]
    assert definitions["shell"] == {"name": "shell", **schema_snapshot["shell"]}


def test_native_mcp_tool_named_model_is_not_decorated(meta_world: _World) -> None:
    definition = _native(meta_world.view)["model"]

    assert definition["description"] == "MCP model tool"
    assert definition["parameters"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# Text/JSON tool descriptions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["spawn_agent", "schedule"])
def test_text_builtin_descriptor_is_decorated_with_pin_and_active_identity(
    meta_world: _World, name: str
) -> None:
    text = prompt_builder.format_tools([BUILTIN_TOOLS[name]], model_catalog_view=meta_world.view)

    assert BUILTIN_TOOLS[name].description in text
    assert meta_world.removed.id in text
    assert meta_world.active.id in text


def test_text_formatting_does_not_mutate_descriptor_instances(meta_world: _World) -> None:
    snapshot = {name: tool.description for name, tool in BUILTIN_TOOLS.items()}

    prompt_builder.format_tools(
        [BUILTIN_TOOLS["spawn_agent"], BUILTIN_TOOLS["schedule"]], model_catalog_view=meta_world.view
    )

    assert {name: tool.description for name, tool in BUILTIN_TOOLS.items()} == snapshot


def test_text_mcp_tool_named_model_is_not_decorated(meta_world: _World) -> None:
    text = prompt_builder.format_tools([_mcp_model_tool()], model_catalog_view=meta_world.view)

    assert text == "  model: MCP model tool"


# ---------------------------------------------------------------------------
# Absent view preserves legacy behaviour
# ---------------------------------------------------------------------------

def test_absent_view_keeps_legacy_models_section_exact() -> None:
    llm = SimpleNamespace(
        _models=[{"name": "Fast", "model": "gpt-x"}, {"name": "", "model": "gpt-y"}],
        llm_cfg={"model": "gpt-x"},
    )

    assert prompt_builder.format_models(llm) == (
        "AVAILABLE MODELS (use the 'model' parameter in spawn_agent to select):\n"
        "  gpt-x (Fast) \u2190 active\n"
        "  gpt-y\n\n"
    )


def test_absent_view_keeps_legacy_text_tools_exact() -> None:
    tools = [SimpleNamespace(name="t", description="d")]

    assert prompt_builder.format_tools(tools) == "  t: d"
    assert prompt_builder.format_tools([]) == "No additional tools registered."


def test_absent_view_keeps_default_native_definitions_exact() -> None:
    expected = [
        {"type": "function", "function": {"name": name, **schema}}
        for name, schema in {**BUILTIN_TOOL_SCHEMAS, **PSEUDO_TOOL_SCHEMAS}.items()
    ]

    assert build_tool_definitions() == expected


def test_renderer_returns_empty_text_for_absent_view() -> None:
    assert _render(None) == ""


def _models_llm() -> SimpleNamespace:
    return SimpleNamespace(_models=[{"name": "Fast", "model": "gpt-x"}], llm_cfg={"model": "gpt-x"})


@pytest.mark.parametrize("kind", ["format_models", "format_tools", "native", *PROMPT_KINDS])
def test_explicit_none_view_equals_omitting_the_argument(kind: str) -> None:
    if kind == "format_models":
        llm = _models_llm()
        assert prompt_builder.format_models(llm, model_catalog_view=None) == prompt_builder.format_models(llm)
    elif kind == "format_tools":
        tools = [BUILTIN_TOOLS["spawn_agent"], BUILTIN_TOOLS["schedule"]]
        assert prompt_builder.format_tools(tools, model_catalog_view=None) == prompt_builder.format_tools(tools)
    elif kind == "native":
        assert build_tool_definitions(model_catalog_view=None) == build_tool_definitions()
    else:
        assert _prompt(kind, model_catalog_view=None) == _prompt(kind)


# ---------------------------------------------------------------------------
# Phase 2 helpers: runtime seam
# ---------------------------------------------------------------------------

_FINISH = '{"action": "finish", "result": "done"}'


def _other_pin_view(world: _World) -> CatalogView:
    """View of the same catalog revision pinned to the admitted active model instead."""
    other_pin = world.registry.admit()
    view = world.registry.view(other_pin)
    assert other_pin.model == world.active
    assert view.pinned_model == world.active
    assert view.revision == world.view.revision
    assert view.selection_revision == world.view.selection_revision
    assert view != world.view
    return view


def _loop_ctx(llm: object, view: CatalogView | None, **overrides: object) -> ReactContext:
    """Harness ReactContext carrying *view* through the declared field."""
    return build_context(llm, RecordingExecutor(), model_catalog_view=view, **overrides)


def _spawn_description(tool_defs: list[dict] | None) -> str:
    by_name = {item["function"]["name"]: item["function"] for item in tool_defs or []}
    return by_name["spawn_agent"]["description"]


def _expected_tool_def_tokens(view: CatalogView) -> int:
    """Token total the loop's grouping yields for fresh definitions of *view*."""
    definitions = build_tool_definitions(None, model_catalog_view=view)
    return sum(group_tool_defs_by_server(definitions, None, None, builtin_tool_names()).values())


def _controller() -> AgentController:
    return AgentController(
        AgentConfig(), ExecutorPaths(), llm=_LLM(), tool_index=_ToolIndex(), memory=_Memory()
    )


class _FailingMCP:
    """External MCP boundary whose tool listing fails."""

    def get_tools(self) -> list:
        raise RuntimeError("mcp listing unavailable")


class _RecordingNativeLLM(NativeScriptedLLM):
    """Records every preview and native dispatch (system + definitions)."""

    def __init__(self, responses: list) -> None:
        super().__init__(responses)
        self.previews: list[tuple[str | None, list | None]] = []
        self.dispatches: list[tuple[str | None, list | None]] = []
        self.on_first_preview: Callable[[], None] | None = None

    def preview_request(self, messages, system=None, *, tools=None, json_mode=False, definition_revision=0):
        self.previews.append((system, copy.deepcopy(tools)))
        hook, self.on_first_preview = self.on_first_preview, None
        if hook is not None:
            hook()
        return super().preview_request(
            messages, system, tools=tools, json_mode=json_mode, definition_revision=definition_revision
        )

    def chat_with_tools_fallback(self, messages, tools, system=None, progress_cb=None):
        self.dispatches.append((system, copy.deepcopy(tools)))
        return super().chat_with_tools_fallback(messages, tools, system=system, progress_cb=progress_cb)


# ---------------------------------------------------------------------------
# Phase 2: dependency transfer
# ---------------------------------------------------------------------------

def test_controller_deps_view_field_is_optional(meta_world: _World) -> None:
    base = dict(
        llm=None, tool_index=None, memory=None, builtin_executor=None, mcp_manager=None, skill_registry=None
    )

    assert ControllerDeps(**base).model_catalog_view is None
    assert ControllerDeps(**base, model_catalog_view=meta_world.view).model_catalog_view is meta_world.view


@pytest.mark.parametrize("with_view", [True, False])
def test_build_react_context_forwards_the_deps_view_object(meta_world: _World, with_view: bool) -> None:
    view = meta_world.view if with_view else None
    deps = ControllerDeps(
        llm=None, tool_index=None, memory=None, builtin_executor=None, mcp_manager=None,
        skill_registry=None, model_catalog_view=view,
    )
    controller = SimpleNamespace(runtime_deps=deps, _context_payload=None, _prompt_variant=None)

    ctx = AgentRuntime.build_react_context(controller, "r-0000beef")

    assert ctx.model_catalog_view is view


def test_controller_runtime_deps_forwards_its_declared_view(meta_world: _World) -> None:
    controller = _controller()
    assert "_model_catalog_view" in vars(controller)
    assert controller._model_catalog_view is None

    controller._model_catalog_view = meta_world.view

    assert controller.runtime_deps.model_catalog_view is meta_world.view


def test_controller_build_system_prompt_presents_its_view_once(meta_world: _World) -> None:
    controller = _controller()
    controller._model_catalog_view = meta_world.view
    section = _render(meta_world.view).strip()

    prompt, _tokens = controller.build_system_prompt("goal")

    assert prompt.count(section) == 1


# ---------------------------------------------------------------------------
# Phase 2: loop consumers share the provided view
# ---------------------------------------------------------------------------

def test_assembled_prompt_and_native_definitions_carry_the_same_pin(meta_world: _World) -> None:
    ctx = _loop_ctx(ScriptedLLM([_FINISH]), meta_world.view)

    system = _assemble_system_prompt(ctx, "goal")
    _ensure_tool_defs(ctx)
    description = _spawn_description(ctx._tool_defs)

    assert system.count(_render(meta_world.view).strip()) == 1
    for model_id in (meta_world.removed.id, meta_world.active.id):
        assert model_id in description


def test_mcp_failure_fallback_keeps_the_view_in_builtin_definitions(meta_world: _World) -> None:
    ctx = _loop_ctx(ScriptedLLM([_FINISH]), meta_world.view, mcp_manager=_FailingMCP())

    _ensure_tool_defs(ctx)

    assert ctx._tool_defs == build_tool_definitions(None, model_catalog_view=meta_world.view)
    assert meta_world.removed.id in _spawn_description(ctx._tool_defs)


def test_strict_definition_build_propagates_mcp_failure(meta_world: _World) -> None:
    ctx = _loop_ctx(ScriptedLLM([_FINISH]), meta_world.view, mcp_manager=_FailingMCP())

    with pytest.raises(RuntimeError, match="mcp listing unavailable"):
        _ensure_tool_defs(ctx, strict=True)


# ---------------------------------------------------------------------------
# Phase 2: replaced view invalidates cached definitions
# ---------------------------------------------------------------------------

def test_same_revision_other_pin_rebuilds_cached_definitions(meta_world: _World) -> None:
    view_b = _other_pin_view(meta_world)
    ctx = _loop_ctx(ScriptedLLM([_FINISH]), meta_world.view)
    _ensure_tool_defs(ctx)
    assert meta_world.removed.id in _spawn_description(ctx._tool_defs)

    ctx.model_catalog_view = view_b
    _ensure_tool_defs(ctx)

    assert ctx._tool_defs == build_tool_definitions(None, model_catalog_view=view_b)
    assert meta_world.removed.id not in _spawn_description(ctx._tool_defs)
    assert meta_world.view == meta_world.registry.view(meta_world.pinned)


def test_view_replaced_by_none_drops_catalog_from_cached_definitions(meta_world: _World) -> None:
    ctx = _loop_ctx(ScriptedLLM([_FINISH]), meta_world.view)
    _ensure_tool_defs(ctx)
    assert meta_world.removed.id in _spawn_description(ctx._tool_defs)

    ctx.model_catalog_view = None
    _ensure_tool_defs(ctx)

    assert ctx._tool_defs == build_tool_definitions()


def test_view_added_after_none_enters_cached_definitions(meta_world: _World) -> None:
    ctx = _loop_ctx(ScriptedLLM([_FINISH]), None)
    _ensure_tool_defs(ctx)
    assert ctx._tool_defs == build_tool_definitions()

    ctx.model_catalog_view = meta_world.view
    _ensure_tool_defs(ctx)

    assert ctx._tool_defs == build_tool_definitions(None, model_catalog_view=meta_world.view)


def test_next_run_with_replaced_view_reports_recomputed_definition_tokens(meta_world: _World) -> None:
    view_b = _other_pin_view(meta_world)
    expected_a = _expected_tool_def_tokens(meta_world.view)
    expected_b = _expected_tool_def_tokens(view_b)
    assert expected_a != expected_b
    monitor = ContextMonitor()
    llm = NativeScriptedLLM([ChatResponse(text=_FINISH)])
    ctx = _loop_ctx(llm, meta_world.view, context_monitor=monitor)
    assert react_loop(ctx, "first") == "done"
    assert monitor.read().tool_defs_tokens == expected_a

    ctx.model_catalog_view = view_b
    assert react_loop(ctx, "second") == "done"

    assert monitor.read().tool_defs_tokens == expected_b


# ---------------------------------------------------------------------------
# Phase 2: guarded first request is one coherent bundle
# ---------------------------------------------------------------------------

def test_guarded_first_request_previews_and_dispatches_one_captured_view(meta_world: _World) -> None:
    view_b = _other_pin_view(meta_world)
    llm = _RecordingNativeLLM([ChatResponse(text=_FINISH)])
    ctx = _loop_ctx(llm, meta_world.view, guarded_first_request=True)
    llm.on_first_preview = lambda: setattr(ctx, "model_catalog_view", view_b)

    assert react_loop(ctx, "goal") == "done"

    assert ctx.model_catalog_view is view_b
    assert len(llm.previews) >= 2 and len(llm.dispatches) == 1
    bundles = [*llm.previews, *llm.dispatches]
    staged_system, staged_tools = bundles[0]
    assert all(bundle == (staged_system, staged_tools) for bundle in bundles)
    assert staged_system.count(_render(meta_world.view).strip()) == 1
    assert meta_world.removed.id in _spawn_description(staged_tools)


# ---------------------------------------------------------------------------
# Review regression: unguarded request bundle uses the view captured at assembly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("captured_initial", ["view", "none"])
def test_unguarded_request_dispatches_the_view_captured_at_assembly(
    meta_world: _World, captured_initial: str
) -> None:
    view_a = meta_world.view if captured_initial == "view" else None
    view_b = _other_pin_view(meta_world)
    defs_a = build_tool_definitions(None, model_catalog_view=view_a)
    defs_b = build_tool_definitions(None, model_catalog_view=view_b)
    assert defs_a != defs_b
    assert meta_world.removed.id not in _spawn_description(defs_b)
    llm = _RecordingNativeLLM([ChatResponse(text=_FINISH)])
    ctx = _loop_ctx(llm, view_a)
    assert ctx.model_catalog_view is view_a
    original_record_event = ctx.memory.record_event
    swaps: list[int] = []

    def record_event(event: str, *args: object, **kwargs: object) -> object:
        result = original_record_event(event, *args, **kwargs)
        if not swaps and event.startswith("User request:"):
            swaps.append(len(llm.dispatches))
            ctx.model_catalog_view = view_b
        return result

    ctx.memory.record_event = record_event

    assert react_loop(ctx, "first") == "done"

    assert swaps == [0]
    assert ctx.model_catalog_view is view_b
    assert len(llm.dispatches) == 1
    first_system, first_tools = llm.dispatches[0]
    assert first_tools == defs_a
    if view_a is None:
        assert first_tools == build_tool_definitions()
        assert "MODEL CATALOG" not in first_system
    else:
        assert first_system.count(_render(view_a).strip()) == 1
        assert meta_world.removed.id in _spawn_description(first_tools)

    assert react_loop(ctx, "second") == "done"

    assert swaps == [0]
    assert len(llm.dispatches) == 2
    second_system, second_tools = llm.dispatches[1]
    assert second_system.count(_render(view_b).strip()) == 1
    assert second_tools == build_tool_definitions(model_catalog_view=view_b)
    assert meta_world.removed.id not in _spawn_description(second_tools)


# ---------------------------------------------------------------------------
# Review regressions: compact tool summary carries current model facts
# ---------------------------------------------------------------------------

def _known_ids(view: CatalogView) -> tuple[str, ...]:
    ids = {model.id for model in view.models}
    if view.pinned_model is not None:
        ids.add(view.pinned_model.id)
    return tuple(sorted(ids))


def _model_facts(text: str, model_id: str, view: CatalogView) -> str:
    """Text following each mention of *model_id* up to the next other model id."""
    others = [other for other in _known_ids(view) if other != model_id]
    segments: list[str] = []
    start = text.find(model_id)
    while start != -1:
        after = start + len(model_id)
        stops = [pos for pos in (text.find(other, after) for other in others) if pos != -1]
        end = min(stops, default=len(text))
        segments.append(text[start:end])
        start = text.find(model_id, end if end > after else after)
    assert segments, f"{model_id} missing from:\n{text}"
    return "\n".join(segments).lower()


def _tool_description(surface: str, name: str, view: CatalogView) -> str:
    if surface == "native":
        return _native(view)[name]["description"]
    assert surface == "text"
    return prompt_builder.format_tools([BUILTIN_TOOLS[name]], model_catalog_view=view)


@pytest.fixture
def rich_active_world(tmp_path: Path) -> _World:
    """Same catalog as ``meta_world`` but the rich model is the active selection."""
    return _build_world(tmp_path, (_BLANK, _RICH), (_LEGACY,), fail_bravo=True)


@pytest.mark.parametrize("pinned", [True, False])
@pytest.mark.parametrize("name", ["spawn_agent", "schedule"])
@pytest.mark.parametrize("surface", ["native", "text"])
def test_tool_summary_projects_current_facts_of_available_models(
    meta_world: _World, surface: str, name: str, pinned: bool
) -> None:
    view = meta_world.view if pinned else meta_world.registry.view(None)
    assert (view.pinned_model is not None) is pinned
    assert view.active_model_id == _BLANK.id
    assert _RICH.id != view.active_model_id

    description = _tool_description(surface, name, view)

    rich = _model_facts(description, _RICH.id, view)
    assert re.search(r"123[,_]?456", rich) and "models.dev" not in rich
    assert re.search(r"7[,_]?777", rich)
    assert "unsupported" in rich and re.search(r"(?<!un)supported", rich)
    assert "unknown" in rich
    legacy = _model_facts(description, _LEGACY.id, view)
    assert re.search(r"32[,_]?768", legacy) and "stale" in legacy
    if pinned:
        assert meta_world.removed.id in description
    else:
        assert meta_world.removed.id not in description


@pytest.mark.parametrize("surface", ["native", "text"])
def test_tool_summary_projects_current_facts_of_the_active_model(
    rich_active_world: _World, surface: str
) -> None:
    view = rich_active_world.view
    assert view.active_model_id == _RICH.id

    description = _tool_description(surface, "spawn_agent", view)

    active = _model_facts(description, _RICH.id, view)
    assert re.search(r"123[,_]?456", active) and "models.dev" not in active
    assert "unsupported" in active
    assert "unknown" in active


# ---------------------------------------------------------------------------
# Review regressions: a pin that stays in inventory with changed metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ChangedPinWorld:
    view: CatalogView
    pinned: RunModel
    current: ModelRecord
    active: ModelRecord


@pytest.fixture
def changed_pin_world(tmp_path: Path) -> _ChangedPinWorld:
    """Pin A through real admission; a later refresh re-lists A with different metadata."""
    historical = _model("pin-a", context=4096)
    current = _model("pin-a", context=16384, vision=False)
    other = _model("zeta-b", context=2000)
    connections = {"alpha": ProviderConfig("alpha", "openai", ALPHA_REF, ALPHA_URL)}
    states = (ProviderState("alpha", "openai", "2026-09-01T00:00:00Z", "fresh"),)
    settings = ModelSettings(tmp_path / "models-config.toml")
    settings.write(CatalogDocument(1, 2, None, states, (historical, other)))
    discovery = _Discovery()
    registry = ModelRegistry(connections, settings, discovery, _resolve)
    registry.select(historical.id)
    pinned = registry.admit()
    registry.select(other.id)
    discovery.responses["alpha"] = _inventory("alpha", "openai", (current, other))
    registry.refresh()
    view = registry.view(pinned)

    assert pinned.model == historical
    assert view.pinned_model == historical
    in_inventory = {item.id: item for item in view.models}
    assert in_inventory[historical.id] == current
    assert in_inventory[historical.id].metadata != view.pinned_model.metadata
    assert (view.pinned_model.metadata.context_tokens, current.metadata.context_tokens) == (4096, 16384)
    assert view.active_model_id == other.id and len(view.models) == 2
    return _ChangedPinWorld(view, pinned, current, other)


@pytest.mark.parametrize("cap", [20, 0])
def test_pin_still_in_inventory_keeps_admission_label_apart_from_current_metadata(
    changed_pin_world: _ChangedPinWorld, cap: int
) -> None:
    pin_id = changed_pin_world.current.id

    section = _render(changed_pin_world.view, max_models=cap)

    mentions = [line for line in section.splitlines() if pin_id in line]
    admission = [line for line in mentions if re.search(r"4[,_]?096", line)]
    assert admission and all("pinned" in line.lower() for line in admission)
    assert not any(re.search(r"16[,_]?384", line) for line in admission)
    current = [line for line in mentions if re.search(r"16[,_]?384", line)]
    for line in current:
        assert "models.dev" not in line and "unsupported" in line.lower()
        assert not re.search(r"4[,_]?096", line)
    if cap == 0:
        assert current or 1 in _omitted_numbers(section)
    else:
        assert current
