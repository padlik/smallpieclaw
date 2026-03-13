"""
telegram_interface.py
---------------------
Telegram bot interface with two security modes:
  - allowlist: only pre-configured user IDs may interact
  - pairing:   users request access; owner approves via /pair command
"""

import asyncio
import logging
import secrets
import time
from typing import Callable, Optional

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
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
        scheduler=None,
        tool_registry=None,
        llm_client=None,
    ):
        tg_cfg = config["telegram"]
        self._config = config
        self.token: str = tg_cfg["bot_token"]
        self.security_mode: str = tg_cfg.get("security_mode", "allowlist")
        self.allowed_ids: set[int] = set(tg_cfg.get("allowed_user_ids", []))
        self.pairing_timeout: int = tg_cfg.get("pairing_timeout", 300)
        self.agent_handler = agent_handler
        self.agent_reset_fn = agent_reset_fn
        self.scheduler = scheduler
        self.tool_registry = tool_registry
        self.llm_client = llm_client
        self._start_time = time.time()

        # Pairing state: {token: user_id}
        self._pending_pairs: dict[str, int] = {}
        self._pairing_token: Optional[str] = None

        self._app: Optional[Application] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def build(self) -> Application:
        self._app = (
            Application.builder()
            .token(self.token)
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
        app.run_polling(allowed_updates=Update.ALL_TYPES)

    # ------------------------------------------------------------------
    # Post-init (register bot commands)
    # ------------------------------------------------------------------

    async def _post_init(self, app: Application) -> None:
        commands = [
            BotCommand("start", "Introduction and usage examples"),
            BotCommand("help", "Help and command reference"),
            BotCommand("status", "Agent status, uptime, and token usage"),
            BotCommand("tools", "List available tools"),
            BotCommand("jobs", "List scheduled jobs"),
            BotCommand("reset", "Save and clear current task context"),
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
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("reset", self._cmd_reset))
        app.add_handler(CommandHandler("jobs", self._cmd_jobs))
        app.add_handler(CommandHandler("tools", self._cmd_tools))
        app.add_handler(CommandHandler("pair", self._cmd_pair))
        app.add_handler(CommandHandler("unpair", self._cmd_unpair))
        app.add_handler(CommandHandler("myid", self._cmd_myid))
        # Catch-all text messages
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_authorized(user.id):
            await self._send_unauthorized(update)
            return
        await update.message.reply_text(
            "👋 Home Server Agent ready.\n"
            "Send me a command like:\n"
            "  • *check disk usage*\n"
            "  • *show CPU temperature*\n"
            "  • *are my Docker containers running?*\n\n"
            "Use /help for more info.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        await update.message.reply_text(
            "🤖 *Home Server Agent*\n\n"
            "Just send a natural language request, e.g.:\n"
            "  `check disk usage`\n"
            "  `show system health`\n"
            "  `how much RAM is free?`\n\n"
            "*Commands:*\n"
            "  /status  — agent status, uptime, token usage\n"
            "  /tools   — list available tools\n"
            "  /jobs    — list scheduled jobs\n"
            "  /reset   — save and clear task context (`/reset discard` to skip saving)\n"
            "  /pair    — pairing token management\n"
            "  /myid    — show your Telegram user ID\n",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return

        uptime_secs = int(time.time() - self._start_time)
        h = uptime_secs // 3600
        m = (uptime_secs % 3600) // 60
        s = uptime_secs % 60

        llm_model = self._config["llm"]["model"]
        emb_cfg = self._config.get("embeddings", {})
        emb_model = emb_cfg.get("model", "N/A")
        emb_key_status = "own key" if emb_cfg.get("api_key") else "using LLM key (fallback)"

        token_line = ""
        if self.llm_client:
            usage = self.llm_client.get_today_usage()
            token_line = (
                f"\n📊 *Token Usage Today:*\n"
                f"  Prompt: {usage['prompt_tokens']:,}\n"
                f"  Completion: {usage['completion_tokens']:,}\n"
                f"  Total: {usage['total_tokens']:,}"
            )

        await update.message.reply_text(
            f"✅ *Agent Status*\n\n"
            f"⏱ Uptime: `{h}h {m}m {s}s`\n"
            f"🤖 LLM: `{llm_model}`\n"
            f"🔍 Embeddings: `{emb_model}` ({emb_key_status})\n"
            f"🔐 Security: `{self.security_mode}`\n"
            f"👥 Authorized users: {len(self.allowed_ids)}"
            f"{token_line}",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        args = ctx.args or []
        discard = "discard" in [a.lower() for a in args]

        status_msg = await update.message.reply_text(
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

    async def _cmd_jobs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        if not self.scheduler:
            await update.message.reply_text("Scheduler not available.")
            return

        jobs = self.scheduler.list_jobs()
        if not jobs:
            await update.message.reply_text("No scheduled jobs configured.")
            return

        lines = [f"📅 *Scheduled Jobs* ({len(jobs)} total)\n"]
        for job in jobs:
            icon = "✅" if job["enabled"] else "⏸"
            last_run = job.get("last_run") or "never"
            lines.append(f"{icon} `{job['tag']}`")
            lines.append(f"   Schedule: {job['schedule']}")
            lines.append(f"   Last run: {last_run}")
            lines.append(f"   {job['task']}\n")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def _cmd_tools(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        if not self.tool_registry:
            await update.message.reply_text("Tool registry not available.")
            return

        tools = self.tool_registry.all()
        if not tools:
            await update.message.reply_text("No tools registered.")
            return

        builtin = [t for t in tools if not t.is_generated]
        generated = [t for t in tools if t.is_generated]

        lines = [f"🔧 *Available Tools* ({len(tools)} total)\n"]
        if builtin:
            lines.append("*Built-in:*")
            for t in builtin:
                lines.append(f"  • `{t.name}` — {t.description}")
        if generated:
            lines.append("\n*Generated:*")
            for t in generated:
                lines.append(f"  • `{t.name}` — {t.description}")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def _cmd_pair(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """
        In pairing mode: if the caller is already authorized, generate a
        single-use pairing token. Another user can run /pair <token> to gain access.
        """
        user = update.effective_user
        if self.security_mode != "pairing":
            await update.message.reply_text("Pairing mode is not enabled.")
            return

        args = ctx.args or []

        if args:
            # Someone submitting a token
            token = args[0].strip()
            if token in self._pending_pairs:
                self.allowed_ids.add(user.id)
                del self._pending_pairs[token]
                logger.info("User %d authorized via pairing token", user.id)
                await update.message.reply_text("✅ Pairing successful! You can now use the agent.")
            else:
                await update.message.reply_text("❌ Invalid or expired pairing token.")
            return

        # Generate a new token (only for already-authorized users)
        if not self._is_authorized(user.id):
            await self._send_unauthorized(update)
            return

        token = secrets.token_hex(8)
        self._pending_pairs[token] = user.id
        logger.info("Pairing token generated by user %d: %s", user.id, token)
        await update.message.reply_text(
            f"🔑 Pairing token (valid until used):\n`{token}`\n\n"
            "Share this with the user who should gain access. "
            "They should run: `/pair <token>`",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_unpair(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /unpair <user_id>")
            return
        try:
            target = int(args[0])
        except ValueError:
            await update.message.reply_text("Invalid user ID.")
            return
        self.allowed_ids.discard(target)
        await update.message.reply_text(f"User {target} removed from allowed list.")

    async def _cmd_myid(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        await update.message.reply_text(f"Your Telegram user ID: `{uid}`", parse_mode=ParseMode.MARKDOWN)

    async def _on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        text = (update.message.text or "").strip()
        if not text:
            return

        if not self._is_authorized(user.id):
            await self._send_unauthorized(update)
            return

        logger.info("Message from user %d: %s", user.id, text[:80])
        status_msg = await update.message.reply_text("🔄 Processing…")
        loop = asyncio.get_event_loop()

        def progress(msg: str):
            asyncio.run_coroutine_threadsafe(
                self._safe_edit(status_msg, msg),
                loop,
            )

        try:
            result = await loop.run_in_executor(
                None,
                lambda: self.agent_handler(user.id, text, progress),
            )
            await self._safe_edit(status_msg, "✅ Done")
            for chunk in self._split_message(result):
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            logger.exception("Agent error for user %d", user.id)
            await self._safe_edit(status_msg, f"❌ Error: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _safe_edit(self, message, text: str) -> None:
        try:
            await message.edit_text(text[:4096])
        except Exception:
            pass

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
            await update.message.reply_text(
                "🔒 Access denied.\n"
                "Ask an authorized user for a pairing token and run:\n"
                "`/pair <token>`",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                f"🔒 Access denied. Your ID is `{uid}`.\n"
                "Ask the admin to add it to the allowed list.",
                parse_mode=ParseMode.MARKDOWN,
            )

    @staticmethod
    def _split_message(text: str, limit: int = 4000) -> list[str]:
        if len(text) <= limit:
            return [text]
        parts = []
        while text:
            parts.append(text[:limit])
            text = text[limit:]
        return parts

    def send_message_to_users(self, text: str) -> None:
        """
        Send a message to all authorized users (used by the scheduler).
        Schedules the send on the bot's running event loop.
        """
        async def _send():
            bot = self._app.bot
            for uid in list(self.allowed_ids):
                try:
                    for chunk in self._split_message(text):
                        await bot.send_message(chat_id=uid, text=chunk)
                except Exception as exc:
                    logger.warning("Could not send scheduled message to %d: %s", uid, exc)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_send())
            else:
                loop.run_until_complete(_send())
        except Exception as exc:
            logger.error("send_message_to_users failed: %s", exc)
