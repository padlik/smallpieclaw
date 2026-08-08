from __future__ import annotations

import asyncio
import functools
import html
import io
import logging
import os
import re as _re
import secrets
import time
from datetime import datetime as _dt
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from sub_agent_registry import get_registry as _get_agent_registry

if TYPE_CHECKING:
    from telegram_interface import TelegramInterface

    assert TelegramInterface

logger = logging.getLogger(__name__)


def _require_auth(fn):
    """Decorator: reject unauthorized callers before running a command."""
    @functools.wraps(fn)
    async def _wrapper(
        iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        if user is None or not iface._is_authorized(user.id):
            await iface._send_unauthorized(update)
            return
        return await fn(iface, update, ctx)
    return _wrapper


def _truncate_desc(text: str, limit: int = 80) -> str:
    """Normalize whitespace, HTML-escape, and truncate for display."""
    normalized = " ".join(html.escape(text).split())
    if len(normalized) > limit:
        return normalized[: limit - 1] + "…"
    return normalized


_ENV_REDACT_KEYWORDS = {"KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "API", "CREDENTIAL", "AUTH"}

_CREDENTIAL_URL_RE = _re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^@\s]{1,200}@")


def _redact_env_var(name: str, value: str) -> str:
    """Return '***' when the var name looks like a secret or value contains embedded credentials."""
    if any(kw in name.upper() for kw in _ENV_REDACT_KEYWORDS):
        return "***"
    if _CREDENTIAL_URL_RE.search(value):
        return "***"
    return value


def _tool_entry(t) -> str:
    """Format one tool as a display line."""
    return f"  • <code>{html.escape(t.name)}</code> — {_truncate_desc(t.description)}"


def _fmt_stat(val: object, suffix: str = "") -> str:
    """Format a stat counter: negative int → 'N/A', int → comma-separated, other → escaped."""
    if isinstance(val, int) and val < 0:
        return "N/A"
    return f"{val:,}{suffix}" if isinstance(val, int) else html.escape(
        str(val) if val is not None else "N/A"
    )


@_require_auth
async def cmd_start(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "👋 Home Server Agent ready.\n"
        "Send me a command like:\n"
        "  • <b>check disk usage</b>\n"
        "  • <b>show CPU temperature</b>\n"
        "  • <b>are my Docker containers running?</b>\n\n"
        "Use /help for more info.",
        parse_mode=ParseMode.HTML,
    )


@_require_auth
async def cmd_help(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
        "  /mode    — set creativity mode (default / planner / explorer / resilient)\n"
        "  /mcp     — manage MCP servers (list / on / off / info)\n"
        "  /jobs    — list scheduled jobs\n"
        "  /prompts — list recent prompts and their status\n"
        "  /reset   — save and clear task context (<code>/reset discard</code> to skip saving)\n"
        "  /pair    — pairing token management\n"
        "  /myid    — show your Telegram user ID\n",
        parse_mode=ParseMode.HTML,
    )


@_require_auth
async def cmd_status(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
    active_agents = _get_agent_registry().count()
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

    # Graph memory state
    graph_memory_line = ""
    _gm_store = getattr(iface, "_graph_memory_store", None)
    _gm_writer = getattr(iface, "_graph_memory_writer", None)
    _gm_cfg = iface._config.get("graph_memory", {})
    if _gm_cfg.get("enabled", False):
        if _gm_store is None:
            graph_memory_line = "\n🧠 Graph Memory: 🔴 failed (check logs)"
        else:
            loop = asyncio.get_running_loop()
            _ss = await loop.run_in_executor(None, _gm_store.get_stats)
            _ws = _gm_writer.get_stats() if _gm_writer is not None else {}
            _ents = _ss.get("entity_count", -1)
            _rels = _ss.get("relation_count", -1)
            _eps = _ss.get("episode_count", -1)
            _hits = _ss.get("retrieval_hits", 0)
            _misses = _ss.get("retrieval_misses", 0)
            _worker_ok = _ws.get("worker_alive", True)
            _q = _ws.get("queue_depth", 0)
            _wfails = _ws.get("write_failures", 0)
            _vidx = _ss.get("vector_index_ok", False)
            # Determine health state
            if _ents == 0 and _eps == 0:
                _health = "active-empty"
                _dot = "🟡"
            elif _hits > 0:
                _health = "active-used"
                _dot = "🟢"
            elif _ws.get("batches_processed", 0) > 0 or _ws.get("episodes_stored", 0) > 0:
                _health = "active-learning"
                _dot = "🟢"
            else:
                _health = "active"
                _dot = "🟢"
            if not _worker_ok or _wfails > 0 or not _vidx:
                _health += "-degraded"
                _dot = "🟠"
            _counts = (
                f"{_ents:,} entities · {_rels:,} facts · {_eps:,} episodes"
                if _ents >= 0 else "counts unavailable"
            )
            _writer_info = f"writer {'ok' if _worker_ok else 'stopped'}, queue {_q}"
            graph_memory_line = (
                f"\n🧠 Graph Memory: {_dot} <code>{html.escape(_health)}</code> | "
                f"{_counts} | "
                f"hits {_hits} / misses {_misses} | {_writer_info}"
            )

    # Current server time
    now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    mode = _current_mode(iface)

    await update.effective_message.reply_text(
        f"✅ <b>Agent Status</b>\n\n"
        f"🕐 Time: <code>{now_str}</code>\n"
        f"⏱ Uptime: <code>{h}h {m}m {s}s</code>\n"
        f"🤖 LLM: <code>{html.escape(llm_model)}</code>\n"
        f"🔍 Embeddings: <code>{html.escape(emb_model)}</code> ({html.escape(emb_key_status)})\n"
        f"🔐 Security: <code>{html.escape(iface.security_mode)}</code>\n"
        f"🎭 Mode: <code>{html.escape(mode)}</code> — <i>{html.escape(_MODE_DESCRIPTIONS.get(mode, ''))}</i>\n"
        f"👥 Authorized users: {len(iface.allowed_ids)}\n"
        f"🔧 Tools: {tools_count} | 📚 Skills: {skills_count}"
        f"{agents_line}"
        f"{scheduler_line}"
        f"{graph_memory_line}"
        f"{token_line}",
        parse_mode=ParseMode.HTML,
    )


@_require_auth
async def cmd_stop(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if iface.agent:
        iface.agent.cancel()
        _get_agent_registry().cancel_all_managed()
        await update.effective_message.reply_text(
            "🛑 Stop signal sent — main agent and all sub-agents cancelling."
        )
    else:
        await update.effective_message.reply_text("ℹ️ No active agent to stop.")


@_require_auth
async def cmd_reset(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []
    discard = "discard" in [a.lower() for a in args]

    status_msg = await update.effective_message.reply_text(
        "🗑️ Discarding task context…" if discard else "💾 Saving task context…"
    )

    if iface.agent_reset_fn:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: iface.agent_reset_fn(save=not discard)
        )
        await iface._safe_edit(status_msg, result)
    else:
        await iface._safe_edit(status_msg, "✅ Context cleared.")


@_require_auth
async def cmd_compress(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    status_msg = await update.effective_message.reply_text("🗜️ Compressing context…")

    if iface.agent_compress_fn:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, iface.agent_compress_fn)
        await iface._safe_edit(status_msg, result)
    else:
        await iface._safe_edit(status_msg, "ℹ️ Compress not available.")


@_require_auth
async def cmd_verbose(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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


_STATUS_ICONS = {
    "running": "🔄",
    "done": "✅",
    "failed": "❌",
    "cancelled": "🛑",
}


def _fmt_prompt_elapsed(started_at: float, ended_at: float | None = None) -> str:
    """Format elapsed time as 'Xm Ys' or 'Ys'."""
    end = ended_at if ended_at is not None else time.time()
    elapsed = max(0, int(end - started_at))
    mins, secs = divmod(elapsed, 60)
    return f"{mins}m {secs}s" if mins else f"{secs}s"


@_require_auth
async def cmd_prompts(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List recent prompts with status, elapsed time, and sub-agent count."""
    registry = getattr(iface, "_prompt_registry", None)
    if registry is None:
        await update.effective_message.reply_text(
            "⚠️ Prompt registry not available.",
            parse_mode=ParseMode.HTML,
        )
        return

    recent = registry.list_recent(20)
    if not recent:
        await update.effective_message.reply_text(
            "ℹ️ No prompts recorded yet.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"📝 <b>Recent Prompts</b> ({len(recent)})\n"]
    for rec in recent:
        icon = _STATUS_ICONS.get(rec.status, "❓")
        elapsed = _fmt_prompt_elapsed(rec.started_at, rec.ended_at)
        sub_count = len(rec.sub_agent_ids)
        sa_info = f" · {sub_count} sub-agent{'s' if sub_count != 1 else ''}" if sub_count else ""
        ts = _dt.fromtimestamp(rec.started_at).strftime("%Y-%m-%d %H:%M")
        text_preview = _truncate_desc(rec.text, 80)
        lines.append(
            f"<code>{html.escape(str(rec.prompt_id))}</code> {icon} <code>{html.escape(rec.status)}</code>\n"
            f"  📅 {ts} · ⏱ {elapsed}{sa_info}\n"
            f"  💬 {text_preview}"
        )

    for chunk in iface._split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


_MODES = ("default", "planner", "explorer", "resilient")
_MODE_DESCRIPTIONS: dict[str, str] = {
    "default": "Standard balanced behavior",
    "planner": "Adds structured planning rules to prompts",
    "explorer": "Adds exploration and investigation rules",
    "resilient": "Adds reflection-on-failure and recovery rules",
}


def _apply_mode(iface: "TelegramInterface", new_mode: str) -> None:
    """Persist ``new_mode`` to the in-memory config and update the live agent.

    Writes ``agent.mode`` and ``agent.creativity_mode`` in the in-memory config
    (this does not touch ``config.toml``, so it reverts to the file value on
    restart) and updates the live agent's ``creativity_mode``. Because each task
    snapshots its mode at start, the change takes effect from the next task.
    """
    agent_cfg = iface._config.setdefault("agent", {})
    agent_cfg["mode"] = new_mode
    agent_cfg["creativity_mode"] = new_mode
    # Update the live agent so the change takes effect from the next task.
    if getattr(iface, "agent", None) is not None:
        try:
            iface.agent.creativity_mode = new_mode
        except Exception:  # noqa: BLE001
            pass


def _current_mode(iface: "TelegramInterface") -> str:
    """Return the effective creativity mode.

    Prefers the live agent's ``creativity_mode``, then the configured
    ``agent.creativity_mode`` (the key the agent is actually built from), then
    the legacy ``agent.mode``, falling back to ``"default"``.
    """
    agent_cfg = iface._config.get("agent", {})
    return (
        getattr(getattr(iface, "agent", None), "creativity_mode", None)
        or agent_cfg.get("creativity_mode")
        or agent_cfg.get("mode")
        or "default"
    )


@_require_auth
async def cmd_mode(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the agent creativity mode, or show a selector when called without args."""
    args = ctx.args or []
    current_mode = _current_mode(iface)

    if args:
        requested = args[0].strip().lower()
        if requested not in _MODE_DESCRIPTIONS:
            valid_list = ", ".join(f"<code>{html.escape(m)}</code>" for m in _MODES)
            await update.effective_message.reply_text(
                f"❌ Unknown mode <code>{html.escape(requested)}</code>.\n"
                f"Valid modes: {valid_list}\n"
                "Usage: <code>/mode</code> to choose from a menu, or <code>/mode &lt;mode&gt;</code> to set explicitly.",
                parse_mode=ParseMode.HTML,
            )
            return
        _apply_mode(iface, requested)
        await update.effective_message.reply_text(
            f"🎭 <b>Mode: {html.escape(requested)}</b>\n"
            f"<i>{html.escape(_MODE_DESCRIPTIONS[requested])}</i>\n"
            f"<i>Takes effect from your next task.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # No args: show current mode and a selector for the other modes.
    lines = [
        f"🎭 <b>Creativity Mode</b> (current: <code>{html.escape(current_mode)}</code>)",
        f"<i>{html.escape(_MODE_DESCRIPTIONS.get(current_mode, ''))}</i>\n",
    ]
    buttons = []
    for name in _MODES:
        active_icon = "✅" if name == current_mode else "⬜"
        lines.append(
            f"{active_icon} <b>{html.escape(name)}</b>: "
            f"<i>{html.escape(_MODE_DESCRIPTIONS.get(name, ''))}</i>"
        )
        if name != current_mode:
            buttons.append([InlineKeyboardButton(
                f"Switch to {name}",
                callback_data=f"mode:{name}",
            )])

    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@_require_auth
async def cmd_jobs(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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


@_require_auth
async def cmd_agents(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
            "<i>Tip: /agents cancel &lt;id&gt; — cancel any visible agent (any source)\n"
            "/agents cancel managed — cancel all capacity-counted agents "
            "(on-demand + scheduled)</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"🤖 <b>Active Sub-Agents</b> ({len(active)})\n"]
    for rec in active:
        tag = f"[{rec.source}]"
        lines.append(f"<code>{html.escape(rec.agent_id)}</code> <b>{html.escape(tag)}</b>")
        lines.append(f"   Model:   <code>{html.escape(rec.model)}</code>")
        lines.append(f"   Task:    {html.escape(rec.task_preview)}{'…' if len(rec.task_preview) >= 80 else ''}")
        lines.append(f"   Started: {rec.elapsed_str()} ago")
        lines.append(f"   Step:    {rec.iteration}/{rec.max_iterations}")
        if rec.is_cancelled:
            lines.append("   <i>⚠️ Cancellation requested…</i>")
        lines.append("")

    lines.append(
        "<i>Tip: /agents cancel &lt;id&gt; — cancel any visible agent (any source)\n"
        "/agents cancel managed — cancel all capacity-counted agents "
        "(on-demand + scheduled)</i>"
    )
    for chunk in iface._split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


@_require_auth
async def cmd_tools(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface.tool_registry:
        await update.effective_message.reply_text("Tool registry not available.")
        return

    tools = iface.tool_registry.all()
    if not tools:
        await update.effective_message.reply_text("No tools registered.")
        return

    # All registered tools are MCP tools now (hand-written tools removed)
    mcp_tools = list(tools)

    lines = [f"🔧 <b>Available Tools</b> ({len(tools)} total)\n"]
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


@_require_auth
async def cmd_skills(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface.skill_registry:
        await update.effective_message.reply_text("Skills not available.")
        return

    skills = iface.skill_registry.all()
    if not skills:
        await update.effective_message.reply_text("📚 No skills found. Add skill directories under the <code>skills/</code> folder.", parse_mode=ParseMode.HTML)
        return

    lines = [f"📚 <b>Available Skills</b> ({len(skills)} total)\n"]
    for s in skills:
        desc = _truncate_desc(s.description)
        lines.append(f"  • <b>{html.escape(s.name)}</b> — {desc}")
    for chunk in iface._split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


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
    name = args[1] if len(args) > 1 else ""

    # /mcp auth [name|status|revoke]
    if sub == "auth":
        if not name:
            await update.effective_message.reply_text(
                "Usage: <code>/mcp auth &lt;name&gt;</code> | "
                "<code>/mcp auth status</code> | "
                "<code>/mcp auth revoke &lt;name&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        if name.lower() == "status":
            await _mcp_auth_status(iface, update, ctx)
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
            await update.effective_message.reply_text(
                f"✅ OAuth flow completed for <code>{html.escape(name)}</code>. "
                f"Server is now active.",
                parse_mode=ParseMode.HTML,
            )
        else:
            error = result.get("error", "OAuth flow failed")
            await update.effective_message.reply_text(
                f"❌ {html.escape(error)}",
                parse_mode=ParseMode.HTML,
            )
        return

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
        return

    # /mcp list (default)
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
        "\n<i>Tip: /mcp on &lt;name&gt; · /mcp off &lt;name&gt; · /mcp info &lt;name&gt;"
        " · /mcp auth &lt;name&gt; · /mcp auth status</i>"
    )
    for chunk in iface._split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)


@_require_auth
async def cmd_reindex(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not iface._tool_index:
        await update.effective_message.reply_text("⚠️ Tool index not available.")
        return

    status_msg = await update.effective_message.reply_text("⏳ Reindexing tools…")
    loop = asyncio.get_running_loop()
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


@_require_auth
async def cmd_unpair(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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


@_require_auth
async def cmd_show_ctx(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden command: send the current LLM system prompt as a file attachment."""
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


@_require_auth
async def cmd_show_env(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden command: show the shell environment available to the agent."""
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
        val = _redact_env_var(key, env[key])
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


@_require_auth
async def cmd_memory(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden command: show graph memory diagnostics (counts, writer health, retrieval stats)."""
    _gm_cfg = iface._config.get("graph_memory", {})
    if not _gm_cfg.get("enabled", False):
        await update.effective_message.reply_text(
            "🧠 <b>Graph Memory</b>\n\nDisabled in config (<code>[graph_memory] enabled = false</code>).",
            parse_mode=ParseMode.HTML,
        )
        return

    _gm_store = getattr(iface, "_graph_memory_store", None)
    _gm_writer = getattr(iface, "_graph_memory_writer", None)

    if _gm_store is None:
        await update.effective_message.reply_text(
            "🧠 <b>Graph Memory</b>\n\n🔴 Initialisation failed — check logs for details.",
            parse_mode=ParseMode.HTML,
        )
        return

    loop = asyncio.get_running_loop()
    ss = await loop.run_in_executor(None, _gm_store.get_stats)
    ws = _gm_writer.get_stats() if _gm_writer is not None else {}

    store_section = (
        "<b>📦 Store</b>\n"
        f"  Entities:  <code>{_fmt_stat(ss.get('entity_count', -1))}</code>\n"
        f"  Facts:     <code>{_fmt_stat(ss.get('relation_count', -1))}</code>\n"
        f"  Episodes:  <code>{_fmt_stat(ss.get('episode_count', -1))}</code>\n"
        f"  Latest:    <code>{html.escape(str(ss.get('latest_episode_ts') or 'none'))}</code>\n"
        f"  Vec index: <code>{'ok' if ss.get('vector_index_ok') else 'error'}</code>"
    )
    if ss.get("stats_error"):
        store_section += f"\n  ⚠️ <i>{html.escape(ss['stats_error'])}</i>"

    worker_alive = ws.get("worker_alive", "N/A")
    writer_section = (
        "<b>✍️ Writer</b>\n"
        f"  Worker:           <code>{'alive' if worker_alive is True else ('stopped' if worker_alive is False else 'N/A')}</code>\n"
        f"  Queue depth:      <code>{_fmt_stat(ws.get('queue_depth', 'N/A'))}</code>\n"
        f"  Pending depth:    <code>{_fmt_stat(ws.get('pending_depth', 'N/A'))}</code>\n"
        f"  Enqueued:         <code>{_fmt_stat(ws.get('enqueued', 0))}</code>\n"
        f"  Skipped short:    <code>{_fmt_stat(ws.get('skipped_short', 0))}</code>\n"
        f"  Batches queued:   <code>{_fmt_stat(ws.get('batches_queued', 0))}</code>\n"
        f"  Batches done:     <code>{_fmt_stat(ws.get('batches_processed', 0))}</code>\n"
        f"  Entities stored:  <code>{_fmt_stat(ws.get('entities_extracted', 0))}</code>\n"
        f"  Facts stored:     <code>{_fmt_stat(ws.get('facts_extracted', 0))}</code>\n"
        f"  Episodes stored:  <code>{_fmt_stat(ws.get('episodes_stored', 0))}</code>\n"
        f"  LLM failures:     <code>{_fmt_stat(ws.get('llm_failures', 0))}</code>\n"
        f"  Parse failures:   <code>{_fmt_stat(ws.get('parse_failures', 0))}</code>\n"
        f"  Write failures:   <code>{_fmt_stat(ws.get('write_failures', 0))}</code>"
    )

    retrieval_section = (
        "<b>🔍 Retrieval</b>\n"
        f"  Hits:             <code>{_fmt_stat(ss.get('retrieval_hits', 0))}</code>\n"
        f"  Misses:           <code>{_fmt_stat(ss.get('retrieval_misses', 0))}</code>\n"
        f"  Injections:       <code>{_fmt_stat(ss.get('context_injections', 0))}</code>"
    )

    text = (
        f"🧠 <b>Graph Memory Diagnostics</b>\n\n"
        f"{store_section}\n\n"
        f"{writer_section}\n\n"
        f"{retrieval_section}"
    )
    chunk_size = 4096
    for i in range(0, len(text), chunk_size):
        await update.effective_message.reply_text(text[i:i + chunk_size], parse_mode=ParseMode.HTML)


@_require_auth
async def cmd_models(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List configured LLM models and allow switching."""
    if not iface.llm_client or not hasattr(iface.llm_client, "list_models"):
        await update.effective_message.reply_text("Multi-model support not available.")
        return

    models = iface.llm_client.list_models()

    lines = [f"🤖 <b>LLM Models</b> ({len(models)} configured)\n"]
    buttons = []
    for m in models:
        active_icon = "✅" if m["active"] else "⬜"
        vision_tag = " 👁" if m.get("vision") else ""
        lines.append(
            f"{active_icon} <b>{html.escape(m['name'])}</b>: "
            f"<code>{html.escape(m['model'])}</code>{vision_tag}"
        )
        if not m["active"]:
            buttons.append([InlineKeyboardButton(
                f"Switch to {m['name']}",
                callback_data=f"model:{m['name']}",
            )])

    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )



@_require_auth
async def cmd_dir(iface: "TelegramInterface", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Operator command to list or remove user-added trusted directories.

    Usage:
      /dir list     -- show user-added trusted dirs
      /dir del N    -- remove entry N (1-based)
      /dir reload   -- reload trusted dirs from disk
    """
    msg = update.effective_message
    if msg is None:
        return

    text = (msg.text or "").strip()
    parts = text.split()
    sub = parts[1].lower() if len(parts) > 1 else "list"

    builtin = getattr(iface, "agent", None)
    builtin = getattr(builtin, "builtin_executor", None) if builtin else None
    checker = getattr(builtin, "trusted_zone_checker", None) if builtin else None

    if checker is None:
        await msg.reply_text("⚠️ Trusted zone checker not available.")
        return

    if sub == "list":
        dirs = checker.list_user_trusted()
        if not dirs:
            await msg.reply_text("No custom trusted directories added yet.")
            return
        lines = [f"  {i}. {d.path} [{d.mode}]  added {d.added[:10]}" for i, d in enumerate(dirs, 1)]
        await msg.reply_text("Trusted directories:\n" + "\n".join(lines))

    elif sub == "del":
        if len(parts) < 3:
            await msg.reply_text("Usage: /dir del N")
            return
        try:
            n = int(parts[2])
        except ValueError:
            await msg.reply_text("Usage: /dir del N (N must be a number)")
            return
        try:
            removed = checker.remove_trusted(n)
            await msg.reply_text(f"Removed: {removed}")
        except IndexError:
            await msg.reply_text(f"No trusted directory #{n}.")

    elif sub == "reload":
        try:
            n = checker.reload_user_trusted()
            await msg.reply_text(f"Reloaded trusted directories from disk: {n} entries.")
        except Exception as exc:
            await msg.reply_text(f"⚠️ Reload failed, kept existing entries: {exc}")

    else:
        await msg.reply_text("Usage: /dir list | /dir del N | /dir reload")
