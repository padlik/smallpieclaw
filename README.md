# Telegram Home Server Agent

A lightweight autonomous Telegram bot that controls and queries your server using a remote LLM for reasoning and semantic tool discovery — no heavy ML libraries run locally.

---

## Features

- **ReAct agent loop** — step-by-step reasoning, tool execution, repeat until done
- **Semantic tool search** — embedding-based cosine similarity to pick the right tool
- **Built-in tools** — `shell`, `file_read`, `file_write`, `file_send`, `schedule` always available; dangerous ops require inline confirmation
- **Secure Telegram bot** — allowlist or pairing-token access control
- **4-tier memory** — short-term conversation, working task context, long-term vector index, and results history
- **Graph memory** (optional) — entity/relationship store via [LadybugDB](https://github.com/kuzudb/ladybug); automatic background extraction; context injected per turn
- **Cron scheduler** — jobs defined in `scheduler.toml`; manage from chat or `/jobs`; ±5 min jitter, hot-reload, overlap policy
- **Streaming responses** — bot edits its message in real time as the agent works
- **MCP server support** — [Model Context Protocol](https://modelcontextprotocol.io) via `stdio` (subprocess) and `http` transports
- **Multi-provider LLM** — OpenAI, OpenRouter, Google Gemini, Anthropic Claude, Ollama (cloud & local)
- **Multimodal vision** — send a photo with a caption; image + text forwarded to vision-capable models
- **File uploads** — any Telegram file saved to `downloads/`
- **Context compaction** — auto-summarises at 85% of token budget; no manual intervention
- **Agent Skills** — reusable task guides per [agentskills.io](https://agentskills.io/specification)
- **Orchestrated multi-agent** — DAG-based parallel sub-agents with two-tier error recovery and strategy memory
- **Token usage tracking** — daily prompt/completion counters per model visible in `/status`

---

## Prerequisites

- Python 3.9+ (3.11+ recommended)
- A Telegram bot token — create one with [@BotFather](https://t.me/BotFather)
- At least one LLM provider API key (OpenAI, Anthropic, Google, OpenRouter) or a local Ollama instance
- An embeddings API key — can be the same as the LLM key
- Your Telegram user ID — message [@userinfobot](https://t.me/userinfobot) to find it

---

## Install

```bash
git clone <your-repo> ~/telegram-agent
cd ~/telegram-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Configure Basics

### Secrets & environment references

Any **string** value in `config.toml` can reference a secret or environment variable:

| Prefix | Resolved from | Notes |
|--------|--------------|-------|
| `sec:KEY` | Agent vault file | **Preferred** — never leaked to subprocesses |
| `env:VAR` | Shell environment at startup | Visible to tool/MCP subprocesses |

The default vault path is `~/.local/share/<agent_name>/secrets.toml`. Override with `SPC_VAULT_FILE`.

Example vault file:

```toml
# ~/.local/share/myagent/secrets.toml
OPENAI_API_KEY     = "<your-openai-key>"
TELEGRAM_BOT_TOKEN = "<your-telegram-token>"
```

At runtime the `secret_get` built-in lets the agent retrieve vault values with your confirmation — useful when skills reference unbound API keys.

### Telegram

```toml
[telegram]
bot_token        = "sec:TELEGRAM_BOT_TOKEN"
security_mode    = "allowlist"    # or "pairing"
allowed_user_ids = []             # add your numeric Telegram user ID
```

> **Pairing mode:** run `/pair` in the bot to generate a single-use token; share it with another user who runs `/pair <token>` to gain access.

### One model (minimum)

```toml
[providers.openai]
api_key  = "sec:OPENAI_API_KEY"
base_url = "https://api.openai.com/v1"

[[models]]
name        = "default"
provider    = "openai"
model       = "gpt-4o-mini"
max_tokens  = 1024
temperature = 0.2

[agent]
agent_name    = "myagent"
default_model = "gpt-4o-mini"    # must match the model = "..." value above
```

Additional per-model fields: `vision` (bool), `top_p`, `request_timeout`, `max_retries`, `retry_delay`.

Provider-level defaults under `[providers.<name>]` are inherited by all matching model entries.

### Embeddings

```toml
[embeddings]
model   = "text-embedding-3-small"
api_key = ""    # empty = inherit from the active model's provider key
```

---

## Run Locally

```bash
source .venv/bin/activate
python main.py
```

On first run the agent creates `data/` and `downloads/`. Logs go to `~/.local/state/<agent_name>/logs/` — human-readable `agent.log` plus structured `agent.jsonl` (daily gzip rotation, 30 backups).

> **Upgrading from a prior version?** The hand-written tool system (`tools/`, `tools_generated/`, `create_tool` action) has been removed. These directories are no longer scanned. You can safely delete them: `rm -rf tools/ tools_generated/`. Any `tools_dir` or `generated_tools_dir` keys in your `config.toml` are now silently ignored.

---

## Optional Service Setup

Run the agent under your platform's process manager. Generic systemd user-service example (substitute your actual paths):

```ini
# ~/.config/systemd/user/telegram-agent.service
[Unit]
Description=Telegram Home Server Agent
After=network-online.target

[Service]
WorkingDirectory=/path/to/telegram-agent
ExecStart=/path/to/telegram-agent/.venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now telegram-agent
```

> `WorkingDirectory` **must** point at the project root — `config.toml` and `data/` are resolved relative to it.

**Secrets in service:** use the vault (`sec:` prefix in `config.toml`), `EnvironmentFile=`, or a secrets manager (1Password, Doppler, Infisical). Note: `env:` values are inherited by tool/MCP subprocesses.

---

## Using the Bot

### Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Introduction and usage examples |
| `/help` | Full command reference |
| `/status` | Uptime, model, tools, scheduler state, graph memory health, per-model token usage |
| `/tools` | List all built-in and MCP tools |
| `/skills` | List available agent skills |
| `/models` | List LLM models and switch active one (👁 = vision-capable) |
| `/jobs` | Scheduled jobs; sub-commands: `reload`, `pause <tag>`, `resume <tag>`, `remove <tag>` |
| `/agents` | Active sub-agents, scheduled jobs, and plan-step agents (with source category); `/agents cancel <id>` to stop one |
| `/dir` | List and remove trusted directories: `list`, `del <n>` |
| `/mcp` | Manage MCP servers: `list`, `on <name>`, `off <name>`, `info <name>` |
| `/stop` | Cancel the current running task |
| `/reset` | Save context to results memory and start fresh |
| `/reset discard` | Clear context without saving |
| `/verbose` | Toggle live tool-call progress; `/verbose on` or `/verbose off` |
| `/reindex` | Force re-embed all tools |
| `/pair` / `/unpair <id>` | Pairing-mode access management |
| `/myid` | Show your Telegram user ID |

Hidden diagnostic commands (not in menu, available to authorised users): `/show_ctx`, `/show_env`, `/memory`, `/compress`.

### Natural-language interaction

Send any message to start a task:

```
check disk usage
is Docker running?
show me the CPU temperature
remind me every day at 9am to check backup logs
```

### File uploads & vision

Send any file to Telegram to save it to `downloads/`. Photos with a caption are forwarded directly to the agent:

```
📷 + "What does this error message say?"
📷 + "Read the text in this screenshot"
📷 + "Is there anything wrong with this network diagram?"
```

Supported vision providers: OpenAI (`gpt-4o`), Anthropic (`claude-3+`), Google Gemini, Ollama (LLaVA, llama3.2-vision). The `vision = true` field in config adds a 👁 badge in `/models` — image encoding is always attempted when images are present.

### Built-in tools reference

| Tool | Description | Confirmation required? |
|------|-------------|----------------------|
| `shell` | Execute shell commands | Yes — if destructive pattern |

The `shell` tool has three backends, selected by `shell_backend` in `[agent]`:

- **`subprocess`** (default) — cross-platform, fully buffered. Every dangerous pattern requires confirmation.
- **`pty`** — POSIX only; gives commands a real TTY for line buffering, colour, and progress bars. Same confirmation rules as `subprocess`.
- **`nsjail`** — Linux only; runs commands inside a kernel-level sandbox (mount/PID/net/user/IPC/UTS/cgroup namespaces + cgroup v2 limits). Confines blast radius to the project dir and explicitly trusted RW dirs. The confirmation gate becomes configurable via `shell_nsjail_confirm_mode` (`always` | `adaptive` | `never`); when nsjail is inactive (binary missing or non-Linux host) all modes fall back to `always`. See [`docs/nsjail-setup.md`](docs/nsjail-setup.md) for installation and prerequisites.
| `file_read` | Read a file; `offset: -5000` reads last 5 KB | Yes — if outside trusted zones |
| `file_write` | Write content to a file | Yes — if outside trusted zones |
| `file_send` | Send a file or photo to Telegram chat | No |
| `schedule` | Manage scheduled jobs and reminders | No |
| `spawn_agent` | Spawn a background sub-agent | No |
| `memory_write` | Read/write persistent key-value memory (`data/memory.json`) | No |
| `memory_graph_search` | Search graph memory (requires graph memory enabled) | No |
| `memory_graph_store` | Store a fact/episode in graph memory | No |

### File access zones

`file_*` tools use zone-based access control. Every path is classified as:

- **Trusted** — inside `workspace_dir` (default `~/Documents`) or a user-added trusted directory. Reads and writes auto-allow with no confirmation.
- **Request-granted** — a directory the user approved for the current request via the `[Allow this request]` button. Allowed for the rest of the current request.
- **Unrecognised** — anything else, including agent-internal directories (`data/`, `skills/`, log dir, vault dir). Prompts the user.

Out-of-zone prompts offer four options: **Approve** (once), **Deny**, **Allow this request** (grants the parent directory for the current request), and **Add to trusted** (persists to `~/.local/state/<agent_name>/trusted_dirs.json` under `XDG_STATE_HOME`).

Trusted directory entries support an optional `mode` field — `"r"` (read-only) auto-allows reads but still prompts for writes; `"rw"` (default) auto-allows both.

Agent-internal directories are always unrecognised even if a parent directory is trusted — the LLM must use dedicated built-ins (`memory_read`, `secret_get`, `log_query`) for agent-internal data.

Use the `/dir` command to list and remove user-added trusted directories: `/dir list`, `/dir del <n>`.

Per-request grants reset at the start of each new user message.

When a dangerous operation is requested the bot sends an inline confirmation prompt. For recurring dangerous actions, **Approve All** suppresses further prompts for the same action type for the rest of the current task.

When the agent hits the interactive step limit (`max_iterations`, default 8), inline buttons offer **Extend 10**, **Unlimited**, or **Cancel**.

---

## Extending the Agent

### Agent Skills

Skills live in `skills/<name>/SKILL.md` — Markdown guides with YAML frontmatter that tell the agent *how* to approach a task type (not callable tools themselves):

```markdown
---
name: my-skill
description: What this skill does and when to use it.
---

# My Skill

Instructions for the agent...
```

Required frontmatter: `name` (must match directory name, lowercase + hyphens), `description`. List with `/skills`; trigger explicitly with *"use skill my-skill"*. Skills activate automatically when a task matches.

### MCP Servers

Connect external tool servers via [Model Context Protocol](https://modelcontextprotocol.io):

```toml
# Subprocess (stdio)
[[mcp_servers]]
name      = "filesystem"
transport = "stdio"
command   = ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
enabled   = true
timeout   = 30

[mcp_servers.env]
MY_ENV_VAR = "value"

# Remote HTTP
[[mcp_servers]]
name      = "my-api"
transport = "http"
url       = "https://api.example.com/mcp"
enabled   = true

[mcp_servers.headers]
Authorization = "env:MY_AUTH_HEADER"    # set MY_AUTH_HEADER="Bearer ..." in env
```

`enabled = false` loads the definition without connecting. Manage live with `/mcp on <name>` / `/mcp off <name>`.

---

## Advanced Topics

### Scheduler

`scheduler.toml` is the single source of truth for all jobs. All schedules use **5-field cron expressions** (local server time):

```
minute  hour  day  month  weekday
  0      2     *     *       *     → daily at 02:00
  0    */6     *     *       *     → every 6 hours
*/30    *      *     *       *     → every 30 minutes
```

```toml
[jobs.nightly_health]
enabled  = true
schedule = "cron"
cron     = "0 2 * * *"
task     = "Run a full system health check and summarize the status."
notify   = true

[jobs.disk_check]
enabled = true
cron    = "0 */6 * * *"
task    = "Check disk usage. Alert if any mount point is above 80%."
notify  = true
```

One-time reminders: `schedule = "once"` with `run_at = "HH:MM"` — auto-removed after execution.

Per-job options: `model`, `preserve_context`, `context_max_messages`, `overlap_policy` (`skip`/`parallel`), `max_iterations`, `notify`.

Key features: hot-reload without restart (`/jobs reload`), automatic backups before each write (last 5 kept), ±5 min jitter on first cron run, run history in `data/scheduler_state.json`.

### Memory architecture

| Tier | Storage | Purpose |
|------|---------|---------|
| Short-term | In-memory (last 20 turns) | Recent conversation context injected every prompt |
| Working | In-memory (current task) | Goal, tool calls, results; cleared on `/reset` |
| Long-term | `data/longterm_memory.json` (vector index) | Nightly summaries; semantically searchable |
| Results | `data/results_memory.json` (vector index) | Past task summaries saved on `/reset` |
| Graph (opt-in) | `data/graph_memory` (LadybugDB) | Entities, relationships, episodes; retrieved per turn |

Context compaction fires automatically at 85% of `ctx_max_tokens` — older messages are summarised by the LLM without operator intervention. Use `/reset` to start fresh, or `/reset discard` to skip saving.

### Graph memory (optional)

Install the extra dependency:

```bash
pip install "ladybug>=0.7.0"
```

Add to `config.toml`:

```toml
[graph_memory]
enabled               = true
db_path               = "data/graph_memory"
buffer_pool_mb        = 256          # use 64 on low-memory hosts
extraction_model      = ""           # empty = use agent.default_model
extract_every_n_turns = 3
min_message_length    = 100
max_context_entries   = 10
```

LadybugDB is embedded — it runs in-process with no separate server.

Seed from existing long-term memory (while agent is stopped):

```bash
python backfill_graph_memory.py --config config.toml --dry-run   # count entries
python backfill_graph_memory.py --config config.toml             # import
python backfill_graph_memory.py --config config.toml --limit 50 --verbose
python backfill_graph_memory.py --config config.toml --force     # re-import all
```

Graph memory health is visible in `/status` and `/memory`. Health states: `active-empty` 🟡, `active-learning` 🟢, `active-used` 🟢, `*-degraded` 🟠, `failed` 🔴.

### Sub-agents

Sub-agents are isolated background ReAct loops — non-blocking, own LLM context, result delivered via Telegram and written to long-term memory. Every scheduled job runs as a sub-agent.

Key configuration:

```toml
[agent]
background_model           = "gpt-4o-mini"    # default model for sub-agents / scheduler
max_iterations             = 8                # interactive step limit
scheduled_max_iterations   = 100             # sub-agent / scheduler limit (0 = no limit)
long_run_warn_minutes      = 30              # Telegram notification threshold (0 = off)
tool_timeout               = 10              # seconds per tool call
ctx_max_tokens             = 90000
```

Per-job overrides in `scheduler.toml`: `model`, `max_iterations`, `preserve_context`, `overlap_policy`.

Manage sub-agents: `/agents` to list, `/agents cancel <id>` or `/agents cancel <job_tag>` to stop.

Sub-agents cannot spawn further sub-agents (max depth: 1).

### LLM providers & resilience

| `provider` value | Provider | Notes |
|-----------------|----------|-------|
| `openai` | OpenAI | GPT-4o, GPT-4o-mini, etc. |
| `openrouter` | OpenRouter | `base_url = "https://openrouter.ai/api/v1"` |
| `google` | Google Gemini | Leave `base_url` empty |
| `anthropic` | Anthropic Claude | Leave `base_url` empty |
| `ollama` | Ollama Cloud | `base_url = "https://ollama.com"` |
| `ollama` | Ollama Local | `base_url = "http://localhost:11434"`, no API key |
| `openai` | Any OAI-compatible | Custom `base_url` (xAI Grok, Together, Fireworks, LM Studio) |

Ollama requires the official Python package: `pip install "ollama>=0.4.0"`.

Switch models at runtime with `/models`. The agent does not auto-switch based on message content.

Resilience behaviours:
- **Native tool calling** — models that support it (Kimi, GLM, DeepSeek, Gemini, Ollama) use the provider's native tool-calling API directly, bypassing JSON parsing entirely; falls back to the text-based JSON path automatically when the provider doesn't support tools, on transient errors, or when the model returns text instead of tool calls
- **Retries** — exponential backoff on timeout/connection errors; live status in Telegram
- **Empty responses** — retried at HTTP level; enable `diagnose_empty_responses = true` in `[agent]` for full HTTP diagnostic logging when they persist
- **Non-JSON prose** — coerced up to 2 times without consuming a step or polluting history
- **Multiple JSON objects** — brace-counting parser extracts the correct `{"action":…}` block
- **Reasoning model support** — `reasoning`/`reasoning_content` field fallback (DeepSeek-R1, Kimi K2.5, QwQ)

### Logging

Logs live under the XDG state directory `~/.local/state/<agent_name>/logs/`, resolved from `agent_name` independently of `agent_home` (a relative `[paths] log_file` lands here; an absolute path overrides). Logging is built on [`structlog`](https://www.structlog.org) integrated with stdlib, writing two sinks from one processor chain so they never drift:

- **`agent.jsonl`** — structured JSON-per-line, the **primary** machine-readable surface. Each event carries identity fields (`trace` and `agent`, the run label), a `level`, and an `event_type` from a closed taxonomy (`TOOL_START/END/FAILED`, `LLM_CALL/FAILED`, `STEP_BEGIN/END`, `RUN_BEGIN/END`, `ERROR`) plus key-values (`tool`, `dur_ms`, `exit`, `err`).
- **`agent.log`** — human-readable prose (secondary), keeping the familiar `[label trace] message` shape for `tail -f`/`grep`.

Both rotate daily with date-suffixed, gzip-compressed backups (30 kept, `log_backup_count`); known vault secret values are redacted from both before serialization. The agent can introspect its own run mid-execution with the **`log_query`** built-in tool — an in-process filter over the active `agent.jsonl`, trace-scoped to the current run by default.

Every line carries a source tag for unambiguous filtering:

| Source | Tag |
|--------|-----|
| Main agent | `[main]` |
| Sub-agent | `[sa-<id>]` |
| Scheduled job | `[sched/<tag>]` |
| LLM retry/error | `[<caller>/<model>]` |

```bash
# Follow a single sub-agent
grep '\[sa-fcf85d\]' ~/.local/state/<agent_name>/logs/agent.log
```

File storage defaults (override in `[paths]`):

| Directory | Purpose |
|-----------|---------|
| `downloads/` | Files the user wants to keep |
| System temp (e.g. `tmp_dir = "/tmp/myagent"`) | Temporary files; cleaned on reboot |

---

## Development

```bash
make install-dev   # pip install -r requirements-dev.txt
make test          # pytest tests/ -v --tb=short
make lint          # ruff check . && vulture . vulture_whitelist.py --min-confidence 80
make check         # lint + test — run before committing
```

Run a single test file or test:

```bash
pytest tests/test_react_loop.py -v
pytest tests/test_react_loop.py::TestExtractJsonCandidates::test_single_object -v
```

> Add new public API symbols that vulture flags as unused to `vulture_whitelist.py`.

### Project structure

```
main.py                  # Entry point & composition root
config.toml              # All configuration
scheduler.toml           # Scheduled job definitions
agent_controller.py      # Thin orchestrator — builds ReactContext, delegates to react_loop.py
react_loop.py            # Canonical ReAct loop logic
llm_client.py            # Multi-provider LLM + embeddings client (token tracking)
builtin_executor.py      # Built-in tools (shell, file_read/write, schedule, spawn_agent, memory)
tool_registry.py         # MCP tool registry
tool_index.py            # Semantic tool search via embedding cosine similarity
memory_store.py          # Short-term, working, long-term, results memory
graph_memory.py          # Optional LadybugDB graph store + background writer
backfill_graph_memory.py # One-time CLI to seed graph from data/longterm_memory.json
scheduler.py             # Cron job scheduler (scheduler.toml is source of truth)
mcp_client.py            # MCP server client — stdio and http transports
skill_registry.py        # Agent Skills discovery
telegram_interface.py    # Telegram bot, security, streaming
interfaces.py            # Protocol classes for structural typing (LLMProvider, ToolBackend, …)
config_schema.py         # Typed config dataclasses (AppConfig, AgentConfig, ModelConfig, …)
exceptions.py            # Exception hierarchy (AgentError → LLMError, ToolError, MCPError, …)
prompt_builder.py        # System prompt assembly; re-exports estimate_tokens
token_estimator.py       # Two-layer token counting (tiktoken + heuristic fallback)
context_manager.py       # Auto-compaction at 85% of ctx_max_tokens
skills/                  # Agent Skills — skills/<name>/SKILL.md
data/                    # Runtime state: tool_index, memory, scheduler state, graph DB
tests/                   # Test suite (pytest); execution harness in tests/execution_harness.py
```
