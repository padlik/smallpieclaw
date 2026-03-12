import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from agent import Agent

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

class TelegramBot:
    def __init__(self):
        self.agent = Agent()
        self.application = Application.builder().token(Config.TELEGRAM_TOKEN).build()
        self._setup_handlers()
    
    def _setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("status", self.status))
        self.application.add_handler(CommandHandler("disk", self.disk))
        self.application.add_handler(CommandHandler("logs", self.logs))
        self.application.add_handler(CommandHandler("ask", self.ask))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
    
    def _check_auth(self, user_id: int) -> bool:
        if not Config.ALLOWED_USERS:
            return True  # If no allowed list set, allow all (not recommended)
        return user_id in Config.ALLOWED_USERS
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not self._check_auth(user_id):
            await update.message.reply_text("Unauthorized")
            return
        
        await update.message.reply_text(
            "Raspberry Pi Agent online.\n"
            "Commands:\n"
            "/status - System status\n"
            "/disk - Disk usage\n"
            "/logs - Recent logs\n"
            "/ask <question> - Ask the agent\n"
            "Or send any message to use the agent."
        )
    
    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update.effective_user.id):
            return
        status = self.agent.get_status()
        await update.message.reply_text(status)
    
    async def disk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update.effective_user.id):
            return
        result = self.agent.executor.execute('check_disk')
        await update.message.reply_text(f"Disk status:\n{result.get('stdout', 'N/A')}")
    
    async def logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update.effective_user.id):
            return
        # Simple log check using system tool or agent
        result = self.agent.executor.execute('docker_status') if 'docker_status' in self.agent.registry.tools else None
        if result:
            await update.message.reply_text(f"Docker status:\n{result.get('stdout', 'N/A')[:1000]}")
        else:
            await update.message.reply_text("No log tool configured")
    
    async def ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update.effective_user.id):
            return
        question = ' '.join(context.args)
        if not question:
            await update.message.reply_text("Usage: /ask <your question>")
            return
        
        await update.message.reply_text("Processing...")
        response = self.agent.process_goal(question)
        await update.message.reply_text(response[:4000])  # Telegram limit
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update.effective_user.id):
            return
        
        text = update.message.text
        await update.message.reply_text("Thinking...")
        
        try:
            response = self.agent.process_goal(text)
            await update.message.reply_text(response[:4000])
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)}")
    
    def run(self):
        print("Starting Telegram Bot...")
        self.application.run_polling()

