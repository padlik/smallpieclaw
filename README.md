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
- **Graph memory** (optional) — semantic entity/relationship/episode store backed by [LadybugDB](https://github.com/kuzudb/ladybug) (embedded graph DB); extracts entities and facts from conversations automatically in the background and injects relevant context per-turn; visible via `/status` and `/memory`; zero overhead when disabled
- **Configurable scheduler** — jobs defined in `scheduler.toml` (auto-updated at runtime); manage jobs from chat or via `/jobs`; supports cron-style schedules and one-time reminders; jobs staggered with ±5 min jitter to avoid thundering herd
- **Self-health diagnosis** — ask the agent in natural language (*"check agent health"*, *"analyze recent errors"*) or configure a periodic job in `scheduler.toml`; reads the log file, analyzes errors, suggests fixes, and rotates logs
- **Streaming responses** — bot edits its "Processing…" message in real time as the agent works
- **Typing indicator** — Telegram shows "typing…" while the agent is reasoning
- **Max-steps extension** — when the agent hits its step limit, inline buttons let you extend by 10 more steps, extend **unlimited**, or cancel; dangerous built-in actions offer an **Approve All** button to skip future confirmation prompts for the same action type
- **MCP server support** — connect external tools via [Model Context Protocol](https://modelcontextprotocol.io) servers; supports `stdio` (subprocess) and `http` transports with per-server headers and env variables; manage via `/mcp`
- **JSON mode enforcement** — the agent instructs the LLM to respond in JSON at the API level (provider-native where supported); non-JSON responses are coerced after 3 consecutive failures
- **Multi-model LLM** — define multiple models; switch via `/models`
- **Multi-provider LLM** — OpenAI, OpenRouter, Google Gemini, Anthropic Claude, Ollama (cloud & local); reasoning models (DeepSeek-R1, Kimi K2.5, QwQ) supported via `reasoning` field fallback
- **Multimodal vision** — send a photo with a caption and the agent forwards both the image and text to vision-capable models (GPT-4o, Claude, Gemini, LLaVA, etc.)
- **File uploads** — send any file (document, photo, audio, video, voice) via Telegram to save it to the agent's `downloads/` folder; photos with a caption are automatically routed to the agent
- **Context compaction** — auto-summarises older messages when the token budget approaches the configured limit (85% of `ctx_max_tokens`); keeps the context window healthy without operator intervention
- **Token usage tracking** — daily prompt/completion counters visible in `/status`
- **Agent Skills** — autonomous skill system (per [agentskills.io](https://agentskills.io/specification)) with progressive disclosure; skills listed via `/skills`
- **File storage guidance** — agent directed to use `/tmp/<agent>` for temporary files and `downloads/` for files the user wants to keep
- **`/stop` command** — immediately cancels the currently running agent task
- **Log rotation safe** — uses `WatchedFileHandler`; re-opens log file automatically after `logrotate` without restart
- **Structured log source tags** — every log line carries a `[source]` or `[source/model]` prefix so concurrent agents, sub-agents, and scheduled jobs are unambiguous in a single log file
- **Orchestrated multi-agent execution** — complex requests are broken into DAG-based execution plans, steps run in parallel sub-agents, and failures are retried or diagnosed automatically (see [Orchestrated Multi-Agent](#orchestrated-multi-agent))

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
graph_memory.py          # Optional LadybugDB graph store, background writer, and retrieval
backfill_graph_memory.py # One-time CLI to seed graph store from data/longterm_memory.json
memory_store.py          # Short-term, working, long-term, and results memory
builtin_executor.py      # Always-available built-in tools (shell, file_read, file_write)
skill_registry.py        # Agent Skills discovery and registry
mcp_client.py            # MCP server client (stdio + http transports)
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
    graph_memory             # LadybugDB graph store (created when graph memory is enabled)
    graph_memory_backfill_state.json  # Progress checkpoint for backfill_graph_memory.py
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

**Environment variable references** — any **string** value in `config.toml` can reference an environment variable:

| Syntax | Behaviour |
|--------|-----------|
| `"env:VAR"` | The entire string must be exactly `env:VAR` — replaced with the value of `VAR` at startup. Missing variable causes a startup error. |

```toml
[telegram]
bot_token = "env:TELEGRAM_BOT_TOKEN"

[[models]]
api_key = "env:OPENAI_API_KEY"
```

For values that need a prefix (e.g. `Bearer` tokens in MCP headers), put the full string in the env var and reference it directly:

```toml
[mcp_servers.headers]
Authorization = "env:MY_AUTH_HEADER"   # set MY_AUTH_HEADER="Bearer sk-..." in env
```

This keeps secrets out of the file. Export the variables in your shell, a `.env` loader (e.g. `direnv`, `dotenvx`), or a `systemd` service `EnvironmentFile=`.

For supported secret fields, the recommended approach is to store values in the agent's vault and reference them with the `sec:` prefix:

```toml
[telegram]
bot_token = "sec:TELEGRAM_BOT_TOKEN"

[providers.openai]
api_key = "sec:OPENAI_API_KEY"
base_url = "https://api.openai.com/v1"
```

The vault is a TOML file at `~/.local/share/<agent_name>/secrets.toml` (override via `SPC_VAULT_FILE`):

```toml
OPENAI_API_KEY     = "sk-..."
TELEGRAM_BOT_TOKEN = "1234567890:..."
OLLAMA_HOST        = "http://localhost:11434"
```

At runtime, the `secret_get` built-in tool lets the agent retrieve vault values with your confirmation. This is useful when skills reference unbound API keys or endpoints (e.g. "Set OLLAMA_HOST to your endpoint").

You can still use `env:` as a fallback (e.g. `env:OPENAI_API_KEY`), but `sec:` is preferred because values are resolved at startup and never cached in `os.environ`, so they are not leaked to shell/tool/MCP subprocesses.

Required settings:

| Key | Description |
|-----|-------------|
| `telegram.bot_token` | From [@BotFather](https://t.me/BotFather) |
| `telegram.security_mode` | `"allowlist"` or `"pairing"` |
| `telegram.allowed_user_ids` | Your Telegram user IDs (for allowlist mode) |
| `agent.default_model` | Must match the `model` field of one `[[models]]` entry |
| `embeddings.api_key` | API key for embeddings — if empty, falls back to the active model's key |
| `embeddings.model` | e.g. `text-embedding-3-small` |

Optional agent identity settings:

| Key | Default | Description |
|-----|---------|-------------|
| `agent.agent_name` | `"piclaw"` | Logical agent name used to derive default shared state locations such as `agent.agent_home` and the default vault directory. |
| `agent.agent_home` | `~/<agent_name>` | Shared state directory for the agent. The vault path is independent and remains `~/.local/share/<agent_name>/secrets.json` unless `SPC_VAULT_FILE` is set. |

> **Tip:** To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

### 5. Configure LLM models

Models are defined as `[[models]]` TOML array-of-tables. At least one entry is required.
The active model is chosen by matching `agent.default_model` to the `model` field of an entry.

| Field | Description |
|-------|-------------|
| `name` | Display name shown in `/models` |
| `provider` | `openai` \| `openrouter` \| `google` \| `anthropic` \| `ollama` |
| `api_key` | Provider API key (empty string for local Ollama; use `sec:` or `env:` prefix) |
| `model` | Exact model identifier (e.g. `gpt-4o-mini`) |
| `base_url` | API base URL (leave empty for Anthropic/Google; Ollama: `https://ollama.com` or `http://localhost:11434`) |
| `max_tokens` | Max tokens in the LLM response |
| `temperature` | Sampling temperature |
| `top_p` | Nucleus sampling probability (optional; omit to use provider default; ignored by OpenAI reasoning models) |
| `request_timeout` | Per-request timeout in seconds (default: 120) |
| `max_retries` | Retry attempts on timeout/connection errors (default: 5) |
| `retry_delay` | Base retry delay in seconds, doubles each attempt (default: 2) |
| `vision` | Set to `true` for vision-capable models; shown with 👁 badge in `/models` (optional) |

Provider-level defaults can be defined under `[providers.<name>]` and inherited by matching model entries. Supported provider fields: `api_key`, `base_url`, `request_timeout`, `max_retries`, `retry_delay`.

```toml
[providers.openai]
api_key = "sec:OPENAI_API_KEY"
base_url = "https://api.openai.com/v1"
request_timeout = 120
max_retries = 5
retry_delay = 2

[[models]]
name            = "default"
provider        = "openai"
model           = "gpt-4o-mini"
max_tokens      = 1024
temperature     = 0.2

[[models]]
name            = "smart"
provider        = "anthropic"
api_key         = "sec:ANTHROPIC_API_KEY"
model           = "claude-3-5-sonnet-20241022"
base_url        = ""
max_tokens      = 8192
temperature     = 0.2
request_timeout = 180
max_retries     = 5
retry_delay     = 2

# Ollama Cloud — requires: pip install ollama>=0.4.0
# API key from https://ollama.com/settings/keys
[[models]]
name            = "ollama-cloud"
provider        = "ollama"
api_key         = "sec:OLLAMA_API_KEY"
model           = "gpt-oss:120b-cloud"
base_url        = "https://ollama.com"
max_tokens      = 4096
temperature     = 0.2
request_timeout = 300
max_retries     = 3
retry_delay     = 2

# OpenAI-compatible providers (e.g. xAI Grok) use provider = "openai" with a custom base_url.
# See "Supported providers" section below for the full list.
# [[models]]
# name            = "grok"
# provider        = "openai"
# api_key         = "sec:XAI_API_KEY"
# base_url        = "https://api.x.ai/v1"
# model           = "grok-2"

[agent]
agent_name = "piclaw"
default_model = "gpt-4o-mini"   # must match one of the model = "..." values above
```

The agent does **not** auto-switch models based on message content. Switch models manually with `/models`.

### 6. (Optional) Configure the scheduler

Edit `scheduler.toml` to enable/disable jobs or change their schedule. All schedules use **5-field cron expressions** (local server time):

```
minute  hour  day  month  weekday
  0      2     *     *       *     → daily at 02:00
  0    */6     *     *       *     → every 6 hours (00:00, 06:00, 12:00, 18:00)
*/30    *      *     *       *     → every 30 minutes
  0      9     *     *       1     → every Monday at 09:00
```

```toml
[jobs.nightly_health]
enabled = true
schedule = "cron"
cron = "0 2 * * *"
task = "Run a full system health check and summarize the status."
notify = true

[jobs.disk_check]
enabled = true
schedule = "cron"
cron = "0 */6 * * *"
task = "Check disk usage on all mount points. Alert if any mount point is above 80% full."
notify = true

[jobs.longterm_memory_update]
enabled = true
schedule = "cron"
cron = "0 3 * * *"
task = "Summarize the key events and findings from today into long-term memory."
notify = false
```

For one-time reminders, use `schedule = "once"` with `run_at = "HH:MM"` — auto-removed after execution.

You can also add, pause, or remove jobs from the Telegram chat at runtime — either by asking the agent (it uses the `schedule` built-in tool) or directly via `/jobs` sub-commands:

- `/jobs` — list all jobs with status and next run time
- `/jobs reload` — hot-reload `scheduler.toml` from disk
- `/jobs remove <tag>` — permanently remove a job (shows refreshed list)
- `/jobs pause <tag>` — disable a job without removing it
- `/jobs resume <tag>` — re-enable a paused job

Scheduler features:
- **`scheduler.toml` is the single source of truth** — all jobs (including user-added reminders) live in this file; no hardcoded defaults exist in code
- **Cron scheduling**: all recurring jobs use 5-field cron expressions (local server time) via `croniter`
- **Backward compatibility**: old `schedule = "daily"` / `schedule = "interval"` configs are automatically migrated to cron on first load
- **One-time reminders**: `once` type with `run_at = "HH:MM"` — auto-removed after execution
- **Next-run visibility**: `/jobs` shows the next scheduled run time for each job
- **Running badge**: `/jobs` shows `🔄 Running` next to jobs currently executing as a sub-agent
- **Per-job model**: each job can specify its own `model` (defaults to `background_model`)
- **Context persistence**: set `preserve_context = true` to carry conversation history between runs (useful for trend analysis)
- **Overlap policy**: `overlap_policy = "skip"` (default) or `"parallel"` per job
- **Jitter**: a random ±5 min offset is applied to the first run of each cron job to avoid thundering herd
- **Persistence**: every structural change (add/remove/enable/disable) writes back to `scheduler.toml` — survives crashes and restarts
- **Run history**: `scheduler_state.json` stores `last_run` and `last_error` for every job ever executed (including removed and one-time jobs); history is restored on restart and survives `/jobs reload`
- **Tag resolution**: job tags are normalized (spaces, hyphens, and underscores are interchangeable), so `longterm-memory-update` and `longterm_memory_update` refer to the same job
- **Automatic backups**: before each write, the previous `scheduler.toml` is copied to `scheduler.toml.bak.YYYYMMDD_HHMMSS`; the last 5 backups are kept
- **Hot-reload**: adding a job via the built-in tool immediately reloads `scheduler.toml` into the live scheduler — no restart needed
- **Manual reload**: `/jobs reload` re-reads `scheduler.toml` from disk at any time
- **Error tracking**: job failures are reported in the Telegram chat and shown in `/jobs`

### 7. (Optional) Enable graph memory

Graph memory is opt-in. Skip this step if you don't need long-term semantic recall.

#### Prerequisites

Install the optional dependency:

```bash
pip install "ladybug>=0.7.0"
```

#### Configuration

Add to `config.toml`:

```toml
[graph_memory]
enabled              = true
db_path              = "data/graph_memory"   # path to the embedded DB file
buffer_pool_mb       = 256                   # LadybugDB buffer pool size
extraction_model     = ""                    # empty = use agent.default_model
extract_every_n_turns = 3                    # batch every N enqueued messages
min_message_length   = 100                   # ignore messages shorter than this
max_context_entries  = 10                    # max entities/facts injected per turn
```

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable/disable graph memory entirely |
| `db_path` | `data/graph_memory` | Path to the embedded LadybugDB file (created on first run) |
| `buffer_pool_mb` | `256` | In-process memory budget for the graph DB in MB |
| `extraction_model` | `""` | Model used for entity/fact extraction; falls back to `agent.default_model` if empty |
| `extract_every_n_turns` | `3` | How many chat messages to accumulate before triggering one extraction batch |
| `min_message_length` | `100` | Messages shorter than this (characters) are skipped |
| `max_context_entries` | `10` | Maximum entities, facts, and episodes injected into each turn's system prompt |

> **Resource note:** LadybugDB is embedded — it runs in the same process and uses no additional server. On a Raspberry Pi 2 GB, `buffer_pool_mb = 64` is a safe starting point.

#### Seeding from existing long-term memory

If you have entries in `data/longterm_memory.json` from previous sessions, import them once with the backfill script **while the main agent is not running**:

```bash
# Dry-run: count entries without touching the DB
python backfill_graph_memory.py --config config.toml --dry-run

# Import everything (incremental — skips already-imported entries)
python backfill_graph_memory.py --config config.toml

# Import in batches and watch progress
python backfill_graph_memory.py --config config.toml --limit 50 --verbose

# Re-import everything regardless of prior state
python backfill_graph_memory.py --config config.toml --force
```

After the initial seeding, the agent continues to grow the graph automatically from each chat session — no further backfill is needed.

### 8. Run

```bash
python main.py
```

To run as a systemd service on the Raspberry Pi:

For a production **systemd user service**, prefer systemd credentials and file-backed secrets:

```ini
# ~/.config/systemd/user/telegram-agent.service
[Unit]
Description=Telegram Home Server Agent
After=network-online.target

[Service]
WorkingDirectory=%h/telegram-agent
ExecStart=%h/telegram-agent/.venv/bin/python main.py
LoadCredential=openai_api_key:%h/.local/share/smallpieclaw/secrets/openai_api_key
LoadCredential=telegram_bot_token:%h/.local/share/smallpieclaw/secrets/telegram_bot_token
Environment=OPENAI_API_KEY_FILE=%d/openai_api_key
Environment=TELEGRAM_BOT_TOKEN_FILE=%d/telegram_bot_token
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Then enable it as the service user:

```bash
systemctl --user daemon-reload
systemctl --user enable --now telegram-agent
```

`WorkingDirectory=` must point at the project root because `config.toml`, `tools/`, `data/`, and related relative paths are resolved from the process working directory.

Use `%d` for the runtime credentials directory instead of hard-coding `/run/credentials/...`; user-service credential paths vary by systemd version and runtime context.

Compatibility fallback: `EnvironmentFile=` or wrappers such as 1Password, Doppler, or Infisical may inject secret values into the environment. That remains supported through `env:VAR`, but those values are inherited by shell/tool/MCP subprocesses and may be exposed through process environment inspection depending on host hardening.

Legacy system service example:

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

The following built-in tools are always available to the agent regardless of the `tools/` directory. The two graph-memory tools (`memory_graph_search`, `memory_graph_store`) require `[graph_memory] enabled = true` and the `ladybug` package — they return a clear error message when unavailable:

| Tool | Description | Dangerous? |
|------|-------------|-----------|
| `shell` | Execute a shell command | Yes — if command matches destructive patterns (`rm -rf`, `dd`, `mkfs`, etc.) |
| `file_read` | Read a file. Supports `offset` (negative = from end, e.g. `-5000` reads last 5 KB like `tail`) | Yes — if path is sensitive (`/etc/passwd`, `.env`, `*.key`, etc.) |
| `file_write` | Write content to a file | Always — requires confirmation |
| `file_send` | Send a local file or photo to the Telegram chat | No |
| `schedule` | Manage scheduled jobs and reminders | No |
| `spawn_agent` | Spawn an isolated background sub-agent | No |
| `memory_write` | Read/write the agent's persistent key-value memory (`data/memory.json`) | No |
| `memory_graph_search` | Search the graph memory store for entities, facts, and episodes relevant to a query | No — requires graph memory enabled |
| `memory_graph_store` | Store a note, episode, or fact directly in graph memory and trigger background extraction | No — requires graph memory enabled |

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

### `memory_write` tool

Reads or writes the agent's persistent memory store (`data/memory.json`) — the key-value facts visible in the `PERSISTENT MEMORY` section of the system prompt.

| Action | Args | Description |
|--------|------|-------------|
| `set` | `key`, `value` | Store any value under a key |
| `append` | `key`, `value` | Append an item to a list key (creates the list if needed) |
| `delete` | `key` | Remove a key |
| `get` | `key` | Read a single key without parsing the full prompt |

Examples:
```json
{"action": "append", "key": "notes",       "value": "Disk replaced 2025-04-01"}
{"action": "set",    "key": "last_backup", "value": "2025-04-05"}
{"action": "delete", "key": "old_service"}
{"action": "get",    "key": "notes"}
```

Memory is shared across all sessions and persisted immediately to disk after every write.

### `memory_graph_search` tool

Searches the graph memory store for entities, relationships, and episodes relevant to a natural-language query. Returns a formatted context block injected into the response — or "No relevant entities or facts found" when the graph is empty or the query has no match.

Requires `[graph_memory] enabled = true` and `ladybug` installed. Returns a clear error message when graph memory is unavailable so the agent can fall back gracefully.

```json
{"action": "memory_graph_search", "query": "What databases are used in this project?"}
```

### `memory_graph_store` tool

Stores a note, observation, or fact directly in the graph memory and triggers immediate background extraction. Useful for the agent to explicitly record information it wants to recall across sessions.

| Arg | Required | Description |
|-----|----------|-------------|
| `content` | ✓ | Text to store (also stored as an Episode) |
| `entity_type` | — | Optional hint for the extraction model (default: `other`) |
| `user_id` | — | Attribution tag (default: `agent`) |

```json
{"action": "memory_graph_store", "content": "The main database is PostgreSQL 16 on /dev/sda2.", "entity_type": "database"}
```

Both tools are no-ops when graph memory is disabled and return a clear error message.

---

## File Uploads

Send any file directly in the Telegram chat to save it to the agent's `downloads/` folder:

| File type | Behaviour |
|-----------|-----------|
| **Photo with caption** | File is saved **and** caption + image are forwarded to the agent |
| **Photo without caption** | File is saved; bot confirms path and size |
| **Document, audio, video, voice** | File is saved; bot confirms path and size |

Photos with a caption are the primary way to trigger multimodal tasks:

> **Send:** 📷 *(screenshot of an error message)* + caption: *"What does this error mean?"*
> **Agent:** Reads the screenshot and explains the error.

Files saved this way are accessible by the agent at their full path for subsequent tasks.

---

## Multimodal Vision

For models that support image input (GPT-4o, Claude 3+, Gemini, LLaVA, etc.), the agent can analyse photos you send directly from Telegram.

### How to use

1. **Send a photo with a caption** — the image is saved and the caption becomes the agent's task:
   - *📷 + "What's in this image?"*
   - *📷 + "Read the text in this screenshot"*
   - *📷 + "Is there anything wrong with this network diagram?"*

2. **Reference a saved file** — after uploading any file, you can ask the agent to process it:
   - *"Analyse the file at /home/pi/downloads/photo_abc123.jpg"*

### Configuration

Mark models as vision-capable in `config.toml` to display the 👁 badge in `/models`:

```toml
[[models]]
name    = "vision"
model   = "gpt-4o"
vision  = true      # informational — enables 👁 badge in /models
provider = "openai"
api_key  = "sk-..."
base_url = "https://api.openai.com/v1"
max_tokens = 2048
temperature = 0.2
```

The `vision` flag is **informational only** — image encoding is always attempted when images are present. Models without vision support will return an API error which is shown to the user.

### Supported providers

| Provider | Vision support |
|----------|---------------|
| OpenAI (`gpt-4o`, `gpt-4o-mini`) | ✅ inline `data:` URL |
| Anthropic (`claude-3+`) | ✅ base64 source block |
| Google Gemini | ✅ `inline_data` part |
| Ollama (LLaVA, llama3.2-vision, etc.) | ✅ `images` field |

Files > 20 MB are skipped with a warning (Telegram photos are typically ≤ 1 MB so this limit is rarely reached).

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

### Orchestrated Multi-Agent

For complex requests, the agent can generate a structured execution plan and
run each step inside its own isolated sub-agent. This makes multi-step work
faster and more reliable without blocking the chat:

- **Structured prompts with creativity modes** — the prompt loader assembles the
  system prompt from Jinja2 sections and can switch to different "modes" (e.g.
  `planner`, `explorer`, `resilient`) that change how creative or cautious the
  agent is.
- **Execution planning with parallel sub-agents** — a request is converted into a
  DAG of tool calls; independent steps run in parallel, dependent steps wait
  for their prerequisites, and the result is summarised back to the chat.
- **Two-tier error recovery** — transient errors (`tool_timeout`,
  `network_error`, `syntax_error`) are retried with exponential backoff; other
  errors spawn a diagnostic sub-agent that suggests a new approach before the
  parent agent re-plans.
- **Strategy memory** — the agent stores and recalls learned approaches for
  recurring task types ("for disk checks, run `df` and `smartctl` in parallel"),
  so it gets better at similar requests over time.
- **Sub-agent context sharing** — a sub-agent receives a compact summary of the
  parent agent's goal, recent tool results, and relevant memory so it starts
  with the context it needs instead of an empty conversation.

| Parameter | Default | Config key |
|-----------|---------|------------|
| Agent name | `piclaw` | `agent.agent_name` |
| Agent home | `~/<agent_name>` | `agent.agent_home` |
| Max agent steps (interactive) | 8 | `agent.max_iterations` |
| Max agent steps (scheduled/sub-agents) | 100 | `agent.scheduled_max_iterations` |
| Long-run watcher threshold | 30 min | `agent.long_run_warn_minutes` |
| Tool timeout | 10 s | `agent.tool_timeout` |
| Max tool output | 4000 chars | `agent.max_output_size` |
| Semantic top-K tools | 3 | `agent.top_tools` |
| Default model | _(first `[[models]]` entry)_ | `agent.default_model` |
| Max context tokens | 90 000 | `agent.ctx_max_tokens` |
| Empty-response diagnostics | off | `agent.diagnose_empty_responses` |

When the agent reaches `max_iterations`, inline buttons appear in the chat:
**⏩ Extend 10 more steps**, **♾ Unlimited**, or **❌ Cancel** (2-minute timeout).
Choosing **Unlimited** lets the agent run until the task is complete (internal safety
ceiling still applies). This prevents silent failures while still giving the operator
control over runaway tasks.

For dangerous built-in actions that require confirmation (e.g. `shell` with destructive
commands), an **✅ Approve All** button is offered alongside the per-action **Yes/No**
buttons. Choosing **Approve All** suppresses further confirmation prompts for the same
action type for the rest of the current task.

Scheduled jobs and sub-agents use `scheduled_max_iterations` (default 100) instead of the
interactive limit. Set to `0` for no limit (internal safety ceiling: 500). Individual jobs
can override this with a `max_iterations` field in `scheduler.toml`.

Context compaction fires automatically at 85% of `ctx_max_tokens`. Older messages are summarised by the LLM and replaced with a compact bullet-point summary before the next request. This is the normal context-window protection mechanism — no manual intervention needed. Use `/reset` to save context and start fresh, or `/reset discard` to clear without saving.

---

## Supported LLM Providers

| Provider | `provider` value | Notes |
|----------|-----------------|-------|
| OpenAI | `openai` | GPT-4o, GPT-4o-mini, etc. |
| OpenRouter | `openrouter` | Set `base_url = "https://openrouter.ai/api/v1"` |
| Google | `google` | Gemini models |
| Anthropic | `anthropic` | Claude models |
| Ollama Cloud | `ollama` | `base_url = "https://ollama.com"` — hosted cloud models (gpt-oss, deepseek, kimi-k2, etc.) |
| Ollama Local | `ollama` | `base_url = "http://localhost:11434"` — local instance, no API key needed |
| xAI Grok | `openai` | `base_url = "https://api.x.ai/v1"` — OpenAI-compatible endpoint |

Any provider with an OpenAI-compatible HTTP API can be used with `provider = "openai"` and a custom `base_url`. Examples: xAI Grok, Together, Fireworks, local LM Studio.

Embeddings can use a different provider/key than the main LLM. If `embeddings.api_key` is empty, the agent falls back to the active model's `api_key` automatically.

### Ollama Setup

Requires the official `ollama` Python package:

```bash
pip install "ollama>=0.4.0"
```

**Cloud API** — access hosted large models directly:

1. Create an API key at [ollama.com/settings/keys](https://ollama.com/settings/keys)
2. Browse available cloud models at [ollama.com/search?c=cloud](https://ollama.com/search?c=cloud)
3. Configure in `config.toml`:

```toml
[[models]]
name     = "ollama-cloud"
provider = "ollama"
api_key  = "YOUR_OLLAMA_API_KEY"
model    = "gpt-oss:120b-cloud"
base_url = "https://ollama.com"
```

**Local instance** — run models on your own hardware:

```toml
[[models]]
name     = "ollama-local"
provider = "ollama"
api_key  = ""
model    = "gemma3:27b"
base_url = "http://localhost:11434"
```

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
| `/status` | Uptime, LLM model, embeddings status, tools/skills count, sub-agent count, scheduler state, graph memory health (when enabled), system time, and per-model token usage today |
| `/tools` | List all built-in, generated, and MCP tools |
| `/skills` | List all available agent skills |
| `/models` | List available LLM models and switch the active one (👁 badge marks vision-capable models) |
| `/jobs` | List all scheduled jobs with running status; sub-commands: `reload`, `remove <tag>`, `pause <tag>`, `resume <tag>` |
| `/agents` | List all active background sub-agents; `/agents cancel <id>` to stop one |
| `/mcp` | Manage MCP servers: `list`, `on <name>`, `off <name>`, `info <name>` |
| `/stop` | Cancel the currently running agent task |
| `/reset` | Save current task context to results memory and start fresh |
| `/reset discard` | Clear task context without saving |
| `/verbose` | Toggle live tool-call progress messages; `/verbose on` or `/verbose off` to set explicitly |
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
| `/memory` | Graph memory diagnostics — **Store** (entity/fact/episode counts, latest episode timestamp, vector index status), **Writer** (worker alive, queue depth, extraction counters, failure counts), and **Retrieval** (hit/miss/injection counts). Only shown when graph memory is enabled. |
| `/compress` | Advanced: manually rewrite `ShortTermMemory` into a single LLM-generated summary without clearing task context. Normal users should rely on automatic compaction; use `/reset` to start fresh. |

#### Graph memory health states (`/status` line)

When graph memory is enabled, `/status` shows a compact health line:

```
🧠 Graph Memory: 🟢 active-used | 47 entities · 91 facts · 28 episodes | hits 14 / misses 3 | writer ok, queue 0
```

| State | Meaning |
|-------|---------|
| `active-empty` 🟡 | Store initialised; no entities or episodes yet |
| `active-learning` 🟢 | Writer has processed at least one batch but no retrieval hit yet |
| `active-used` 🟢 | Graph context has been injected into at least one turn |
| `*-degraded` 🟠 | Any of: writer thread stopped, write failures, vector index probe failed |
| 🔴 failed | Enabled in config but store did not initialise — check logs |

Or just send a natural language message:
- *"check disk usage"*
- *"is Docker running?"*
- *"show me the CPU temperature"*
- *"create a tool that lists all open ports"*
- *"remind me every day at 9am to check the backup logs"*

Or send a **photo with a caption**:
- 📷 *"What does this error message say?"*
- 📷 *"Is this network diagram correct?"*
- 📷 *"Read the text from this screenshot"*

---

## Memory Architecture

The agent uses a four-tier memory system with an optional semantic graph layer:

| Tier | Storage | Purpose |
|------|---------|---------|
| **Short-term** | In-memory (ring buffer, last 20 turns) | Recent conversation context injected into every prompt |
| **Working** | In-memory (current task) | Tracks the current goal, tool calls, and results; cleared on `/reset` |
| **Long-term** | `data/longterm_memory.json` (vector index) | Nightly summaries and manually added facts; semantically searchable |
| **Results** | `data/results_memory.json` (vector index) | Past task summaries saved on `/reset` or task completion |
| **Graph memory** (optional) | `data/graph_memory` (LadybugDB) | Entities, typed relationships, and conversation episodes extracted from chat; semantically retrieved per-turn and injected into the system prompt |

When you send `/reset`, the working memory is summarised by the LLM and persisted to results memory before being cleared. Use `/reset discard` to skip saving.

### Graph memory in detail

Graph memory is an **opt-in semantic layer** that runs alongside the other tiers when `[graph_memory] enabled = true`. It differs from the existing tiers:

- **Structure**: stores a graph of typed entities (`person`, `tool`, `service`, `concept`, …) connected by labelled relationships (`USES`, `DEPENDS_ON`, `RUNS_ON`, …) and timestamped episode nodes.
- **Automatic extraction**: every `extract_every_n_turns` messages, a background thread calls the extraction model, parses entity/fact JSON, and upserts the results into LadybugDB without blocking the agent turn.
- **Per-turn retrieval**: at the start of each ReAct loop, the agent's goal is embedded and used to probe the HNSW vector index on entities and episodes; relevant results are injected into the system prompt as an untrusted-memory block (clearly labelled, never overriding explicit instructions).
- **Privacy-safe logging**: logs expose only counts (`extracted 3 entities, 2 facts from 4 messages`), health states, and queue depths — never entity names, fact text, or episode content.
- **Relationship to long-term memory**: `data/longterm_memory.json` is a flat vector index of text summaries; graph memory is a structured relational store. Use `backfill_graph_memory.py` to import existing long-term entries into the graph at initial setup time.

---

## Sub-Agents

The agent can spawn **isolated background sub-agents** — separate ReAct loops running in their own threads with a dedicated LLM context. Sub-agents are used for:

- **Non-blocking tasks** — long-running jobs (log analysis, multi-step diagnostics) run in the background while chat stays responsive
- **Model selection** — use a smarter or cheaper model for a specific task without changing the main agent's model
- **Scheduler jobs** — every scheduled job is executed as a sub-agent

### How it works

Sub-agents are always **asynchronous**. The main agent's `spawn_agent` tool returns immediately with a spawned status. Results are delivered via Telegram when the sub-agent finishes and written to long-term memory.

```
Main agent                          Sub-agent thread
──────────                          ────────────────
spawn_agent(task="...",             SubAgentRunner.run(task)
            model="gpt-4o")          ├── own LLMClient (model override)
→ {status: "spawned",                ├── own ShortTermMemory (blank or loaded)
   agent_id: "sa-a1b2c3"}            ├── own WorkingMemory (blank)
                                      └── shared: tools, skills, long-term memory
                                              │
                                     on finish:
                                       ├── notify user via Telegram
                                       └── write result to long-term memory
```

### spawn_agent tool

The LLM can call `spawn_agent` when it decides a task is long-running or requires a different model:

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task` | ✓ | Natural-language description of the task for the sub-agent |
| `model` | — | Model ID from `[[models]]` config; defaults to `background_model` |
| `context_key` | — | Key for persisting sub-agent context across runs (useful for recurrent jobs) |

Sub-agents cannot call `spawn_agent` themselves (max depth: 1).

### Notifications

**On start** (main agent reply):
```
🤖 Sub-agent started
ID: sa-a1b2c3 | Model: gpt-4o-mini
Task: Analyze Docker container logs for errors...
I'll notify you when it's done.
```

**On success**:
```
✅ Sub-agent sa-a1b2c3 finished (42s)
Model: gpt-4o-mini

[result text, up to 3000 chars]
```

**On failure** (always notified, even for silent jobs):
```
❌ Sub-agent sa-a1b2c3 failed (12s)
Error: LLM timeout after 3 retries
```

### Configuration

Add to `config.toml`:

```toml
[agent]
# Model used by background tasks and scheduled jobs (defaults to default_model)
background_model = "gpt-4o-mini"
```

### Scheduler integration

Each scheduled job runs as a sub-agent. Per-job model and context options can be set in `scheduler.toml`:

```toml
[jobs.agent_health]
enabled = true
cron = "0 */4 * * *"
task = "Perform a self-health check: analyze agent.log for errors and summarize status."
notify = true
model = "gpt-4o-mini"           # optional: override model for this job
preserve_context = true          # optional: persist context between runs (default: false)
context_max_messages = 50        # optional: cap on saved messages (default: 50)
overlap_policy = "skip"          # optional: skip|parallel when previous run is still active
max_iterations = 50              # optional: per-job step cap (overrides scheduled_max_iterations)
```

**`notify` flag** — set `notify = false` to suppress Telegram output for a job entirely.
The job runs silently; results are only written to the log file. Useful for high-frequency
monitoring jobs that should only alert on anomalies (the agent's task prompt can
still send explicit messages via the `notify_user` tool when thresholds are exceeded).

**Step limits** — scheduled jobs use `scheduled_max_iterations` (default 100) to allow
complex multi-step automation that would be too long for an interactive session. Override
per-job with `max_iterations` in the TOML or when creating a job from chat.

### Long-running agent watcher

When a sub-agent or scheduled job runs longer than `long_run_warn_minutes` (default 30),
the operator receives a Telegram notification:

```
⏱ Sub-agent running for 35m
Job: disk_check | Model: gpt-4o-mini
Task: Check disk usage and alert if above 85%...
Agent ID: sa-abc123 — use /agents to monitor or cancel
```

Each agent is warned only once. Set `long_run_warn_minutes = 0` to disable the watcher.

When `preserve_context = true`, the sub-agent's conversation history is saved to
`data/job_contexts/<job_tag>.json` after each run and reloaded on the next. This lets
recurrent jobs track progress and spot trends over time.

### Overlap handling

If a scheduled job hasn't finished when its next run time arrives, the default policy is `skip`:
- Logged at WARNING level in `agent.log`
- Next run is skipped silently (no Telegram notification)
- `/jobs` shows `🔄 Running` next to the job name

Use `overlap_policy = "parallel"` to allow concurrent instances (advanced use only).

### Managing sub-agents

```
/agents                    → list all active sub-agents with model, task preview, elapsed time
/agents cancel sa-a1b2c3   → cooperatively cancel a sub-agent (takes effect between iterations)
/agents cancel agent_health → cancel a scheduled job's sub-agent by job tag
```

Cancellation is cooperative — a running LLM call completes before the cancel is checked. The sub-agent stops at the next iteration boundary.

### Token usage

`/status` shows a per-model token breakdown across all active LLM clients (main agent + all sub-agents):

```
📊 Token Usage Today:
  gpt-4o-mini          2,341 + 456  =  2,797 total
  gpt-4o               8,234 + 1,203 = 9,437 total
  ──────────────────────────────────────────
  Total               10,575 + 1,659 = 12,234 total
```

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

### Source tags

Every log line carries a consistent source prefix so concurrent agents, sub-agents, and scheduler jobs are unambiguous in a shared log file.

| Source | Tag format | Example |
|--------|-----------|---------|
| Main agent | `[main]` | `[main] step 2/25 \| model: gemma4:27b` |
| Sub-agent | `[sa-<id>]` | `[sa-fcf85d] step 4/10 \| model: kimi-k2.5:cloud` |
| Built-in tool (shell, file_read, …) | `[<caller>]` | `[sa-fcf85d] Built-in shell executing: yt-dlp …` |
| LLM retries / errors | `[<caller>/<model>]` | `[sa-fcf85d/kimi-k2.5:cloud] Empty LLM response (attempt 1/3)` |
| Scheduled job | `[sched/<tag>]` | `[sched/morning-report] Running scheduled job` |

This makes it straightforward to `grep` a single sub-agent's full activity:

```bash
grep '\[sa-fcf85d\]' agent.log
```

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

## MCP Servers

The agent supports external tool servers via the [Model Context Protocol](https://modelcontextprotocol.io) (MCP). MCP servers expose additional tools that appear alongside built-in and generated tools in the agent's ReAct loop.

### Transports

| Transport | When to use |
|-----------|-------------|
| `stdio` | Subprocess — MCP server is a local command (e.g. `npx …`, Python script) |
| `http` | HTTP/HTTPS — MCP server is a remote or local HTTP service |

### Configuration (`config.toml`)

```toml
# Filesystem MCP server via stdio
[[mcp_servers]]
name      = "filesystem"
transport = "stdio"
command   = ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
enabled   = true
timeout   = 30        # seconds (default: 30)

[mcp_servers.env]     # optional environment variables for the subprocess
MY_ENV_VAR = "value"

# Remote API MCP server via HTTP
[[mcp_servers]]
name      = "my-api"
transport = "http"
url       = "https://api.example.com/mcp"
enabled   = true
timeout   = 30

[mcp_servers.headers]   # optional HTTP headers (e.g. auth)
Authorization = "Bearer your-token-here"
```

`enabled = false` loads the server definition but does not connect at startup. You can activate it later with `/mcp on <name>`.

### Telegram commands

| Command | Description |
|---------|-------------|
| `/mcp list` | Show all configured servers with transport type and status |
| `/mcp on <name>` | Connect a server and register its tools |
| `/mcp off <name>` | Disconnect a server and remove its tools |
| `/mcp info <name>` | Show server details, tool list, and last error |

MCP errors and tool calls are written to `agent.log` with the prefix `MCP [<name>]`.

---

## Requirements

```
python-telegram-bot>=21.0
httpx~=0.27
requests>=2.31
tomli==2.0.1
schedule==1.2.1
croniter>=1.4
ollama>=0.4.0
```

**Optional** — required only when `[graph_memory] enabled = true`:

```
ladybug>=0.7.0
```

Python 3.9+ required. Python 3.11+ uses the built-in `tomllib` (no `tomli` needed).

> **Note for Python 3.10 (Raspberry Pi default):** `tomllib` is only stdlib in 3.11+. The `tomli` package in `requirements.txt` provides it. Run `pip install -r requirements.txt` to ensure it is installed.
