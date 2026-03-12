# main.py
import os
import sys

def create_sample_tools():
    """Create sample tools if they don't exist"""
    os.makedirs("tools", exist_ok=True)
    
    # Check disk tool
    disk_tool = """#!/bin/bash
# description: check disk usage and free space
df -h
"""
    
    docker_tool = """#!/bin/bash
# description: check docker container status
docker ps --format "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}" 2>/dev/null || echo "Docker not running"
"""
    
    temp_tool = """#!/bin/bash
# description: check CPU temperature
vcgencmd measure_temp 2>/dev/null || cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo "N/A"
"""
    
    for name, content in [("check_disk.sh", disk_tool), ("docker_status.sh", docker_tool), ("temperature.sh", temp_tool)]:
        path = os.path.join("tools", name)
        if not os.path.exists(path):
            with open(path, 'w') as f:
                f.write(content)
            os.chmod(path, 0o755)

def main():
    if not Config.TELEGRAM_TOKEN:
        print("Error: TELEGRAM_TOKEN not set")
        sys.exit(1)
    
    # Create sample tools
    create_sample_tools()
    
    # Initialize components
    agent = Agent()
    bot = TelegramBot()
    scheduler = Scheduler(agent, bot)
    
    # Setup scheduled jobs
    scheduler.setup_jobs()
    scheduler.start_thread()
    
    # Run bot
    bot.run()

if __name__ == "__main__":
    main()

