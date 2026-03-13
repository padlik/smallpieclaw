# Telegram Home Server Agent

A lightweight autonomous Telegram bot for controlling and querying your home server,
designed to run on a **Raspberry Pi with 2 GB RAM**. Uses a remote LLM for reasoning
and semantic tool discovery, so no heavy ML libraries run locally.

---

## Features

- **ReAct agent loop** — reasons step-by-step, executes tools, and loops until done
- **Semantic tool search** — finds the right tool using embedding-based cosine similarity
- **Self-building tools** — the LLM can create new `.sh`/`.py` tools when a capability is missing; creation is reported to the user immediately
- **Secure Telegram bot** — allowlist or pairing-token access control
- **4-tier memory architecture** — short-term conversation history, working task context, long-term vector knowledge index, and results history
- **Configurable scheduler** — jobs defined in `scheduler.toml`; manage jobs from chat or via `/jobs`
- **Streaming responses** — bot edits its "Processing…" message in real time as the agent works
- **Multi-provider LLM** — OpenAI, OpenRouter, Google Gemini, Anthropic Claude
- **Token usage tracking** — daily prompt/completion counters visible in `/status`

---

## Project Structure

```
main.py                  # Entry point
config.toml              # All configuration
scheduler.toml           # Scheduled job definitions
llm_client.py            # LLM + embeddings client (multi-provider, token tracking)
agent_controller.py      # ReAct agent loop with 4-tier memory
telegram_interface.py    # Telegram bot with security and streaming
tool_registry.py         # Discovers and registers tools
tool_executor.py         # Runs tools in subprocess
tool_index.py            # Semantic search over tool descriptions
tool_creator.py          # LLM-generated tools with safety validation
scheduler.py             # Background task scheduler
memory_store.py          # Short-term, working, long-term, and results memory
tools/                   # Built-in tools (.sh and .py)
tools_generated/         # Tools created by the LLM at runtime
data/
    tool_index.json          # Persisted embedding vectors for tools
    memory.json              # Persistent agent key-value memory
    longterm_memory.json     # Long-term vector knowledge index
    results_memory.json      # Past task summaries and results
    scheduler_state.json     # Current scheduler job state (read by manage_scheduler tool)
```

---

## Installation

### 1. Clone / copy files

```bash
git clone <your-repo> ~/telegram-agent
cd ~/telegram-agent
```

### 2. Create a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure

Copy and edit the config file:

```bash
cp config.toml config.toml.bak   # optional backup
nano config.toml
```

Required settings:

