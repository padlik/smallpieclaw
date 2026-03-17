# Telegram Home Server Agent

A lightweight autonomous Telegram bot for controlling and querying your home server,
designed to run on a **Raspberry Pi with 2 GB RAM**. Uses a remote LLM for reasoning
and semantic tool discovery, so no heavy ML libraries run locally.

---

## Features

- **ReAct agent loop** — reasons step-by-step, executes tools, and loops until done
- **Semantic tool search** — finds the right tool using embedding-based cosine similarity
- **Self-building tools** — the LLM can create new `.sh`/`.py` tools when a capability is missing; creation is reported to the user immediately
- **Built-in tools** — `shell`, `file_read`, `file_write` always available; dangerous ops require inline-button confirmation
- **Secure Telegram bot** — allowlist or pairing-token access control
- **4-tier memory architecture** — short-term conversation history, working task context, long-term vector knowledge index, and results history
- **Configurable scheduler** — jobs defined in `scheduler.toml`; manage jobs from chat or via `/jobs`
- **Streaming responses** — bot edits its "Processing…" message in real time as the agent works
- **Multi-model LLM** — define multiple models with hint keywords; agent auto-selects; switch via `/models`
- **Multi-provider LLM** — OpenAI, OpenRouter, Google Gemini, Anthropic Claude
- **Context compaction** — auto-summarises older messages when the token budget is near the configured limit
- **Token usage tracking** — daily prompt/completion counters visible in `/status`

---

## Project Structure

```
main.py                  # Entry point
config.toml              # All configuration
scheduler.toml           # Scheduled job definitions
llm_client.py            # LLM + embeddings client (multi-provider, multi-model, token tracking)
agent_controller.py      # ReAct agent loop with 4-tier memory and context compaction
telegram_interface.py    # Telegram bot with security and streaming
tool_registry.py         # Discovers and registers tools
tool_executor.py         # Runs tools in subprocess
tool_index.py            # Semantic search over tool descriptions
tool_creator.py          # LLM-generated tools with safety validation
scheduler.py             # Background task scheduler
memory_store.py          # Short-term, working, long-term, and results memory
builtin_executor.py      # Always-available built-in tools (shell, file_read, file_write)
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
| `agent.default_model` | Must match the `model` field of one `[[models]]` entry |
| `embeddings.api_key` | API key for embeddings — if empty, falls back to the active model's key |
| `embeddings.model` | e.g. `text-embedding-3-small` |

> **Tip:** To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

### 5. Configure LLM models

Models are defined as `[[models]]` TOML array-of-tables. At least one entry is required.
The active model is chosen by matching `agent.default_model` to the `model` field of an entry.

| Field | Description |
|-------|-------------|
| `name` | Display name shown in `/models` |
| `provider` | `openai` \| `openrouter` \| `google` \| `anthropic` |
| `api_key` | Provider API key |
| `model` | Exact model identifier (e.g. `gpt-4o-mini`) |
| `base_url` | API base URL (leave empty for Anthropic/Google) |
| `max_tokens` | Max tokens in the LLM response |
| `temperature` | Sampling temperature |
| `hint` | Space-separated keywords for auto-selection at runtime |
| `request_timeout` | Per-request timeout in seconds (default: 120) |
| `max_retries` | Retry attempts on timeout/connection errors (default: 3) |
| `retry_delay` | Base retry delay in seconds, doubles each attempt (default: 2) |

```toml
[[models]]
name            = "default"
provider        = "openai"
api_key         = "sk-..."
model           = "gpt-4o-mini"
base_url        = "https://api.openai.com/v1"
max_tokens      = 1024
temperature     = 0.2
hint            = "general quick default"
request_timeout = 120
max_retries     = 3
retry_delay     = 2

[[models]]
name            = "smart"
provider        = "anthropic"
api_key         = "sk-ant-..."
model           = "claude-3-5-sonnet-20241022"
base_url        = ""
max_tokens      = 8192
temperature     = 0.2
hint            = "complex analyze reason large file code review"
request_timeout = 180
max_retries     = 3
retry_delay     = 2

[agent]
default_model = "gpt-4o-mini"   # must match one of the model = "..." values above
```

The agent auto-selects a model when `hint` keywords appear in the user's message, and users can switch manually with `/models`.

### 6. (Optional) Configure the scheduler

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

### 7. Run

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

## Built-in Tools

Three tools are always available to the agent regardless of the `tools/` directory:

| Tool | Description | Dangerous? |
|------|-------------|-----------|
| `shell` | Execute a shell command | Yes — if command matches destructive patterns (`rm -rf`, `dd`, `mkfs`, etc.) |
| `file_read` | Read a file from the filesystem | Yes — if path is sensitive (`/etc/passwd`, `.env`, `*.key`, etc.) |
| `file_write` | Write content to a file | Always — requires confirmation |

When a dangerous operation is requested, the bot sends an inline confirmation prompt:

> ⚠️ **Confirm operation**
> `rm -rf /tmp/test`
> [✅ Yes, execute] [❌ No, cancel]

The agent loop blocks until the user confirms or cancels (5-minute timeout).

The agent always prefers built-in tools over creating new scripts for shell, file read, and file write operations.

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
| Default model | _(first `[[models]]` entry)_ | `agent.default_model` |
| Max context tokens | 90 000 | `agent.ctx_max_tokens` |

Context compaction fires automatically at 85% of `ctx_max_tokens`. Older messages are summarised by the LLM and replaced with a compact bullet-point summary before the next request.

---

## Supported LLM Providers

| Provider | `provider` value | Notes |
|----------|-----------------|-------|
| OpenAI | `openai` | GPT-4o, GPT-4o-mini, etc. |
| OpenRouter | `openrouter` | Set `base_url = "https://openrouter.ai/api/v1"` |
| Google | `google` | Gemini models |
| Anthropic | `anthropic` | Claude models |

Embeddings can use a different provider/key than the main LLM. If `embeddings.api_key` is empty, the agent falls back to the active model's `api_key` automatically.

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Introduction and usage examples |
| `/help` | Full command reference |
| `/status` | Uptime, LLM model, embeddings status, and today's token usage |
| `/tools` | List all built-in and generated tools |
| `/models` | List available LLM models and switch the active one |
| `/jobs` | List all scheduled jobs with last-run times |
| `/reset` | Save current task context to results memory and start fresh |
| `/reset discard` | Clear task context without saving |
| `/reindex` | Force re-embed all tools in the semantic index |
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
