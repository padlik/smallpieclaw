## Context

The current system prompt in `prompt_builder.py` is a single 100-line Python string literal using `str.format()` with named placeholders, plus a `str.replace()` hack to inject job history. It cannot be edited without touching code, has no validation, and offers no way to visualize or manage conflicts between sections.

Sub-agents are spawned with zero shared context — only a `task` string. The parent has no mechanism to pass conversation history, relevant memory, or past tool results. Sub-agents cannot spawn further sub-agents (depth=1 hardcoded). When sub-agents fail, the parent gets a plain text error string with no structured recovery path.

Skills are passive markdown documents. The LLM must manually discover them via `file_read`, wasting turns. There is no automatic skill context injection.

Error handling is fragmented: `exceptions.py` defines a hierarchy that is barely used, `llm_client.py` defines its own parallel hierarchy, and most errors are plain text strings fed back to the LLM. There is no retry logic at the tool level beyond empty-response retries in the LLM client.

## Goals / Non-Goals

**Goals:**
- Replace hardcoded system prompt with validated, file-based Jinja2 sections
- Add explicit creativity modes (default/planner/explorer/resilient) with mode-conditional prompt injection
- Enable the LLM to emit structured execution plans with parallel sub-agent execution
- Implement two-tier recovery: automatic retry for transient errors, diagnostic sub-agents for complex failures
- Share parent context (conversation summary, memory) with spawned sub-agents
- Lay foundation for strategy memory (learned task-type-to-approach persistence)

**Non-Goals:**
- Do NOT turn skills into active/scripted execution engines (skills remain LLM-driven)
- Do NOT implement full hierarchical multi-agent depth > 1 (depth remains 1, but context sharing makes sub-agents more effective)
- Do NOT replace the ReAct loop with a fundamentally different architecture (evolution, not revolution)
- Do NOT add a separate planning LLM (planning is an action within the existing loop)

## Decisions

### Decision 1: Jinja2 for templating, not str.format()
**Why**: `str.format()` has no conditionals, loops, or mode-dependent blocks. Jinja2 enables `{% if mode == "planner" %}...{% endif %}` within sections, making creativity modes natural. The dependency is lightweight (`pip install jinja2` or likely already present).
**Alternatives considered**: Keep `str.format()` and use separate files per mode (rejected — too many files, hard to compare); use Mustache (rejected — Jinja2 is more powerful and Python-native).

### Decision 2: 5 section files, not one file per section
**Why**: With ~20 fine-grained sections, users would scroll endlessly. Grouping into 5 semantic files keeps the mental model simple while preserving internal structure. Each file can contain mode-conditional blocks.
**Alternatives considered**: 20 individual files (rejected — too fragmented); 1 monolithic file (rejected — defeats the purpose of modularity).

### Decision 3: Plan as a ReAct action, not a separate planner module
**Why**: Adding `"plan"` as a new action type minimizes changes to `react_loop.py` (one new `elif` branch). The heavy lifting lives in a new `execution_plan.py` module. This keeps the architecture evolution incremental.
**Alternatives considered**: Separate `Planner` class wrapping ReAct (rejected — more moving parts, harder to test); pre-planning before loop starts (rejected — loses ability to re-plan mid-execution).

### Decision 3a: Prompt separation applies to all prompt types, not just system prompt
**Why**: The `prompts/` directory structure supports multiple prompt variants: `system/`, `sub-agent/`, `skill-executor/`, `compaction/`, etc. Each variant is a set of sections that can be loaded independently. The `prompt_loader.py` is generic — it loads any directory of `.md` section files. This means compaction prompts (used by `context_manager.py`), extraction prompts (used by `graph_memory.py`), and any future prompts all follow the same pattern.
**Scope for this change**: Phase 1 focuses on `prompts/system/` and `prompts/sub-agent/`. Other prompt types (compaction, extraction) are migrated opportunistically or left as Python strings if they are stable and rarely edited.

### Decision 4: Sub-agents execute plan steps, not inline tool calls
**Why**: Reusing `spawn_agent` for each plan step gives isolation, timeout handling, and cancellation for free. It also means plan steps can use any tool (built-in, custom, MCP) without the executor needing to know about them.
**Alternatives considered**: Inline tool calls in the executor (rejected — would duplicate tool dispatch logic); new lightweight task runner (rejected — adds complexity, sub-agents already exist).

