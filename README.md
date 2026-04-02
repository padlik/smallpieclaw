# Telegram Home Server Agent

A lightweight autonomous Telegram bot for controlling and querying your home server,
designed to run on a **Raspberry Pi with 2 GB RAM**. Uses a remote LLM for reasoning
and semantic tool discovery, so no heavy ML libraries run locally.

---

## Features

- **ReAct agent loop** — reasons step-by-step, executes tools, and loops until done
- **Semantic tool search** — finds the right tool using embedding-based cosine similarity
- **Self-building tools** — the LLM can propose new `.py`/`.sh` tools; operator reviews code and chooses **Create**, **Run Once**, or **Cancel** via inline buttons
- **Built-in tools** — `shell`, `file_read`, `file_write`, `file_send`, `schedule` always available; dangerous ops require inline-button confirmation
- **Secure Telegram bot** — allowlist or pairing-token access control
- **4-tier memory architecture** — short-term conversation history, working task context, long-term vector knowledge index, and results history
- **Configurable scheduler** — jobs defined in `scheduler.toml` (auto-updated at runtime); manage jobs from chat or via `/jobs`; supports recurring (daily/interval) and one-time reminders; interval jobs staggered with ±5 min jitter to avoid thundering herd
- **Self-health diagnosis** — `/health` command and automatic 4-hour periodic job: reads log file, analyzes errors, suggests fixes, rotates logs
- **Streaming responses** — bot edits its "Processing…" message in real time as the agent works
- **Typing indicator** — Telegram shows "typing…" while the agent is reasoning
- **Max-steps extension** — when the agent hits its step limit, inline buttons let you extend by 10 more steps or cancel
- **Multi-model LLM** — define multiple models with hint keywords; agent auto-selects; switch via `/models`
- **Multi-provider LLM** — OpenAI, OpenRouter, Google Gemini, Anthropic Claude; reasoning models (DeepSeek-R1, Kimi K2.5, QwQ) supported via `reasoning` field fallback
- **Context compaction** — auto-summarises older messages when the token budget is near the configured limit
- **Token usage tracking** — daily prompt/completion counters visible in `/status`
- **Agent Skills** — autonomous skill system (per [agentskills.io](https://agentskills.io/specification)) with progressive disclosure; skills listed via `/skills`
- **File storage guidance** — agent directed to use `/tmp/<agent>` for temporary files and `downloads/` for files the user wants to keep
- **Log rotation safe** — uses `WatchedFileHandler`; re-opens log file automatically after `logrotate` without restart

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
skill_registry.py        # Agent Skills discovery and registry
tools/                   # Built-in tools (.sh and .py)
tools_generated/         # Tools created by the LLM at runtime
skills/                  # Agent Skills (each skill is a subdirectory with SKILL.md)
    system-health/
        SKILL.md         # Example skill
data/
    tool_index.json          # Persisted embedding vectors for tools
    memory.json              # Persistent agent key-value memory
    longterm_memory.json     # Long-term vector knowledge index
    results_memory.json      # Past task summaries and results
    scheduler_state.json     # Scheduler run history (last_run + last_error for all jobs ever executed)
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
| `hint` | Space-separated keywords; model is **not** auto-selected — only used for display purposes. Switch models manually with `/models` |
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

The agent does **not** auto-switch models based on message content. Switch models manually with `/models`.

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

You can also add, pause, or remove jobs from the Telegram chat at runtime (the agent uses the `schedule` built-in tool), or use `/jobs` to see all active jobs.

Scheduler features:
- **`scheduler.toml` is the single source of truth** — all jobs (including user-added reminders) live in this file; no hardcoded defaults exist in code
- **Recurring jobs**: `daily` (at a fixed time) or `interval` (every N hours/minutes)
- **One-time reminders**: `once` type with `run_at = "HH:MM"` — auto-removed after execution
- **Jitter**: interval jobs get a random ±5 min offset at startup to avoid thundering herd when multiple jobs share the same interval
- **Persistence**: every structural change (add/remove/enable/disable) writes back to `scheduler.toml` — survives crashes and restarts
- **Run history**: `scheduler_state.json` stores `last_run` and `last_error` for every job ever executed (including removed and one-time jobs); history is restored on restart and survives `/jobs reload`
- **Tag resolution**: job tags are normalized (spaces, hyphens, and underscores are interchangeable), so `longterm-memory-update` and `longterm_memory_update` refer to the same job
- **Automatic backups**: before each write, the previous `scheduler.toml` is copied to `scheduler.toml.bak.YYYYMMDD_HHMMSS`; the last 5 backups are kept
- **Hot-reload**: adding a job via the built-in tool immediately reloads `scheduler.toml` into the live scheduler — no restart needed
- **Manual reload**: `/jobs reload` re-reads `scheduler.toml` from disk at any time
- **Error tracking**: job failures are reported in the Telegram chat and shown in `/jobs`

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

Five tools are always available to the agent regardless of the `tools/` directory:

| Tool | Description | Dangerous? |
|------|-------------|-----------|
| `shell` | Execute a shell command | Yes — if command matches destructive patterns (`rm -rf`, `dd`, `mkfs`, etc.) |
| `file_read` | Read a file. Supports `offset` (negative = from end, e.g. `-5000` reads last 5 KB like `tail`) | Yes — if path is sensitive (`/etc/passwd`, `.env`, `*.key`, etc.) |
| `file_write` | Write content to a file | Always — requires confirmation |
| `file_send` | Send a local file or photo to the Telegram chat | No |
| `schedule` | Manage scheduled jobs and reminders | No |

When a dangerous operation is requested, the bot sends an inline confirmation prompt:

> ⚠️ **Confirm operation**
> `rm -rf /tmp/test`
> [✅ Yes, execute] [❌ No, cancel]

The agent loop blocks until the user confirms or cancels (5-minute timeout).

The agent always prefers built-in tools over creating new scripts for shell, file read, and file write operations.

### `file_send` tool

Sends any local file or photo from the server to the Telegram chat:

```
file_send(path="/home/pi/documents/photo.png", caption="Here you go")
```

- Images (`.jpg`, `.png`, `.gif`, `.webp`, `.bmp`) are sent as photos
- All other files are sent as documents
- Files larger than 50 MB are rejected with a clear error
- `~` home paths are expanded automatically

---

## File Storage

The agent is instructed to use specific directories for different file types:

| Directory | Purpose | Config key |
|-----------|---------|------------|
| `/tmp/<agent_name>` | Temporary files — QR codes, downloaded configs, intermediate outputs. Cleaned by OS on reboot. | `paths.tmp_dir` |
| `downloads/` | Permanent downloads — files the user wants to keep and access later. | `paths.downloads_dir` |

Both directories are created automatically at startup. The agent is told never to write files into the script directory.

Override in `config.toml`:

```toml
[paths]
downloads_dir = "/home/pi/agent-downloads"
tmp_dir       = "/tmp/myagent"
```

---

## Writing Custom Tools

Create a `.py` or `.sh` file in the `tools/` directory.
The file **must** include a `description:` comment near the top (first 15 lines).
Multi-line descriptions are supported by continuing the comment with extra indentation:

```bash
#!/bin/bash
# description: check disk usage across all mount points
#   and alert if any volume exceeds 90% capacity
df -h
```

**Tool creation policy:** Tools must follow the **UNIX paradigm** — one tool, one task.
A tool should be reusable across many scenarios, not a single-use script. For one-off
tasks, the agent uses the `shell` built-in directly.

When the LLM proposes creating a tool, the operator receives the full source code and
can choose:
- **✅ Create Tool** — save to `tools_generated/` for permanent reuse
- **⚡ Run Once** — execute the code immediately as a one-off script (not saved)
- **❌ Cancel** — reject the proposal; agent tries a different approach

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
| Empty-response diagnostics | off | `agent.diagnose_empty_responses` |

When the agent reaches `max_iterations`, inline buttons appear in the chat:
**⏩ Extend 10 more steps** or **❌ Cancel** (2-minute timeout). This prevents
silent failures while still giving the operator control over runaway tasks.

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

### LLM Resilience

The client handles transient failures transparently:

- **Timeout/connection retries** — exponential backoff (`retry_delay` doubles each attempt); live retry status shown in Telegram (`⏳ LLM request failed (timeout), retry 1/3…`)
- **Empty responses** — if the provider returns an empty string (network glitch), it is retried at the HTTP level before the agent sees it
- **Non-JSON prose** — if the LLM returns prose instead of a JSON action, the agent retries in-place up to 2 times without consuming a step or polluting the message history
- **Multiple JSON objects** — a brace-counting parser extracts the correct `{"action":…}` object even when the model wraps it in explanatory text or emits multiple objects
- **Reasoning model support** — if `content` is empty but `reasoning` or `reasoning_content` is populated (DeepSeek-R1, Kimi K2.5, QwQ, etc.), the agent uses that field transparently and logs a warning

#### Empty-response diagnostics

When empty responses persist through all retries, enable diagnostic mode to identify the root cause:

```toml
[agent]
diagnose_empty_responses = true
```

This logs at `ERROR` level and includes:
1. Full HTTP response — status code, headers, raw body
2. Stream/non-stream mismatch check — detects SSE responses sent to a non-streaming client
3. `finish_reason` check — surfaces `content_filter`, `length` truncation, etc.
4. `curl` probe — re-runs the same request via subprocess and logs the output

Disable after diagnosing to keep logs clean.

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Introduction and usage examples |
| `/help` | Full command reference |
| `/status` | Uptime, LLM model, embeddings status, tools/skills count, and today's token usage |
| `/health` | Run self-health diagnosis, analyze logs, rotate log file |
| `/tools` | List all built-in and generated tools |
| `/skills` | List all available agent skills |
| `/models` | List available LLM models and switch the active one |
| `/jobs` | List all scheduled jobs; `/jobs reload` to reload scheduler.toml from disk |
| `/reset` | Save current task context to results memory and start fresh |
| `/reset discard` | Clear task context without saving |
| `/reindex` | Force re-embed all tools in the semantic index |
| `/pair` | Generate or submit pairing token |
| `/unpair <id>` | Remove a user from the allowlist |
| `/myid` | Show your Telegram user ID |

Typing `/` in Telegram shows the full command list with descriptions (registered via BotFather's `setMyCommands`).

### Hidden diagnostic commands

These commands are not shown in the Telegram menu but are available to authorized users:

| Command | Description |
|---------|-------------|
| `/show_ctx` | Download the current LLM system prompt as `context.md` (with estimated token count) |
| `/show_env` | Show the shell environment the agent runs commands in — all env vars (secrets redacted), PATH entries per line, and configured agent paths |

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

## Agent Skills

The agent supports **Agent Skills** — reusable task guides following the [agentskills.io specification](https://agentskills.io/specification).

### What is a Skill?

A skill is a directory inside `skills/` containing a `SKILL.md` file with YAML frontmatter and Markdown instructions. Skills are *not* tools — they are instructions that guide the agent on how to approach a specific type of task.

```
skills/
    system-health/
        SKILL.md          ← Required: YAML frontmatter + Markdown instructions
        scripts/          ← Optional: helper scripts
        references/       ← Optional: reference documents
        assets/           ← Optional: templates, data files
```

### SKILL.md format

```markdown
---
name: system-health
description: Comprehensive system health diagnostics for Linux/Raspberry Pi.
license: MIT
compatibility: Linux
---

# Skill instructions (Markdown)
...
```

Required fields: `name` (must match directory name, lowercase + hyphens), `description`.

### How it works

1. **Startup** — all skill names and descriptions are injected into the agent's system prompt
2. **Activation** — when a task matches a skill, the agent reads the full `SKILL.md` via `file_read`
3. **Execution** — the agent follows the skill's instructions, using available tools

To explicitly trigger a skill: *"use skill system-health"*

### Adding a skill

Create a directory with a valid `SKILL.md`:

```bash
mkdir skills/my-skill
cat > skills/my-skill/SKILL.md << 'EOF'
---
name: my-skill
description: What this skill does and when to use it.
---

# My Skill

Instructions for the agent...
EOF
```

The skill will be available on next agent startup (or after `/reset`).

### Managing skills

- `/skills` — list all available skills with name and description (truncated to 80 chars)

### Important: skill-described capabilities are not tools

SKILL.md files may describe sub-commands, binary flags, or helper operations using the word "tools". These are **documentation only** — the agent must implement them via `shell`, `file_read`, or other registered tools. The agent will not call names from a SKILL.md as if they were registered tools.

---

## Logging

The agent handles log rotation internally — no `logrotate` or external tooling required.

### How it works

- **Active log**: always `agent.log` (or the path set in config) — the agent writes here continuously
- **Rotation**: every night at **00:00 local time** the active log is rotated
- **Linux-style numbered suffixes** — same convention as logrotate without `dateext`:

```
agent.log        ← always the active log (today)
agent.log.1      ← yesterday
agent.log.2      ← 2 days ago
…
agent.log.30     ← oldest (deleted when limit is reached)
```

### Configuration

```toml
[paths]
log_file         = "agent.log"   # default: <agent_dir>/agent.log
log_backup_count = 30            # number of rotated files to keep (default: 30)
```

The `log_file` default is always anchored to the directory containing `main.py`, regardless of the working directory the process is launched from.

---

## Requirements

```
python-telegram-bot==20.7
httpx~=0.25.2
tomli==2.0.1
schedule==1.2.1
```

Python 3.9+ required. Python 3.11+ uses the built-in `tomllib` (no `tomli` needed).

> **Note for Python 3.10 (Raspberry Pi default):** `tomllib` is only stdlib in 3.11+. The `tomli` package in `requirements.txt` provides it. Run `pip install -r requirements.txt` to ensure it is installed.