| Key | Description |
|-----|-------------|
| `telegram.bot_token` | From [@BotFather](https://t.me/BotFather) |
| `telegram.security_mode` | `"allowlist"` or `"pairing"` |
| `telegram.allowed_user_ids` | Your Telegram user IDs (for allowlist mode) |
| `llm.api_key` | Your LLM provider API key |
| `llm.provider` | `openai`, `openrouter`, `google`, or `anthropic` |
| `llm.model` | e.g. `gpt-4o-mini`, `gemini-1.5-flash`, `claude-3-haiku-20240307` |
| `embeddings.api_key` | API key for embeddings — if empty, falls back to `llm.api_key` |
| `embeddings.model` | e.g. `text-embedding-3-small` |

> **Tip:** To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

### 5. (Optional) Configure the scheduler

Edit `scheduler.toml` to enable/disable jobs or change their schedule:

```toml
[jobs.nightly_health]
enabled = true
schedule = "daily"
time = "02:00"
task = "Run a full system health check and summarize the status."
notify = true

[jobs.disk_check]
enabled = true
schedule = "interval"
hours = 6
task = "Check disk usage on all mount points. Alert if any mount point is above 80% full."
notify = true

[jobs.longterm_memory_update]
enabled = true
schedule = "daily"
time = "03:00"
task = "Summarize the key events and findings from today into long-term memory."
notify = false
```

You can also add, pause, or remove jobs from the Telegram chat at runtime (the agent uses the `manage_scheduler` tool), or use `/jobs` to see all active jobs.

### 5. Run

```bash
python main.py
```

To run as a systemd service on the Raspberry Pi:

```ini
# /etc/systemd/system/telegram-agent.service
[Unit]
Description=Telegram Home Server Agent
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/telegram-agent
ExecStart=/home/pi/telegram-agent/.venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now telegram-agent
```

---

## Security

### Allowlist mode
Add your Telegram user IDs to `config.toml`:

```toml
[telegram]
security_mode = "allowlist"
allowed_user_ids = [123456789]
```

### Pairing mode
1. Set `security_mode = "pairing"` and add yourself to `allowed_user_ids`.
2. Run `/pair` in the bot to generate a single-use token.
3. Share the token with another user — they run `/pair <token>` to gain access.

---

## Writing Custom Tools

Create a `.sh` or `.py` file in the `tools/` directory.
The file **must** include a `description:` comment in the first 10 lines:

```bash
#!/bin/bash
# description: check if nginx is running and show its status
systemctl status nginx
```

```python
#!/usr/bin/env python3
# description: show Python package versions installed on this system
import pkg_resources
for pkg in sorted(pkg_resources.working_set, key=lambda p: p.project_name):
    print(f"{pkg.project_name}=={pkg.version}")
```

Restart the agent (or wait for the next query) to pick up new tools.

---

## Agent Limits

| Parameter | Default | Config key |
|-----------|---------|------------|
| Max agent steps | 8 | `agent.max_iterations` |
| Tool timeout | 10 s | `agent.tool_timeout` |
| Max tool output | 4000 chars | `agent.max_output_size` |
| Semantic top-K tools | 3 | `agent.top_tools` |

---

## Supported LLM Providers

| Provider | `provider` value | Notes |
|----------|-----------------|-------|
| OpenAI | `openai` | GPT-4o, GPT-4o-mini, etc. |
| OpenRouter | `openrouter` | Set `base_url = "https://openrouter.ai/api/v1"` |
| Google | `google` | Gemini models |
| Anthropic | `anthropic` | Claude models |

Embeddings can use a different provider/key than the main LLM. If `embeddings.api_key` is empty, the agent falls back to `llm.api_key` automatically.

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Introduction and usage examples |
| `/help` | Full command reference |
| `/status` | Uptime, LLM model, embeddings status, and today's token usage |
| `/tools` | List all built-in and generated tools |
| `/jobs` | List all scheduled jobs with last-run times |
| `/reset` | Save current task context to results memory and start fresh |
| `/reset discard` | Clear task context without saving |
| `/pair` | Generate or submit pairing token |
| `/unpair <id>` | Remove a user from the allowlist |
| `/myid` | Show your Telegram user ID |

Typing `/` in Telegram shows the full command list with descriptions (registered via BotFather's `setMyCommands`).

Or just send a natural language message:
- *"check disk usage"*
- *"is Docker running?"*
- *"show me the CPU temperature"*
- *"create a tool that lists all open ports"*
- *"remind me every day at 9am to check the backup logs"*

---

## Memory Architecture

The agent uses a four-tier memory system:

| Tier | Storage | Purpose |
|------|---------|---------|
| **Short-term** | In-memory (ring buffer, last 20 turns) | Recent conversation context injected into every prompt |
| **Working** | In-memory (current task) | Tracks the current goal, tool calls, and results; cleared on `/reset` |
| **Long-term** | `data/longterm_memory.json` (vector index) | Nightly summaries and manually added facts; semantically searchable |
| **Results** | `data/results_memory.json` (vector index) | Past task summaries saved on `/reset` or task completion |

When you send `/reset`, the working memory is summarised by the LLM and persisted to results memory before being cleared. Use `/reset discard` to skip saving.

---

## Requirements

```
python-telegram-bot==20.7
httpx==0.26.0
tomli==2.0.1
schedule==1.2.1
```

Python 3.9+ required. Python 3.11+ uses the built-in `tomllib` (no `tomli` needed).
