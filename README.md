# 🤖 Raspberry Pi Telegram AI Agent

A lightweight semi-autonomous AI agent for Raspberry Pi (2GB) that operates via Telegram.  
All heavy reasoning is delegated to a remote LLM API — the Pi only orchestrates and executes tools.

---

## Features

- **ReAct agent loop** — reason → choose tool → execute → observe → repeat
- **Semantic tool discovery** — vector embeddings (or TF-IDF fallback) to find the right tool
- **Self-building tools** — LLM can create new bash/Python tools at runtime
- **Multi-provider LLM** — OpenAI, Claude, Google Gemini, OpenRouter
- **Scheduler** — autonomous cron jobs with Telegram reporting
- **Persistent memory** — lightweight key-value state across sessions
- **Security** — user allow-list or PIN pairing

---

## Requirements

- Raspberry Pi (2GB+ RAM) running Raspberry Pi OS / Debian
- Python 3.10+
- A Telegram bot token ([BotFather](https://t.me/BotFather))
- API key for at least one LLM provider

---

## Setup

### 1. Clone / copy files

```bash
scp -r agent/ pi@raspberrypi.local:~/agent
```

### 2. Install dependencies

```bash
cd ~/agent
pip install -r requirements.txt --break-system-packages
```

Install only the LLM providers you need:
```bash
pip install openai                 # OpenAI / OpenRouter
pip install anthropic              # Claude
pip install google-generativeai   # Google Gemini
```

### 3. Configure

Copy and edit the config:
```bash
cp config.py config_local.py
nano config_local.py
```

Or set environment variables:
```bash
export TELEGRAM_TOKEN="your-token"
export LLM_PROVIDER="openai"          # openai | claude | google | openrouter
export OPENAI_API_KEY="sk-..."
export PAIRING_PIN="mysecretpin"
```

### 4. Run

```bash
python main.py
```

To run as a systemd service:
```bash
sudo cp agent.service /etc/systemd/system/
sudo systemctl enable --now agent
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | — | Bot token from BotFather |
| `LLM_PROVIDER` | `openai` | `openai` / `claude` / `google` / `openrouter` |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `CLAUDE_API_KEY` | — | Anthropic API key |
| `GOOGLE_API_KEY` | — | Google AI Studio key |
| `OPENROUTER_API_KEY` | — | OpenRouter key |
| `EMBEDDING_PROVIDER` | `openai` | `openai` / `google` / `none` |
| `ALLOWED_USER_IDS` | `[]` | Comma-separated Telegram user IDs; empty = pairing mode |
| `PAIRING_PIN` | `changeme123` | PIN for pairing mode |
| `MAX_STEPS` | `8` | Max ReAct loop iterations |
| `TOOL_TIMEOUT_SEC` | `10` | Tool execution timeout |
| `SCHEDULER_JOBS` | see config | List of (cron, goal, id) tuples |

---

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/pair <PIN>` | Authenticate with PIN |
| `/status` | System status (CPU, disk, temp) |
| `/disk` | Disk usage |
| `/logs` | Recent system logs |
| `/ask <q>` | Ask the agent anything |
| `/tools` | List all registered tools |
| `/rebuild` | Rebuild semantic tool index |
| `/memory` | Show persistent memory |
| `/help` | Command reference |

Any free-form text is passed directly to the agent loop.

---

## Adding Tools

Create a `.sh` or `.py` file in `tools/` with a `# description:` comment:

```bash
#!/bin/bash
# description: restart the nginx web server and verify it started

sudo systemctl restart nginx
sleep 2
systemctl status nginx --no-pager | head -10
```

Then rebuild the index:
```
/rebuild
```
or the agent will pick it up on the next restart.

---

## Folder Structure

```
agent/
├── main.py            # Entry point
├── config.py          # Configuration
├── agent.py           # ReAct agent controller
├── telegram_bot.py    # Telegram interface
├── tool_registry.py   # Tool discovery & registration
├── tool_index.py      # Semantic search index
├── tool_executor.py   # Safe script execution
├── tool_creator.py    # Self-building tool system
├── scheduler.py       # Background task scheduler
├── memory.py          # Persistent key-value memory
├── security.py        # Auth: allow-list / PIN pairing
├── llm_client.py      # Multi-provider LLM abstraction
├── requirements.txt
├── memory.json        # (auto-created) persistent memory
├── tool_index.json    # (auto-created) semantic index
├── tools/             # Built-in tool scripts
│   ├── check_disk.sh
│   ├── check_cpu.sh
│   ├── check_logs.sh
│   ├── check_network.sh
│   ├── docker_status.sh
│   ├── temperature.sh
│   └── system_health.py
└── tools_generated/   # LLM-created tools (auto-populated)
```

---

## Security Notes

- Generated tools are scanned for dangerous patterns before saving
- An optional LLM safety review runs before any new tool is executed
- Tool execution is sandboxed to the `tools/` directories
- All executions have a hard timeout

---

## Memory Usage

Target: **< 120 MB RSS**

- Python interpreter + telegram-bot: ~40 MB
- No numpy, no local ML models
- Embeddings computed via API (zero local cost)
- TF-IDF fallback uses pure Python

---

## systemd Service

```ini
# /etc/systemd/system/agent.service
[Unit]
Description=Telegram AI Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/agent
EnvironmentFile=/home/pi/agent/.env
ExecStart=/usr/bin/python3 /home/pi/agent/main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```
