# Agent Capabilities Reference

> This document is a static human-readable reference for contributors and operators.
> It describes all features available in the smallpieclaw agent.
>
> **Last updated:** 2026-05-21

---

## Decision Guidance

These substitutions help the agent make qualified tool choices:

| Need | Use instead of |
|---|---|
| Fetch a URL / call an HTTP API | `shell` + `curl` (no native web-search tool) |
| Query a database | `shell` + `sqlite3` / `psql` / `mysql -e` |
| Small targeted file edits | `file_patch` (not `file_read` + `file_write`) |
| Process/transform a large file | `shell` + `awk`/`sed`/`jq`/`python3 -c` |
| Run independent tasks in parallel | `spawn_agent` × N, then `get_agent_result` × N |
| Analyse an image or photo | `vision_query` (not `shell` + base64) |
| Send a file to the user | `file_send` (not copy-pasting content) |
| Remember a fact across sessions | `memory_write` action=set/append |
| Trigger a recurring job | `schedule` action=add with cron= |

---

## Built-in Tools

Always available — prefer these before creating new tools or using registered tools.

### `shell`
Execute any shell command on the host.
- `command` (str, required)
- `timeout` (int, default 30 s)
- Requires confirmation for destructive patterns (`rm -rf`, writes to `/etc`, etc.)

### `file_read`
Read a file from the filesystem.
- `path` (str, required)
- `max_bytes` (int, default 50 000)
- `offset` (int, default 0; negative = read from end, like `tail`)

### `file_write`
Write content to a file.
- `path` (str, required)
- `content` (str, required)
- `mode` (str: `w` overwrite | `a` append, default `w`)
- Always requires confirmation.

### `file_patch`
Surgical search-and-replace inside a file. **Prefer this for small targeted edits.**
- `path` (str, required)
- `old_str` (str, required — include enough context to be unambiguous)
- `new_str` (str, required — empty string to delete)
- `occurrence` (int, default 1; 0 = replace all)
- Returns error without touching the file if `old_str` not found or ambiguous.
- Always requires confirmation (shows diff-style preview).

### `file_send`
Send a local file or photo to the Telegram chat.
- `path` (str, required)
- `caption` (str, optional)

### `vision_query`
Ask the LLM to analyse a local image file. **Use for any "what's in this image" request.**
- `path` (str, required — absolute path)
- `question` (str, required)
- Only works with vision-capable models (GPT-4o, Claude 3+, Gemini, etc.).

