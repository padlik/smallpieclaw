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
import logging
import os
import time
from functools import partial
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

from telegram_formatter import (
    split_message as _split_message_impl,
    format_jobs_list as _format_jobs_list_impl,
    md_to_html as _md_to_html,
)
from telegram_commands import (
    cmd_start, cmd_help, cmd_status, cmd_stop, cmd_reset, cmd_compress,
    cmd_verbose, cmd_jobs, cmd_agents, cmd_tools, cmd_skills, cmd_mcp,
    cmd_reindex, cmd_pair, cmd_unpair, cmd_myid, cmd_health,
    cmd_show_ctx, cmd_show_env, cmd_models, cmd_mode,
    cb_confirm, cb_extend, cb_tool_create, cb_model_switch,
    cb_mode_switch, cb_regulator_plan,
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
        mcp_manager=None,             # Optional[MCPManager]
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
        self.mcp_manager = mcp_manager
        self._downloads_dir = os.path.abspath(downloads_dir)
        self._start_time = time.time()

        # Pairing state: {token: user_id}
        self._pending_pairs: dict[str, int] = {}

        # Verbose mode: send each agent action as a new message instead of editing
        self._verbose: bool = False

        # Regulator mode state
        self._agent_mode: str = "agent"   # "agent" | "regulator"
        self._pending_plan: dict | None = None
        self.sub_agent_factory = None     # wired from main.py

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
            BotCommand("mode", "Switch between Agent and Regulator modes"),
            BotCommand("jobs", "List scheduled jobs"),
            BotCommand("agents", "List and manage active sub-agents"),
            BotCommand("reset", "Save and clear current task context"),
            BotCommand("compress", "Summarise and compress agent context"),
            BotCommand("verbose", "Toggle live tool-call progress messages"),
            BotCommand("reindex", "Re-embed all tools in the semantic index"),
            BotCommand("pair", "Generate or submit pairing token"),
            BotCommand("unpair", "Remove a user from access list"),
            BotCommand("myid", "Show your Telegram user ID"),
            BotCommand("mcp", "List and manage MCP servers"),
            BotCommand("show_ctx", "Show current system prompt snapshot"),
            BotCommand("show_env", "Show runtime environment info"),
            BotCommand("stop", "Cancel the currently running task"),
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
        app.add_handler(CommandHandler("stop", partial(cmd_stop, self)))
        app.add_handler(CommandHandler("start", partial(cmd_start, self)))
        app.add_handler(CommandHandler("help", partial(cmd_help, self)))
        app.add_handler(CommandHandler("status", partial(cmd_status, self)))
        app.add_handler(CommandHandler("health", partial(cmd_health, self)))
        app.add_handler(CommandHandler("reset", partial(cmd_reset, self)))
        app.add_handler(CommandHandler("compress", partial(cmd_compress, self)))
        app.add_handler(CommandHandler("verbose", partial(cmd_verbose, self)))
        app.add_handler(CommandHandler("jobs", partial(cmd_jobs, self)))
        app.add_handler(CommandHandler("tools", partial(cmd_tools, self)))
        app.add_handler(CommandHandler("skills", partial(cmd_skills, self)))
        app.add_handler(CommandHandler("models", partial(cmd_models, self)))
        app.add_handler(CommandHandler("mode", partial(cmd_mode, self)))
        app.add_handler(CommandHandler("mcp", partial(cmd_mcp, self)))
        app.add_handler(CommandHandler("reindex", partial(cmd_reindex, self)))
        app.add_handler(CommandHandler("pair", partial(cmd_pair, self)))
        app.add_handler(CommandHandler("unpair", partial(cmd_unpair, self)))
        app.add_handler(CommandHandler("myid", partial(cmd_myid, self)))
        app.add_handler(CommandHandler("agents", partial(cmd_agents, self)))
        # Hidden diagnostic commands (not registered with BotFather)
        app.add_handler(CommandHandler("show_ctx", partial(cmd_show_ctx, self)))
        app.add_handler(CommandHandler("show_env", partial(cmd_show_env, self)))
        # Inline button callbacks
        app.add_handler(CallbackQueryHandler(partial(cb_model_switch, self), pattern=r"^model:"))
        app.add_handler(CallbackQueryHandler(partial(cb_mode_switch, self), pattern=r"^mode:"))
        app.add_handler(CallbackQueryHandler(partial(cb_regulator_plan, self), pattern=r"^reg_"))
        app.add_handler(CallbackQueryHandler(partial(cb_confirm, self), pattern=r"^confirm_(yes|no|all):"))
        app.add_handler(CallbackQueryHandler(partial(cb_extend, self), pattern=r"^extend_(yes|no|unlimited):"))
        app.add_handler(CallbackQueryHandler(partial(cb_tool_create, self), pattern=r"^tool_create_"))
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

    @staticmethod
    def _format_jobs_list(jobs: list) -> str:
        """Render a list of job dicts (from scheduler.list_jobs()) as HTML."""
        return _format_jobs_list_impl(jobs)

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
        # Regulator mode: route to planner instead
        if self._agent_mode == "regulator":
            await self._run_regulator_task(update, ctx, task_text, images)
            return

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
        """Split text into chunks ≤ limit chars with balanced HTML tags."""
        return _split_message_impl(text, limit)

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

    # ------------------------------------------------------------------
    # Regulator mode methods
    # ------------------------------------------------------------------

    async def _run_regulator_task(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        task_text: str,
        images: Optional[list[str]] = None,
    ) -> None:
        """Plan a task in Regulator mode and present it for approval."""
        if not self.sub_agent_factory:
            await update.effective_message.reply_text(
                "❌ Regulator mode not available (sub_agent_factory not wired).",
                parse_mode=ParseMode.HTML,
            )
            return

        if not self.llm_client:
            await update.effective_message.reply_text(
                "❌ Regulator mode requires an LLM client.",
                parse_mode=ParseMode.HTML,
            )
            return

        status_msg = await update.effective_message.reply_text("🧠 <b>Regulator</b> — planning…", parse_mode=ParseMode.HTML)
        loop = asyncio.get_running_loop()

        # Get configured models for constrained selection
        configured_models = []
        if hasattr(self.llm_client, "list_models"):
            configured_models = self.llm_client.list_models()

        def _run_planning():
            from regulator import RegulatorOrchestrator, load_models_capabilities
            caps = load_models_capabilities(os.path.join(os.getcwd(), "data"))
            return RegulatorOrchestrator().plan_task(
                task_text, self.llm_client, caps,
                configured_models=configured_models,
                images=images,
            )

        try:
            plan = await loop.run_in_executor(None, _run_planning)
            self._pending_plan = plan

            from regulator import RegulatorOrchestrator
            plan_html = RegulatorOrchestrator().format_plan_html(plan)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data="reg_approve")],
                [
                    InlineKeyboardButton("💾 Save Plan", callback_data="reg_save"),
                    InlineKeyboardButton("❌ Discard", callback_data="reg_discard"),
                ],
            ])
            await status_msg.edit_text(
                plan_html,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.exception("Regulator planning error")
            await self._safe_edit(status_msg, f"❌ Planning failed: {exc}")

    async def _run_regulator_execute(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        plan: Optional[dict] = None,
    ) -> None:
        """Execute a Regulator plan after user approval."""
        if not plan:
            await update.effective_message.reply_text(
                "❌ No plan to execute.", parse_mode=ParseMode.HTML
            )
            return

        status_msg = await update.effective_message.reply_text("⚙️ Executing plan…")
        loop = asyncio.get_running_loop()

        def _run_execution():
            from regulator import RegulatorOrchestrator
            return RegulatorOrchestrator().execute_plan(
                plan, self.sub_agent_factory, notify_fn=None
            )

        try:
            results, failure = await loop.run_in_executor(None, _run_execution)

            if failure is None:
                # All sub-tasks completed successfully
                lines = ["✅ <b>Plan completed</b>\n"]
                for res in results:
                    result_text = res.get("result", "")
                    preview = html.escape(result_text[:300])
                    ellipsis = "…" if len(result_text) > 300 else ""
                    lines.append(
                        f"<b>{html.escape(res['id'])}. {html.escape(res['name'])}</b>\n"
                        f"  {preview}{ellipsis}\n"
                    )
                await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
            else:
                # Partial failure — show completed + failure info
                lines = ["⚠️ <b>Plan partially completed</b>\n"]
                for res in results:
                    result_text = res.get("result", "")
                    preview = html.escape(result_text[:200])
                    ellipsis = "…" if len(result_text) > 200 else ""
                    lines.append(
                        f"✅ <b>{html.escape(res['id'])}. {html.escape(res['name'])}</b>\n"
                        f"  {preview}{ellipsis}\n"
                    )
                lines.append(
                    f"\n❌ <b>Failed: {html.escape(failure['subtask_id'])}</b>\n"
                    f"Model: <code>{html.escape(failure['model_name'])}</code>\n"
                    f"Error: {html.escape(failure['error'][:300])}"
                )
                if failure["remaining"]:
                    lines.append(
                        f"\n⏸ Remaining: {', '.join(html.escape(r) for r in failure['remaining'])}"
                    )
                await status_msg.edit_text(
                    "\n".join(lines), parse_mode=ParseMode.HTML
                )

        except Exception as exc:
            logger.exception("Regulator execution error")
            msg = f"❌ Execution failed: {html.escape(str(exc))}"
            await status_msg.edit_text(msg, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# Progress message prefixes that represent agent "actions" (tool calls, results,
# errors, model switches) — shown as new messages in verbose mode.
_VERBOSE_EVENT_PREFIXES = ("🔧", "✅ C", "❌", "🛠️", "⚡", "⚠️ ")
