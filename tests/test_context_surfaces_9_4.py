"""Task 9.4: `/context`, `context_profile` and idle `/mcp` snapshot refresh on real wiring (ADR-0043).

Spec ``context-monitoring``: a refresh after a tool-changing `/mcp` command recomputes only the tool
definitions, preserves counts, threshold, turn and model identity, and publishes ``is_live=False``;
empty MCP servers appear with zero; built-ins are classified by schema name, not by the MCP-only
registry. A real ``ContextMonitor``, ``ToolRegistry``, ``build_tool_definitions`` and the real command
functions run; only the MCP server transport and Telegram are substituted.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from builtin_tools.context_profile import exec_context_profile
from builtin_tools.descriptors import BUILTIN_TOOLS
from builtin_tools.schemas import BUILTIN_TOOL_SCHEMAS
from context_monitor import ContextMonitor, ContextSnapshot
from telegram_commands import cmd_context
from telegram_mcp_commands import _mcp_auth, _mcp_auth_revoke, _mcp_off, _mcp_on
from tests.test_telegram_command_surface import _get_help_text, _make_iface as _make_command_iface
from tool_registry import Tool, ToolRegistry

SERVER = "google-workspace"
IDLE = ContextSnapshot(
    system_prompt_tokens=111, chat_history_tokens=2222, tool_defs_tokens=0,
    tool_defs_by_server={"builtin": 0, SERVER: 0, "empty": 0}, compaction_threshold=3333,
    is_live=True, turn=9, model_id="pa/pinned-model",
)
TOOL_FIELDS = {"tool_defs_tokens", "tool_defs_by_server", "is_live"}


class FakeMcpManager:
    """MCP transport boundary: servers, enabled flags and the tools each exposes."""

    def __init__(self) -> None:
        self.tools = {SERVER: [self._tool(f"gw_{n}") for n in ("search", "send", "list")], "empty": []}
        self.enabled = {SERVER: False, "empty": True}
        self.authenticated = False

    @staticmethod
    def _tool(name: str) -> Tool:
        return Tool(name, "mcp", f"{name} tool " * 12, True, SERVER,
                    {"type": "object", "properties": {"q": {"type": "string"}}})

    def list_servers(self) -> list[dict]:
        return [{"name": name} for name in self.tools]

    def get_tools(self) -> list[Tool]:
        return [t for name, tools in self.tools.items() if self.enabled[name] for t in tools]

    def get_server_info(self, name: str) -> dict:
        return {"tools": self.tools[name]}

    def set_enabled(self, name: str, value: bool) -> bool:
        self.enabled[name] = value
        return True

    def server_has_oauth(self, _name: str) -> bool:
        return True

    def get_oauth_timeout(self, _name: str) -> int:
        return 60

    def start_oauth_flow(self, name: str, _chat_id: int) -> dict:
        self.enabled[name] = self.authenticated = True
        return {"success": True}

    def revoke_server(self, name: str) -> bool:
        self.enabled[name] = self.authenticated = False
        return True


class World:
    """Idle agent with a published snapshot, real registry and the fake MCP transport."""

    def __init__(self, snapshot: ContextSnapshot | None = IDLE) -> None:
        self.monitor = ContextMonitor()
        if snapshot is not None:
            self.monitor.publish(snapshot)
        self.manager = FakeMcpManager()
        self.registry = ToolRegistry()
        self.iface = MagicMock()
        self.iface.agent.context_monitor = self.monitor
        self.iface.tool_registry, self.iface.mcp_manager = self.registry, self.manager
        self.iface.model_registry = None
        self.iface._is_authorized = MagicMock(return_value=True)
        self.replies: list[str] = []
        self.update = MagicMock()
        self.update.effective_chat.id = 1001
        self.update.effective_message.reply_text = AsyncMock(side_effect=lambda text, **_: self.replies.append(text))

    def run(self, command, *args: str) -> None:
        ctx = MagicMock()
        ctx.args = list(args)
        asyncio.run(command(self.iface, self.update, ctx, *([args[-1]] if command is _mcp_auth_revoke else [])))

    def snap(self) -> ContextSnapshot:
        snapshot = self.monitor.read()
        assert snapshot is not None
        return snapshot


def assert_only_tool_fields_changed(before: ContextSnapshot, after: ContextSnapshot) -> None:
    old, new = asdict(before), asdict(after)
    for key in old.keys() - TOOL_FIELDS:
        assert new[key] == old[key], key
    assert after.is_live is False
    assert after.tool_defs_tokens == sum(after.tool_defs_by_server.values())


def test_mcp_on_publishes_nonzero_server_and_preserves_every_other_field() -> None:
    world = World()
    world.run(_mcp_on, "on", SERVER)
    after = world.snap()
    assert after.tool_defs_by_server[SERVER] > 0 and after.tool_defs_by_server["empty"] == 0
    assert after.tool_defs_by_server["builtin"] > 0
    assert_only_tool_fields_changed(IDLE, after)


def test_refresh_classifies_builtins_by_schema_name_not_by_the_mcp_only_registry() -> None:
    world = World()
    world.run(_mcp_on, "on", SERVER)
    groups = world.snap().tool_defs_by_server
    assert "unknown" not in groups and set(groups) == {"builtin", SERVER, "empty"}
    assert world.registry.get("shell") is None and groups["builtin"] > 0


def test_mcp_off_drops_the_server_to_zero_and_the_total_falls_by_exactly_its_cost() -> None:
    world = World()
    world.run(_mcp_on, "on", SERVER)
    before = world.snap()
    world.run(_mcp_off, "off", SERVER)
    after = world.snap()
    assert after.tool_defs_by_server[SERVER] == 0 and SERVER in after.tool_defs_by_server
    assert before.tool_defs_tokens - after.tool_defs_tokens == before.tool_defs_by_server[SERVER]
    assert_only_tool_fields_changed(IDLE, after)


def test_oauth_success_attributes_new_tools_and_revoke_returns_them_to_zero() -> None:
    world = World()
    world.run(_mcp_auth, "auth", SERVER)
    granted = world.snap()
    assert granted.tool_defs_by_server[SERVER] > 0
    assert_only_tool_fields_changed(IDLE, granted)
    # The real revoke clears tools inside the MCP manager; the refresh reads them via build_tool_definitions,
    # not by unregistering from the ToolRegistry, so the fake mirrors that and the registry is left alone.
    world.run(_mcp_auth_revoke, "auth", "revoke", SERVER)
    revoked = world.snap()
    assert revoked.tool_defs_by_server[SERVER] == 0
    assert granted.tool_defs_tokens - revoked.tool_defs_tokens == granted.tool_defs_by_server[SERVER]
    assert_only_tool_fields_changed(IDLE, revoked)


@pytest.mark.parametrize("command,args", [(_mcp_on, ("on", SERVER)), (_mcp_off, ("off", SERVER))], ids=["on", "off"])
def test_tool_changing_command_without_a_snapshot_publishes_nothing(command, args) -> None:
    world = World(snapshot=None)
    world.run(command, *args)
    assert world.monitor.read() is None


def test_context_and_profile_render_the_refreshed_idle_snapshot_with_zero_servers() -> None:
    world = World()
    world.run(_mcp_on, "on", SERVER)
    asyncio.run(cmd_context(world.iface, world.update, MagicMock()))
    text = world.replies[-1]
    assert "model: pa/pinned-model" in text and "idle (last run turn 9)" in text and "LIVE" not in text
    assert "Compaction threshold: 3,333 tokens" in text
    assert "  empty: 0" in text and f"  {SERVER}: " in text and "unknown" not in text
    profile = exec_context_profile(world.monitor)
    assert json.loads(json.dumps(profile))["tool_defs_by_server"]["empty"] == 0
    snap = world.snap()
    assert profile["total_tokens"] == snap.system_prompt_tokens + snap.chat_history_tokens + snap.tool_defs_tokens
    assert profile["model_id"] == "pa/pinned-model" and profile["is_live"] is False and profile["turn"] == 9


def test_context_profile_result_is_isolated_from_the_published_snapshot() -> None:
    monitor = ContextMonitor()
    monitor.publish(IDLE)
    result = exec_context_profile(monitor)
    result["tool_defs_by_server"]["injected"] = 99
    result["tool_defs_by_server"]["builtin"] = 12345
    published = monitor.read()
    assert published == IDLE and "injected" not in published.tool_defs_by_server


REMOVED_WORDS = ("reserve", "headroom", "danger", "window", "capacity", "completion")
PAYLOAD_WORDS = ("compaction_threshold", "tool_defs_by_server", "model_id", "is_live", "turn")


def test_context_profile_descriptions_describe_the_payload_not_removed_fields() -> None:
    """Both tool registries tell the model what the JSON really contains (ADR-0043)."""
    descriptions = {
        "schemas": BUILTIN_TOOL_SCHEMAS["context_profile"]["description"],
        "descriptors": BUILTIN_TOOLS["context_profile"].description,
    }
    payload = set(exec_context_profile(_published_monitor()))
    for where, text in descriptions.items():
        lowered = text.lower()
        assert not [w for w in REMOVED_WORDS if w in lowered], (where, text)
        assert all(w in text for w in PAYLOAD_WORDS) and "zero" in lowered, (where, text)
        assert {w for w in PAYLOAD_WORDS} <= payload


def _published_monitor() -> ContextMonitor:
    monitor = ContextMonitor()
    monitor.publish(IDLE)
    return monitor


def test_context_is_described_as_estimated_tokens_in_help_and_the_command_menu() -> None:
    """The public `/context` entry stays discoverable without the removed window concept."""
    iface = _make_command_iface()
    help_text = _get_help_text(iface)
    menu: dict[str, str] = {}

    async def collect() -> None:
        async def set_my_commands(commands) -> None:
            menu.update({c.command: c.description for c in commands})

        app = MagicMock()
        app.bot.set_my_commands = set_my_commands
        await iface._post_init(app)

    asyncio.run(collect())
    assert "/context  — show estimated input tokens and compaction threshold" in help_text
    assert menu["context"] == "Show estimated input tokens and compaction threshold"
    assert "window" not in help_text.lower().split("/context", 1)[1].split("\n", 1)[0]
    assert "window" not in menu["context"].lower() and "compress" not in menu
