"""
telegram_bot.py — Telegram interface for the agent.

Commands:
  /start   — welcome + auth info
  /pair <PIN> — pair this user (pairing mode only)
  /status  — quick system status via agent
  /disk    — disk usage
  /logs    — recent system logs
  /ask <q> — explicit agent query
  /tools   — list registered tools
  /rebuild — rebuild tool index
  /memory  — show persistent memory
  /help    — command reference

Any free text is passed to the agent loop.
"""

from __future__ import annotations
import asyncio
import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from agent import Agent, AgentResult
from memory import Memory
from scheduler import Scheduler
from security import Security
from tool_registry import ToolRegistry
from tool_index import ToolIndex

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(
        self,
        agent: Agent,
        registry: ToolRegistry,
        index: ToolIndex,
        memory: Memory,
        security: Security,
    ):
        self._agent = agent
        self._registry = registry
        self._index = index
        self._memory = memory
        self._security = security
        self._scheduler: Scheduler | None = None
        self._admin_chat_id: int | None = None  # set on first authorized message

        self._app = (
            Application.builder()
            .token(config.TELEGRAM_TOKEN)
            .build()
        )
        self._register_handlers()

    # ── setup ──────────────────────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        add = self._app.add_handler
        add(CommandHandler("start",   self._cmd_start))
        add(CommandHandler("pair",    self._cmd_pair))
        add(CommandHandler("status",  self._cmd_status))
        add(CommandHandler("disk",    self._cmd_disk))
        add(CommandHandler("logs",    self._cmd_logs))
        add(CommandHandler("ask",     self._cmd_ask))
        add(CommandHandler("tools",   self._cmd_tools))
        add(CommandHandler("rebuild", self._cmd_rebuild))
        add(CommandHandler("memory",  self._cmd_memory))
        add(CommandHandler("help",    self._cmd_help))
        # Catch-all text → agent
        add(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_free_text))

    def set_scheduler(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler

    def send_message_sync(self, text: str) -> None:
        """Synchronous helper for use from the scheduler thread."""
        if self._admin_chat_id is None:
            logger.warning("No admin chat ID — cannot send scheduler message.")
            return
        asyncio.run_coroutine_threadsafe(
            self._app.bot.send_message(
                chat_id=self._admin_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            ),
            self._app.bot._loop,  # type: ignore[attr-defined]
        )

    # ── auth guard ─────────────────────────────────────────────────────────────

    async def _check_auth(self, update: Update) -> bool:
        user_id = update.effective_user.id
        if self._security.is_allowed(user_id):
            if self._admin_chat_id is None:
                self._admin_chat_id = update.effective_chat.id
            return True
        await update.message.reply_text(
            "⛔ Unauthorized.\n"
            + ("Use /pair <PIN> to authenticate." if not self._security.using_allowlist else "")
        )
        return False

    # ── command handlers ───────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        if self._security.using_allowlist:
            if self._security.is_allowed(uid):
                await update.message.reply_text("✅ You are on the allow-list. Send me anything!")
            else:
                await update.message.reply_text("⛔ Your user ID is not on the allow-list.")
        else:
            if self._security.is_allowed(uid):
                await update.message.reply_text("✅ Already paired. Send me anything!")
            else:
                await update.message.reply_text(
                    "👋 Hi! This bot uses PIN pairing for security.\n"
                    "Use: /pair <your-PIN>"
                )

    async def _cmd_pair(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        pin = args[0] if args else ""
        uid = update.effective_user.id
        if self._security.try_pair(uid, pin):
            self._admin_chat_id = update.effective_chat.id
            await update.message.reply_text("✅ Paired! You can now use the agent.")
        else:
            await update.message.reply_text("❌ Wrong PIN. Try again.")

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await update.message.reply_text(
            "🤖 *Agent commands*\n\n"
            "/status — system status\n"
            "/disk — disk usage\n"
            "/logs — recent logs\n"
            "/ask <question> — ask the agent\n"
            "/tools — list registered tools\n"
            "/rebuild — rebuild semantic tool index\n"
            "/memory — show persistent memory\n"
            "/help — this message\n\n"
            "Or just type anything and the agent will handle it.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await self._run_agent_goal(update, "Check system status: CPU, memory, disk, temperature")

    async def _cmd_disk(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await self._run_agent_goal(update, "Check disk usage and report free space")

    async def _cmd_logs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await self._run_agent_goal(update, "Show the last 20 lines of system logs")

    async def _cmd_ask(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        question = " ".join(ctx.args or []).strip()
        if not question:
            await update.message.reply_text("Usage: /ask <your question>")
            return
        await self._run_agent_goal(update, question)

    async def _cmd_tools(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        summary = self._registry.summary()
        await update.message.reply_text(f"🔧 *Registered tools*\n\n{summary}", parse_mode=ParseMode.MARKDOWN)

    async def _cmd_rebuild(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        msg = await update.message.reply_text("♻️ Rebuilding tool index…")
        self._registry.scan()
        self._index.build()
        await msg.edit_text(f"✅ Index rebuilt. {len(self._registry.all_tools())} tools indexed.")

    async def _cmd_memory(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        snippet = self._memory.to_prompt_snippet()
        text = snippet if snippet else "_(memory is empty)_"
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _handle_free_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        goal = (update.message.text or "").strip()
        if goal:
            await self._run_agent_goal(update, goal)

    # ── agent runner ───────────────────────────────────────────────────────────

    async def _run_agent_goal(self, update: Update, goal: str) -> None:
        status_msg = await update.message.reply_text("⏳ Thinking…")

        def on_status(text: str) -> None:
            """Fire-and-forget status update via asyncio."""
            coro = status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            asyncio.ensure_future(coro)

        loop = asyncio.get_event_loop()
        result: AgentResult = await loop.run_in_executor(
            None, lambda: self._agent.run(goal, status_cb=on_status)
        )

        # Build final reply
        lines = [result.answer]
        if result.tool_calls:
            lines.append(f"\n_Tools used: {', '.join(result.tool_calls)}_")
        if result.created_tools:
            lines.append(f"_New tools created: {', '.join(result.created_tools)}_")
        if result.error:
            lines.append(f"\n⚠️ Error: {result.error}")

        final_text = "\n".join(lines)
        try:
            await status_msg.edit_text(final_text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(final_text, parse_mode=ParseMode.MARKDOWN)

    # ── run ────────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the bot (blocking)."""
        logger.info("Starting Telegram bot…")
        self._app.run_polling(drop_pending_updates=True)
