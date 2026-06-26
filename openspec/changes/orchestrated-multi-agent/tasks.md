## 1. Structured Prompts — Foundation

### 1.1 Prompt directory and file creation
- [x] 1.1 Create `prompts/system/` directory with 5 section files
- [x] 1.2 Port content from `SYSTEM_PROMPT_TEMPLATE` into `01-identity.md`, `02-context.md`, `03-capabilities.md`, `04-execution.md`, `05-response-format.md`
- [x] 1.3 Add YAML frontmatter to each section (section, order, required, mode, variables)
- [x] 1.4 Write `prompts/sub-agent/` variant with simplified sections

### 1.2 Prompt loader implementation
- [x] 1.5 Create `prompt_loader.py` with `PromptSection` dataclass
- [x] 1.6 Implement section discovery from `prompts/system/*.md`
- [x] 1.7 Implement YAML frontmatter parsing
- [x] 1.8 Implement Jinja2 template rendering with variable injection
- [x] 1.9 Implement mode-based section filtering (`all`, `default`, `planner`, `explorer`, `resilient`)
- [x] 1.10 Implement validation: required sections, unresolved variables, duplicate order values, mode conflicts
- [x] 1.11 Add parsed template caching to avoid re-parsing on every step
- [x] 1.12 Add legacy fallback to `SYSTEM_PROMPT_TEMPLATE` when `prompts/` is missing

### 1.3 Integration and config
- [x] 1.13 Add `creativity_mode` field to `AgentConfig` in `config_schema.py` (default: "default")
- [x] 1.14 Add `prompts_dir` field to `PathsConfig` (default: "prompts")
- [x] 1.15 Wire `prompt_loader.py` into `react_loop.py` replacing `_build_system_prompt`
- [x] 1.16 Add `/mode` command to `telegram_commands.py` (cycles through modes or sets explicitly)
- [x] 1.17 Update `vulture_whitelist.py` for new public symbols

### 1.4 Testing
- [x] 1.18 Write unit tests for `PromptSection` parsing
- [x] 1.19 Write unit tests for section ordering and mode filtering
- [x] 1.20 Write unit tests for validation (missing required, unresolved vars, conflicts)
- [x] 1.21 Write integration test: full system prompt assembly with all modes
- [x] 1.22 Run `make check` — all tests pass, lint clean

## 2. Execution Planning — Orchestration

### 2.1 ExecutionPlan data model
- [x] 2.1 Create `execution_plan.py` with `ExecutionPlan` and `PlanStep` dataclasses
- [x] 2.2 Implement topological sort for dependency ordering
- [x] 2.3 Implement parallel batch detection (groups of independent steps)
- [x] 2.4 Implement result templating (`{{step_id}}` substitution in args)
- [x] 2.5 Implement plan validation (duplicate IDs, circular dependencies, unknown refs)

### 2.2 Plan executor
- [x] 2.6 Create `PlanExecutor` class with `execute(plan, ctx)` method
- [x] 2.7 Implement concurrent sub-agent spawning for independent steps
- [x] 2.8 Implement sequential waiting for dependent steps
- [x] 2.9 Implement result collection and failure propagation
- [x] 2.10 Implement plan execution timeout (default 300s, configurable)
- [x] 2.11 Implement concurrency cap awareness (batch when `max_subagents` exceeded)

### 2.3 ReAct loop integration
- [x] 2.12 Add `elif action == "plan"` branch in `react_loop.py`
- [x] 2.13 Parse `action_obj["plan"]` into `ExecutionPlan`
- [x] 2.14 Call `PlanExecutor.execute()` and collect results
- [x] 2.15 Feed plan results back into conversation as user message
- [x] 2.16 Ensure plan execution does not count against parent `max_iterations`

### 2.4 max_iterations reconsideration
- [x] 2.22 Design alternative to `max_iterations` for plan execution
- [x] 2.23 Options: plan completion as natural end, user confirmation for open-ended tasks, "budget" of total sub-agent calls
- [x] 2.24 Implement chosen approach: agent runs until plan completes or user cancels, with soft "still working?" prompt after N minutes inactivity
- [x] 2.25 Update `config_schema.py` if new config fields needed

### 2.5 Testing
- [x] 2.26 Write unit tests for topological sort (linear, parallel, diamond DAG)
- [x] 2.27 Write unit tests for plan validation (circular, missing deps, duplicates)
- [x] 2.28 Write unit tests for result templating
- [x] 2.29 Write integration test: full plan execution with mock sub-agents
- [x] 2.30 Write test for timeout behavior
- [x] 2.31 Run `make check` — all tests pass, lint clean

## 3. Agent Recovery — Resilience

### 3.1 Structured error fields
- [x] 3.1 Update built-in tool return dicts in `builtin_executor.py` to include `error_type`, `recoverable`, `suggestion`
- [x] 3.2 Map existing error conditions to structured fields (timeout, permission denied, file not found, etc.)
- [x] 3.3 Create `ErrorTypeRegistry` with known types and default retry policies
- [x] 3.4 Register built-in error types on startup

