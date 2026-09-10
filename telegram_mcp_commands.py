"""MCP subcommands for the Telegram bot (the /mcp command family).

Extracted from ``telegram_commands.py`` along the same seam as
``telegram_callbacks.py``. Self-contained cluster: token-info formatting,
the _mcp_* subcommand handlers, the dispatch table, and cmd_mcp.
"""

import asyncio
import html
import logging
from dataclasses import replace as dataclass_replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from builtin_tools.schemas import build_tool_definitions, builtin_tool_names
from context_monitor import (
    compute_danger_level,
    compute_headroom_real,
    group_tool_defs_by_server,
)
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from telegram_commands import _require_auth

if TYPE_CHECKING:
    from telegram_interface import TelegramInterface

logger = logging.getLogger(__name__)


def _fmt_mcp_token_info(token_info: dict[str, Any] | None) -> str:
    """Format token expiry and refresh availability for display.

    Args:
        token_info: Result of ``MCPManager.get_token_info()``, or ``None``.

    Returns:
        A compact HTML-safe parenthetical string like
        ``"authenticated, expires in 3600s, refresh: available"``.
    """
    if token_info is None:
        return "no OAuth"
    if not token_info["has_token"]:
        return "needs authentication — run /mcp auth &lt;name&gt;"

    parts = ["authenticated"]
    if token_info["expires_in"] is not None:
        parts.append(f"expires in {token_info['expires_in']}s")
    else:
        parts.append("expiry unknown")
    if token_info["has_refresh"]:
        parts.append("refresh: available")
    else:
        parts.append("refresh: none")
    return ", ".join(parts)