### Decision 5: StrategyMemory with JSON as primary, GraphMemory as optional
**Why**: GraphMemory is opt-in (disabled by default) and adds a dependency on `ladybugdb`. For a home server agent, simplicity is paramount. The primary storage is a JSON file (`data/strategies.json`) that is loaded at startup and persisted periodically. If GraphMemory is enabled, strategies are additionally stored there for richer querying, but JSON remains the source of truth.
**Alternatives considered**: GraphMemory only (rejected — forces dependency on opt-in feature); ResultsMemory (rejected — wrong semantic).

### Decision 6: Creativity modes are config-driven, not automatic
**Why**: Explicit modes are predictable and debuggable. The user knows what mode the agent is in. Automatic mode selection based on task characteristics is a future enhancement.
**Alternatives considered**: Automatic mode selection (rejected — too complex for Phase 1, risk of wrong mode selection).

### Decision 7: Structured errors use dict fields, not exceptions
**Why**: The ReAct loop already uses dict-based tool outcomes (`{"success": bool, "output": str, "error": str}`). Adding `error_type`, `recoverable`, `suggestion` fields is a natural extension. Using exceptions would require refactoring the entire loop.
**Alternatives considered**: New exception hierarchy (rejected — would touch every tool dispatcher); JSON error envelopes (rejected — adds wrapping layer, dict fields are simpler).

## Risks / Trade-offs

**[Risk] Token bloat from context payload** → Mitigation: Default 2000-char limit on `context_payload`, with truncation preserving structure.

**[Risk] Plan action increases step count** → Mitigation: Planning counts as one step; the executor runs all plan steps "for free" (not counted against parent max_iterations). This prevents a 3-step plan from consuming 4+ parent steps.

**[Risk] Parallel sub-agents exceed concurrency cap** → Mitigation: Executor respects `max_subagents`. If a plan has more independent steps than the cap, it batches them (run N, wait, run next N).

**[Risk] Jinja2 injection if user controls prompt variables** → Mitigation: Variables are system-provided (memory, tools, etc.), never user-controlled. The prompt loader uses `jinja2.Environment` with autoescape disabled (prompts are trusted), but validates all variables are declared.

**[Risk] Strategy extraction adds LLM calls** → Mitigation: Extraction is fire-and-forget on a background thread. If it fails or times out, the main execution is unaffected.

**[Trade-off] Sub-agent overhead vs. parallelism** → Each sub-agent spawns a new `LLMClient`, `ShortTermMemory`, etc. Parallel execution of 3 simple shell commands is slower than sequential inline execution due to setup overhead. The executor should only use sub-agents for steps that genuinely benefit from isolation (LLM calls, complex tool chains).

**[Trade-off] Prompt file I/O on every step** → Loading and parsing Jinja2 templates on every ReAct step would be expensive. The prompt loader caches parsed templates at startup and only re-renders with new variable values.

## Migration Plan

**Phase 1 (no downtime):**
1. Create `prompts/` directory with 5 section files (copy existing content from `SYSTEM_PROMPT_TEMPLATE`)
2. Implement `prompt_loader.py` with validation
3. Add `creativity_mode` to config (default: "default")
4. Wire new loader into `react_loop.py` with fallback to legacy template
5. Deploy and test

**Phase 2 (incremental):**
6. Implement `ExecutionPlan` and executor
7. Add `"plan"` action to ReAct loop
8. Add structured error fields to built-in tools
9. Add recovery logic
10. Deploy and test

**Phase 3 (incremental):**
11. Implement `StrategyMemory` with JSON fallback
12. Add background extraction pass
13. Add strategy context injection to prompt loader
14. Deploy and test

**Rollback**: If issues arise, set `agent.creativity_mode = "default"` and remove `prompts/` directory to fall back to legacy template.

## Open Questions

1. ~~Should the executor inline simple tool calls?~~ **Decision**: No. Sub-agent overhead is acceptable for consistency. Every plan step runs as a sub-agent.

2. ~~How does plan execution interact with max_iterations?~~ **Decision**: Explore replacing `max_iterations` with a smarter completion criterion. Options: plan completion naturally ends, user confirmation for open-ended tasks, or a "budget" of total sub-agent calls. The current hard step limit forces artificial interruption. Design goal: agent runs until the plan completes or the user cancels, with a soft "are you still working?" prompt after N minutes of inactivity.

3. ~~Should strategies be shared across users?~~ **Decision**: Single-user personal agent. Strategies are global (no per-user isolation needed).

4. ~~Should the prompt loader watch for file changes?~~ **Decision**: No. Prompt changes require restart. Monitor usage frequency and revisit if operators iterate prompts heavily.
