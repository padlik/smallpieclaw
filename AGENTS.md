# AGENTS.md — OpenCode Instructions for smallpieclaw

> **Note:** `AGENT.md` (no S) is the *user-facing* reference for the Telegram bot's capabilities. This file (`AGENTS.md`) is for AI coding agents working on the codebase.

## Dev Commands

```bash
make test       # pytest tests/ -v --tb=short
make lint       # ruff check . && vulture . vulture_whitelist.py --min-confidence 80 --exclude interfaces.py
make check      # lint + test (run this before committing)
make install-dev  # pip install -r requirements-dev.txt
```

Run a single test file: `pytest tests/test_react_loop.py -v`
Run a single test: `pytest tests/test_react_loop.py::TestExtractJsonCandidates::test_single_object -v`

## Architecture

**Composition root:** `main.py` constructs all objects and wires dependencies explicitly. No DI container or service locator.

**ReAct loop:** Extracted from `agent_controller.py` into standalone `react_loop.py`. The loop receives a `ReactContext` dataclass with all deps and mutable state. `agent_controller.py` is now a thin orchestrator that builds the context and delegates to `react_loop()`.

**Protocols:** `interfaces.py` defines `Protocol` classes (`LLMProvider`, `ToolBackend`, `MemoryBackend`, etc.) for structural typing. Existing classes conform without explicit inheritance. Used for type-safe DI and test mocking.

**Config:** `config_schema.py` provides typed dataclasses (`AppConfig`, `AgentConfig`, `ModelConfig`, etc.) via `parse_config()`. Migration from raw `cfg` dict to typed config is **incremental** — both forms coexist. `env:VAR` references in string values are whole-string only (no inline interpolation). Missing env vars cause startup errors.

**Exception hierarchy:** `exceptions.py` — `AgentError` → `LLMError`, `ToolError`, `MCPError`, `ConfigError`, `SecurityError`. Use specific exceptions; broad `except Exception` is tolerated for daemon resilience but narrowing is the goal.

## Key Modules

| Module | Role |
|---|---|
| `llm_client.py` | Multi-provider LLM (openai, openrouter, google, anthropic, ollama) + embeddings |
| `builtin_executor.py` | All built-in tools: shell, file_read/write, schedule, spawn_agent, memory_write, memory_graph_* |
| `tool_registry.py` | Discovers `.sh`/`.py` tools from `tools/` and `tools_generated/`; `Tool` dataclass |
| `tool_index.py` | Semantic tool search via embedding cosine similarity; persists to `data/tool_index.json` |
| `tool_executor.py` | Runs tools in subprocess with timeout |
| `tool_creator.py` | LLM-proposed tool creation with operator approval flow |
| `memory_store.py` | `MemoryStore` (KV), `ShortTermMemory`, `WorkingMemory`, `ResultsMemory`, `LongTermMemory` |
| `graph_memory.py` | Opt-in LadybugDB entity/relationship store; `GraphMemoryStore` + `GraphMemoryWriter` |
| `backfill_graph_memory.py` | One-time CLI to seed graph from `data/longterm_memory.json` |
| `scheduler.py` | Cron jobs via `scheduler.toml` (single source of truth); uses `croniter` |
| `mcp_client.py` | MCP server client — stdio (subprocess) and http transports |
| `skill_registry.py` | Discovers Agent Skills from `skills/<name>/SKILL.md` |
| `telegram_interface.py` | Telegram bot with allowlist/pairing security, streaming, inline confirmations |
| `telegram_formatter.py` | Pure formatting: md→html, message splitting, job list formatting |
| `telegram_commands.py` | All `/` command handlers |
| `prompt_builder.py` | System prompt assembly; re-exports `estimate_tokens` from `token_estimator.py` for backward compat |
| `token_estimator.py` | Two-layer token counting: tiktoken (OpenAI models) + conservative heuristic fallback |
| `context_manager.py` | Auto-compaction at 85% of `ctx_max_tokens`; content-aware trimming |
| `confirmation.py` | Thread-safe confirmations via `threading.Event` (agent thread blocks, Telegram callback signals) |
| `trace_context.py` | `r-<8 hex>` trace IDs for log correlation across agents/sub-agents/scheduler |
| `sub_agent_registry.py` | Tracks all active `SubAgentRunner` instances for `/agents` command |
| `token_usage.py` | Per-model daily prompt/completion counters; thread-safe registry |

## Testing

**Fixtures** in `tests/conftest.py`:
- `minimal_config` — valid raw config dict
- `mock_llm_response` — factory returning MagicMock LLM with scripted JSON responses
- `finish_response` / `shell_response` — standard JSON action strings
- `mock_subprocess` — patches `subprocess.run`
- `tmp_agent_dir` — temp dir with `tools/`, `tools_generated/`, `data/`, `downloads/`

**Execution harness** (`tests/execution_harness.py`): `ScriptedLLM`, `RecordingExecutor`, `run_react()` for deterministic multi-step ReAct testing without network, Telegram, or graph DB.

**Graph memory tests:** `test_graph_memory_e2e.py` uses `pytest.importorskip("ladybug")` — entire module skips when ladybug not installed. `test_graph_memory_integration.py` tests wiring without real DB.

**Mocking:** Tests use `unittest.mock` (MagicMock, patch). No external mocking frameworks.

## Conventions & Gotchas

- **`vulture_whitelist.py`** must be updated when adding new public API symbols that vulture flags as unused (Protocol methods, dataclass fields, logging overrides, backfill API).
- **`prompt_builder.py`** re-exports `estimate_tokens`/`estimate_messages_tokens` from `token_estimator.py`. New token-estimation code goes in `token_estimator.py`; keep the re-export in `prompt_builder.py` for backward compat.
- **`react_loop.py`** is the canonical loop logic. `agent_controller.py` delegates to it. Don't add loop logic to the controller.
- **Config migration** is incremental. New code should use `app_cfg` (typed `AppConfig`); old code may still access `cfg` dict. Both are passed through `_run()`.
- **Graph memory** is opt-in. Always guard with `if graph_memory_store is not None:` or check `app_cfg.graph_memory.enabled`. The `memory_graph_*` built-in tools return graceful errors when unavailable.
- **Sub-agents** have depth limit 1 — they cannot spawn further sub-agents.
- **PID file locking** in `main.py` uses `fcntl.flock()` — the OS releases the lock on process exit, so stale PID files from crashes are handled automatically.
- **Log format:** `%(asctime)s [%(levelname)s] %(name)s: %(message)s` with source tags like `[main]`, `[sa-<id>]`, `[sched/<tag>]`.
- **`tomli`** is used for TOML parsing (fallback for Python < 3.11 where `tomllib` is stdlib).
- **`AGENT.md`** (no S) is the user-facing bot capabilities reference — do not confuse with this file.
