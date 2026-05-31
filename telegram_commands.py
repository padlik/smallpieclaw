from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import secrets
import time
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from telegram_interface import TelegramInterface

    assert TelegramInterface

logger = logging.getLogger(__name__)


async def cmd_start(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not iface._is_authorized(user.id):
        await iface._send_unauthorized(update)
        return
    await update.effective_message.reply_text(
        "👋 Home Server Agent ready.\n"
        "Send me a command like:\n"
        "  • <b>check disk usage</b>\n"
        "  • <b>show CPU temperature</b>\n"
        "  • <b>are my Docker containers running?</b>\n\n"
        "Use /help for more info.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    await update.effective_message.reply_text(
        "🤖 <b>Home Server Agent</b>\n\n"
        "Just send a natural language request, e.g.:\n"
        "  <code>check disk usage</code>\n"
        "  <code>show system health</code>\n"
        "  <code>how much RAM is free?</code>\n\n"
        "<b>Commands:</b>\n"
        "  /status  — agent status, uptime, token usage\n"
        "  /tools   — list available tools\n"
        "  /models  — list and switch LLM models\n"
        "  /mcp     — manage MCP servers (list / on / off / info)\n"
        "  /jobs    — list scheduled jobs\n"
        "  /reset   — save and clear task context (<code>/reset discard</code> to skip saving)\n"
        "  /mad_plan — Model Adaptive Planner\n"
        "      <code>/mad_plan</code> or <code>/mad_plan plan</code> — start planning a task\n"
        "      <code>/mad_plan list</code>            — list saved plans\n"
        "      <code>/mad_plan execute &lt;name&gt;</code> — execute a saved plan\n"
        "      <code>/mad_plan delete &lt;name&gt;</code>  — delete a saved plan\n"
        "  /agent   — return to standard agent mode\n"
        "  /pair    — pairing token management\n"
        "  /myid    — show your Telegram user ID\n",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return

    uptime_secs = int(time.time() - iface._start_time)
    h = uptime_secs // 3600
    m = (uptime_secs % 3600) // 60
    s = uptime_secs % 60

    if iface.llm_client:
        active = iface.llm_client.llm_cfg
        llm_model = f"{active.get('name', '')} / {active.get('model', 'N/A')}".lstrip("/ ")
    else:
        llm_model = "N/A"
    emb_cfg = iface._config.get("embeddings", {})
    emb_model = emb_cfg.get("model", "N/A")
    emb_key_status = "own key" if emb_cfg.get("api_key") else "using active model key (fallback)"

    tools_count = len(iface.tool_registry.all()) if iface.tool_registry else 0
    skills_count = iface.skill_registry.count() if iface.skill_registry else 0

    # Sub-agent count
    from sub_agent_registry import get_registry as _get_agents
    active_agents = _get_agents().count()
    agents_line = f"\n🤖 Sub-agents: {active_agents} running" if active_agents > 0 else ""

    # Per-model token usage from shared registry
    token_line = ""
    if iface._usage_registry:
        today_usage = iface._usage_registry.get_today()
        if today_usage:
            rows = []
            max_model_len = max(len(m) for m in today_usage) if today_usage else 10
            col = max(max_model_len, 10)
            for model_name, u in sorted(today_usage.items()):
                pad = col - len(model_name)
                rows.append(
                    f"  {html.escape(model_name)}{' ' * pad}  "
                    f"{u['prompt']:,} + {u['completion']:,} = {u['total']:,}"
                )
            totals = iface._usage_registry.get_today_totals()
            rows.append(
                f"  {'─' * (col + 2)}\n"
                f"  {'Total':<{col}}  "
                f"{totals['prompt']:,} + {totals['completion']:,} = {totals['total']:,}"
            )
            token_line = "\n📊 <b>Token Usage Today (prompt + completion = total):</b>\n<pre>" + "\n".join(rows) + "</pre>"
    elif iface.llm_client:
        usage = iface.llm_client.get_today_usage()
        token_line = (
            f"\n📊 <b>Token Usage Today:</b>\n"
            f"  Prompt: {usage['prompt_tokens']:,}\n"
            f"  Completion: {usage['completion_tokens']:,}\n"
            f"  Total: {usage['total_tokens']:,}"
        )

    # Scheduler state
    scheduler_line = ""
    if iface.scheduler:
        jobs = iface.scheduler.list_jobs()
        enabled = sum(1 for j in jobs if j.get("enabled", True))
        total = len(jobs)
        sched_state = "enabled" if iface.scheduler.enabled else "disabled"
        scheduler_line = f"\n📅 Scheduler: <code>{sched_state}</code> | {enabled}/{total} jobs active"

    # Current agent mode
    current_mode = getattr(iface, "_agent_mode", "agent")
    mode_line = "\n🧠 Mode: <b>MadPlan</b>" if current_mode == "madplan" else ""

    # Current server time
    from datetime import datetime as _dt
    now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    await update.effective_message.reply_text(
        f"✅ <b>Agent Status</b>\n\n"
        f"🕐 Time: <code>{now_str}</code>\n"
        f"⏱ Uptime: <code>{h}h {m}m {s}s</code>\n"
        f"🤖 LLM: <code>{html.escape(llm_model)}</code>\n"
        f"🔍 Embeddings: <code>{html.escape(emb_model)}</code> ({html.escape(emb_key_status)})\n"
        f"🔐 Security: <code>{html.escape(iface.security_mode)}</code>\n"
        f"👥 Authorized users: {len(iface.allowed_ids)}\n"
        f"🔧 Tools: {tools_count} | 📚 Skills: {skills_count}"
        f"{agents_line}"
        f"{scheduler_line}"
        f"{mode_line}"
        f"{token_line}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stop(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    if iface.agent:
        iface.agent.cancel()
        await update.effective_message.reply_text(
            "🛑 Stop signal sent — current task will end after the current step."
        )
    else:
        await update.effective_message.reply_text("ℹ️ No active agent to stop.")


async def cmd_reset(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    args = ctx.args or []
    discard = "discard" in [a.lower() for a in args]

    status_msg = await update.effective_message.reply_text(
        "🗑️ Discarding task context…" if discard else "💾 Saving task context…"
    )

    if iface.agent_reset_fn:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: iface.agent_reset_fn(save=not discard)
        )
        await iface._safe_edit(status_msg, result)
    else:
        await iface._safe_edit(status_msg, "✅ Context cleared.")


async def cmd_compress(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return

    status_msg = await update.effective_message.reply_text("🗜️ Compressing context…")

    if iface.agent_compress_fn:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, iface.agent_compress_fn)
        await iface._safe_edit(status_msg, result)
    else:
        await iface._safe_edit(status_msg, "ℹ️ Compress not available.")


async def cmd_verbose(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    args = ctx.args or []
    if args:
        sub = args[0].lower()
        if sub == "on":
            iface._verbose = True
        elif sub == "off":
            iface._verbose = False
        else:
            await update.effective_message.reply_text(
                "Usage: <code>/verbose on</code> or <code>/verbose off</code>",
                parse_mode=ParseMode.HTML,
            )
            return
    else:
        iface._verbose = not iface._verbose  # toggle
    if iface._verbose:
        text = (
            "🔊 <b>Verbose mode on</b>\n"
            "<i>Each tool call and result will be sent as a separate message during task execution.</i>"
        )
    else:
        text = "🔇 <b>Verbose mode off</b>\n<i>Progress updates will edit a single status message.</i>"
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_jobs(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    if not iface.scheduler:
        await update.effective_message.reply_text("Scheduler not available.")
        return

    args = ctx.args or []
    sub = args[0].lower() if args else ""
    tag = args[1] if len(args) > 1 else ""

    # /jobs reload — hot-reload scheduler.toml from disk
    if sub == "reload":
        result = iface.scheduler.reload()
        await update.effective_message.reply_text(
            f"🔄 Scheduler reloaded.\n"
            f"  ✅ Active jobs: <b>{result['reloaded']}</b>\n"
            f"  ❌ Failed: <b>{result['failed']}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    # /jobs remove <tag>
    if sub == "remove":
        if not tag:
            await update.effective_message.reply_text(
                "Usage: <code>/jobs remove &lt;tag&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        ok = iface.scheduler.remove_job(tag)
        status = f"🗑 Job <code>{html.escape(tag)}</code> removed." if ok else f"❌ Job <code>{html.escape(tag)}</code> not found."
        await update.effective_message.reply_text(status, parse_mode=ParseMode.HTML)
        jobs = iface.scheduler.list_jobs()
        for chunk in iface._split_message(iface._format_jobs_list(jobs)):
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)
        return

    # /jobs pause <tag>
    if sub == "pause":
        if not tag:
            await update.effective_message.reply_text(
                "Usage: <code>/jobs pause &lt;tag&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        ok = iface.scheduler.pause_job(tag)
        status = f"⏸ Job <code>{html.escape(tag)}</code> paused." if ok else f"❌ Job <code>{html.escape(tag)}</code> not found."
        await update.effective_message.reply_text(status, parse_mode=ParseMode.HTML)
        jobs = iface.scheduler.list_jobs()
        for chunk in iface._split_message(iface._format_jobs_list(jobs)):
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)
        return

    # /jobs resume <tag>
    if sub == "resume":
        if not tag:
            await update.effective_message.reply_text(
                "Usage: <code>/jobs resume &lt;tag&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        ok = iface.scheduler.resume_job(tag)
        status = f"▶️ Job <code>{html.escape(tag)}</code> resumed." if ok else f"❌ Job <code>{html.escape(tag)}</code> not found."
        await update.effective_message.reply_text(status, parse_mode=ParseMode.HTML)
        jobs = iface.scheduler.list_jobs()
        for chunk in iface._split_message(iface._format_jobs_list(jobs)):
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)
        return

    # Default: list all jobs
    jobs = iface.scheduler.list_jobs()
    for chunk in iface._split_message(iface._format_jobs_list(jobs)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def cmd_agents(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return

    from sub_agent_registry import get_registry as _get_agent_registry

    args = ctx.args or []

    # /agents cancel managed  — atomically cancel all on-demand agents
    if len(args) >= 2 and args[0].lower() == "cancel" and args[1].lower() == "managed":
        count = _get_agent_registry().cancel_all_managed()
        if count == 0:
            await update.effective_message.reply_text(
                "ℹ️ No managed sub-agents are currently running.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.effective_message.reply_text(
                f"🛑 Cancellation requested for <b>{count}</b> managed sub-agent(s).\n"
                f"Any in-progress LLM calls will be interrupted immediately.",
                parse_mode=ParseMode.HTML,
            )
        return

    # /agents cancel <id>
    if args and args[0].lower() == "cancel":
        agent_id = args[1] if len(args) > 1 else ""
        if not agent_id:
            await update.effective_message.reply_text(
                "Usage: /agents cancel &lt;id&gt;\n"
                "       /agents cancel managed",
                parse_mode=ParseMode.HTML,
            )
            return
        ok = _get_agent_registry().cancel(agent_id)
        if ok:
            await update.effective_message.reply_text(
                f"🛑 Cancellation requested for <code>{html.escape(agent_id)}</code>.\n"
                f"Any in-progress LLM call will be interrupted immediately.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.effective_message.reply_text(
                f"❌ No active sub-agent with id <code>{html.escape(agent_id)}</code>.",
                parse_mode=ParseMode.HTML,
            )
        return

    active = _get_agent_registry().list_active()
    if not active:
        await update.effective_message.reply_text(
            "🤖 No sub-agents currently running.\n"
            "<i>Tip: /agents cancel &lt;id&gt; — cancel specific agent\n"
            "/agents cancel managed — cancel all managed agents</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"🤖 <b>Active Sub-Agents</b> ({len(active)})\n"]
    for rec in active:
        tag = "[autonomous]" if rec.source == "scheduled" else "[managed]"
        lines.append(f"<code>{html.escape(rec.agent_id)}</code> <b>{tag}</b>")
        lines.append(f"   Model:   <code>{html.escape(rec.model)}</code>")
        lines.append(f"   Task:    {html.escape(rec.task_preview)}{'…' if len(rec.task_preview) >= 80 else ''}")
        lines.append(f"   Started: {rec.elapsed_str()} ago")
        lines.append(f"   Step:    {rec.iteration}/{rec.max_iterations}")
        if rec.is_cancelled:
            lines.append("   <i>⚠️ Cancellation requested…</i>")
        lines.append("")

    lines.append(
        "<i>Tip: /agents cancel &lt;id&gt; — cancel specific agent\n"
        "/agents cancel managed — cancel all managed agents</i>"
    )
    for chunk in iface._split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def cmd_tools(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    if not iface.tool_registry:
        await update.effective_message.reply_text("Tool registry not available.")
        return

    tools = iface.tool_registry.all()
    if not tools:
        await update.effective_message.reply_text("No tools registered.")
        return

    # Split into categories
    local_tools = [t for t in tools if not t.is_generated and not t.is_mcp]
    generated = [t for t in tools if t.is_generated and not t.is_mcp]
    mcp_tools = [t for t in tools if t.is_mcp]

    def _tool_entry(t) -> str:
        desc = " ".join(html.escape(t.description).split())
        if len(desc) > 80:
            desc = desc[:77] + "…"
        return f"  • <code>{html.escape(t.name)}</code> — {desc}"

    lines = [f"🔧 <b>Available Tools</b> ({len(tools)} total)\n"]
    if local_tools:
        lines.append("<b>Built-in:</b>")
        for t in local_tools:
            lines.append(_tool_entry(t))
    if generated:
        lines.append("\n<b>Generated:</b>")
        for t in generated:
            lines.append(_tool_entry(t))
    if mcp_tools:
        # Group by server
        servers: dict[str, list] = {}
        for t in mcp_tools:
            servers.setdefault(t.server_name, []).append(t)
        lines.append("\n🔌 <b>MCP Tools:</b>")
        for srv_name, srv_tools in servers.items():
            lines.append(f"  <i>{html.escape(srv_name)}</i>")
            for t in srv_tools:
                lines.append(_tool_entry(t))

    for chunk in iface._split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def cmd_skills(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    if not iface.skill_registry:
        await update.effective_message.reply_text("Skills not available.")
        return

    skills = iface.skill_registry.all()
    if not skills:
        await update.effective_message.reply_text("📚 No skills found. Add skill directories under the <code>skills/</code> folder.", parse_mode=ParseMode.HTML)
        return

    lines = [f"📚 <b>Available Skills</b> ({len(skills)} total)\n"]
    for s in skills:
        # Normalize and truncate description
        desc = " ".join(html.escape(s.description).split())
        if len(desc) > 80:
            desc = desc[:77] + "…"
        lines.append(f"  • <b>{html.escape(s.name)}</b> — {desc}")
    for chunk in iface._split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def cmd_mcp(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mcp [list|on|off|info] [name]"""
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    if not iface.mcp_manager:
        await update.effective_message.reply_text(
            "🔌 No MCP servers configured.\n"
            "Add <code>[[mcp_servers]]</code> sections to <code>config.toml</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    args = ctx.args or []
    sub = args[0].lower() if args else "list"
    name = args[1] if len(args) > 1 else ""

    # /mcp on <name>
    if sub == "on":
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
        return

    # /mcp off <name>
    if sub == "off":
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
        return

    # /mcp info <name>
    if sub == "info":
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
        status_icon = {"active": "●", "off": "○", "error": "⚠️"}.get(info["status"], "?")
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
        return

    # /mcp list (default)
    servers = iface.mcp_manager.list_servers()
    if not servers:
        await update.effective_message.reply_text("🔌 No MCP servers configured.")
        return
    lines = ["🔌 <b>MCP Servers</b>\n"]
    for s in servers:
        icon = "●" if s["status"] == "active" else ("○" if s["status"] == "off" else "⚠️")
        tools_str = f"  — {s['tool_count']} tool(s)" if s["tool_count"] else ""
        err_str = "  ⚠️ error" if s["last_error"] else ""
        lines.append(
            f"{icon} <b>{html.escape(s['name'])}</b>"
            f"  [{s['transport']}]  {s['status']}{tools_str}{err_str}"
        )
    lines.append(
        "\n<i>Tip: /mcp on &lt;name&gt; · /mcp off &lt;name&gt; · /mcp info &lt;name&gt;</i>"
    )
    for chunk in iface._split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def cmd_reindex(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    if not iface._tool_index:
        await update.effective_message.reply_text("⚠️ Tool index not available.")
        return

    status_msg = await update.effective_message.reply_text("⏳ Reindexing tools…")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, iface._tool_index.rebuild)

    msg = (
        f"✅ <b>Reindex complete</b>\n"
        f"  Embedded: {result['embedded']}\n"
        f"  Failed: {result['failed']}\n"
        f"  Total: {result['total']}"
    )
    await status_msg.edit_text(msg, parse_mode=ParseMode.HTML)


async def cmd_pair(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    In pairing mode: if the caller is already authorized, generate a
    single-use pairing token. Another user can run /pair <token> to gain access.
    """
    user = update.effective_user
    if iface.security_mode != "pairing":
        await update.effective_message.reply_text("Pairing mode is not enabled.")
        return

    args = ctx.args or []

    if args:
        # Someone submitting a token
        token = args[0].strip()
        redeemed_by = iface._pending_pairs.pop(token, None)
        if redeemed_by is not None:
            iface.allowed_ids.add(user.id)
            logger.info("User %d authorized via pairing token", user.id)
            await update.effective_message.reply_text("✅ Pairing successful! You can now use the agent.")
        else:
            await update.effective_message.reply_text("❌ Invalid or expired pairing token.")
        return

    # Generate a new token (only for already-authorized users)
    if not iface._is_authorized(user.id):
        await iface._send_unauthorized(update)
        return

    token = secrets.token_hex(8)
    iface._pending_pairs[token] = user.id
    logger.info("Pairing token generated by user %d: %s", user.id, token)
    await update.effective_message.reply_text(
        f"🔑 Pairing token (valid until used):\n<code>{html.escape(token)}</code>\n\n"
        "Share this with the user who should gain access. "
        "They should run: <code>/pair &lt;token&gt;</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_unpair(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text("Usage: /unpair <user_id>")
        return
    try:
        target = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid user ID.")
        return
    iface.allowed_ids.discard(target)
    await update.effective_message.reply_text(f"User {target} removed from allowed list.")


async def cmd_myid(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.effective_message.reply_text(f"Your Telegram user ID: <code>{uid}</code>", parse_mode=ParseMode.HTML)


async def cmd_health(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    health_task = (
        "Perform a self-health diagnosis: "
        "(1) Read the last 500 lines of agent.log using file_read with offset=-25000. "
        "(2) Analyze for errors, warnings, repeated failures, and anomalies. "
        "(3) Identify root causes and provide actionable suggestions for each issue. "
        "(4) Report findings as a structured summary."
    )
    await iface._run_agent_task(update, ctx, health_task)


async def cmd_show_ctx(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden command: send the current LLM system prompt as a file attachment."""
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    if not iface.agent:
        await update.effective_message.reply_text("Agent not available.")
        return
    try:
        prompt, tokens = iface.agent.build_system_prompt()
    except Exception as exc:
        await update.effective_message.reply_text(f"Error building context: {exc}")
        return
    buf = io.BytesIO(prompt.encode("utf-8"))
    buf.name = "context.md"
    await update.effective_message.reply_document(
        document=buf,
        filename="context.md",
        caption=f"📋 Current agent context (~{tokens:,} tokens)",
    )


async def cmd_show_env(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden command: show the shell environment available to the agent."""
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return

    _REDACT = {"KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "API", "CREDENTIAL", "AUTH"}

    def _redact(name: str, value: str) -> str:
        if any(kw in name.upper() for kw in _REDACT):
            return "***"
        return value

    env = os.environ
    lines = ["<b>🌐 Shell Environment</b>\n"]

    # PATH — one entry per line
    path_val = env.get("PATH", "")
    if path_val:
        lines.append("<b>PATH:</b>")
        for entry in path_val.split(os.pathsep):
            lines.append(f"  {html.escape(entry)}")
        lines.append("")

    # All other variables (sorted, secrets redacted)
    lines.append("<b>Environment variables:</b>")
    for key in sorted(env.keys()):
        if key == "PATH":
            continue
        val = _redact(key, env[key])
        lines.append(f"  <code>{html.escape(key)}</code> = {html.escape(val)}")

    # Agent-configured paths
    paths_cfg = iface._config.get("paths", {})
    if paths_cfg:
        lines.append("\n<b>Agent paths (from config):</b>")
        for k, v in paths_cfg.items():
            lines.append(f"  <code>{html.escape(k)}</code> = {html.escape(str(v))}")

    text = "\n".join(lines)
    # Telegram max message length is 4096; split if needed
    chunk_size = 4096
    for i in range(0, len(text), chunk_size):
        await update.effective_message.reply_text(text[i:i + chunk_size], parse_mode=ParseMode.HTML)


async def cmd_models(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List configured LLM models and allow switching."""
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    if not iface.llm_client or not hasattr(iface.llm_client, "list_models"):
        await update.effective_message.reply_text("Multi-model support not available.")
        return

    models = iface.llm_client.list_models()

    # Load capabilities for presence indicator (best-effort)
    cap_model_names: set[str] = set()
    try:
        import os
        from mad_plan import load_models_capabilities, validate_models_for_mad_plan
        caps = load_models_capabilities(os.path.join(os.getcwd(), "data"))
        if caps:
            validation = validate_models_for_mad_plan(models, caps)
            cap_model_names = {
                e["model"] for e in validation["with_capabilities"]
            }
    except Exception:
        pass

    lines = [f"🤖 <b>LLM Models</b> ({len(models)} configured)\n"]
    buttons = []
    for m in models:
        active_icon = "✅" if m["active"] else "⬜"
        vision_tag = " 👁" if m.get("vision") else ""
        cap_tag = " 📊" if m.get("model") in cap_model_names else ""
        lines.append(
            f"{active_icon} <b>{html.escape(m['name'])}</b>: "
            f"<code>{html.escape(m['model'])}</code>{vision_tag}{cap_tag}"
        )
        if not m["active"]:
            buttons.append([InlineKeyboardButton(
                f"Switch to {m['name']}",
                callback_data=f"model:{m['name']}",
            )])

    if cap_model_names:
        lines.append("\n<i>📊 = capabilities data available for MadPlan model selection</i>")

    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def cb_confirm(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Yes / No / Approve-all confirmation button presses."""
    query = update.callback_query
    data = query.data  # "confirm_yes:<token>" | "confirm_no:<token>" | "confirm_all:<token>:<tool>"

    if data.startswith("confirm_all:"):
        # Format: confirm_all:{token}:{tool_name}
        parts = data.split(":", 2)
        token = parts[1]
        tool_name = parts[2] if len(parts) > 2 else ""
        logger.info("Approve-all callback: tool=%s token=%s", tool_name, token[:8])
        if iface.agent:
            iface.agent.resume_approve_all(token, tool_name)
        else:
            logger.warning("_cb_confirm: iface.agent is None — cannot resume agent")
        try:
            await query.answer()
        except Exception as exc:
            logger.warning("query.answer() failed: %s", exc)
        result_text = f"✅✅ All future <code>{html.escape(tool_name)}</code> operations in this task auto-approved."
        try:
            await query.edit_message_text(
                f"⚠️ <b>Confirmation</b>\n\n{result_text}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.debug("Could not edit confirmation message: %s", exc)
        return

    confirmed = data.startswith("confirm_yes:")
    token = data.split(":", 1)[1]

    logger.info("Confirmation callback: confirmed=%s token=%s agent=%s",
                confirmed, token[:8], "set" if iface.agent else "None")

    # Resume the agent FIRST — before any Telegram API calls that might fail
    if iface.agent:
        iface.agent.resume(token, confirmed)
    else:
        logger.warning("_cb_confirm: iface.agent is None — cannot resume agent")

    # Acknowledge the button press (best-effort; Telegram requires this within ~10s)
    try:
        await query.answer()
    except Exception as exc:
        logger.warning("query.answer() failed (button may show spinner): %s", exc)

    # Edit the message to reflect the decision (best-effort)
    result_text = "✅ Confirmed — executing…" if confirmed else "❌ Cancelled."
    try:
        await query.edit_message_text(
            f"⚠️ <b>Confirmation</b>\n\n{result_text}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.debug("Could not edit confirmation message: %s", exc)


async def cb_extend(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Extend / Unlimited / Cancel button presses for max-steps extension."""
    query = update.callback_query
    data = query.data  # "extend_yes:<token>" | "extend_unlimited:<token>" | "extend_no:<token>"

    if data.startswith("extend_unlimited:"):
        token = data.split(":", 1)[1]
        response = "unlimited"
        result_text = "♾️ Running until done…"
    elif data.startswith("extend_yes:"):
        token = data.split(":", 1)[1]
        response = "yes"
        result_text = "⏩ Extending…"
    else:
        token = data.split(":", 1)[1]
        response = "no"
        result_text = "❌ Cancelled."

    if iface.agent:
        iface.agent.resume_extend(token, response)
    else:
        logger.warning("_cb_extend: agent is None")

    try:
        await query.answer()
    except Exception as exc:
        logger.warning("query.answer() failed: %s", exc)

    try:
        await query.edit_message_text(
            f"⏱ <b>Max steps</b>\n\n{result_text}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.debug("Could not edit extend message: %s", exc)


async def cb_tool_create(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Create Tool / Run Once / Cancel button presses."""
    query = update.callback_query
    data = query.data
    if data.startswith("tool_create_yes:"):
        action = "create"
        token = data.split(":", 1)[1]
        label = "✅ Creating tool…"
    elif data.startswith("tool_create_run:"):
        action = "run"
        token = data.split(":", 1)[1]
        label = "⚡ Running as one-off script…"
    else:
        action = "cancel"
        token = data.split(":", 1)[1]
        label = "❌ Cancelled."

    if iface.agent:
        iface.agent.resume_tool_create(token, action)
    else:
        logger.warning("_cb_tool_create: agent is None")

    try:
        await query.answer()
    except Exception as exc:
        logger.warning("query.answer() failed: %s", exc)

    try:
        await query.edit_message_text(
            f"🛠️ <b>Tool creation</b>\n\n{label}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.debug("Could not edit tool_create message: %s", exc)


async def cb_model_switch(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle model switch button presses."""
    query = update.callback_query
    await query.answer()
    model_name = query.data.split(":", 1)[1]

    if iface.llm_client and hasattr(iface.llm_client, "set_model"):
        success = iface.llm_client.set_model(model_name)
        if success:
            active = iface.llm_client.llm_cfg
            text = (
                f"✅ Switched to <b>{html.escape(active.get('name', model_name))}</b>"
                f" (<code>{html.escape(model_name)}</code>)\n"
                f"<i>Takes effect from your next message.</i>"
            )
        else:
            text = f"❌ Model <code>{html.escape(model_name)}</code> not found."
    else:
        text = "❌ Model switching not available."

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def cmd_mad_plan(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mad_plan [plan|list|execute] [plan_name] command."""
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return

    text = update.effective_message.text or ""
    # Strip the command itself and parse args
    parts = text.split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) > 1 else ""
    plan_name_arg = parts[2].strip() if len(parts) > 2 else ""

    plans_dir = os.path.join(os.getcwd(), "plans")

    if subcommand == "list":
        from mad_plan import list_plans as _list_plans
        names = _list_plans(plans_dir)
        if not names:
            await update.effective_message.reply_text(
                "📋 <b>MadPlan — Saved Plans</b>\n\n<i>No plans found.</i>",
                parse_mode=ParseMode.HTML,
            )
        else:
            lines = ["📋 <b>MadPlan — Saved Plans</b>\n"]
            for name in names:
                lines.append(f"  • <code>{html.escape(name)}</code>")
            await update.effective_message.reply_text(
                "\n".join(lines), parse_mode=ParseMode.HTML
            )
        return

    if subcommand == "execute":
        if not plan_name_arg:
            await update.effective_message.reply_text(
                "❌ Usage: <code>/mad_plan execute &lt;plan_name&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        user_state = iface._get_user_state(update.effective_user.id)
        user_state.agent_mode = "madplan"
        from mad_plan import MadPlanOrchestrator, MadPlanError
        try:
            plan = MadPlanOrchestrator().load_plan(plan_name_arg, plans_dir)
        except MadPlanError as exc:
            await update.effective_message.reply_text(
                f"❌ Could not load plan: {html.escape(str(exc))}", parse_mode=ParseMode.HTML
            )
            return
        plan["_plan_name"] = plan_name_arg
        user_state.pending_plan = plan
        await iface._run_mad_plan_execute(update, ctx, plan)
        return

    if subcommand == "delete":
        if not plan_name_arg:
            await update.effective_message.reply_text(
                "❌ Usage: <code>/mad_plan delete &lt;plan_name&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        from mad_plan import MadPlanError, list_plans as _list_plans
        names = _list_plans(plans_dir)
        if plan_name_arg not in names:
            await update.effective_message.reply_text(
                f"❌ Plan <code>{html.escape(plan_name_arg)}</code> not found.",
                parse_mode=ParseMode.HTML,
            )
            return
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🗑️ Yes, delete",
                callback_data=f"madplan_delete_confirm:{plan_name_arg}",
            ),
            InlineKeyboardButton("↩️ Cancel", callback_data="madplan_delete_cancel"),
        ]])
        await update.effective_message.reply_text(
            f"⚠️ Delete plan <code>{html.escape(plan_name_arg)}</code>? "
            "This cannot be undone.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    if subcommand in ("plan", ""):
        # Switch to madplan mode; optional plan_name stored for the next message
        user_state = iface._get_user_state(update.effective_user.id)
        user_state.agent_mode = "madplan"
        user_state.pending_plan_name_override = plan_name_arg

        # Validate configured models against capabilities
        validation_text = ""
        try:
            import os as _os
            from mad_plan import load_models_capabilities, validate_models_for_mad_plan
            caps = load_models_capabilities(_os.path.join(_os.getcwd(), "data"))
            if caps and iface.llm_client and hasattr(iface.llm_client, "list_models"):
                configured = iface.llm_client.list_models()
                v = validate_models_for_mad_plan(configured, caps)
                model_lines = ["\n\n📋 <b>Model readiness:</b>"]
                for entry in v["available"]:
                    icon = "✅" if entry["capabilities"] else "⚠️"
                    model_lines.append(
                        f"  {icon} <code>{html.escape(entry['model'])}</code> ({html.escape(entry['name'])})"
                    )
                if v["missing_capabilities"]:
                    model_lines.append("<i>⚠️ = no capabilities data (model still usable)</i>")
                validation_text = "\n".join(model_lines)
        except Exception:
            pass

        await update.effective_message.reply_text(
            "🧠 <b>MadPlan mode active</b>\n\n"
            "Send me a task description and I'll analyse it, decompose it into "
            "sub-tasks, select the best model for each one, and present a plan "
            "for your review before any execution begins.\n\n"
            "<b>Available subcommands:</b>\n"
            "  <code>/mad_plan list</code>            — list saved plans\n"
            "  <code>/mad_plan execute &lt;name&gt;</code> — execute a saved plan\n"
            "  <code>/mad_plan delete &lt;name&gt;</code>  — delete a saved plan\n"
            "  <code>/agent</code>                    — return to standard agent mode\n\n"
            "💡 <i>Just send your task to start planning.</i>"
            + validation_text,
            parse_mode=ParseMode.HTML,
        )
        return

    await update.effective_message.reply_text(
        "❌ Unknown subcommand. Usage:\n"
        "  <code>/mad_plan plan</code> — start planning\n"
        "  <code>/mad_plan list</code> — list saved plans\n"
        "  <code>/mad_plan execute &lt;name&gt;</code> — execute a saved plan\n"
        "  <code>/mad_plan delete &lt;name&gt;</code> — delete a saved plan",
        parse_mode=ParseMode.HTML,
    )


async def cmd_agent(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to standard agent mode."""
    if not iface._is_authorized(update.effective_user.id):
        await iface._send_unauthorized(update)
        return
    user_state = iface._get_user_state(update.effective_user.id)
    user_state.agent_mode = "agent"
    user_state.pending_plan = None
    await update.effective_message.reply_text(
        "🤖 <b>Agent mode active</b>\n<i>Standard execution restored.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cb_mad_plan_review(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle MadPlan plan review buttons: madplan_approve / madplan_reject / madplan_show."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception as exc:
        logger.warning("cb_mad_plan_review query.answer() failed: %s", exc)

    data = query.data
    user_id = query.from_user.id
    user_state = iface._get_user_state(user_id)

    if data.startswith("madplan_approve"):
        plan = user_state.pending_plan
        if not plan:
            try:
                await query.edit_message_text("❌ No active plan.", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return
        # Verify plan identity to prevent approving a stale/replaced plan
        parts = data.split(":", 1)
        if len(parts) > 1:
            expected_id = parts[1]
            if expected_id != user_state.plan_id:
                try:
                    await query.edit_message_text(
                        "⚠️ This plan is outdated. Please review the current plan.",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                return
        try:
            await query.edit_message_text("⚙️ <b>Executing plan…</b>", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await iface._run_mad_plan_execute(update, ctx, plan)

    elif data.startswith("madplan_reject"):
        user_state.pending_plan = None
        user_state.agent_mode = "agent"
        try:
            await query.edit_message_text(
                "❌ <b>Plan rejected.</b> Switched to agent mode.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif data == "madplan_show":
        plan = user_state.pending_plan
        saved_path = plan.get("_saved_path") if plan else None
        if saved_path and os.path.exists(saved_path):
            try:
                with open(saved_path, "rb") as fh:
                    await query.message.reply_document(
                        document=fh,
                        filename=os.path.basename(saved_path),
                    )
            except Exception as exc:
                await query.message.reply_text(
                    f"❌ Could not send plan file: {html.escape(str(exc))}",
                    parse_mode=ParseMode.HTML,
                )
        else:
            await query.message.reply_text(
                "❌ Plan file not found.", parse_mode=ParseMode.HTML
            )

    elif data.startswith("madplan_delete_confirm:"):
        plan_name = data.split(":", 1)[1]
        plans_dir = os.path.join(os.getcwd(), "plans")
        from mad_plan import delete_plan, MadPlanError
        try:
            delete_plan(plan_name, plans_dir)
            try:
                await query.edit_message_text(
                    f"✅ Plan <code>{html.escape(plan_name)}</code> deleted.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        except MadPlanError as exc:
            try:
                await query.edit_message_text(
                    f"❌ {html.escape(str(exc))}", parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    elif data == "madplan_delete_cancel":
        try:
            await query.edit_message_text("↩️ Deletion cancelled.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
