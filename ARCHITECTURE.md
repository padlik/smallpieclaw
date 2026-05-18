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
│  │    agent threads  │  │  • Logs to agent.log         │    │
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
│   ├── tool_creator → tool_index, tool_registry
│   ├── tool_executor → tool_registry
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