### `schedule`
Manage scheduled background jobs.
- `action`: `list` | `add` | `remove` | `pause` | `resume` | `run_now`
- `tag` (str, unique job name)
- `task` (str, REQUIRED for add — the natural-language goal)
- `cron` (str, 5-field cron in local time, e.g. `0 */6 * * *`)
- `notify` (bool, default true — send Telegram message when done)
- `model` (str, optional — model id for this job's sub-agent)
- `preserve_context` (bool, default false — keep conversation history between runs)
- `max_iterations` (int, optional; 0 = unlimited)

### `spawn_agent`
Spawn an isolated sub-agent in the background. Returns `agent_id` immediately.
Sub-agents have **no access** to main agent memory, history, or files unless
explicitly included in the `task` string.
- `task` (str, REQUIRED — self-contained instructions, include ALL context)
- `model` (str, optional — model id from AVAILABLE MODELS)
- `response_format` (str: `text` | `json` | `file`, default `text`)
- `context_key` (str, optional — persist conversation history between calls)
- Sub-agents **cannot** spawn further sub-agents (depth limit = 1).

### `get_agent_result`
Wait for a sub-agent and retrieve its result. **Always pair with `spawn_agent`.**
- `agent_id` (str, required)
- `timeout` (int, optional — seconds, default from config)
- Returns: `{status, result_type, result}`

### `memory_write`
Read/write the agent's persistent memory (`data/memory.json`).
- `action`: `set` | `append` | `delete` | `get`
- `key` (str), `value` (any JSON-serialisable value)
- Keys starting with `_` are internal and protected from automatic purge.
- Do NOT store model names or provider configuration — these are always injected fresh.

---

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | List all commands |
| `/status` | Current agent status, model, active tasks |
| `/stop` | Cancel the currently running task |
| `/reset` | Clear conversation context (optionally save to results memory) |
| `/compress` | Summarise and compress current conversation history in place |
| `/verbose` | Toggle verbose progress updates |
| `/jobs` | List / add / remove / pause / resume / run scheduled jobs |
| `/agents` | List / cancel active sub-agents |
| `/tools` | List all registered tools (built-in + custom) |
| `/skills` | List available skills |
| `/mcp` | List / enable / disable MCP servers; show server info |
| `/reindex` | Rebuild the semantic tool index |
| `/pair` | Pair a new Telegram user (via one-time code) |
| `/unpair` | Remove a paired user |
| `/myid` | Show your Telegram user ID |
| `/health` | Quick health check (memory, scheduler, MCP status) |
| `/ctx` | Show current context snapshot (system prompt preview) |
| `/env` | Show runtime environment (paths, config values) |
| `/models` | List configured models; switch active model |

---

## Scheduler

Cron-based background jobs managed via the `schedule` built-in tool or `/jobs` command.

- Each job runs as an isolated sub-agent with its own LLM context.
- Jobs with `notify=false` run silently (no Telegram message on completion).
- Jobs with `notify=true` (default) send a Telegram message when done.
- `preserve_context=true` keeps conversation history between runs (stateful jobs).
- Scheduler config persists in `data/scheduler.toml` (or path from config).
- Jobs survive restarts.

---

## Sub-Agents

Pattern for parallel or model-specific work:

```
spawn_agent(task="...", model="gpt-4o")   → agent_id
spawn_agent(task="...", model="gemini-2") → agent_id2
get_agent_result(agent_id)                → result
get_agent_result(agent_id2)               → result2
```

Sub-agents are isolated — pass ALL necessary data in the `task` string.
Use `response_format="json"` when the result needs structured parsing.
Use `response_format="file"` when the result is a file (returns path).

---

## Memory System

Three layers of persistent memory:

| Layer | Scope | Tool |
|---|---|---|
| `MemoryStore` (short-term KV) | Persists across sessions | `memory_write` |
| `LongTermMemory` | Semantic search of past summaries | internal |
| `ResultsMemory` | Indexed results of past tasks | internal |

**Key rules for `memory_write`:**
- Values must be native JSON (object, array, number, string) — do NOT pre-serialize.
- Use `append` on key `notes` to accumulate persistent notes.
- Never store model names, API keys, or provider config — these are always stale.
- Internal keys (`_` prefix) are protected from automatic cleanup.

---

## MCP Servers (Model Context Protocol)

MCP servers provide additional tools beyond the built-ins.

**Supported transports:**
- `stdio` — spawns a local process and communicates via stdin/stdout
- `web` (HTTP/HTTPS) — connects to a remote MCP endpoint

**Not supported:** OAuth authentication flows.

**Per-server configuration options:**
- HTTP headers (for bearer tokens, API keys, etc.)
- Environment variables (injected into stdio process)
- `enabled` flag (can be toggled via `/mcp on|off <name>`)

**Agent commands:**
- `/mcp list` — show all configured servers with transport and status
- `/mcp on <name>` / `/mcp off <name>` — activate/deactivate
- `/mcp info <name>` — show server details and available tools

---

## Skills

Skills extend the agent with domain-specific knowledge via `SKILL.md` files.

- Located in `skills/<name>/SKILL.md`
- Activated by reading the SKILL.md file: `file_read(path="skills/<name>/SKILL.md")`
- SKILL.md contains YAML frontmatter + Markdown instructions
- Skill instructions describe how to accomplish tasks using existing tools
- Scripts/assets in the skill directory are accessed via absolute paths
- List available skills with `/skills`

---

## Key Behaviors

### Operator Confirmation
Required before: dangerous shell commands, any file write, tool creation.
The operator can "Approve All" for a tool type to skip further confirmations in the same task.

### Task Cancellation
`/stop` signals cancellation. The current step finishes, then the run exits cleanly.

### Context Compaction
When conversation context approaches the token limit (85% of `ctx_max_tokens`),
the middle of the conversation is automatically summarised to free up space.
`/compress` triggers this manually.

### Step Extension
When `max_iterations` is reached, the agent asks the operator to extend (+10 steps),
run unlimited, or stop. The operator has 2 minutes to respond.

### Tool Creation
The agent can propose new Python or bash tools for operator approval.
Approved tools are saved to `tools_generated/` and immediately available.
One-off scripts can be run without saving.