async def _mcp_auth_status(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render /mcp auth status output."""
    assert iface.mcp_manager is not None
    servers = iface.mcp_manager.list_servers()
    if not servers:
        await update.effective_message.reply_text("🔌 No MCP servers configured.")
        return
    lines = ["🔐 <b>MCP OAuth Status</b>\n"]
    for s in servers:
        name = s["name"]
        status = s["status"]
        has_oauth = iface.mcp_manager.server_has_oauth(name)
        if not has_oauth:
            auth_state = "no OAuth"
        elif status == "needs_auth":
            auth_state = "needs authentication — run /mcp auth &lt;name&gt;"
        elif status == "active":
            token_info = iface.mcp_manager.get_token_info(name)
            auth_state = _fmt_mcp_token_info(token_info)
        else:
            auth_state = "OAuth configured"
        lines.append(
            f"• <b>{html.escape(name)}</b> — {html.escape(status)} ({auth_state})"
        )
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def _mcp_auth_revoke(
    iface: "TelegramInterface",
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    name: str,
) -> None:
    """Revoke stored OAuth tokens for a server and mark it as needing auth."""
    assert iface.mcp_manager is not None
    revoked = iface.mcp_manager.revoke_server(name)
    if not revoked:
        # Determine whether the server exists but lacks OAuth for a precise message.
        servers = {s["name"] for s in iface.mcp_manager.list_servers()}
        if name not in servers:
            await update.effective_message.reply_text(
                f"❌ Server <code>{html.escape(name)}</code> not found.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.effective_message.reply_text(
                f"❌ Server <code>{html.escape(name)}</code> has no OAuth configuration.",
                parse_mode=ParseMode.HTML,
            )
        return

    await update.effective_message.reply_text(
        f"🔒 Token revoked for <code>{html.escape(name)}</code>.\n"
        f"Server status: needs_auth.\n"
        f"Run <code>/mcp auth {html.escape(name)}</code> to re-authenticate.",
        parse_mode=ParseMode.HTML,
    )
    # Note: Unlike _mcp_off, we don't call tool_registry.unregister_mcp_server
    # here — revoke_server() clears the tools internally (deletes
    # _tool_to_server entries and calls wrapper.clear_tools()). The refresh
    # picks up the cleared state via build_tool_definitions(mcp_manager).
    _refresh_tool_defs_snapshot(iface)


def _refresh_tool_defs_snapshot(iface: "TelegramInterface") -> None:
    """Refresh the context monitor snapshot after the tool set changes.

    Recomputes the tool-definitions-by-server breakdown, token total, danger
    level, and real headroom from the most recent snapshot. The updated
    snapshot is published as non-live so ``/context`` reflects the latest
    available tool configuration immediately after a tool-changing command.
    """
    if iface.agent is None or iface.agent.context_monitor is None:
        return
    if iface.tool_registry is None or iface.mcp_manager is None:
        return

    last = iface.agent.context_monitor.read()
    if last is None:
        return

    # Thread-safety: This read-modify-write sequence runs on the Telegram event
    # loop thread and is not atomic with respect to the agent thread's
    # _publish_context_snapshot. A concurrent live publish could be briefly
    # clobbered by this is_live=False snapshot. This is benign and self-healing
    # — the ReAct loop republishes a live snapshot every step.
    builtin_names = builtin_tool_names()
    fresh = group_tool_defs_by_server(
        build_tool_definitions(iface.mcp_manager),
        iface.tool_registry,
        iface.mcp_manager,
        builtin_names,
    )
    # NOTE: The refresh uses the full tool set from build_tool_definitions, not
    # a per-run filtered subset like ctx._tool_defs in the ReAct loop. The
    # is_live=False marker flags this snapshot as approximate.
    fresh_tokens = sum(fresh.values())
    total = last.system_prompt_tokens + last.chat_history_tokens + fresh_tokens
    danger = compute_danger_level(total, last.compaction_threshold)
    headroom = compute_headroom_real(
        last.compaction_threshold,
        last.system_prompt_tokens,
        last.chat_history_tokens,
        fresh_tokens,
    )
    updated = dataclass_replace(
        last,
        tool_defs_by_server=fresh,
        tool_defs_tokens=fresh_tokens,
        danger_level=danger,
        headroom_real=headroom,
        is_live=False,
    )
    iface.agent.context_monitor.publish(updated)


async def _mcp_on(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable an MCP server and sync its tools into the registry."""
    assert iface.mcp_manager is not None
    args = ctx.args or []
    name = args[1] if len(args) > 1 else ""
    if not name:
        await update.effective_message.reply_text(
            "Usage: <code>/mcp on &lt;name&gt;</code>", parse_mode=ParseMode.HTML)
        return
    ok = iface.mcp_manager.set_enabled(name, True)
    if not ok:
        await update.effective_message.reply_text(
            f"❌ MCP server <code>{html.escape(name)}</code> not found.",
            parse_mode=ParseMode.HTML)
        return
    # Sync newly connected tools into tool_registry
    if iface.tool_registry and iface.mcp_manager:
        info = iface.mcp_manager.get_server_info(name)
        if info:
            iface.tool_registry.register_mcp_tools(name, info["tools"])
    await update.effective_message.reply_text(
        f"✅ MCP server <code>{html.escape(name)}</code> enabled.",
        parse_mode=ParseMode.HTML)
    _refresh_tool_defs_snapshot(iface)


async def _mcp_off(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable an MCP server and remove its tools from the registry."""
    assert iface.mcp_manager is not None
    args = ctx.args or []
    name = args[1] if len(args) > 1 else ""
    if not name:
        await update.effective_message.reply_text(
            "Usage: <code>/mcp off &lt;name&gt;</code>", parse_mode=ParseMode.HTML)
        return
    ok = iface.mcp_manager.set_enabled(name, False)
    if not ok:
        await update.effective_message.reply_text(
            f"❌ MCP server <code>{html.escape(name)}</code> not found.",
            parse_mode=ParseMode.HTML)
        return
    if iface.tool_registry:
        iface.tool_registry.unregister_mcp_server(name)
    await update.effective_message.reply_text(
        f"⏹ MCP server <code>{html.escape(name)}</code> disabled.",
        parse_mode=ParseMode.HTML)
    _refresh_tool_defs_snapshot(iface)


async def _mcp_info(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed information about an MCP server."""
    assert iface.mcp_manager is not None
    args = ctx.args or []
    name = args[1] if len(args) > 1 else ""
    if not name:
        await update.effective_message.reply_text(
            "Usage: <code>/mcp info &lt;name&gt;</code>", parse_mode=ParseMode.HTML)
        return
    info = iface.mcp_manager.get_server_info(name)
    if not info:
        await update.effective_message.reply_text(
            f"❌ MCP server <code>{html.escape(name)}</code> not found.",
            parse_mode=ParseMode.HTML)
        return
    status_icon = {"active": "●", "off": "○", "error": "⚠️", "needs_auth": "🔐"}.get(info["status"], "?")
    lines = [
        f"🔌 <b>MCP Server: {html.escape(name)}</b>",
        f"  Status:    {status_icon} {info['status']}",
        f"  Transport: {info['transport']}",
    ]
    if info["url"]:
        lines.append(f"  URL:       <code>{html.escape(info['url'])}</code>")
    if info["command"]:
        cmd_str = " ".join(info["command"])
        lines.append(f"  Command:   <code>{html.escape(cmd_str)}</code>")
    if info["headers"]:
        lines.append(f"  Headers:   {len(info['headers'])} configured")
    if info["env"]:
        lines.append(f"  Env vars:  {len(info['env'])} configured")
    if info["tools"]:
        lines.append(f"\n  <b>Tools ({len(info['tools'])}):</b>")
        for t in info["tools"]:
            desc = " ".join(t.description.split())[:60]
            lines.append(f"    • <code>{html.escape(t.name)}</code> — {html.escape(desc)}")
    if info["last_error"]:
        lines.append(f"\n  ⚠️ <b>Last error:</b> {html.escape(info['last_error'][:300])}")
    for chunk in iface._split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def _mcp_list(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List all configured MCP servers and their status."""
    assert iface.mcp_manager is not None
    servers = iface.mcp_manager.list_servers()
    if not servers:
        await update.effective_message.reply_text("🔌 No MCP servers configured.")
        return
    lines = ["🔌 <b>MCP Servers</b>\n"]
    for s in servers:
        icon = {"active": "●", "off": "○", "needs_auth": "🔐"}.get(s["status"], "⚠️")
        tools_str = f"  — {s['tool_count']} tool(s)" if s["tool_count"] else ""
        err_str = "  ⚠️ error" if s["last_error"] else ""
        if s["status"] == "needs_auth":
            auth_hint = " — auth required, use /mcp auth &lt;name&gt;"
        else:
            auth_hint = ""
        lines.append(
            f"{icon} <b>{html.escape(s['name'])}</b>"
            f"  [{s['transport']}]  {s['status']}{auth_hint}{tools_str}{err_str}"
        )
    lines.append(
        "\n<i>Commands: /mcp list · /mcp on &lt;name&gt; · /mcp off &lt;name&gt; · /mcp info &lt;name&gt; "
        "· /mcp auth &lt;name&gt; · /mcp auth status · /mcp auth cancel · /mcp auth revoke &lt;name&gt;</i>"
    )
    for chunk in iface._split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def _mcp_auth(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch /mcp auth <name|status|cancel|revoke>."""
    assert iface.mcp_manager is not None
    args = ctx.args or []
    name = args[1] if len(args) > 1 else ""

    if not name:
        await update.effective_message.reply_text(
            "Usage: <code>/mcp auth &lt;name&gt;</code> | "
            "<code>/mcp auth status</code> | "
            "<code>/mcp auth cancel</code> | "
            "<code>/mcp auth revoke &lt;name&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if name.lower() == "status":
        await _mcp_auth_status(iface, update, ctx)
        return
    if name.lower() == "cancel":
        result = iface.mcp_manager.cancel_oauth_flow()
        if result.get("success"):
            await update.effective_message.reply_text(
                "🛑 OAuth flow cancellation requested. "
                "The flow will abort shortly.",
            )
        else:
            error = result.get("error", "Unable to cancel")
            await update.effective_message.reply_text(
                f"❌ {html.escape(error)}",
                parse_mode=ParseMode.HTML,
            )
        return
    if name.lower() == "revoke":
        revoke_name = args[2] if len(args) > 2 else ""
        if not revoke_name:
            await update.effective_message.reply_text(
                "Usage: <code>/mcp auth revoke &lt;name&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        await _mcp_auth_revoke(iface, update, ctx, revoke_name)
        return

    # Quick validation before promising the user anything.
    if not iface.mcp_manager.server_has_oauth(name):
        await update.effective_message.reply_text(
            f"❌ Server <code>{html.escape(name)}</code> not found or has no OAuth configuration.",
            parse_mode=ParseMode.HTML,
        )
        return

    timeout = iface.mcp_manager.get_oauth_timeout(name)
    timeout_str = f"{timeout // 60} min" if timeout >= 60 else f"{timeout} sec"
    await update.effective_message.reply_text(
        f"🔐 Starting OAuth flow for <code>{html.escape(name)}</code>…\n"
        f"An authorization link will appear here shortly "
        f"(timeout: {timeout_str}).",
        parse_mode=ParseMode.HTML,
    )

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, iface.mcp_manager.start_oauth_flow, name, update.effective_chat.id
    )
    if result.get("success"):
        # Register newly discovered tools in the ToolRegistry (mirrors /mcp on).
        if iface.tool_registry and iface.mcp_manager:
            info = iface.mcp_manager.get_server_info(name)
            if info:
                iface.tool_registry.register_mcp_tools(name, info["tools"])
        await update.effective_message.reply_text(
            f"✅ OAuth flow completed for <code>{html.escape(name)}</code>. "
            f"Server is now active.",
            parse_mode=ParseMode.HTML,
        )
        _refresh_tool_defs_snapshot(iface)
    else:
        error = result.get("error", "OAuth flow failed")
        await update.effective_message.reply_text(
            f"❌ {html.escape(error)}",
            parse_mode=ParseMode.HTML,
        )


_MCP_DISPATCH: dict[str, Callable[["TelegramInterface", Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]] = {
    "on": _mcp_on,
    "off": _mcp_off,
    "info": _mcp_info,
    "auth": _mcp_auth,
    "list": _mcp_list,
}


@_require_auth
async def cmd_mcp(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mcp [list|on|off|info|auth] [name]"""
    if not iface.mcp_manager:
        await update.effective_message.reply_text(
            "🔌 No MCP servers configured.\n"
            "Add <code>[[mcp_servers]]</code> sections to <code>config.toml</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    args = ctx.args or []
    sub = args[0].lower() if args else "list"

    handler = _MCP_DISPATCH.get(sub, _mcp_list)

    await handler(iface, update, ctx)
