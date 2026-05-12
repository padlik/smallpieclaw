"""
telegram_interface.py
---------------------
Telegram bot interface with two security modes:
  - allowlist: only pre-configured user IDs may interact
  - pairing:   users request access; owner approves via /pair command
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import logging
import os
import re
import secrets
import time
from datetime import datetime
from typing import Callable, Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)


class TelegramInterface:
    """
    Wraps python-telegram-bot and enforces access control.
    Calls `agent_handler(user_id, text, reply_fn)` for each authorized message.
    """

    def __init__(
        self,
        config: dict,
        agent_handler: Callable,
        agent_reset_fn: Optional[Callable] = None,
        agent_compress_fn: Optional[Callable] = None,
        scheduler=None,
        tool_registry=None,
        llm_client=None,
        tool_index=None,
        skill_registry=None,
        usage_registry=None,
        downloads_dir: str = "downloads",
    ):
        tg_cfg = config["telegram"]
        self._config = config
        self.token: str = tg_cfg["bot_token"]
        self.security_mode: str = tg_cfg.get("security_mode", "allowlist")
        self.allowed_ids: set[int] = {int(uid) for uid in tg_cfg.get("allowed_user_ids", [])}
        self.agent_handler = agent_handler
        self.agent_reset_fn = agent_reset_fn
        self.agent_compress_fn = agent_compress_fn
        self.agent = None  # Set by main.py for resume() support
        self.scheduler = scheduler
        self.tool_registry = tool_registry
        self.llm_client = llm_client
        self._tool_index = tool_index
        self.skill_registry = skill_registry
        self._usage_registry = usage_registry  # TokenUsageRegistry
        self._downloads_dir = os.path.abspath(downloads_dir)
        self._start_time = time.time()

        # Pairing state: {token: user_id}
        self._pending_pairs: dict[str, int] = {}

        # Verbose mode: send each agent action as a new message instead of editing
        self._verbose: bool = False

        self._app: Optional[Application] = None
        # Saved when run() starts — used by send_message_to_users() from threads
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def build(self) -> Application:
        self._app = (
            Application.builder()
            .token(self.token)
            .concurrent_updates(True)  # allow callback queries while agent is running
            .post_init(self._post_init)
            .build()
        )
        self._register_handlers()
        logger.info(
            "Telegram bot built. Security mode: %s. Allowed IDs: %s",
            self.security_mode,
            self.allowed_ids or "(any via pairing)",
        )
        return self._app

    def run(self) -> None:
        """Start polling (blocking)."""
        app = self.build()
        logger.info("Starting Telegram bot polling…")
        # Save the event loop before run_polling takes over — needed for
        # send_message_to_users() which is called from the scheduler thread.
        self._loop = asyncio.get_event_loop()
        app.run_polling(allowed_updates=Update.ALL_TYPES)

    # ------------------------------------------------------------------
    # Post-init (register bot commands)
    # ------------------------------------------------------------------

    async def _post_init(self, app: Application) -> None:
        commands = [
            BotCommand("start", "Introduction and usage examples"),
            BotCommand("help", "Help and command reference"),
            BotCommand("status", "Agent status, uptime, and token usage"),
            BotCommand("health", "Run self-health diagnosis"),
            BotCommand("tools", "List available tools"),
            BotCommand("skills", "List available agent skills"),
            BotCommand("models", "List and switch LLM models"),
            BotCommand("jobs", "List scheduled jobs"),
            BotCommand("agents", "List and manage active sub-agents"),
            BotCommand("reset", "Save and clear current task context"),
            BotCommand("compress", "Summarise and compress agent context"),
            BotCommand("verbose", "Toggle live tool-call progress messages"),
            BotCommand("reindex", "Re-embed all tools in the semantic index"),
            BotCommand("pair", "Generate or submit pairing token"),
            BotCommand("unpair", "Remove a user from access list"),
            BotCommand("myid", "Show your Telegram user ID"),
        ]
        try:
            await app.bot.set_my_commands(commands)
            logger.info("Bot commands registered with Telegram.")
        except Exception as exc:
            logger.warning("Could not register bot commands: %s", exc)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        app = self._app
        app.add_handler(CommandHandler("stop", self._cmd_stop))
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("health", self._cmd_health))
        app.add_handler(CommandHandler("reset", self._cmd_reset))
        app.add_handler(CommandHandler("compress", self._cmd_compress))
        app.add_handler(CommandHandler("verbose", self._cmd_verbose))
        app.add_handler(CommandHandler("jobs", self._cmd_jobs))
        app.add_handler(CommandHandler("tools", self._cmd_tools))
        app.add_handler(CommandHandler("skills", self._cmd_skills))
        app.add_handler(CommandHandler("models", self._cmd_models))
        app.add_handler(CommandHandler("reindex", self._cmd_reindex))
        app.add_handler(CommandHandler("pair", self._cmd_pair))
        app.add_handler(CommandHandler("unpair", self._cmd_unpair))
        app.add_handler(CommandHandler("myid", self._cmd_myid))
        app.add_handler(CommandHandler("agents", self._cmd_agents))
        # Hidden diagnostic commands (not registered with BotFather)
        app.add_handler(CommandHandler("show_ctx", self._cmd_show_ctx))
        app.add_handler(CommandHandler("show_env", self._cmd_show_env))
        # Inline button callbacks
        app.add_handler(CallbackQueryHandler(self._cb_model_switch, pattern=r"^model:"))
        app.add_handler(CallbackQueryHandler(self._cb_confirm, pattern=r"^confirm_(yes|no):"))
        app.add_handler(CallbackQueryHandler(self._cb_extend, pattern=r"^extend_(yes|no):"))
        app.add_handler(CallbackQueryHandler(self._cb_tool_create, pattern=r"^tool_create_"))
        # File upload handlers (document, photo, audio, video, voice)
        app.add_handler(MessageHandler(filters.Document.ALL, self._on_file))
        app.add_handler(MessageHandler(filters.PHOTO, self._on_file))
        app.add_handler(MessageHandler(filters.AUDIO, self._on_file))
        app.add_handler(MessageHandler(filters.VIDEO, self._on_file))
        app.add_handler(MessageHandler(filters.VOICE, self._on_file))
        # Catch-all text messages
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        # Global error handler — catches unhandled exceptions in any handler
        app.add_error_handler(self._error_handler)

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_authorized(user.id):
            await self._send_unauthorized(update)
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

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
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
            "  /jobs    — list scheduled jobs\n"
            "  /reset   — save and clear task context (<code>/reset discard</code> to skip saving)\n"
            "  /pair    — pairing token management\n"
            "  /myid    — show your Telegram user ID\n",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return

        uptime_secs = int(time.time() - self._start_time)
        h = uptime_secs // 3600
        m = (uptime_secs % 3600) // 60
        s = uptime_secs % 60

        if self.llm_client:
            active = self.llm_client.llm_cfg
            llm_model = f"{active.get('name', '')} / {active.get('model', 'N/A')}".lstrip("/ ")
        else:
            llm_model = "N/A"
        emb_cfg = self._config.get("embeddings", {})
        emb_model = emb_cfg.get("model", "N/A")
        emb_key_status = "own key" if emb_cfg.get("api_key") else "using active model key (fallback)"

        tools_count = len(self.tool_registry.all()) if self.tool_registry else 0
        skills_count = self.skill_registry.count() if self.skill_registry else 0

        # Sub-agent count
        from sub_agent_registry import get_registry as _get_agents
        active_agents = _get_agents().count()
        agents_line = f"\n🤖 Sub-agents: {active_agents} running" if active_agents > 0 else ""

        # Per-model token usage from shared registry
        token_line = ""
        if self._usage_registry:
            today_usage = self._usage_registry.get_today()
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
                totals = self._usage_registry.get_today_totals()
                rows.append(
                    f"  {'─' * (col + 2)}\n"
                    f"  {'Total':<{col}}  "
                    f"{totals['prompt']:,} + {totals['completion']:,} = {totals['total']:,}"
                )
                token_line = "\n📊 <b>Token Usage Today (prompt + completion = total):</b>\n<pre>" + "\n".join(rows) + "</pre>"
        elif self.llm_client:
            usage = self.llm_client.get_today_usage()
            token_line = (
                f"\n📊 <b>Token Usage Today:</b>\n"
                f"  Prompt: {usage['prompt_tokens']:,}\n"
                f"  Completion: {usage['completion_tokens']:,}\n"
                f"  Total: {usage['total_tokens']:,}"
            )

        # Scheduler state
        scheduler_line = ""
        if self.scheduler:
            jobs = self.scheduler.list_jobs()
            enabled = sum(1 for j in jobs if j.get("enabled", True))
            total = len(jobs)
            sched_state = "enabled" if self.scheduler.enabled else "disabled"
            scheduler_line = f"\n📅 Scheduler: <code>{sched_state}</code> | {enabled}/{total} jobs active"

        # Current server time
        from datetime import datetime as _dt
        now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

        await update.effective_message.reply_text(
            f"✅ <b>Agent Status</b>\n\n"
            f"🕐 Time: <code>{now_str}</code>\n"
            f"⏱ Uptime: <code>{h}h {m}m {s}s</code>\n"
            f"🤖 LLM: <code>{html.escape(llm_model)}</code>\n"
            f"🔍 Embeddings: <code>{html.escape(emb_model)}</code> ({html.escape(emb_key_status)})\n"
            f"🔐 Security: <code>{html.escape(self.security_mode)}</code>\n"
            f"👥 Authorized users: {len(self.allowed_ids)}\n"
            f"🔧 Tools: {tools_count} | 📚 Skills: {skills_count}"
            f"{agents_line}"
            f"{scheduler_line}"
            f"{token_line}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        if self.agent:
            self.agent.cancel()
            await update.effective_message.reply_text(
                "🛑 Stop signal sent — current task will end after the current step."
            )
        else:
            await update.effective_message.reply_text("ℹ️ No active agent to stop.")

    async def _cmd_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        args = ctx.args or []
        discard = "discard" in [a.lower() for a in args]

        status_msg = await update.effective_message.reply_text(
            "🗑️ Discarding task context…" if discard else "💾 Saving task context…"
        )

        if self.agent_reset_fn:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: self.agent_reset_fn(save=not discard)
            )
            await self._safe_edit(status_msg, result)
        else:
            await self._safe_edit(status_msg, "✅ Context cleared.")

    async def _cmd_compress(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return

        status_msg = await update.effective_message.reply_text("🗜️ Compressing context…")

        if self.agent_compress_fn:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self.agent_compress_fn)
            await self._safe_edit(status_msg, result)
        else:
            await self._safe_edit(status_msg, "ℹ️ Compress not available.")

    @staticmethod
    def _format_jobs_list(jobs: list) -> str:
        """Render a list of job dicts (from scheduler.list_jobs()) as HTML."""
        if not jobs:
            return "No scheduled jobs configured."
        lines = [f"📅 <b>Scheduled Jobs</b> ({len(jobs)} total)\n"]
        for job in jobs:
            is_running = job.get("is_running", False)
            if is_running:
                icon = "🔄"
            elif job["enabled"]:
                icon = "✅"
            else:
                icon = "⏸"
            last_run = job.get("last_run") or "never"
            next_run = job.get("next_run")
            stype = job.get("schedule_type", "cron")
            task_label = "🔔 Message" if stype == "once" else "📝 Task"
            task_text = job.get("task", "")
            task_display = html.escape(task_text[:300] + ("…" if len(task_text) > 300 else ""))
            tag_line = f"{icon} <code>{html.escape(job['tag'])}</code>"
            if is_running:
                tag_line += " <i>[running]</i>"
            lines.append(tag_line)
            lines.append(f"   Schedule: {html.escape(job['schedule'])}")
            lines.append(f"   Last run: {html.escape(str(last_run))}")
            if next_run:
                try:
                    nr = datetime.fromisoformat(next_run).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    nr = next_run
                lines.append(f"   Next run: {html.escape(nr)}")
            if job.get("model"):
                lines.append(f"   Model: <code>{html.escape(job['model'])}</code>")
            if job.get("fallback_models"):
                fb_str = ", ".join(f"<code>{html.escape(m)}</code>" for m in job["fallback_models"])
                lines.append(f"   Fallbacks: {fb_str}")
            elif job.get("fallback_models") == []:
                lines.append("   Fallbacks: <i>disabled</i>")
            if job.get("preserve_context"):
                lines.append("   🧠 Context: preserved between runs")
            if job.get("last_error"):
                lines.append(f"   ⚠️ Last error: {html.escape(str(job['last_error'])[:120])}")
            lines.append(f"   {task_label}: {task_display}\n")
        lines.append(
            "<i>Tip: /jobs reload · /jobs remove &lt;tag&gt; · /jobs pause &lt;tag&gt; · /jobs resume &lt;tag&gt;</i>"
        )
        return "\n".join(lines)

    async def _cmd_verbose(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        args = ctx.args or []
        if args:
            sub = args[0].lower()
            if sub == "on":
                self._verbose = True
            elif sub == "off":
                self._verbose = False
            else:
                await update.effective_message.reply_text(
                    "Usage: <code>/verbose on</code> or <code>/verbose off</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
        else:
            self._verbose = not self._verbose  # toggle
        if self._verbose:
            text = (
                "🔊 <b>Verbose mode on</b>\n"
                "<i>Each tool call and result will be sent as a separate message during task execution.</i>"
            )
        else:
            text = "🔇 <b>Verbose mode off</b>\n<i>Progress updates will edit a single status message.</i>"
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_jobs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        if not self.scheduler:
            await update.effective_message.reply_text("Scheduler not available.")
            return

        args = ctx.args or []
        sub = args[0].lower() if args else ""
        tag = args[1] if len(args) > 1 else ""

        # /jobs reload — hot-reload scheduler.toml from disk
        if sub == "reload":
            result = self.scheduler.reload()
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
            ok = self.scheduler.remove_job(tag)
            status = f"🗑 Job <code>{html.escape(tag)}</code> removed." if ok else f"❌ Job <code>{html.escape(tag)}</code> not found."
            await update.effective_message.reply_text(status, parse_mode=ParseMode.HTML)
            jobs = self.scheduler.list_jobs()
            for chunk in self._split_message(self._format_jobs_list(jobs)):
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
            ok = self.scheduler.pause_job(tag)
            status = f"⏸ Job <code>{html.escape(tag)}</code> paused." if ok else f"❌ Job <code>{html.escape(tag)}</code> not found."
            await update.effective_message.reply_text(status, parse_mode=ParseMode.HTML)
            jobs = self.scheduler.list_jobs()
            for chunk in self._split_message(self._format_jobs_list(jobs)):
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
            ok = self.scheduler.resume_job(tag)
            status = f"▶️ Job <code>{html.escape(tag)}</code> resumed." if ok else f"❌ Job <code>{html.escape(tag)}</code> not found."
            await update.effective_message.reply_text(status, parse_mode=ParseMode.HTML)
            jobs = self.scheduler.list_jobs()
            for chunk in self._split_message(self._format_jobs_list(jobs)):
                await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)
            return

        # Default: list all jobs
        jobs = self.scheduler.list_jobs()
        for chunk in self._split_message(self._format_jobs_list(jobs)):
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)

    async def _cmd_agents(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
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
        for chunk in self._split_message("\n".join(lines)):
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)

    async def _cmd_tools(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        if not self.tool_registry:
            await update.effective_message.reply_text("Tool registry not available.")
            return

        tools = self.tool_registry.all()
        if not tools:
            await update.effective_message.reply_text("No tools registered.")
            return

        builtin = [t for t in tools if not t.is_generated]
        generated = [t for t in tools if t.is_generated]

        def _tool_entry(t) -> str:
            # Normalize and truncate description
            desc = " ".join(html.escape(t.description).split())
            if len(desc) > 80:
                desc = desc[:77] + "…"
            return f"  • <code>{html.escape(t.name)}</code> — {desc}"

        lines = [f"🔧 <b>Available Tools</b> ({len(tools)} total)\n"]
        if builtin:
            lines.append("<b>Built-in:</b>")
            for t in builtin:
                lines.append(_tool_entry(t))
        if generated:
            lines.append("\n<b>Generated:</b>")
            for t in generated:
                lines.append(_tool_entry(t))

        for chunk in self._split_message("\n".join(lines)):
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)

    async def _cmd_skills(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        if not self.skill_registry:
            await update.effective_message.reply_text("Skills not available.")
            return

        skills = self.skill_registry.all()
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
        for chunk in self._split_message("\n".join(lines)):
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML)

    async def _cmd_reindex(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        if not self._tool_index:
            await update.effective_message.reply_text("⚠️ Tool index not available.")
            return

        status_msg = await update.effective_message.reply_text("⏳ Reindexing tools…")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._tool_index.rebuild)

        msg = (
            f"✅ <b>Reindex complete</b>\n"
            f"  Embedded: {result['embedded']}\n"
            f"  Failed: {result['failed']}\n"
            f"  Total: {result['total']}"
        )
        await status_msg.edit_text(msg, parse_mode=ParseMode.HTML)

    async def _cmd_pair(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """
        In pairing mode: if the caller is already authorized, generate a
        single-use pairing token. Another user can run /pair <token> to gain access.
        """
        user = update.effective_user
        if self.security_mode != "pairing":
            await update.effective_message.reply_text("Pairing mode is not enabled.")
            return

        args = ctx.args or []

        if args:
            # Someone submitting a token
            token = args[0].strip()
            redeemed_by = self._pending_pairs.pop(token, None)
            if redeemed_by is not None:
                self.allowed_ids.add(user.id)
                logger.info("User %d authorized via pairing token", user.id)
                await update.effective_message.reply_text("✅ Pairing successful! You can now use the agent.")
            else:
                await update.effective_message.reply_text("❌ Invalid or expired pairing token.")
            return

        # Generate a new token (only for already-authorized users)
        if not self._is_authorized(user.id):
            await self._send_unauthorized(update)
            return

        token = secrets.token_hex(8)
        self._pending_pairs[token] = user.id
        logger.info("Pairing token generated by user %d: %s", user.id, token)
        await update.effective_message.reply_text(
            f"🔑 Pairing token (valid until used):\n<code>{html.escape(token)}</code>\n\n"
            "Share this with the user who should gain access. "
            "They should run: <code>/pair &lt;token&gt;</code>",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_unpair(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
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
        self.allowed_ids.discard(target)
        await update.effective_message.reply_text(f"User {target} removed from allowed list.")

    async def _cmd_myid(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        await update.effective_message.reply_text(f"Your Telegram user ID: <code>{uid}</code>", parse_mode=ParseMode.HTML)

    async def _cmd_health(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        health_task = (
            "Perform a self-health diagnosis: "
            "(1) Read the last 500 lines of agent.log using file_read with offset=-25000. "
            "(2) Analyze for errors, warnings, repeated failures, and anomalies. "
            "(3) Identify root causes and provide actionable suggestions for each issue. "
            "(4) Report findings as a structured summary."
        )
        await self._run_agent_task(update, ctx, health_task)

    async def _cmd_show_ctx(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Hidden command: send the current LLM system prompt as a file attachment."""
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        if not self.agent:
            await update.effective_message.reply_text("Agent not available.")
            return
        try:
            prompt, tokens = self.agent.build_system_prompt()
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

    async def _cmd_show_env(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Hidden command: show the shell environment available to the agent."""
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
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
        paths_cfg = self._config.get("paths", {})
        if paths_cfg:
            lines.append("\n<b>Agent paths (from config):</b>")
            for k, v in paths_cfg.items():
                lines.append(f"  <code>{html.escape(k)}</code> = {html.escape(str(v))}")

        text = "\n".join(lines)
        # Telegram max message length is 4096; split if needed
        chunk_size = 4096
        for i in range(0, len(text), chunk_size):
            await update.effective_message.reply_text(text[i:i + chunk_size], parse_mode=ParseMode.HTML)

    async def _on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming files (documents, photos, audio, video, voice)."""
        if not update.effective_message:
            return
        user = update.effective_user
        if not self._is_authorized(user.id):
            await self._send_unauthorized(update)
            return

        msg = update.effective_message
        os.makedirs(self._downloads_dir, exist_ok=True)

        # Determine telegram file object + desired filename
        tg_file_id: str | None = None
        filename: str | None = None

        if msg.document:
            tg_file_id = msg.document.file_id
            filename = msg.document.file_name or f"document_{msg.document.file_unique_id}"
        elif msg.photo:
            # Telegram sends multiple resolutions; take the largest (last entry)
            best = msg.photo[-1]
            tg_file_id = best.file_id
            filename = f"photo_{best.file_unique_id}.jpg"
        elif msg.audio:
            tg_file_id = msg.audio.file_id
            filename = msg.audio.file_name or f"audio_{msg.audio.file_unique_id}"
        elif msg.video:
            tg_file_id = msg.video.file_id
            filename = msg.video.file_name or f"video_{msg.video.file_unique_id}.mp4"
        elif msg.voice:
            tg_file_id = msg.voice.file_id
            filename = f"voice_{msg.voice.file_unique_id}.ogg"

        if not tg_file_id or not filename:
            await msg.reply_text("⚠️ Unsupported file type.")
            return

        # Deduplicate: foo.pdf → foo_2.pdf → foo_3.pdf …
        dest = os.path.join(self._downloads_dir, filename)
        if os.path.exists(dest):
            base, ext = os.path.splitext(filename)
            counter = 2
            while os.path.exists(dest):
                dest = os.path.join(self._downloads_dir, f"{base}_{counter}{ext}")
                counter += 1

        status_msg = await msg.reply_text("📥 Downloading…")
        try:
            tg_file = await ctx.bot.get_file(tg_file_id)
            await tg_file.download_to_drive(dest)
            size_kb = os.path.getsize(dest) / 1024
            size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
            logger.info("File upload from user %d: %s (%s)", user.id, dest, size_str)

            # If the message is an image (photo or image document) with a caption,
            # forward the caption + image to the agent instead of just confirming save.
            caption = (msg.caption or "").strip()
            is_image = bool(msg.photo) or (
                msg.document and (msg.document.mime_type or "").startswith("image/")
            )
            if caption and is_image:
                await self._safe_edit(status_msg, f"📥 Saved ({size_str}) — sending to agent…")
                await self._run_agent_task(update, ctx, caption, images=[dest])
            else:
                await self._safe_edit(
                    status_msg,
                    f"📥 Saved: <code>{html.escape(dest)}</code> ({size_str})",
                )
        except Exception as exc:
            logger.exception("File download failed for user %d", user.id)
            await self._safe_edit(status_msg, f"❌ Download failed: {html.escape(str(exc))}")

    async def _on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        user = update.effective_user
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        if not self._is_authorized(user.id):
            await self._send_unauthorized(update)
            return
        logger.info("Message from user %d: %s", user.id, text[:80])
        await self._run_agent_task(update, ctx, text)

    async def _run_agent_task(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, task_text: str,
        images: Optional[list[str]] = None,
    ) -> None:
        """Run the agent with a given task, showing streaming progress."""
        user = update.effective_user
        status_msg = await update.effective_message.reply_text("🔄 Processing…")
        loop = asyncio.get_running_loop()
        chat_id = update.effective_chat.id

        # Keep "typing…" indicator alive while the agent is working
        async def _typing_loop():
            while True:
                try:
                    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except Exception:
                    pass
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(_typing_loop())

        def progress(msg: str):
            if msg.startswith("__CONFIRM__:"):
                # Format: __CONFIRM__:{token}:{tool_name}:{description}
                parts = msg.split(":", 3)
                token = parts[1]
                tool_name = parts[2] if len(parts) > 2 else ""
                description = parts[3] if len(parts) > 3 else tool_name
                asyncio.run_coroutine_threadsafe(
                    self._send_confirmation_prompt(update.effective_message, token, tool_name, description),
                    loop,
                )
                return
            if msg.startswith("__EXTEND__:"):
                parts = msg.split(":", 2)
                token = parts[1]
                current_steps = parts[2] if len(parts) > 2 else "?"
                asyncio.run_coroutine_threadsafe(
                    self._send_extend_prompt(update.effective_message, token, current_steps),
                    loop,
                )
                return
            if msg.startswith("__TOOL_CREATE__:"):
                token = msg.split(":", 1)[1]
                asyncio.run_coroutine_threadsafe(
                    self._send_tool_create_prompt(update.effective_message, token),
                    loop,
                )
                return
            if msg.startswith("__FILE__:"):
                _, path_b64, caption_b64 = msg.split(":", 2)
                try:
                    file_path = base64.b64decode(path_b64).decode()
                    caption = base64.b64decode(caption_b64).decode()
                except Exception:
                    file_path, caption = "", ""
                if file_path:
                    asyncio.run_coroutine_threadsafe(
                        self._send_file_to_chat(update.effective_message, file_path, caption),
                        loop,
                    )
                return
            if self._verbose and any(msg.startswith(p) for p in _VERBOSE_EVENT_PREFIXES):
                asyncio.run_coroutine_threadsafe(
                    self._send_verbose_event(ctx.bot, chat_id, msg),
                    loop,
                )
            else:
                asyncio.run_coroutine_threadsafe(
                    self._safe_edit(status_msg, msg),
                    loop,
                )

        try:
            result = await loop.run_in_executor(
                None,
                lambda: self.agent_handler(user.id, task_text, progress, images=images),
            )
            await self._safe_edit(status_msg, "✅ Done")
            for chunk in self._split_message(result):
                await self._send_safe(update.effective_message, chunk)
        except Exception as exc:
            logger.exception("Agent error for user %d", user.id)
            await self._safe_edit(status_msg, f"❌ Error: {exc}")
        finally:
            typing_task.cancel()

    async def _send_verbose_event(self, bot, chat_id: int, text: str) -> None:
        """Send a verbose progress event as a new top-level message (not a reply)."""
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=_md_to_html(text)[:4096],
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            try:
                await bot.send_message(chat_id=chat_id, text=text[:4096])
            except Exception:
                pass

    async def _send_file_to_chat(self, message, file_path: str, caption: str) -> None:
        """Send a local file or photo to the chat (called from the progress callback)."""
        import os
        _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
        _MAX_CAPTION = 1024
        ext = os.path.splitext(file_path)[1].lower()
        if caption and len(caption) > _MAX_CAPTION:
            caption = caption[:_MAX_CAPTION - 3] + "…"
        try:
            with open(file_path, "rb") as f:
                if ext in _IMAGE_EXTS:
                    await message.reply_photo(photo=f, caption=caption or None)
                else:
                    await message.reply_document(document=f, caption=caption or None)
        except FileNotFoundError:
            await message.reply_text(
                f"❌ File not found: <code>{html.escape(file_path)}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.error("Failed to send file %s: %s", file_path, exc)
            await message.reply_text(
                f"❌ Could not send file: {html.escape(str(exc))}",
                parse_mode=ParseMode.HTML,
            )

    async def _send_confirmation_prompt(self, message, token: str, tool_name: str, description: str) -> None:
        """Send an inline-button confirmation prompt for a dangerous operation."""
        approve_all_label = f"✅✅ Approve all {tool_name}" if tool_name else "✅✅ Approve all"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, execute", callback_data=f"confirm_yes:{token}"),
            InlineKeyboardButton("❌ No, cancel",   callback_data=f"confirm_no:{token}"),
        ], [
            InlineKeyboardButton(approve_all_label, callback_data=f"confirm_all:{token}:{tool_name}"),
        ]])
        await message.reply_text(
            f"⚠️ <b>Confirmation required</b>\n\n{_md_to_html(description)}",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    async def _cb_confirm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle Yes / No / Approve-all confirmation button presses."""
        query = update.callback_query
        data = query.data  # "confirm_yes:<token>" | "confirm_no:<token>" | "confirm_all:<token>:<tool>"

        if data.startswith("confirm_all:"):
            # Format: confirm_all:{token}:{tool_name}
            parts = data.split(":", 2)
            token = parts[1]
            tool_name = parts[2] if len(parts) > 2 else ""
            logger.info("Approve-all callback: tool=%s token=%s", tool_name, token[:8])
            if self.agent:
                self.agent.resume_approve_all(token, tool_name)
            else:
                logger.warning("_cb_confirm: self.agent is None — cannot resume agent")
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
                    confirmed, token[:8], "set" if self.agent else "None")

        # Resume the agent FIRST — before any Telegram API calls that might fail
        if self.agent:
            self.agent.resume(token, confirmed)
        else:
            logger.warning("_cb_confirm: self.agent is None — cannot resume agent")

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

    async def _send_extend_prompt(self, message, token: str, current_steps: str) -> None:
        """Send inline buttons asking whether to extend the agent step limit."""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏩ Extend 10 more steps", callback_data=f"extend_yes:{token}"),
            InlineKeyboardButton("♾️ Run until done",       callback_data=f"extend_unlimited:{token}"),
            InlineKeyboardButton("❌ Cancel",               callback_data=f"extend_no:{token}"),
        ]])
        await message.reply_text(
            f"⏱ <b>Max steps reached</b> ({current_steps} steps)\n\n"
            "The agent hasn't finished yet. Extend by 10 more steps, run until done, or cancel?",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    async def _cb_extend(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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

        if self.agent:
            self.agent.resume_extend(token, response)
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

    async def _send_tool_create_prompt(self, message, token: str) -> None:
        """Show tool code to operator with 3-way choice: Create / Run Once / Cancel."""
        if not self.agent:
            return
        data = self.agent.get_pending_tool_create(token)
        if not data:
            return
        name = html.escape(data.get("name", "?"))
        lang = html.escape(data.get("language", "?"))
        desc = html.escape(data.get("description", ""))
        code = html.escape(data.get("code", ""))
        # Truncate code display to avoid Telegram message size limit
        code_display = code[:2000] + ("\n…(truncated)" if len(code) > 2000 else "")
        text = (
            f"🛠️ <b>Tool creation request</b>\n\n"
            f"<b>Name:</b> <code>{name}</code>\n"
            f"<b>Language:</b> {lang}\n"
            f"<b>Description:</b> {desc}\n\n"
            f"<b>Code:</b>\n<pre><code>{code_display}</code></pre>"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Create Tool", callback_data=f"tool_create_yes:{token}"),
            InlineKeyboardButton("⚡ Run Once",   callback_data=f"tool_create_run:{token}"),
            InlineKeyboardButton("❌ Cancel",      callback_data=f"tool_create_no:{token}"),
        ]])
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def _cb_tool_create(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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

        if self.agent:
            self.agent.resume_tool_create(token, action)
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

    async def _error_handler(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Global PTB error handler — logs with full context and notifies users."""
        logger.error("Unhandled exception in update handler", exc_info=ctx.error)
        # Try to notify the user who triggered the error
        try:
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "❌ An unexpected error occurred. Please try again or use /reset.",
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            pass

    async def _cmd_models(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """List configured LLM models and allow switching."""
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        if not self.llm_client or not hasattr(self.llm_client, "list_models"):
            await update.effective_message.reply_text("Multi-model support not available.")
            return

        models = self.llm_client.list_models()
        lines = [f"🤖 <b>LLM Models</b> ({len(models)} configured)\n"]
        buttons = []
        for m in models:
            icon = "✅" if m["active"] else "⬜"
            vision_tag = " 👁" if m.get("vision") else ""
            lines.append(
                f"{icon} <b>{html.escape(m['name'])}</b>: <code>{html.escape(m['model'])}</code>{vision_tag}"
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

    async def _cb_model_switch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle model switch button presses."""
        query = update.callback_query
        await query.answer()
        model_name = query.data.split(":", 1)[1]

        if self.llm_client and hasattr(self.llm_client, "set_model"):
            success = self.llm_client.set_model(model_name)
            if success:
                active = self.llm_client.llm_cfg
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _safe_edit(self, message, text: str) -> None:
        try:
            await message.edit_text(_md_to_html(text)[:4096], parse_mode=ParseMode.HTML)
        except Exception:
            try:
                await message.edit_text(text[:4096])
            except Exception:
                pass

    @staticmethod
    async def _send_safe(message, text: str) -> None:
        """Convert Markdown to HTML and send; fall back to plain text on any error."""
        try:
            await message.reply_text(_md_to_html(text), parse_mode=ParseMode.HTML)
        except Exception:
            await message.reply_text(text)

    def _is_authorized(self, user_id: int) -> bool:
        if self.security_mode == "allowlist":
            return user_id in self.allowed_ids
        elif self.security_mode == "pairing":
            return user_id in self.allowed_ids
        return False

    async def _send_unauthorized(self, update: Update) -> None:
        uid = update.effective_user.id
        logger.warning("Unauthorized access attempt from user %d", uid)
        if self.security_mode == "pairing":
            await update.effective_message.reply_text(
                "🔒 Access denied.\n"
                "Ask an authorized user for a pairing token and run:\n"
                "<code>/pair &lt;token&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.effective_message.reply_text(
                f"🔒 Access denied. Your ID is <code>{uid}</code>.\n"
                "Ask the admin to add it to the allowed list.",
                parse_mode=ParseMode.HTML,
            )

    @staticmethod
    def _split_message(text: str, limit: int = 4000) -> list[str]:
        """
        Split text into chunks of at most `limit` characters.
        Tries to split at paragraph boundaries (\\n\\n), then line boundaries (\\n),
        then word boundaries, to avoid cutting mid-sentence or mid-HTML-tag.

        Each chunk is passed through ``_sanitize_html`` to close any HTML tags that
        were opened in the chunk but not yet closed (e.g. ``<b>`` split across a
        chunk boundary), preventing Telegram API "can't parse entities" errors.

        ``_sanitize_html`` can append synthetic close tags after the split point,
        inflating the chunk length.  To guarantee the final chunk never exceeds
        ``limit``, we split against ``effective`` = ``limit`` minus the worst-case
        close-tag overhead (all 8 tracked tags open at once: ~46 chars → 48 buffer).
        """
        # Maximum extra chars _sanitize_html may append (one </tag> per tracked tag)
        _MAX_TAG_OVERHEAD = 48
        effective = limit - _MAX_TAG_OVERHEAD

        if len(text) <= effective:
            return [_sanitize_html(text)]

        parts = []
        while len(text) > effective:
            chunk = text[:effective]
            # Try to split at a paragraph break
            split_at = chunk.rfind("\n\n")
            if split_at > effective // 2:
                parts.append(_sanitize_html(text[:split_at].rstrip()))
                text = text[split_at:].lstrip("\n")
                continue
            # Try to split at a line break
            split_at = chunk.rfind("\n")
            if split_at > effective // 2:
                parts.append(_sanitize_html(text[:split_at].rstrip()))
                text = text[split_at:].lstrip("\n")
                continue
            # Try to split at a word boundary
            split_at = chunk.rfind(" ")
            if split_at > effective // 2:
                parts.append(_sanitize_html(text[:split_at].rstrip()))
                text = text[split_at:].lstrip(" ")
                continue
            # Hard split — no good boundary found
            parts.append(_sanitize_html(text[:effective]))
            text = text[effective:]

        if text:
            parts.append(_sanitize_html(text))
        return parts

    def send_message_to_users(self, text: str) -> None:
        """
        Send a message to all authorized users (used by the scheduler / built-in tools).
        Safe to call from any thread — uses run_coroutine_threadsafe to post onto the
        bot's event loop.
        """
        if not self._app:
            logger.warning("send_message_to_users: app not built yet, dropping message")
            return

        async def _send():
            bot = self._app.bot
            html_text = _md_to_html(text)
            delivered = 0
            for uid in list(self.allowed_ids):
                try:
                    for chunk in self._split_message(html_text):
                        await bot.send_message(chat_id=uid, text=chunk, parse_mode=ParseMode.HTML)
                    delivered += 1
                except Exception as exc:
                    logger.warning("Could not send message to %d: %s", uid, exc)
            if delivered:
                logger.info("Message delivered to %d user(s) (%d chars)", delivered, len(html_text))

        loop = self._loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(_send(), loop)
        else:
            # Fallback for edge cases (e.g. called before run() or in tests)
            try:
                asyncio.run(_send())
            except Exception as exc:
                logger.error("send_message_to_users fallback failed: %s", exc)

    def send_html_to_users(self, html_text: str) -> None:
        """
        Send pre-formatted Telegram HTML to all authorised users.

        Unlike ``send_message_to_users``, this method skips Markdown conversion —
        the caller is responsible for providing valid Telegram HTML.  Use this when
        the message already contains HTML tags (e.g. ``<blockquote expandable>``).
        Safe to call from any thread.
        """
        if not self._app:
            logger.warning("send_html_to_users: app not built yet, dropping message")
            return

        async def _send():
            bot = self._app.bot
            delivered = 0
            for uid in list(self.allowed_ids):
                try:
                    for chunk in self._split_message(html_text):
                        await bot.send_message(chat_id=uid, text=chunk, parse_mode=ParseMode.HTML)
                    delivered += 1
                except Exception as exc:
                    logger.warning("Could not send HTML message to %d: %s", uid, exc)
            if delivered:
                logger.info("HTML message delivered to %d user(s) (%d chars)", delivered, len(html_text))

        loop = self._loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(_send(), loop)
        else:
            try:
                asyncio.run(_send())
            except Exception as exc:
                logger.error("send_html_to_users fallback failed: %s", exc)


# ---------------------------------------------------------------------------
# Markdown → Telegram HTML converter
# ---------------------------------------------------------------------------

def _md_to_html(text: str) -> str:
    """
    Convert a Markdown-flavoured string to Telegram HTML (ParseMode.HTML).

    Handles:
      - Fenced code blocks  ```lang\\ncode\\n```  →  <pre><code>…</code></pre>
      - Inline code         `code`                →  <code>…</code>
      - Bold                **text** or __text__  →  <b>…</b>
      - Italic              *text*  or _text_     →  <i>…</i>
      - Strikethrough       ~~text~~              →  <s>…</s>
      - Markdown links      [text](url)           →  <a href="url">text</a>
      - Bare URLs           https://…             →  <a href="url">url</a>

    All prose is HTML-escaped so that <, >, & never break the parser.
    Code block contents are also HTML-escaped so that shell/Python snippets
    with <, >, & display correctly inside <pre><code>.
    URLs are extracted before HTML-escaping so that underscores and ampersands
    in query parameters are never misinterpreted as italic/bold markers.
    """
    # ---- Step 1: extract fenced code blocks to protect them ----
    # We replace them with placeholders, process the rest, then reinsert.
    placeholders: list[str] = []

    def _extract_fence(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        code = html.escape(m.group(2))
        lang_attr = f' class="language-{html.escape(lang)}"' if lang else ""
        block = f"<pre><code{lang_attr}>{code}</code></pre>"
        placeholders.append(block)
        return f"\x00BLOCK{len(placeholders) - 1}\x00"

    text = re.sub(r"```(\w*)\n?(.*?)```", _extract_fence, text, flags=re.DOTALL)

    # ---- Step 2: extract inline code spans ----
    def _extract_inline(m: re.Match) -> str:
        code = html.escape(m.group(1))
        placeholders.append(f"<code>{code}</code>")
        return f"\x00BLOCK{len(placeholders) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _extract_inline, text)

    # ---- Step 2.5: extract URLs before html.escape / markdown processing ----
    # Markdown links [label](url) first so they aren't also matched as bare URLs.
    def _extract_md_link(m: re.Match) -> str:
        label = html.escape(m.group(1))
        esc_url = html.escape(m.group(2))
        placeholders.append(f'<a href="{esc_url}">{label}</a>')
        return f"\x00BLOCK{len(placeholders) - 1}\x00"

    text = re.sub(r'\[([^\]\n]+)\]\((https?://[^)\s]+)\)', _extract_md_link, text)

    # Bare https?:// URLs: wrap in <a> so underscores/& in query params are
    # never touched by the italic regex. Strip common trailing punctuation that
    # is not part of the URL (e.g. "See https://example.com.")
    def _extract_bare_url(m: re.Match) -> str:
        url = m.group(0).rstrip(".,;:!?)'\"")
        esc_url = html.escape(url)
        placeholders.append(f'<a href="{esc_url}">{esc_url}</a>')
        tail = m.group(0)[len(url):]
        return f"\x00BLOCK{len(placeholders) - 1}\x00{tail}"

    text = re.sub(r'https?://[^\s<>"\'`\x00]+', _extract_bare_url, text)

    # ---- Step 3: HTML-escape the remaining prose ----
    text = html.escape(text)

    # ---- Step 4: apply inline formatting to prose ----
    # Bold: **text** only — __text__ is intentionally not supported to avoid
    # corrupting Python dunder names like __init__ or __all__ in agent output.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    # Italic: *text* or _text_ (single, not already consumed by bold)
    # _text_ requires non-word-char boundaries so snake_case identifiers like
    # ollama_health_check are never split into italic fragments.
    text = re.sub(r"\*(?!\*)(.+?)(?<!\*)\*", r"<i>\1</i>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)", r"<i>\1</i>", text, flags=re.DOTALL)
    # Strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)

    # ---- Step 5: reinsert extracted blocks ----
    for i, block in enumerate(placeholders):
        text = text.replace(f"\x00BLOCK{i}\x00", block)

    return _sanitize_html(text)


# ---------------------------------------------------------------------------
# HTML tag balancer
# ---------------------------------------------------------------------------

# Telegram HTML only recognises these tags; anything else is rejected.
_TELEGRAM_TAGS = frozenset({"b", "i", "s", "u", "code", "pre", "a", "blockquote"})
# Self-contained pattern that matches any opening or closing tag we care about.
_TAG_RE = re.compile(r"<(/?)(\w+)(\s[^>]*)?>", re.DOTALL)

# Progress message prefixes that represent agent "actions" (tool calls, results,
# errors, model switches) — shown as new messages in verbose mode.
_VERBOSE_EVENT_PREFIXES = ("🔧", "✅ C", "❌", "🛠️", "⚡", "⚠️ ")


def _sanitize_html(text: str) -> str:
    """Ensure all Telegram-HTML tags are properly balanced.

    Walks *text* character-by-character via a regex tag scanner and:
    - keeps every opening tag in ``_TELEGRAM_TAGS``, pushing it onto a stack
    - keeps every closing tag only when it matches the current top of the stack
      (drops unmatched / misnested close tags instead of forwarding them)
    - after the full string is consumed, appends synthetic close tags for any
      tags that were opened but never closed (in reverse order)

    Tags outside ``_TELEGRAM_TAGS`` (e.g. ``<div>``) are passed through
    unchanged because they were either already HTML-escaped prose that slipped
    through, or placeholders — altering them would corrupt code blocks.

    Inputs that are already valid pass through with zero mutations.

    Examples::

        >>> _sanitize_html("<b>hello</b>")
        '<b>hello</b>'
        >>> _sanitize_html("<b>unclosed")
        '<b>unclosed</b>'
        >>> _sanitize_html("foo <b>bar</b> <i>baz")
        'foo <b>bar</b> <i>baz</i>'
        >>> _sanitize_html("<b><i>ok</i></b>")
        '<b><i>ok</i></b>'
    """
    stack: list[str] = []
    result: list[str] = []
    pos = 0

    for m in _TAG_RE.finditer(text):
        # Append everything between previous match end and this tag
        result.append(text[pos:m.start()])
        pos = m.end()

        is_close = bool(m.group(1))
        tag = m.group(2).lower()
        attrs = m.group(3) or ""

        if tag not in _TELEGRAM_TAGS:
            # Not a Telegram formatting tag — pass through verbatim
            result.append(m.group(0))
            continue

        if not is_close:
            stack.append(tag)
            result.append(f"<{tag}{attrs}>")
        else:
            if stack and stack[-1] == tag:
                stack.pop()
                result.append(f"</{tag}>")
            # else: drop the unmatched / misnested close tag

    # Append any trailing text after the last tag
    result.append(text[pos:])

    # Close any still-open tags (innermost first)
    for tag in reversed(stack):
        result.append(f"</{tag}>")

    return "".join(result)
