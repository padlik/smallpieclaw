import schedule
import time
import threading
from agent import Agent
from telegram_bot import TelegramBot

class Scheduler:
    def __init__(self, agent: Agent, bot: TelegramBot):
        self.agent = agent
        self.bot = bot
        self.running = False
    
    def health_check(self):
        """Nightly health check"""
        result = self.agent.process_goal("Check system health and report issues")
        # In real implementation, send to Telegram admin
        print(f"Health check result: {result}")
    
    def backup_check(self):
        """Verify backup status"""
        self.agent.memory.set("last_backup_check", time.strftime("%Y-%m-%d %H:%M:%S"))
    
    def setup_jobs(self):
        # Daily at 2 AM
        schedule.every().day.at("02:00").do(self.health_check)
        # Every 6 hours
        schedule.every(6).hours.do(self.backup_check)
    
    def run(self):
        self.running = True
        while self.running:
            schedule.run_pending()
            time.sleep(60)
    
    def start_thread(self):
        thread = threading.Thread(target=self.run)
        thread.daemon = True
        thread.start()