### 3.2 Simple recovery (first line)
- [x] 3.5 Implement automatic retry in `PlanExecutor` for `recoverable: true` + known `error_type`
- [x] 3.6 Implement exponential backoff (base 2s, max 3 retries)
- [x] 3.7 Ensure retry respects cancellation events
- [x] 3.8 Add retry count to plan execution result metadata

### 3.3 Complex recovery (escalation)
- [x] 3.9 Implement diagnostic sub-agent spawn when `recoverable: false` or retries exhausted
- [x] 3.10 Build diagnostic task template: "Analyze failure and suggest alternative approach"
- [x] 3.11 Feed diagnostic result back to parent agent as user message
- [x] 3.12 Allow parent agent to emit revised `plan` action in response to diagnostic
- [x] 3.13 Document recovery sequence: simple (automatic) → complex (diagnostic) → abort

### 3.4 Error type classification
- [x] 3.13 Classify all built-in tool errors into categories: transient (retryable), planning (no retry), fatal
- [x] 3.14 Document error type taxonomy in code comments

### 3.5 Testing
- [x] 3.15 Write unit tests for `ErrorTypeRegistry`
- [x] 3.16 Write unit tests for simple recovery (retry on timeout, no retry on permission)
- [x] 3.17 Write unit tests for complex recovery (diagnostic spawn, result fed back)
- [x] 3.18 Run `make check` — all tests pass, lint clean

## 4. Sub-Agent Context Sharing

### 4.1 Context payload mechanism
- [x] 4.1 Add `context_payload` parameter to `spawn_agent` tool schema in `builtin_executor.py`
- [x] 4.2 Add `context_payload` to `SubAgentRunner.__init__`
- [x] 4.3 Inject `context_payload` into sub-agent system prompt as `PARENT CONTEXT` section
- [x] 4.4 Implement `context_payload` size limit (default 2000 chars) with truncation
- [x] 4.5 Ensure `context_payload` is excluded from `context_key` persistence

### 4.2 Automatic context summarization
- [x] 4.6 Implement automatic summary generation when `context_payload` is omitted
- [x] 4.7 Summary includes: current user goal, last 2 tool results, relevant memory entries
- [x] 4.8 Add summary generation to `SubAgentRunner` or parent `AgentController`
- [x] 4.8a Wire `build_spawn_context_summary()` into `builtin_executor.py` spawn_agent fallback

### 4.3 Sub-agent prompt variant
- [x] 4.9 Load `prompts/sub-agent/` variant when sub-agent is spawned from plan execution
- [x] 4.10 Load `prompts/system/` variant for direct `spawn_agent` calls (backward compatible)

### 4.4 Testing
- [x] 4.11 Write unit tests for `context_payload` injection and truncation
- [x] 4.12 Write unit tests for automatic summary generation
- [x] 4.13 Write unit tests for prompt variant selection
- [x] 4.14 Run `make check` — all tests pass, lint clean

## 5. Strategy Memory — Learning

### 5.1 StrategyMemory data model
- [x] 5.1 Create `strategy_memory.py` with `Strategy` dataclass
- [x] 5.2 Implement `StrategyMemory` class with CRUD operations
- [x] 5.3 Implement confidence scoring and decay (30-day half-life)
- [x] 5.4 Implement task type classification from user goal (simple keyword/heuristic matching)
- [x] 5.5 Use GraphMemory for storage when enabled, else JSON file (`data/strategies.json`)

### 5.2 Background extraction
- [x] 5.6 Implement `extract_strategy()` function: analyzes execution outcome via LLM call
- [x] 5.7 Wire extraction as fire-and-forget background thread after execution completes
- [x] 5.8 Extract task type, approach used, success/failure, lessons learned
- [x] 5.9 Update strategy confidence and counters based on extraction result

### 5.3 Context injection
- [x] 5.10 Query `StrategyMemory` during prompt building for strategies matching current task type
- [x] 5.11 Inject top-K strategies (default 2) into system prompt via `{{strategies}}` variable
- [x] 5.12 Handle strategy conflicts (two strategies with similar confidence)

### 5.4 Testing
- [x] 5.13 Write unit tests for `Strategy` confidence decay
- [x] 5.14 Write unit tests for task type classification
- [x] 5.15 Write unit tests for strategy injection and conflict resolution
- [x] 5.16 Write integration test: full execution → extraction → strategy persistence
- [x] 5.17 Run `make check` — all tests pass, lint clean

## 6. Integration and Polish

- [x] 6.1 Update `README.md` with new architecture description
- [x] 6.2 Update `AGENTS.md` module table with new modules
- [x] 6.3 Add example `prompts/` directory to repo with default sections
- [x] 6.4 Add `/mode` Telegram command to command list in `telegram_commands.py`
- [x] 6.5 Ensure all new files have proper docstrings and type hints
- [x] 6.6 Final `make check` run — all tests pass, lint clean, vulture clean
