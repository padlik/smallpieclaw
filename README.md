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
- **Multi-provider LLM** — OpenAI, OpenRouter, Google Gemini, Anthropic Claude, Ollama Cloud
- **Multimodal vision** — send a photo with a caption; image + text sent to the pinned model (provider support determines whether it succeeds)
- **File uploads** — any Telegram file saved to `downloads/`
- **Context compaction** — auto-summarises older history when the estimated request input exceeds `agent.compaction_threshold_tokens` (default 60000); no manual intervention
- **Agent Skills** — reusable task guides per [agentskills.io](https://agentskills.io/specification)
- **Orchestrated multi-agent** — DAG-based parallel sub-agents with two-tier error recovery and strategy memory
- **Token usage tracking** — daily prompt/completion counters per model visible in `/status`

---

## Prerequisites

- Python 3.9+ (3.11+ recommended)
- A Telegram bot token — create one with [@BotFather](https://t.me/BotFather)
- At least one LLM provider API key (OpenAI, Anthropic, Google, OpenRouter, Ollama Cloud)
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

> **Pairing mode:** run `/pair` in the bot to generate a single-use token (expires after `telegram.pairing_timeout`, default 300 seconds); share it with another user who runs `/pair <token>` to gain access. The issuer must remain authorized until redemption. `/unpair <id>` also revokes outstanding tokens issued by that user.

### Provider connections

Model inventories and selection are managed by the application. You only configure discovery accounts — one `[providers.<id>]` connection per account — and pick the active model with `/models` after the agent discovers what the account offers.

```toml
[providers.openai-main]
type           = "openai"
api_key        = "sec:OPENAI_API_KEY"     # literal, env:NAME or sec:NAME; optional
base_url       = "https://api.openai.com/v1"
request_timeout = 120
```

Connection fields are exactly `type`, `api_key` (optional), `base_url` (optional) and `request_timeout` — nothing else is accepted. A credential resolves only when the matching provider client is constructed, never at config load, and per-model keys, endpoints, tuning (`temperature`, `top_p`, `max_tokens`) and retry overrides are rejected with an actionable error. Retries are configured agent-wide with `[agent] max_retries` / `retry_delay`.

Provider connections are read once at startup and stay fixed for the life of the process: editing a `[providers.<id>]` section (adding, removing or changing `type`, `api_key` reference, `base_url` or `request_timeout`) takes effect only after a restart. Until then, `/models` refresh and new runs keep using the connections the process started with. The value behind an `env:`/`sec:` reference is still read when a provider client is constructed.

There is no default-model setting: the remembered selection is made (and changed) with `/models` and survives restarts. It is stored on its own in `$XDG_CONFIG_HOME/<agent>/model-selection.toml` as a single line `model = "<connection-id>/<upstream-id>"` — never a key, key reference or endpoint. A `/models` choice is written there (temporary file, then atomic replace) before it becomes active; if the write fails you get an error and the previous choice and file stay as they were. A preference file that cannot be read or parsed is left byte-for-byte untouched (nothing is remembered) until you pick a model with `/models`, which replaces it immediately. The older `models-config.toml` is retired: it is never opened, read, parsed, changed or deleted, and nothing is derived from it. The runtime no longer references it at all, so it produces no notice; select a model deliberately with `/models`. Discovered models are kept apart from that choice, in a disposable cache at `$XDG_CACHE_HOME/<agent>/models-cache.json` (candidate ids, per-provider fresh/stale state and provider-reported display data only — no selection, key, key reference or endpoint). The cache is written atomically but best-effort: if it cannot be saved, `/models` and the startup log say so, the models just discovered stay usable for the session, and your saved choice is unchanged. A cache that cannot be read or parsed is treated as empty (a startup warning is logged), is not rewritten at startup, and is replaced by the next successful refresh; it can always be deleted safely. The retired `models-config.toml` is neither read nor written for the cache. Discovery contacts only your configured provider endpoints (no public model-metadata service). Provider-reported vision and tool-calling flags are display-only: false or unknown flags do not locally block image or native-tool requests. Provider-reported token limits are display-only and never reject or shrink a request locally: an oversized request is sent as is and a genuine provider context-length rejection is reported as a normal failure (see "Context compaction"). The agent never switches models on its own — a pinned model the provider rejects fails that request with the provider's sanitized error instead of substituting another model.

### Choosing a model (`/models`)

Selection is by buttons only. `/models` lists up to 20 cached candidates with a 🔄 Refresh button and one select button per candidate; there is no typed selection command, and arguments after `/models` never select.

- **Unsaved bootstrap.** With no `model-selection.toml`, the first cached candidate (ascending code-point order of `<connection-id>/<upstream-id>`) is chosen in memory only: it is active but unsaved (shown as "automatic, unsaved"), is derived again at every restart, and is never written. Press a candidate's button to make a deliberate, saved choice.
- **Saved choice.** A well-formed saved selector naming a configured connection stays active and displayed whether or not it is cached; the agent never falls back to another model, and a provider rejection of that model is reported as a normal failure.
- **Empty cache: Refresh first.** Select buttons are drawn from the cache, so with no cached candidates press Refresh first and choose a model once candidates appear. Refresh changes only the cache; it never writes or repairs your selection.
- **Corrupt or unreadable preference.** Nothing is active, nothing is chosen automatically and the file stays byte-for-byte untouched. If candidates are cached, press one button: it replaces the file atomically before the choice becomes active, with no restart. With an empty cache, Refresh first, then press a button. If every provider still returns nothing, chat cannot repair it: fix or delete `model-selection.toml` (one line, `model = "<connection-id>/<upstream-id>"`) yourself and restart.
- **Explicit routes.** A spawned sub-agent's `model` argument and a job's `model` key take the same exact `<connection-id>/<upstream-id>` form (split at the first `/`; the connection must be a configured `[providers.<id>]` exactly and the rest is sent verbatim, using only that connection's credential). No cache entry or active Main is needed — this holds even when the preference file is corrupt — and nothing is ever substituted. Only requests that omit the selector inherit the active Main selection and are refused while there is none.

### Embeddings

```toml
[embeddings]
provider = "openai"                       # adapter type: openai, openrouter or google
base_url = "https://api.openai.com/v1"    # endpoint of the embedding service
api_key  = "env:OPENAI_API_KEY"           # env:/sec: reference (or a literal key)
model    = "text-embedding-3-small"
```

All four fields are required. Embeddings never inherit anything from a `[providers.<id>]` chat connection (not even one of the same adapter type), from the active chat selection, or from graph extraction settings, so `provider` names an adapter type rather than a connection id, and there is no default endpoint. A local service still needs all four fields (`api_key` may be a placeholder value it ignores). The `api_key` reference is resolved only when the embedding client is created, and only for this endpoint.

If any field is missing, blank, not a string, or invalid, the bot still starts and chat keeps working: tool search runs in its degraded text fallback, no embedding request is sent, and graph memory is disabled. The startup log names the offending fields (for example `embeddings.base_url, embeddings.api_key must be configured explicitly`) and never prints their values. Tool indexing and graph vectors share the one embedding service built from these settings.

### Obsolete model configuration is rejected

There is no model-configuration migration tool, and nothing is converted or ignored. Two different things happen depending on the file.

#### `config.toml`: startup errors

Chat configuration that still uses the old model settings stops startup with an error naming the key and its replacement:

- a top-level `[[models]]` table (any field) — configure `[providers.<connection-id>]` and choose a model with `/models`;
- `[agent]` `default_model`, `background_model`, `fallback_model`, `fallback_models` — choose models with `/models`;
- `[agent]` `ctx_max_tokens` — use `agent.compaction_threshold_tokens`;
- any `[providers.<id>]` field other than `type`, `api_key`, `base_url` and `request_timeout` (for example `model`, `max_tokens`, `temperature`, `top_p`, `max_retries`, `retry_delay`, `retries`) — retries are configured once under `[agent]` (`max_retries`, `retry_delay`).

#### `scheduler.toml`: contained per job

Obsolete job fields do not stop the daemon. Only the offending job is affected, shown with a `config_error` in `/jobs` and the `schedule` tool, and every other job keeps running:

- `schedule = "daily"`/`"interval"` and any `time`, `hours` or `minutes` field — that job is unscheduled until you rewrite it as `cron` or `once`;
- a job's `fallback_models` — the job stays listed but each launch fails with an alert until you remove the key.

Error messages never print the rejected values. Old files that may still sit on disk are inert: the retired combined `models-config.toml` is ignored completely (no path, no check, no notice) and the state-directory `scheduler_jobs.json` is only checked for existence (one notice). Both are never read, parsed, converted, written, renamed or deleted. The XDG storage-layout `migrate.py` is a different tool and is unaffected.

### Upgrading manually from the old model configuration

**Removed in this release (no compatibility wrappers):** a leftover `models-config.toml` is ignored and left untouched on disk — choose a model with `/models` (you may delete the file yourself); `agent.diagnose_empty_responses` is rejected at startup, so remove the key (empty-response diagnostics were removed; empty replies are still classified and retried as before); and the internal `chat_with_fallback` / `chat_with_tools_fallback` aliases, `ModelSettings`, the graph helper chain (`prepare_backfill_prerequisites`, `resolve_graph_prerequisites`, `ModelRegistry.resolve_graph_model`) and the provider retry stubs no longer exist, with no replacements other than `chat`, `chat_with_tools` and `resolve_graph_startup`. Explicit `[graph_memory]` extraction settings are unchanged.

Nothing is migrated for you. Do this with the agent stopped:

1. **Back up** (copy, do not move) `config.toml`, `scheduler.toml`, `models-config.toml`, the state-directory `scheduler_jobs.json` (if present) and `model-selection.toml` (if present).
2. **Transcribe `config.toml` by hand.** For each distinct account in the old `[[models]]` entries add one `[providers.<id>]` with only `type`, `api_key` (keep your `env:`/`sec:` reference), `base_url` and `request_timeout`; delete the `[[models]]` tables and the obsolete `[agent]` keys above. Move retry settings to `[agent] max_retries`/`retry_delay`. Set `agent.compaction_threshold_tokens` yourself (an estimated-input token count, not a model limit) and, if you use Anthropic, `agent.required_output_cap_tokens`. Give `[embeddings]` its four fields and, for graph memory, the four `extraction_*` fields — neither inherits from the old model entries.
3. **Transcribe jobs.** In `scheduler.toml` remove `fallback_models`, rewrite `daily`/`interval` schedules as `cron` or `once` and delete the old `time`, `hours` and `minutes` fields (leaving any of them behind keeps the job invalid even next to `cron`), and change any job `model` to `<connection-id>/<upstream-id>` (or delete it to inherit the Main selection). Recreate any job you still need from `scheduler_jobs.json` as a `cron`/`once` job.
4. **Restart**, then use `/models` (Refresh first if the cache is empty) and press a button to choose a model. Until you do, only the unsaved automatic choice may be active.

**Rollback:** stop the agent, restore the old release together with the backed-up `config.toml`, `scheduler.toml`, `models-config.toml` and `scheduler_jobs.json`, and restart. The new release does not read, change or delete the retired `models-config.toml` and `scheduler_jobs.json` (it still rewrites `scheduler.toml` when you change jobs through `/jobs` or the `schedule` tool, so restore your backed-up copy); its separate `model-selection.toml` and `models-cache.json` are not an old combined catalog, so do not copy them over `models-config.toml`.

---

## Run Locally

```bash
source .venv/bin/activate
python main.py
```

On first run the agent creates `data/` and `downloads/`. Logs go to `~/.local/state/<agent_name>/logs/` — structured `agent_logs.sqlite` (SQLite WAL, indexed, 30-day retention), human-readable `agent.log` (daily gzip rotation, 30 backups), plus `graph_memory.log` (component log for the optional graph-memory feature).

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
| `/context` | Estimated input tokens by category (system prompt, chat history, tool definitions), the compaction threshold, and the per-MCP-server tool-def breakdown |
| `/tools` | List all built-in and MCP tools |
| `/skills` | List available agent skills |
| `/models` | List discovered models, Refresh them, and select one with its button — no typed selection (👁 = provider reports vision) |
| `/jobs` | Scheduled jobs; sub-commands: `reload`, `pause <tag>`, `resume <tag>`, `remove <tag>`; read-only views: `history <tag>`, `result <execution-id>` |
| `/agents` | Active sub-agents, scheduled jobs, and plan-step agents (with source category); `/agents cancel <id>` to stop one |
| `/prompts` | Recent prompts with status and elapsed time; `/prompts search <query> [Nd/Nh] [--status=<S>] [--trace=<T>] [--since=<ISO>] [--until=<ISO>] [--page=<N>]` to search history; `/prompts show <id>` for full record |
| `/mcp` | Manage MCP servers: `list`, `on <name>`, `off <name>`, `info <name>` |
| `/stop` | Cancel the current running task |
| `/resume` | Resume an interrupted run from a saved checkpoint |
| `/reset` | Save context to results memory and start fresh |
| `/reset discard` | Clear context without saving |
| `/verbose` | Toggle live tool-call progress; `/verbose on` or `/verbose off` |
| `/reindex` | Force re-embed all tools |
| `/pair` / `/unpair <id>` | Pairing-mode access management |
| `/myid` | Show your Telegram user ID |
| `/mcp auth <name>` | Initiate OAuth 2.0 authorization flow for an MCP server |
| `/mcp auth status` | Show OAuth token status (expiry, refresh token availability) for all servers |
| `/mcp auth revoke <name>` | Delete stored OAuth tokens and disconnect the server |

Hidden diagnostic commands (not in menu, available to authorised users): `/show_ctx`, `/show_env`, `/memory`, `/compress`.

`/models` lists a sanitized
view of up to 20 discovered models with refresh and selection buttons:
each provider's fresh/stale status, the current selection state
(active and saved, active as an unsaved automatic choice, no selection, or a saved
choice that's no longer available — an active selector is shown even when it is
not in the cache), and each
model's provider-reported capabilities and token limits (known/unknown/unsupported).
Everything shown is advisory, provider-reported display data: capability flags and
context/token limits never cause a local admission decision or rejection, although the
provider or its adapter can still reject a request.
Appearing in this list reflects what a provider advertises, not a guarantee the
model is currently callable.
An unreadable model cache or an invalid saved selection is reported in `/models`
with a fixed message and never shows file content. With an empty cache the list
tells you to tap Refresh (which updates only the cache, never your selection)
and choose a model with its button once candidates appear.
Selection buttons use opaque tokens bound to your user and chat for five minutes.
Expired buttons, a bot restart, or token-cache eviction require reopening `/models`.
A pressed button is not rechecked against the cache or the provider: it still
selects a model that a later refresh removed from the list, as long as its
provider connection remains configured. The choice is saved before success is
shown; a failed save leaves the previous selection unchanged.

Prompt-building components and model-selecting tool descriptions also accept an
optional sanitized catalog view. It distinguishes the running model's
admission-time snapshot from the selection for future runs and current discovery
metadata. The caller supplies this view; rendering does not acquire the registry
or perform model admission, and discovery still does not guarantee callability.

At startup the main model comes from the saved selection, independent of the
cache: a well-formed `model-selection.toml` naming a configured connection is
reported as the active selection even when that model is not cached, and a run
admitted on it sends that exact upstream model ID once to its connection; a
provider rejection of an unknown ID is reported as a normal failure, with no
fallback to another model. Likewise an explicit `<connection-id>/<model>`
selector (split at the first `/`; the connection must match a configured
`[providers.<id>]` exactly, and everything after the slash is sent verbatim)
needs neither an active selection nor a cached candidate. An Anthropic
connection sends the configured `agent.required_output_cap_tokens` (positive
integer, default 1024) as `max_tokens`, so an uncached Anthropic model is sent
without any discovered limit; other adapters send no output cap. Graph
extraction keeps its own fixed cap.
With no selection
file, the first model of the cache as loaded at startup, in ascending ordinal
order of its full `<connection-id>/<upstream-id>` (code-point order, so
uppercase sorts before lowercase), is active in memory only, even if the
startup refresh then replaces the catalog and that model is no longer listed
(it is then subject to the same limitation); only an empty cache uses the
refreshed models. Nothing is saved and the choice is derived again at every
restart. Cached candidates of connections that are no longer configured are
ignored. If the selection file is invalid, unreadable or
names a connection that is not configured, a startup warning says so, no
model is active, none is chosen automatically and the file is left
byte-for-byte untouched; the bot and its commands still start. Ordinary tasks and any
sub-agent or job that inherits the Main selection are refused until you pick a model with
`/models` (which replaces the file) or fix the file; an explicit
`<connection-id>/<upstream-id>` selector still runs without an active Main, even with an
invalid preference file.

Startup discovers models only when nothing is known: there is no selection
file (an invalid or unreadable one counts as present) and no cached candidate
of a configured connection (an unreadable cache counts as empty). Then every
configured provider is fetched once (all pages, the same path as `/models`),
with no retry or background loop; a provider that fails keeps its last-good
entries as stale, and if nothing could be discovered the bot still starts, no
model is active and ordinary tasks and inheriting requests are refused with a message to use `/models` (explicit selectors are unaffected). In every
other case startup makes no model-discovery request, so cached entries are used
as they are until you press Refresh in `/models`. Discovery refreshes have a 120-second cooperative elapsed budget per provider connection, separate from the configured `request_timeout` per HTTP operation. Discovery checks the remaining budget before requests, while consuming streamed response chunks, and before publishing a fully normalized inventory; it also limits each transport timeout phase to the remaining budget. A budget expiry leaves that provider's last-good inventory stale, without publishing partial pages. This is not a hard deadline: a blocked synchronous transport/DNS call or JSON processing cannot be preempted; other providers receive their own budget. A refresh publishes a
provider's inventory only when all of its pages are valid; a malformed or
incomplete page keeps that provider's previous inventory (marked stale). A
refresh that returns an empty inventory clears that provider's candidates but
never changes your selection or the models already running a task. A selection
that is no longer in the catalog (uncached or removed by a refresh) still runs
as saved. There is no standing chat client:
each task admits the active model once and uses a client pinned to it, so a
`/models` change mid-task affects only later tasks, and the task's prompt shows
both its pinned model and the current selection. Tool search and results recall
use only the explicit `[embeddings]` settings; when those are unavailable the
agent logs a warning and runs with degraded tool search.

`/reset` and `/compress` are rejected while the agent is busy running a task —
wait for it to finish or `/stop` it first. A message you send while one of these
commands is running is not run automatically; it is saved and offered back with
Run / Discard controls once the command finishes.

`/compress` admits its own summary client from the currently selected Main model
each time it runs (it never reuses a finished run's client) and closes it
afterwards. If no model is selected or admission fails, it reports a short fixed
reason (never provider or credential details) and leaves the short-term history
unchanged; select a model with `/models` and retry.
A failed, cancelled, empty or invalid summary likewise reports a short fixed
failure, never truncates the history and leaves it unchanged; retry `/compress`
later. Only a validated non-empty summary replaces the history (with one entry).
Cancellation is cooperative: a request already on the wire is not aborted, but a
cancel seen before the call or before the reply is used keeps the history unchanged.

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

Supported vision providers: OpenAI (`gpt-4o`), Anthropic (`claude-3+`), Google Gemini, Ollama (LLaVA, llama3.2-vision). The `/models` view badges models whose provider reports vision support; that badge is advisory (there is no `vision` config field) and never blocks a request. Images are always sent to the run's pinned model on its own route; if that model cannot accept images the provider's sanitized rejection is reported as a normal failure — select a vision-capable model yourself (for example via `/models`); the agent never switches models automatically.

### Built-in tools reference

| Tool | Description | Confirmation required? |
|------|-------------|----------------------|
| `shell` | Execute shell commands | Yes — if destructive pattern |

The `shell` tool has three backends, selected by `shell_backend` in `[agent]`:

- **`subprocess`** (default) — cross-platform, fully buffered. Every dangerous pattern requires confirmation.
- **`pty`** — POSIX only; gives commands a real TTY for line buffering, colour, and progress bars. Same confirmation rules as `subprocess`.
- **`nsjail`** — Linux only; runs commands inside a kernel-level sandbox (mount/PID/net/user/IPC/UTS/cgroup namespaces + cgroup v2 limits). The mount table is derived once per session from the frozen path policy (agent-controlled dirs + `[security] allowed_dirs`); prohibited paths are never mounted. The confirmation gate becomes configurable via `shell_nsjail_confirm_mode` (`always` | `adaptive` | `never`); when nsjail is inactive (binary missing or non-Linux host) all modes fall back to `always`. See [`docs/nsjail-setup.md`](docs/nsjail-setup.md) for installation and prerequisites.
| `file_read` | Read a file; `offset: -5000` reads last 5 KB | Yes — if outside allowed tiers |
| `file_write` | Write content to a file | Yes — if outside allowed tiers |
| `file_send` | Send a file or photo to Telegram chat | No |
| `schedule` | Manage scheduled jobs and reminders | No |
| `spawn_agent` | Spawn a background sub-agent | No |
| `memory_write` | Read/write persistent key-value memory (`data/memory.json`) | No |
| `memory_graph_search` | Search graph memory (requires graph memory enabled) | No |
| `memory_graph_store` | Store a fact/episode in graph memory | No |
| `context_profile` | Return a JSON snapshot of estimated input tokens by category, compaction threshold, model identity, turn and liveness | No |

`spawn_agent` and `plan` calls do not accept per-call generation settings. A `spawn_agent` argument, or a top-level key of a `plan` or one of its steps, named `max_tokens`, `temperature` or `top_p` (any value, including `null`) is rejected before any sub-agent, client or credential is set up; the call returns an error naming the field. Models use their configured settings; to change model, pass `model` to `spawn_agent`. A plan step's `args` are the opaque arguments of its tool and are not interpreted as generation settings.

### File access policy

`file_*` tools classify every path (after `realpath` resolution) through a frozen three-tier path policy, constructed once at startup from hardcoded rules plus the `[security]` config section:

- **Prohibited** — system directories, credential homes (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`), the agent's own XDG data/state/config homes, the vault and config files, plus anything the operator adds to `[security] prohibited_dirs` (append-only). Access is a **hard error** — never prompted, never grantable, and immune to operator recklessness (e.g. `allowed_dirs = ["/"]` changes nothing for prohibited paths).
- **Allowed** — agent-controlled directories (`workspace_dir`, `downloads_dir`, `/tmp/<agent_name>`, `~/.<agent_name>/skills` read-only, `~/.<agent_name>/results` read-write) plus operator dirs in `[security] allowed_dirs` (read-write; no mode syntax). Auto-allowed without confirmation.
- **Unrecognised** — everything else: the grant ledger is consulted; on a miss the bot sends a confirmation prompt.

Out-of-policy prompts offer three buttons: **[✅ Confirm]** (execute once + grant the parent directory for the current prompt cycle), **[✅✅ Till /reset]** (execute once + grant until `/reset`), **[❌ Deny]** (refuse; the agent re-plans). `shell` and `secret_get` always ask per-operation and render Confirm/Deny only — they can never hold standing grants.

Grants are `(tool, directory-recursive)`, in-memory only, never persisted to disk. Prompt-cycle grants die at the next user message; session grants die at `/reset`. Grants created from a sub-agent's confirmation expire with that sub-agent's run.

**Migration from trusted dirs:** the dynamic trust store (`trusted_dirs.json`), the `/dir` command, and the zone/approve-all buttons are removed. Standing filesystem access now comes from `[security] allowed_dirs` in config.toml (edit + restart — the policy is frozen per session); in-session consent uses the grant buttons above. Stale `trusted_dirs.json` files are silently ignored.

Agent-internal data is reachable only through dedicated tools — `log_query` for the structured log store, `memory_*` for memory, `secret_get` for vault secrets. The agent can never read or patch its own config or vault by filesystem path.

Run results (oversized shell output, sub-agent handoffs) land in `~/.<agent_name>/results/<trace-id>/` — kept until manually deleted; there is no automatic cleanup.

There is no interactive step limit. The agent runs until the task completes, the operator stops it, or it self-terminates: when the same tool call fails with the same error three consecutive times, the agent stops with a doom-loop message instead of burning budget on the repeat. The main agent also sends a fire-and-forget Telegram ping every `step_notify_interval` steps (default 30, `0` disables) so long runs stay visible; sub-agents never send milestone pings. The old step caps (`max_iterations`, `scheduled_max_iterations`, `plan_max_iterations`) are deprecated no-ops — they still load from existing config files but have no effect.

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

# Remote HTTP with static headers
[[mcp_servers]]
name      = "my-api"
transport = "http"
url       = "https://api.example.com/mcp"
enabled   = true

[mcp_servers.headers]
Authorization = "env:MY_AUTH_HEADER"    # set MY_AUTH_HEADER="Bearer ..." in env
```

`enabled = false` loads the definition without connecting. Manage live with `/mcp on <name>` / `/mcp off <name>`.

#### OAuth 2.0 (Authorization-Code + PKCE) for HTTP Servers

HTTP-transport MCP servers like Google's Gmail MCP require OAuth 2.0 authentication. The agent supports the full authorization-code + PKCE flow with an ephemeral callback server:

```toml
[[mcp_servers]]
name      = "gmail"
transport = "http"
url       = "https://gmailmcp.googleapis.com/mcp/v1"
enabled   = true
timeout   = 30

[mcp_servers.oauth]
# OAuth client credentials (register your app with the provider)
client_id     = "sec:GMAIL_CLIENT_ID"
client_secret = "sec:GMAIL_CLIENT_SECRET"
# Redirect URI must match exactly what you registered with the provider
redirect_uri  = "https://<your-hostname>:8000/oauth/callback"
# Scopes required by the API (space-separated or URL-encoded)
scope         = "https://www.googleapis.com/auth/gmail.readonly"
# Ephemeral callback server (listens only during auth flow)
callback_port = 8000
callback_bind = "0.0.0.0"          # or "127.0.0.1" for local-only
# TLS certificate for HTTPS callback (self-signed or Let's Encrypt)
cert_path     = "/path/to/cert.pem"
key_path      = "/path/to/key.pem"
# Diagnostic flag: logs the full authorization URL at INFO when true.
# Not for production use — enable only when diagnosing OAuth issues.
trace         = false

# Provider-specific authorization parameters (optional).
# The MCP SDK sends only spec-standard params (response_type, client_id,
# redirect_uri, state, code_challenge, code_challenge_method). Some providers
# need extra params — e.g. Google requires access_type=offline and
# prompt=consent to return a refresh_token. Without a refresh_token the
# access_token expires in ~1 hour and re-auth via browser is needed.
# Keys are param names, values are param values. Default: empty (no injection).
# Note: prompt=consent is a standard OIDC param — non-Google OIDC servers
# (Okta, Auth0, Microsoft) will honor it and force the consent screen.
# Only set this for providers that need it.
extra_auth_params = { access_type = "offline", prompt = "consent" }
```

**Setup steps:**

1. **Register OAuth app** with the provider (e.g., Google Cloud Console) and obtain `client_id` and `client_secret`
2. **Generate TLS certificate** for your hostname:
   ```bash
   openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes
   ```
3. **Register redirect URI** in your OAuth app settings: `https://<your-hostname>:<callback_port>/oauth/callback`
4. **Port-forward** the callback port through your router to this machine
5. **Store secrets** in the vault (`~/.local/state/<agent_name>/secrets.toml`):
   ```toml
   GMAIL_CLIENT_ID = "your-client-id"
   GMAIL_CLIENT_SECRET = "your-client-secret"
   ```

**Authorization flow:**

- Run `/mcp auth gmail` in Telegram
- Tap the authorization URL button (opens provider's login page)
- Authenticate and grant permissions
- Agent receives the callback, exchanges the code for tokens, and stores them in `~/.local/state/<agent_name>/mcp_tokens/gmail.json` (mode `0600`)
- Server connects with Bearer token; tools become available

**Token management:**

- **Automatic refresh:** When a tool call returns 401, the agent automatically refreshes the token using the stored refresh token (outbound only, no callback needed). This requires Google servers to have returned a refresh token — make sure `extra_auth_params` includes `access_type = "offline"` for Google flows.
- **Manual re-auth:** Run `/mcp auth gmail` again to revoke and re-authenticate
- **Token status:** Run `/mcp auth status` to see expiry times and refresh token availability
- **Revoke tokens:** Run `/mcp auth revoke gmail` to delete stored tokens and disconnect the server

---

## Advanced Topics

### MCP OAuth 2.0 (Security & Troubleshooting)

**Callback server:**
- Opens **only during auth flow** (~30 seconds), then closes automatically
- Protected by **state parameter** (CSRF token) + **PKCE** (code interception)
- Requires **HTTPS** (TLS cert must be valid; self-signed is acceptable)
- Binds to `callback_bind` (default `0.0.0.0`; restrict to `127.0.0.1` for local-only flows)
- Port must be accessible from the OAuth provider's servers (router port-forward required)

**Token storage:**
- Stored as `~/.local/state/<agent_name>/mcp_tokens/<server>.json` with `0600` permissions
- Contains `access_token`, `refresh_token`, `scope`, `token_type`, and `client_info`
- Stores token timing as `issued_at` + `expires_in` (relative seconds from issuance; absolute expiry = `issued_at` + `expires_in`)
- May contain an optional top-level `oauth_metadata` block with authorization-server metadata discovered during the flow (e.g. `issuer`, `authorization_endpoint`, `token_endpoint`); persisted only after the token exchange completes, used by silent token refresh, and carried forward across token writes
- Vault integration deferred — store client secrets in `secrets.toml` with `sec:` prefix, not plaintext

**Migration:** after deploying this change, run `/mcp auth <server>` once per OAuth-configured server. The flow rewrites the token file with a fresh `issued_at` and persists `oauth_metadata`; token files written before the change load unchanged (restores are skipped for missing keys) and behave exactly as before until re-authenticated.

**Troubleshooting:**

| Issue | Cause | Fix |
|-------|-------|-----|
| "Certificate validation failed" | TLS cert missing, unreadable, or expired | Regenerate cert; ensure `cert_path` and `key_path` are readable |
| "Callback server failed to start" | Port already in use or permission denied | Change `callback_port` or stop the process using the port |
| "Connection timed out" | Router port-forward not configured or hostname wrong | Verify port-forward; test with `curl https://<hostname>:<port>` |
| "OAuth flow timed out" | Callback URL not reached within 300s | Check hostname resolution; verify port-forward; ensure HTTPS is working |
| "401 Unauthorized" after auth | Tokens expired and refresh token invalid | Run `/mcp auth <name>` to re-authenticate |
| "Token refresh failed" | Refresh token revoked or expired | Run `/mcp auth <name>` to re-authenticate; check scope permissions |
| Auth reports success but tools stall | Server allows unauthenticated discovery; no token stored | Set `trace = true` in the `[mcp_servers.oauth]` block and re-run `/mcp auth <name>`; check logs for the "no token file found" warning |

**Security best practices:**

- Use a **hostname** (not IP address) for the redirect URI to survive network changes
- Use **Let's Encrypt** for production (self-signed is fine for testing)
- Store `client_secret` in the **vault** (not config.toml)
- Restrict `callback_bind` to **`127.0.0.1`** if the agent is local-only
- Regularly run `/mcp auth status` to monitor **token expiry**
- Use `/mcp auth revoke <name>` to **disconnect** servers you no longer need

---

### Scheduler

Writable job definitions live in `scheduler.toml`. Cron schedules use **5-field cron expressions** (local server time):

```
minute  hour  day  month  weekday
  0      2     *     *       *     → daily at 02:00
  0    */6     *     *       *     → every 6 hours
*/30    *      *     *       *     → every 30 minutes
```

Old-style schedules are **not converted**: `schedule = "daily"`/`"interval"` and any `time`, `hours` or `minutes` field leave that one job unscheduled with a visible `config_error` (shown by `/jobs` and the `schedule` tool) telling you to rewrite it with `schedule = "cron"` + `cron`, or `schedule = "once"` + `run_at`; the file is never rewritten, and other jobs keep running. The `schedule` tool refuses these fields (and `fallback_models`) on add and modify before changing anything.

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

Per-job options: `model`, `preserve_context`, `context_max_messages`, `overlap_policy` (`skip`/`parallel`), `notify`. (A legacy `max_iterations` job key is still accepted and silently ignored.)

**Job model selection.** A job's saved `model` selector is read once, when the job runs. A job without a `model` key inherits the active Main selection at launch (and fails with an actionable error if there is none). A `model` key is used exactly as saved: it needs neither an active Main nor a cached catalog entry, and only the connection part must match a configured connection exactly (no trimming; the part after the first `/` is used verbatim). A blank, non-string, malformed or unknown-connection selector fails only that job's run — a failed run record plus a direct alert (even with `notify = false`) showing the bounded, redacted selector — and never falls back to Main or another model; the saved value is left untouched. A per-job `fallback_models` key is obsolete: the job stays visible with a `config_error` (remove the key), and every launch of it fails as a recorded launch failure with an alert until you remove it — scheduled jobs never fall back to another model.

Key features: hot-reload without restart (`/jobs reload`), automatic backups before each write (last 5 kept), ±5 min jitter on first cron run, run history in `data/scheduler_state.json`. Scheduler file changes flush and sync a temporary file, atomically replace `scheduler.toml`, then sync its parent directory before publishing runtime state. If the directory sync fails *after* replacement, the new bytes may already be visible but their crash durability is uncertain: automatic runs pause, further changes refuse, and `/jobs reload` is required to reconcile the actual file. A failure *before* replacement leaves the previous file and schedule in place.

Scheduler state stores schedule/enable fields and run-history values, not job definitions or raw model selectors. Definitions remain in `scheduler.toml`. If state serialization fails, the scheduler logs the failure and keeps the previous state file without stopping scheduling.

**Legacy `scheduler_jobs.json` (detected, never loaded).** If a `scheduler_jobs.json` exists in the agent's state directory, the scheduler only checks that it exists, at startup and on every `/jobs reload` attempt (even one that fails). It is never read, run, converted, written, renamed or deleted, so none of its jobs are listed, scheduled or run. While it exists, one notice names its path and tells you to recreate any job you still need by hand as a `cron` or `once` job in `scheduler.toml`; the notice is shown in `/jobs`, the `schedule` tool's list output and at startup, and disappears at the next reload after you delete the file. It never blocks `scheduler.toml` jobs, changes or removals, even when it holds the same tag as a `scheduler.toml` job.

**Run results and notices.** Every scheduled run gets its own execution ID (`run-<32 hex>`) and a complete result record in `job_execution_log.jsonl` in the state directory. Records are kept for `[scheduler] execution_log_max_age_hours` (default 48) and at most `execution_log_max_per_job` (default 10) per job; older records expire and their results become unavailable. A scheduled run uses its own job context only — it does not read or write operator memory, and a run's result never enters your chat context.

With `notify = true`, the result notice for a finished, failed, or cancelled run is held until your current reply has been sent and typing has stopped, then delivered **silently** (no sound). Failure notices include the error type and next run time. A result that fits in one message appears in an expandable quote under the summary; a longer one is replaced by a compact notice with the execution ID and a **Details** button that delivers the complete result (as a message or `.txt` document). If a run's result could not be saved, its notice still shows the run outcome (including the failure header, error type and next run time for a failed run), states that the result was not retained and Details are unavailable, and has no Details button. With `notify = false` no result notice is sent, but the run is still recorded. Notices are queued in memory only: if the bot restarts before a queued notice is sent, it is dropped, but the run stays available in history. Launch failures, empty-task reminders, and long-running-agent warnings are still sent immediately.

Read-only history views (authorised operator only; they never start an agent turn or touch memory):

| Command | Shows |
|---------|-------|
| `/jobs history <tag>` | Retained run IDs, statuses, and timestamps for a job, newest first. Removed `once` jobs stay listed until their records expire. Records written before this feature appear as `legacy/partial` (status and timestamp only, no execution ID or full output). A tag with no definition and no retained runs is reported as unknown; an active job with no retained runs reports no retained runs |
| `/jobs result <execution-id>` | The complete result of one retained run: sent as a message if it fits in Telegram's 4,096-character limit, otherwise as a complete UTF-8 `<execution-id>.txt` document. Unknown or expired IDs report the result as unavailable |

Both commands show usage if the `<tag>` or `<execution-id>` argument is missing.

### Memory architecture

| Tier | Storage | Purpose |
|------|---------|---------|
| Short-term | In-memory (last 20 turns) | Recent conversation context injected every prompt |
| Working | In-memory (current task) | Goal, tool calls, results; cleared on `/reset` |
| Long-term | `data/longterm_memory.json` (vector index) | Nightly summaries; semantically searchable |
| Results | `data/results_memory.json` (vector index) | Past task summaries saved on `/reset` |
| Graph (opt-in) | `data/graph_memory` (LadybugDB) | Entities, relationships, episodes; retrieved per turn |

Context compaction fires automatically when the estimated input (system prompt, full chat history including images, and the complete current tool definitions) exceeds `agent.compaction_threshold_tokens` (default 60000) — older messages are summarised by the LLM without operator intervention, once per over-threshold request. The current goal and the last two messages (extended to whole tool-call/result groups) are kept unchanged and only the messages between them are summarised; when there is nothing between them, nothing is changed or truncated. The summary is requested through the run's own model connection with its own independent one-shot attempt accounting, separate from the main request: each summary makes a single outbound attempt, with no summary retries or backoff waits, and never consumes the main request's retry allowance. If the summary fails, returns empty output, or yields an invalid result, the original history is sent unchanged with a sanitized warning and automatic compaction is disabled for the rest of that run only (a new run tries again). The threshold is model-independent: discovered context or input limits never trigger, shrink or reject a request. Use `/reset` to start fresh, or `/reset discard` to skip saving.

A genuine provider context-length rejection is never repaired automatically: it is classified `context` and returns immediate guidance without an error card, new checkpoint or `/resume` recovery, with no forced compaction, truncation, automatic resend or model switch. Reduce the request/context using `/reset` or the hidden `/compress`, or select a larger-context model with `/models`, then retry. The only output-token cap is `agent.required_output_cap_tokens` (positive integer, default 1024), sent only to adapters whose API requires one (currently Anthropic) and never sized from context or discovered limits.

### Graph memory (optional)

Install the extra dependency:

```bash
pip install "ladybug>=0.7.0"
```

Add to `config.toml`:

```toml
[graph_memory]
enabled               = true
buffer_pool_mb        = 256          # use 64 on low-memory hosts
extraction_provider   = "openai"                    # adapter type: openai, openrouter, anthropic, google or ollama
extraction_base_url   = "https://api.openai.com/v1"  # graph extraction's own endpoint
extraction_api_key    = "sec:graph_extraction_key"   # literal, env:VAR or sec:KEY — used only for extraction
extraction_model      = "gpt-example-mini"           # upstream model string, sent verbatim
extract_every_n_turns = 3
min_message_length    = 100
max_context_entries   = 10
```

(The database path is not configurable — it is always derived from `XDG_DATA_HOME`.)

LadybugDB is embedded — it runs in-process with no separate server.

When graph memory is enabled, all four `extraction_*` settings are required and are used exactly as written: nothing is inherited from a chat connection (even one of the same adapter type), the model catalog, the Main selection or `[embeddings]`, and the extraction credential is sent only to `extraction_base_url`. Extraction always uses a fixed 1024-token output cap and does not use `agent.required_output_cap_tokens`; temperature `0.1` is sent where the adapter accepts it. No local fit, reserve or output-limit check runs for graph extraction (neither at startup nor per request, whatever the local context default or any discovered limit says): an oversized input is sent and a provider rejection fails only that extraction. Each extraction builds and closes its own client. If any of the four is missing, blank, not a string, or invalid (unsupported `extraction_provider`, malformed URL, unresolvable credential reference), the bot logs a warning naming the field and starts with graph memory disabled; chat and indexing keep working. Graph vectors use the explicit `[embeddings]` settings. Unused `extraction_*` values are not checked while `enabled = false`.

Graph startup and the live `backfill_graph_memory.py` each validate their own settings — the four `extraction_*` fields, the `[embeddings]` fields, adapter/endpoint compatibility (for example `ollama` requires the official `https://ollama.com` origin) and both credential references — before any graph database, embedding client or extraction client is created, without provider discovery, the saved model catalog or the model preference. Live backfill needs no chat `[providers.*]` connection and fails with an actionable message (field names only, never values, URLs or reference names) and a non-zero exit before touching the database. `--dry-run` reads only `longterm_memory.json` and the backfill state file: it resolves no credential and creates no client, database, embedding or inference, so it works even when those settings are invalid. On shutdown, if the graph writer does not stop within its bounded timeout, the failure is reported and the store and worker-used dependencies stay open — shutdown never claims success while they are unclosed.

Seed from existing long-term memory (while agent is stopped):

```bash
python backfill_graph_memory.py --config config.toml --dry-run   # count entries
python backfill_graph_memory.py --config config.toml             # import
python backfill_graph_memory.py --config config.toml --limit 50 --verbose
python backfill_graph_memory.py --config config.toml --force     # re-import all
```

Graph memory health is visible in `/status` and `/memory`. Health states: `active-empty` 🟡, `active-learning` 🟢, `active-used` 🟢, `*-degraded` 🟠, `failed` 🔴.

#### Graph database open failures

Graph startup makes one normal database-engine open attempt, allowing native WAL replay. If it fails, graph memory stays unavailable while the main agent continues without it; live backfill exits nonzero without importing records. Logs provide recovery guidance and the exception class, not raw engine error text. There is no new Telegram alert or automatic repair: the application does not delete, truncate, rename or recreate database/WAL files to recover from an open failure.

Before investigating or restoring:

1. Stop all graph users, including the agent and any live backfill.
2. Back up the database and **all sidecars together**, including `.wal` and `.wal.checkpoint` when present. Do not discard WAL files: they may contain committed transactions not yet checkpointed.
3. Investigate offline or restore a consistent backup using supported database procedures. Any destructive salvage requires explicit operator approval.
4. Restart after repair and check graph memory health.

### Sub-agents

Sub-agents are isolated background ReAct loops — non-blocking, with their own LLM context. Every agentic scheduled job runs as a sub-agent with its own job context: its complete result is kept in job history under its execution ID, and nothing from a scheduled run enters operator memory or recall. For jobs with `notify` on, the run-result notice is deferred until your current turn is idle and arrives silently (an expandable quote, or a compact message with a Details button for long results); with `notify` off nothing is sent but the result is still recorded. See "Run results and notices" in the scheduler section.

Key configuration:

```toml
[agent]
step_notify_interval        = 30              # main-agent Telegram ping every N steps (0 = off)
long_run_warn_minutes      = 30              # Telegram notification threshold (0 = off)
tool_timeout               = 10              # seconds per tool call
compaction_threshold_tokens = 60000          # estimated input tokens that trigger automatic compaction
```

There is no sub-agent or scheduler model setting in `config.toml`. A sub-agent or scheduled job without a model selector runs on the operator's selected Main model. An explicit selector — a spawned sub-agent's `model` argument or a job's `model` key — runs on that exact catalog model ID; a malformed or unknown-connection sub-agent or job selector is refused (for a job, that run fails as described under "Job model selection"). Only runs that inherit — no selector given — require an active Main selection.

(The legacy `max_iterations` / `scheduled_max_iterations` / `plan_max_iterations` keys are still accepted in config files but ignored — the step gate is gone; see the security section above.)

Manage sub-agents: `/agents` to list, `/agents cancel <id>` or `/agents cancel <job_tag>` to stop.

Sub-agents cannot spawn further sub-agents (max depth: 1).

During shutdown, queued sub-agents are cancelled and cleaned up before the normal bounded wait for running work. Running clients remain open until their execution unwinds; this does not add a time limit to synchronous cleanup callbacks. New spawn requests are refused while the supervisor is stopping, with guidance to restart the agent before retrying. Scheduled launch failures retain their existing reporting.

### LLM providers & resilience

| `type` value | Provider | Notes |
|--------------|----------|-------|
| `openai` | OpenAI | GPT-4o, GPT-4o-mini, etc. |
| `openrouter` | OpenRouter | `base_url = "https://openrouter.ai/api/v1"` |
| `google` | Google Gemini | Leave `base_url` empty |
| `anthropic` | Anthropic Claude | Leave `base_url` empty |
| `ollama` | Ollama Cloud | `base_url = "https://ollama.com"`, API key required (local/keyless Ollama is not supported) |
| `openai` | Any OAI-compatible | Custom `base_url` (xAI Grok, Together, Fireworks, LM Studio) |

Ollama requires the official Python package: `pip install "ollama>=0.4.0"`.

Switch models at runtime with `/models`. The agent does not auto-switch based on message content.

Resilience behaviours:
- **Native tool calling** — models that support it (Kimi, GLM, DeepSeek, Gemini, Ollama) use the provider's native tool-calling API directly, bypassing JSON parsing entirely; falls back to the text-based JSON path automatically when the provider doesn't support tools, on transient errors, or when the model returns text instead of tool calls
- **Retries** — exponential backoff on timeout/connection errors; live status in Telegram
- **Empty responses** — classified and retried under the shared retry policy
- **Non-JSON prose** — coerced up to 2 times without consuming a step or polluting history
- **Multiple JSON objects** — brace-counting parser extracts the correct `{"action":…}` block
- **Reasoning model support** — `reasoning`/`reasoning_content` field fallback (DeepSeek-R1, Kimi K2.5, QwQ)

### LLM error recovery

LLM failures are classified as `timeout`, `connection`, `rate_limit`, `empty`, `context`, `permanent`, or `unknown`. Existing automatic retry budgets are unchanged; not every category is automatically retried.

**Context and permanent failures** return immediate sanitized `❌` guidance: reduce the request/context, or check the model/provider configuration, respectively. They do not create a checkpoint or show a blocking card.

**Headless runs** (on-demand sub-agents, scheduled agents, plan steps and diagnostic agents) also return terminal failure after the existing automatic policy finishes. Notifications and sensitive-tool confirmation do not enable LLM retry cards. Failed work is not automatically replayed as a whole task.

**Telegram root requests and `/resume`** are the only runs eligible for durable recovery. For a retryable failure the request ends immediately — there is no Retry/Cancel card, token or waiting period — and the agent:

1. **Saves a checkpoint**, when `checkpoint_enabled` is on, to `data/run_checkpoints/{trace_id}.json` containing the full conversation state, completed tool results and pending request attempt state.
2. **Verifies the saved file** against the exact snapshot it wrote. Only then does the failure message end with "Saved progress is available. Use /resume to continue."
3. **Returns the failure** and releases the run's resources through the normal cleanup (child draining, client close, checkpoint lease).

If checkpoint writing is disabled or the save fails (including a disk error), the message instead ends with "Progress could not be saved for recovery. This request cannot be resumed." There is no in-memory retry any more: that failed request is lost, so send it again. Returning a failure never undoes tool effects that already completed. Cancellation always wins and creates no checkpoint.

Retry/Cancel buttons on messages sent before an upgrade are inert: pressing one only answers that the buttons expired and points to `/resume`.

### `/resume` command

Resumes an eligible interrupted main-agent run from a saved checkpoint:

- `/resume` — resumes the sole eligible checkpoint, or lists multiple eligible checkpoints newest first
- `/resume N` — resumes the Nth checkpoint from that filtered, contiguously numbered list
- No eligible checkpoints — reports "No unfinished runs to resume."
- Agent busy — reports "⚠️ Agent is currently running. Wait for it to finish or /stop it first."

The controller enforces the same eligibility rule used by Telegram and startup: explicit `error_info.retryable=false` prevents resume, even for direct controller callers. Older otherwise-valid checkpoints without that flag remain eligible. Non-retryable files remain untouched on disk but are hidden from actionable lists and startup notices.

**Resume uses the currently selected model.** `/resume` admits the model selected with `/models` once, ignoring the model the checkpoint was created with (which may no longer be configured). If the two differ, a notice follows the "Resuming" line before anything is sent: "Resuming with B; saved progress was created with A. Saved conversation and tool results will be sent to the selected model's provider." No extra approval is requested, so check `/models` first when switching providers matters to you. If no model is selected or the selected one cannot be admitted, resume fails locally with guidance and the checkpoint is left as it was. Changing the selection afterwards affects only later runs.

Resume restores the saved messages, completed tool results, loop position, trace and attempt counters. Completed tools are not executed again, the automatic retry budget is not refilled (an explicit resume grants one manual continuation), and nothing falls back to another model or restarts the task. This is not a promise of lossless provider conversion: saved native tool turns use the existing adapter conversion (for providers without native tool calling they are converted to a compact conversation, which can abbreviate long tool arguments), and the new provider can still reject the request because of context length, images or other restrictions. Such a rejection is reported as a normal failure.

The saved file is retired only at the moment a request is actually sent. If resume fails before that (validation, admission, cancellation), the file is unchanged. If the new request fails and the failure is eligible, a fresh checkpoint is saved that records the model used for that attempt. A new checkpoint is never created from a non-retryable failure.

Checkpoints you do not resume stay listed and in startup notices; the removal of the Retry/Cancel cards adds no expiry, discard command or other cleanup. The existing retention cap is unchanged: the store keeps at most 20 supported checkpoints and each new save removes the oldest ones beyond that (checkpoints in another format, unreadable files and files owned by an active run are never pruned). Remove other stale files under `data/run_checkpoints/` yourself if you no longer want them.

On startup, a sole eligible checkpoint produces: "💾 Found unfinished run: '{goal}'. Send /resume to continue." With multiple eligible checkpoints, the notice names the newest eligible goal and directs the operator to `/resume`. With none, no recovery notice is sent.

### Scheduled job failure notification

Scheduled jobs that fail due to LLM errors are classified and reported with the error type and next scheduled run time. No auto-retry — the next cron run is the natural retry.

### Configuration

```toml
[llm_error_handling]
checkpoint_enabled = true    # whether new failures write a checkpoint for /resume (default true)
```

`checkpoint_enabled = false` stops new checkpoints (a failed request then reports that it cannot be resumed); checkpoints already on disk can still be resumed. The setting never makes headless runs eligible. The former `retry_timeout_seconds` key was removed together with the Retry/Cancel cards: configuration that still contains it (with any value) is rejected at startup with a message naming the key — delete the line.

### Logging

Logs live under the XDG state directory `~/.local/state/<agent_name>/logs/`, resolved from `agent_name` independently of `agent_home` (a relative `[paths] log_file` lands here; an absolute path overrides). Logging is built on [`structlog`](https://www.structlog.org) integrated with stdlib, writing the two agent sinks from one processor chain so they never drift (plus an isolated component log for the optional graph-memory feature):

- **`agent_logs.sqlite`** — structured SQLite store (WAL mode, indexed), the **primary** machine-readable surface. Each event carries identity fields (`trace` and `agent`, the run label), a `level`, and an `event_type` from a closed taxonomy (`TOOL_START/END/FAILED`, `LLM_CALL/FAILED`, `STEP_BEGIN/END`, `RUN_BEGIN/END`, `ERROR`) plus key-values (`tool`, `dur_ms`, `exit`, `err`). The store keeps 30 days of events via time-based `DELETE` retention and a daily `PRAGMA wal_checkpoint(TRUNCATE)`.
- **`agent.log`** — human-readable prose (secondary), keeping the familiar `[label trace] message` shape for `tail -f`/`grep`.
- **`graph_memory.log`** — dedicated prose sink for the optional graph-memory background component; isolated from the agent sinks, daily gzip rotation like `agent.log`, stdout shows only WARNING+ for this component.

`agent.jsonl` is no longer written. Existing `agent.jsonl` and `agent.jsonl.*.gz` archive files remain on disk untouched as a forensic fallback.

Known vault secret values are redacted from all sinks before serialization. The agent can introspect its own run mid-execution with the **`log_query`** built-in tool — an in-process SQL query over the full 30-day store, trace-scoped to the current run by default.

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

# Recent errors across the full retention window
sqlite3 ~/.local/state/<agent_name>/logs/agent_logs.sqlite "SELECT ts, agent, msg FROM events WHERE level IN ('error','critical') ORDER BY ts DESC LIMIT 20;"

# All records for a trace
sqlite3 ~/.local/state/<agent_name>/logs/agent_logs.sqlite "SELECT ts, event_type, msg, extra FROM events WHERE trace='r-1a2b3c4d' ORDER BY ts;"

# Tool failures
sqlite3 ~/.local/state/<agent_name>/logs/agent_logs.sqlite "SELECT ts, msg, extra FROM events WHERE event_type='TOOL_FAILED' ORDER BY ts DESC;"
```

Back up the store by copying `agent_logs.sqlite` together with `agent_logs.sqlite-wal` (and `-shm`), or run `PRAGMA wal_checkpoint(TRUNCATE);` first and then copy the single file.

One-time backfill from existing JSONL archives:

```bash
python backfill_log_store.py --agent-name <name> --dry-run   # count entries
python backfill_log_store.py --agent-name <name>             # import
python backfill_log_store.py --agent-name <name> --verbose
```

Backfill is idempotent: re-running it skips already-imported lines. Source archives are never modified.

File storage defaults (override in `[paths]`):

| Directory | Purpose |
|-----------|---------|
| `downloads/` | Files the user wants to keep |
| System temp (`/tmp/<agent_name>`, not configurable) | Temporary files; cleaned on reboot |

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
llm_client.py            # Multi-provider chat client; delegates embedding requests
embedding_client.py      # Independent embedding dispatch and bounded cache
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
context_manager.py       # Auto-compaction when estimated input exceeds agent.compaction_threshold_tokens
context_monitor.py       # Context-window consumption monitor (push snapshot per turn)
skills/                  # Agent Skills — skills/<name>/SKILL.md
data/                    # Runtime state: tool_index, memory, scheduler state, graph DB
tests/                   # Test suite (pytest); execution harness in tests/execution_harness.py
```
