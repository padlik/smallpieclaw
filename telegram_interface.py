"""
telegram_interface.py
---------------------
Telegram bot interface with two security modes:
  - allowlist: only pre-configured user IDs may interact
  - pairing:   users request access; owner approves via /pair command
"""

import logging
import secrets
from typing import Callable, Optional

from telegram import Update
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

    def __init__(self, config: dict, agent_handler: Callable):
        tg_cfg = config["telegram"]
        self.token: str = tg_cfg["bot_token"]
        self.security_mode: str = tg_cfg.get("security_mode", "allowlist")
        self.allowed_ids: set[int] = set(tg_cfg.get("allowed_user_ids", []))
        self.pairing_timeout: int = tg_cfg.get("pairing_timeout", 300)
        self.agent_handler = agent_handler

        # Pairing state: {token: user_id}
        self._pending_pairs: dict[str, int] = {}
        self._pairing_token: Optional[str] = None

        self._app: Optional[Application] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def build(self) -> Application:
        self._app = Application.builder().token(self.token).build()
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
    # Handlers
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        app = self._app
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
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
            "  /status — bot + system status\n"
            "  /pair   — generate pairing token (admin)\n"
            "  /myid   — show your Telegram user ID\n",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            await self._send_unauthorized(update)
            return
        await update.message.reply_text(
            f"✅ Agent online.\n"
            f"Security: `{self.security_mode}`\n"
            f"Authorized users: {len(self.allowed_ids)}",
            parse_mode=ParseMode.MARKDOWN,
        )

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
        await update.message.reply_text("🔄 Processing…")

        async def reply(msg: str):
            # Telegram message size limit is 4096 chars; split if needed
            for chunk in self._split_message(msg):
                await update.message.reply_text(chunk)

        # progress_callback runs in a sync context inside the agent;
        # we schedule coroutines via the event loop
        def progress(msg: str):
            import asyncio
            loop = ctx.application.update_queue._loop if hasattr(ctx.application.update_queue, "_loop") else None
            # Best-effort: log only (sending from sync thread is complex)
            logger.debug("Agent progress: %s", msg)

        try:
            result = await ctx.application.run_coroutine(
                self._run_agent_async(user.id, text, progress)
            ) if False else self._run_agent_sync(user.id, text, progress)
            await reply(result)
        except Exception as exc:
            logger.exception("Agent error for user %d", user.id)
            await update.message.reply_text(f"❌ Error: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_agent_sync(self, user_id: int, text: str, progress_cb) -> str:
        """Delegates to the injected agent_handler (synchronous)."""
        return self.agent_handler(user_id, text, progress_cb)

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
        Must be called from within an async context or wrapped appropriately.
        """
        import asyncio

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
