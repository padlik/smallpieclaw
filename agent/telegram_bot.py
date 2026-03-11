import logging
import json
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

import config
import scheduler as scheduler_mod
from agent import Agent
from allowed_ids import AllowedIDs

config_instance = config.config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("telegram_bot")

# --------------------------------------------------------------------- #
# Load the allow‑list once
# --------------------------------------------------------------------- #
ALLOWED = AllowedIDs(
    Path(config_instance.ALLOWED_IDS_PATH),
    config_instance.ADMIN_ID,
    config_instance.PAIR_SECRET
)

# --------------------------------------------------------------------- #
# Decorator that blocks all non‑allowed users
# --------------------------------------------------------------------- #
def require_allowed(func):
    async def wrapper(update: Update, context: CallbackContext):
        uid = update.effective_user.id
        if not ALLOWED.is_allowed(uid):
            await update.message.reply_text("You are not authorized to use this bot.")
            return
        return await func(update, context)
    return wrapper

# --------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------- #
async def start(update: Update, context: CallbackContext):
    """Unauthenticated – just greets the user."""
    await update.message.reply_text(
        "🤖 Lightweight Telegram Agent online. "
        "Use /status, /disk, /logs, /ask, or /pair."
    )

@require_allowed
async def status(update: Update, context: CallbackContext):
    await update.message.reply_text("Agent is running. Use /disk for disk usage or ask a question.")

@require_allowed
async def disk(update: Update, context: CallbackContext):
    await update.message.reply_text("Running disk check...")
    agent = Agent()
    result = agent.run_goal(
        "Check disk usage and free space, summarize key info.",
        chat_id=update.effective_chat.id,
        steps_override=4
    )
    await update.message.reply_text(result[:4000])

@require_allowed
async def logs(update: Update, context: CallbackContext):
    await update.message.reply_text("Logs (last 20 lines):\n... (Integrate with your logging system)")

@require_allowed
async def ask(update: Update, context: CallbackContext):
    user_text = update.message.text
    agent = Agent()
    result = agent.run_goal(user_text, chat_id=update.effective_chat.id)
    await update.message.reply_text(result[:4000])

async def pair(update: Update, context: CallbackContext):
    """Add the caller to the allow‑list after providing the secret."""
    if not config_instance.PAIR_SECRET:
        await update.message.reply_text("Pairing is disabled. Set TM_PAIR_SECRET to enable.")
        return
    parts = update.message.text.strip().split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /pair <secret>")
        return
    secret = parts[1]
    if secret != config_instance.PAIR_SECRET:
        await update.message.reply_text("Invalid secret.")
        return
    uid = update.effective_user.id
    ALLOWED.add(uid)
    await update.message.reply_text(f"✅ Paired. Your ID {uid} added to the allow‑list.")

@require_allowed
async def allow(update: Update, context: CallbackContext):
    """Allow a specific user (admin only)."""
    if config_instance.ADMIN_ID and update.effective_user.id != config_instance.ADMIN_ID:
        await update.message.reply_text("Only the admin can use this command.")
        return
    parts = update.message.text.strip().split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /allow <user_id>")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    ALLOWED.add(target_id)
    await update.message.reply_text(f"User {target_id} added to the allow‑list.")

@require_allowed
async def deny(update: Update, context: CallbackContext):
    """Remove a user from the allow‑list (admin only)."""
    if config_instance.ADMIN_ID and update.effective_user.id != config_instance.ADMIN_ID:
        await update.message.reply_text("Only the admin can use this command.")
        return
    parts = update.message.text.strip().split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /deny <user_id>")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    ALLOWED.remove(target_id)
    await update.message.reply_text(f"User {target_id} removed from the allow‑list.")

# --------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------- #
def main():
    app = Application.builder().token(config_instance.TELEGRAM_TOKEN).build()

    # -----------------------------------------------------------------
    # Telegram sender for the scheduler – it receives an optional chat_id
    # -----------------------------------------------------------------
    def sender(msg: str, chat_id: int | None = None):
        # If the caller gave a specific chat_id we use it, otherwise fall back
        target = chat_id or config_instance.ADMIN_ID or (ALLOWED.list()[0] if ALLOWED.list() else None)
        if target is None:
            logger.warning("No chat id available to send scheduler message.")
            return
        app.bot.send_message(chat_id=target, text=msg)

    # Expose sender + allow‑list to the scheduler module
    scheduler_mod.TELEGRAM_SENDER = sender
    scheduler_mod.SCHEDULER_ALLOWED_IDS = ALLOWED

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pair", pair))
    app.add_handler(CommandHandler("allow", allow))
    app.add_handler(CommandHandler("deny", deny))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("disk", disk))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("ask", ask))
    # Any free‑text message (that isn’t a command) goes to ask()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ask))

    # Start the background scheduler (jobs will use the sender we just set)
    scheduler_mod.start_scheduler()

    logger.info("🤖 Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()

