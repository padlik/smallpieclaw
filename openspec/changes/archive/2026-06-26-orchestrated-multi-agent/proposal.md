## Why

The agent's system prompt is currently a hardcoded Python string literal in `prompt_builder.py`, making it impossible to iterate on prompts without code changes. Sub-agents run in complete isolation with zero shared context, forcing verbose task strings and preventing any retry/recovery when things fail. Skills are passive markdown documents that the LLM must manually discover and execute. There is no structured planning before execution, no strategy persistence across sessions, and error handling is fragmented between plain text strings, dicts, and exceptions. This change introduces a structured prompt system, an orchestrated multi-agent execution model with planning and recovery, and a foundation for strategy learning.

## What Changes

- **Structured Prompt System**: Replace the hardcoded `SYSTEM_PROMPT_TEMPLATE` with Jinja2-based `.md` section files in `prompts/system/`. Each section declares its variables, mode applicability, and ordering. Prompts are validated at load time for missing sections, unresolved variables, and mode conflicts.
- **Creativity Modes**: Introduce explicit `default` / `planner` / `explorer` / `resilient` modes. Config sets the default mode; Telegram `/mode` overrides it at runtime. Modes conditionally inject prompt sections: `planner` adds planning rules, `explorer` adds exploration rules, `resilient` adds reflection-on-failure rules. All modes support planning when the task benefits from it — the mode changes *how* the agent thinks, not whether it plans.
- **Planning Action**: Add `"plan"` action to the ReAct loop. The LLM emits a JSON `ExecutionPlan` (DAG of tool calls with `depends_on`). The executor topologically sorts the DAG, runs independent steps in parallel via sub-agents, and feeds results back to dependent steps.
- **Execution Plan Executor**: New `ExecutionPlan` dataclass and executor module. Handles topological sort, parallel sub-agent spawning, result collection, and failure propagation. Reuses existing `spawn_agent` infrastructure.
- **Two-Tier Recovery**: Structured errors with `error_type`, `recoverable`, `suggestion` fields. **Simple recovery** is automatic retry for known transient errors (timeout, syntax, network). **Complex recovery** spawns a diagnostic sub-agent to analyze failures and suggest alternative approaches, enabling re-planning. Recovery is applied in sequence: simple first, then complex.
- **Strategy Memory (Phase 1)**: Add `StrategyMemory` layer to persist learned approaches per task type (e.g., "for scanned PDFs, prefer vision model over local OCR"). Populated by post-execution extraction and injected into system prompt context for relevant tasks.
- **Skill Quality Improvements**: Skills remain passive (LLM-driven) but get better prompting — the system automatically injects relevant skill content into the plan context when a skill matches the task, reducing wasted discovery turns.
- **Sub-Agent Context Sharing**: Parent agent passes a summarized conversation context + relevant memory to sub-agents via a new `context_payload` parameter, replacing the current zero-context isolation.

## Capabilities

### New Capabilities
- `structured-prompts`: Jinja2-based prompt section management with validation, ordering, and mode-dependent injection.
- `execution-planning`: DAG-based plan generation and execution with parallel/sequential orchestration.
- `agent-recovery`: Two-tier error recovery (automatic retry + diagnostic re-planning).
- `strategy-memory`: Learned task-type-to-approach persistence and context injection.
- `sub-agent-context`: Shared parent context (conversation summary, relevant memory) for spawned sub-agents.

### Modified Capabilities
- `agent-execution` (react_loop): New `"plan"` action branch; structured error propagation from tools.
- `telegram-commands`: New `/mode` command for creativity mode toggling.

## Impact

- **`prompt_builder.py`**: Deprecated and replaced by `prompt_loader.py` + `prompts/` directory.
- **`react_loop.py`**: New `elif action == "plan"` branch; structured error handling from tool dispatch.
- **`builtin_executor.py`**: `spawn_agent` accepts `context_payload` for parent context sharing.
- **`agent_controller.py`**: Creativity mode wired through to `ReactContext`.
- **`config_schema.py`**: New `AgentConfig.creativity_mode` field.
- **`telegram_commands.py`**: New `/mode` command.
- **New modules**: `execution_plan.py`, `strategy_memory.py`, `prompt_loader.py`, `prompts/` directory.
- **No breaking changes** to existing tool APIs or config format — all additions are opt-in.
