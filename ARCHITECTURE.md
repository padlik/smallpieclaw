# Architecture

## Threading Model

The agent uses a hybrid async/threaded architecture:

```
┌─────────────────────────────────────────────────────────────┐
│  Main Thread (asyncio event loop)                           │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  python-telegram-bot Application                     │    │
│  │  • All Telegram handlers (async)                    │    │
│  │  • Message dispatch → run_in_executor(agent.run)    │    │
│  │  • Callback queries (confirm/extend/approve-all)    │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
         │                    ▲
         │ run_in_executor    │ run_coroutine_threadsafe
         ▼                    │
┌─────────────────────────────────────────────────────────────┐
│  Worker Threads (executor pool)                             │
│  ┌────────────────────────┐  ┌────────────────────────┐    │
│  │  Interactive agent task │  │  Sub-agent thread (N)  │    │
│  │  • AgentController.run  │  │  • SubAgentRunner.run  │    │
│  │  • Blocks on confirm    │  │  • Independent context │    │
│  │    via threading.Event  │  │  • Notifies via TG     │    │
│  └────────────────────────┘  └────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│  Background Threads                                         │
│  ┌─────────────────┐  ┌──────────────────────────────┐    │
│  │  Scheduler       │  │  MCP stderr drains (daemon)  │    │
│  │  • Single thread │  │  • One per stdio MCP server  │    │
│  │  • Spawns sub-   │  │  • Reads subprocess stderr   │    │
│  │    agent threads  │  │  • Logs: jsonl + prose (XDG)  │    │
│  └─────────────────┘  └──────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

## Thread Safety Guarantees

| Component | Thread-safe? | Mechanism |
|-----------|-------------|-----------|
| `MemoryStore` | ✅ Yes | `threading.RLock` on all mutations |
| `TokenUsageRegistry` | ✅ Yes | `threading.Lock` on counters |
| `MCPManager` | ✅ Yes | Per-client `threading.Lock` |
| `SubAgentRegistry` | ✅ Yes | `threading.Lock` + `Event` |
| `ToolRegistry` | ⚠️ Read-mostly | Safe for concurrent reads; `register_mcp_tools()` and `unregister_mcp_server()` not called concurrently in practice |
| `AgentController` | ❌ No | One instance per interactive session; not shared between threads |
| `SubAgentRunner` | ❌ No | Each sub-agent gets its own instance; runs in its own thread |
| `Scheduler` | ✅ Internal | Own thread + Lock; callbacks execute in scheduler thread |

## Cross-Thread Communication

### Confirmation Flow (agent ↔ Telegram)

1. Agent thread reaches a dangerous action → creates `threading.Event`, stores pending state
2. Agent thread calls `event.wait(timeout=120)` — blocks
3. Telegram async callback (main loop) receives inline button press
4. Callback sets `_confirmation_result` and calls `event.set()`
5. Agent thread unblocks, reads result, proceeds or aborts

This is safe because:
- `Event.set()` is thread-safe (Python guarantees this)
- The pending state dict is only written by the agent thread before `wait()`, and read by the callback thread after `set()` — no concurrent mutation

### Extension Flow (step limit reached)

Same pattern as confirmation: agent blocks on `_extend_event.wait()`, Telegram callback sets the choice and signals.

### Notify from Worker → Telegram

Worker threads (sub-agents, scheduler callbacks) call `tg.send_message_to_users(text)` which internally uses `asyncio.run_coroutine_threadsafe(coro, loop)` to safely post onto the main asyncio loop.

## Module Dependency Graph

```
main.py (composition root)
├── config_schema.py (parse + validate)
├── llm_client.py (LLM providers)
├── agent_controller.py (ReAct loop)
│   ├── llm_client
│   ├── tool_index → tool_registry, llm_client
│   ├── memory_store
│   ├── mcp_client → tool_registry
│   └── builtin_executor
├── telegram_interface.py (bot UI)
│   ├── telegram_formatter.py (pure formatting)
│   ├── scheduler
│   ├── tool_registry
│   ├── skill_registry
│   └── mcp_client
├── scheduler.py (cron jobs)
│   └── builtin_executor (sub-agent spawning)
├── mcp_client.py (MCP servers)
│   └── tool_registry (Tool dataclass)
└── exceptions.py (error hierarchy)
    interfaces.py (Protocol contracts)
```

## Key Design Decisions

1. **Composition root pattern** — `main.py` constructs all objects and wires dependencies explicitly. No service locator or DI container.

2. **Raw config dict (transitioning to typed)** — `config_schema.py` provides typed dataclasses; migration from raw `cfg` dict is incremental.

3. **Single-user assumption** — One `AgentController` instance serves the interactive session. Sub-agents get independent `SubAgentRunner` instances.

4. **Process isolation for tools** — All tool execution (shell, scripts) runs in subprocesses with timeouts. The agent process itself never `exec()`s untrusted code.

5. **Resilience over correctness** — Broad `except Exception` blocks prevent daemon crashes at the cost of sometimes hiding bugs. The exception hierarchy in `exceptions.py` enables gradual narrowing.

## Graph-Based Memory (Optional Feature)

Graph memory is an **opt-in** feature that stores entities, relationships, and episodic memory in a LadybugDB graph database (embedded, community fork of KuzuDB). It is **disabled by default** — no overhead is incurred unless explicitly enabled.

### Activation

```toml
# config.toml
[graph_memory]
enabled = true
db_path = "data/graph_memory"
buffer_pool_mb = 256
extraction_model = "gpt-4o-mini"   # falls back to agent.default_model
```

Install the optional dependency: `pip install ladybug`

### Architecture

```
[graph_memory] enabled = true
         │
         ▼
main.py calls create_graph_memory()
         │
         ├─ GraphMemoryStore (graph_memory.py)
         │    ├── LadybugDB (embedded graph DB)
         │    ├── Entity table   — semantic layer (people, tools, concepts)
         │    ├── Episode table  — episodic layer (timestamped interactions)
         │    ├── RELATES_TO rel — directed fact edges
         │    ├── HNSW vector index (embeddings from [embeddings] config)
         │    └── search() — hybrid: vector ANN + 1-hop graph expansion
         │
         └─ GraphMemoryWriter (background daemon thread)
              ├── Queue (fire-and-forget from caller)
              ├── LLM triplet extraction (configurable model, temperature=0.1)
              └── Writes to GraphMemoryStore on every N user turns
```

### Integration Points

| File | What changes |
|------|-------------|
| `react_loop.py` | `ReactContext.graph_memory` / `graph_memory_writer` fields; pre-injection of context before LLM call; user message enqueued after turn |
| `prompt_builder.py` | `graph_context_section` parameter; `{graph_context_section}` placeholder in template; graph memory rules added to agent instructions |
| `builtin_executor.py` | `memory_graph_search` and `memory_graph_store` built-in tools |
| `agent_controller.py` | `_graph_memory` / `_graph_memory_writer` fields; passed to `ReactContext` |
| `main.py` | Conditional init via `create_graph_memory()`; graceful fallback if `ladybug` not installed |

### Graceful Degradation

If `[graph_memory] enabled = false` (default), or if the `ladybug` package is not installed, the feature is silently skipped — `graph_memory` remains `None` on `ReactContext`, no graph context is injected, and the `memory_graph_*` tools return informative error messages.

### One-Time Backfill: LongTermMemory → Graph

Existing `LongTermMemory` entries (`data/longterm_memory.json`) are **not** automatically migrated into the graph. To seed the graph from those entries, run the one-time backfill CLI:

```bash
# Dry-run (no graph writes, no state update)
python backfill_graph_memory.py --config config.toml --dry-run

# Import everything (incremental — already-imported entries are skipped)
python backfill_graph_memory.py --config config.toml

# Re-import all entries regardless of prior state
python backfill_graph_memory.py --config config.toml --force

# Import only the first 20 entries
python backfill_graph_memory.py --config config.toml --limit 20
```

**Prerequisites:** `[graph_memory] enabled = true` in config.toml and `pip install ladybug`.
The main agent **must not** be running against the same `db_path` at the same time (single-process DB).

**How it works:**

1. Loads all `LongTermMemory` entries sorted oldest-first via `LongTermMemory.entries()`.
2. Filters out IDs already recorded in `data/graph_memory_backfill_state.json` (matching checksum).
3. For each remaining entry, runs the same LLM triplet-extraction pipeline used by `GraphMemoryWriter`.
4. Writes entities, facts, and an episode with `source="longterm_memory_backfill"`.
5. Saves the entry ID, SHA-256 checksum, episode ID, and timestamp to the state file atomically after each entry.

**Idempotency:** The state file (`data/graph_memory_backfill_state.json`) tracks imported entry IDs and content checksums. Rerunning is safe — unchanged entries are skipped. Use `--force` to reprocess all entries. The state file is written atomically (tmp file + `os.replace`) after each entry, so a partial run leaves no corrupt state.

**Fidelity note:** The graph stores *distilled* knowledge (extracted entities and facts), not verbatim transcripts. `Episode.content` is stored up to 2000 characters per entry; original LongTermMemory timestamps are preserved in the state file and episode text, but not in the graph schema's timestamp fields.

