# AGENTS.md — OpenCode Instructions for smallpieclaw

## CRITICAL: Skill loading

**Before invoking ANY OpenSpec stage or /opsx command, you MUST load TWO skills:**

1. Load `openspec-workflow` — the standard workflow framework
2. Load the stage-specific skill (e.g. `openspec-apply-change`, `openspec-propose`, etc.)

**Concrete triggers — load both skills when you see:**
- User says `/opsx-apply`, `/opsx-propose`, `/opsx-verify`, `/opsx-archive`, `/opsx-explore`, `/opsx-sync`
- User says "implement tasks from an OpenSpec change", "apply this change", "propose a change", etc.
- You are about to run `openspec instructions apply`, `openspec status`, `openspec list`, etc.
- You are reading files under `openspec/changes/<name>/`

Failure to load `openspec-workflow` will result in missing critical workflow context.

## Reasoning

- Prefer retrieval-led reasoning over relying on pretrained knowledge.
- Inspect the existing codebase, configuration, documentation, tests, and relevant skills before making assumptions.
- Reuse existing patterns, utilities, abstractions, and conventions unless there is a concrete reason to introduce something new.
- For unfamiliar APIs, libraries, frameworks, or project-specific behavior, verify against authoritative documentation or the repository before implementing.
- Distinguish clearly between facts verified from the repository, facts retrieved from external documentation, and assumptions.
- Do not invent APIs, configuration options, files, commands, or project conventions.
- Before implementing a non-trivial change, identify affected components, dependencies, interfaces, tests, documentation, and configuration.
- Prefer the smallest correct change that satisfies the requirement. Avoid unrelated refactoring.



## Code quality

- Follow the existing project architecture and conventions.
- Avoid unnecessary superclasses, deep inheritance hierarchies, and large files.
- Prefer small, cohesive modules and functions with clear responsibilities.
- Follow PEP 8 and the project’s established formatting/linting conventions.
- Include type hints for all function parameters and return types.
- Write docstrings for all public modules, classes, functions, and methods.
- Prefer explicit, readable code over clever or overly abstract implementations.
- Avoid premature abstractions. Introduce an abstraction only when it provides a clear maintainability or reuse benefit.
- Preserve backward compatibility unless the requested change explicitly requires breaking it.
- Handle errors explicitly and preserve useful error context.
- Do not silently swallow exceptions or introduce broad exception handling without justification.
- Keep configuration, secrets, environment-specific values, and business logic properly separated.
- Do not introduce dependencies when the existing standard library or project dependencies are sufficient.


## Verification

- After every code change, run:
    - ruff check .
    - vulture . vulture_whitelist.py --min-confidence 80
- Run the most relevant tests after each meaningful implementation step.
- Before declaring a task complete, run the complete applicable test suite and all required static checks.
- If a check fails:
    1. Investigate the root cause.
    2. Fix the issue.
    3. Re-run the failed check.
    4. Do not declare the task complete while required checks remain failing.
- Do not weaken, disable, suppress, or modify lint/test rules merely to make verification pass unless explicitly requested.
- When tests are missing for new behavior, add appropriate tests unless there is a documented reason not to.
- Verify both the changed behavior and relevant regression scenarios.


## Development discipline 

- Never work directly on the main branch.
- Before modifying code, inspect the current Git state and branch.
- Use a feature/fix branch when a single agent is modifying code.
- Use a Git workspace/worktree when multiple agents are modifying different tasks in parallel.
- Keep parallel work isolated to prevent agents from modifying the same working tree.
- Delegate independent, well-defined tasks to sub-agents where this improves correctness or efficiency.
- Clearly define the scope and expected output when delegating work.
- Review sub-agent results before incorporating them into the main implementation.

## Git

- Do not commit or merge changes without explicit user approval or a direct command.
- Do not rewrite Git history unless explicitly instructed.
- Do not force-push unless explicitly instructed.
- Keep commits focused when the user has requested commits.
- Do not include unrelated changes in the requested work.
- Before committing, inspect the diff and verify that only intended changes are included.

## OpenSpec

- When using OpenSpec, follow its defined workflow strictly and in order.
- Do not skip, reorder, or implicitly advance OpenSpec steps.
- Do not move to the next OpenSpec step without an explicit user command.
- For propose, apply, verify, and archive workflows, use the local openspec-git-discipline skill.
- Follow the skill’s proposal-commit-before-apply and merge-before-archive requirements exactly.
- Do not perform OpenSpec archive/merge operations without the required user approval and workflow state.

## Tools and environment

- Do not install tools, packages, programs, scripts, plugins, or dependencies without explicit user approval or a direct command.
- Do not modify global system configuration without explicit approval.
- Do not mount filesystems without explicit user approval.
- Do not make destructive operations unless explicitly authorized.

## Documentation

- When a feature changes user-visible behavior, update the relevant README/documentation as part of the implementation.
- Update configuration examples when configuration changes.
- Update usage examples when CLI/API behavior changes.
- Update architecture/developer documentation when the implementation changes an architectural contract.
- Do not add documentation that merely repeats obvious implementation details.
- Documentation must describe the actual implemented behavior, not intended or hypothetical behavior.

## Usability

- Consider usability for every user-facing change.
-  Check error messages, CLI/API ergonomics, defaults, discoverability, and failure behavior.
-  Prefer actionable error messages that explain what went wrong and, where possible, how to fix it.
-  Preserve existing user workflows unless a change intentionally modifies them.
-  When adding a feature, consider:
    - configuration ergonomics;
    - sensible defaults;
    - backward compatibility;
    - clear error handling;
    - help/usage output;
    - examples;
    - logging/observability;
    - migration requirements;
    - accessibility where applicable.
- Do not add unnecessary UX complexity merely to expose internal implementation details.

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

**Built-in tools package:** `builtin_executor.py` is now a dispatcher/facade that imports from the `builtin_tools/` subpackage. Tool logic lives in submodules (`shell.py`, `files.py`, `memory.py`, `agents.py`, `schedule.py`, `context_io.py`, etc.); `descriptors.py` owns the `BUILTIN_TOOLS` registry. `builtin_executor.py` retains confirmation-token management and error-classification contract.

**Agent runtime (ADR-0007):** `agent_runtime.py` introduces `RuntimeProfile` (MAIN, ON_DEMAND_SUBAGENT, SCHEDULED_AGENT, PLAN_STEP_AGENT, DIAGNOSTIC_AGENT) as a construction-time policy enum. `AgentRuntime.create` is Phase 2 scaffolding — intentionally unimplemented.

**Sub-agent supervision:** `sub_agent_supervisor.py` centralizes sub-agent lifecycle (admission, execution, cleanup, callbacks) via `SubAgentSupervisor`. Replaces ad-hoc supervision in `agent_controller.py`. Uses `SupervisionOptions` and `SubmissionRequest` dataclasses.

## Key Modules

| Module | Role |
|---|---|
| `llm_client.py` | Multi-provider LLM (openai, openrouter, google, anthropic, ollama) + embeddings |
| `builtin_executor.py` | Dispatcher/facade for built-in tools; confirmation-token management and error-classification contract; imports from `builtin_tools/` |
| `builtin_tools/` | Built-in tool subpackage: `shell.py`, `files.py`, `memory.py`, `agents.py`, `schedule.py`, `context_io.py`, `descriptors.py` (registry), `schemas.py`, `patterns.py`, `logquery_helpers.py`, `secrets_log.py`, `text_utils.py` |
| `agent_logging.py` | structlog-based dual-sink logging; `LogEvent` enum (TOOL_START/END/FAILED, LLM_CALL/FAILED, STEP_BEGIN/END, RUN_BEGIN/END, ERROR); `setup_logging()`, `log_event()`, `bind_run_context()` |
| `agent_runtime.py` | Construction-time policy via `RuntimeProfile` enum (MAIN, ON_DEMAND_SUBAGENT, SCHEDULED_AGENT, PLAN_STEP_AGENT, DIAGNOSTIC_AGENT); ADR-0007 scaffolding |
| `sub_agent_supervisor.py` | Sub-agent lifecycle (admission, execution, cleanup, callbacks) via `SubAgentSupervisor`; `SupervisionOptions` + `SubmissionRequest` dataclasses |
| `vector_utils.py` | Vector math utilities for embeddings; `cosine_similarity(a, b) → float` |
| `tool_registry.py` | MCP tool registry |
| `tool_index.py` | Semantic tool search via embedding cosine similarity; persists to `data/tool_index.json` |
| `memory_store.py` | `MemoryStore` (KV), `ShortTermMemory`, `WorkingMemory`, `ResultsMemory`, `LongTermMemory` |
| `graph_memory.py` | Opt-in LadybugDB entity/relationship store; `GraphMemoryStore` + `GraphMemoryWriter` |
| `backfill_graph_memory.py` | One-time CLI to seed graph from `data/longterm_memory.json` |
| `scheduler.py` | Cron jobs via `scheduler.toml` (single source of truth); uses `croniter` |
| `mcp_client.py` | MCP server client — stdio (subprocess) and http transports |
| `skill_registry.py` | Discovers Agent Skills from `skills/<name>/SKILL.md` |
| `telegram_interface.py` | Telegram bot with allowlist/pairing security, streaming, inline confirmations |
| `telegram_formatter.py` | Pure formatting: md→html, message splitting, job list formatting |
| `telegram_commands.py` | All `/` command handlers |
| `telegram_callbacks.py` | Inline-button callback handlers (split from `telegram_commands.py`) |
| `prompt_builder.py` | System prompt assembly; re-exports `estimate_tokens` from `token_estimator.py` for backward compat |
| `token_estimator.py` | Two-layer token counting: tiktoken (OpenAI models) + conservative heuristic fallback |
| `context_manager.py` | Auto-compaction at 85% of effective context window (per-model `context_window` or `agent.ctx_max_tokens`), reserving completion tokens with a 256-token floor; content-aware trimming |
| `context_monitor.py` | Context-window consumption monitor; `ContextSnapshot` (immutable) + `ContextMonitor` (push snapshot per turn, reference swap); `compute_danger_level`, `compute_headroom_real`, `group_tool_defs_by_server` |
| `confirmation.py` | Thread-safe confirmations via `threading.Event` (agent thread blocks, Telegram callback signals) |
| `trace_context.py` | `r-<8 hex>` trace IDs for log correlation across agents/sub-agents/scheduler |
| `sub_agent_registry.py` | Tracks all active `SubAgentRunner` instances for `/agents` command |
| `prompt_registry.py` | ULID prompt-ID registry with dual-write persistence (event log + snapshot archive); `search()`, `show()`, `find_in_archive()`, in-memory eviction at 100, `SearchPage` dataclass; `prompts.jsonl` (event log) + `prompts_archive.jsonl` (snapshot archive) |
| `token_usage.py` | Per-model daily prompt/completion counters; thread-safe registry |
| `execution_plan.py` | DAG-based plan generation and execution with parallel/sequential orchestration |
| `strategy_memory.py` | Learned task-type-to-approach persistence and context injection |
| `prompt_loader.py` | Jinja2-based prompt section management with validation and mode filtering |
| `error_registry.py` | Error type registry with retry policies for agent recovery |

## Testing

**Fixtures** in `tests/conftest.py`:
- `minimal_config` — valid raw config dict
- `mock_llm_response` — factory returning MagicMock LLM with scripted JSON responses
- `finish_response` / `shell_response` — standard JSON action strings
- `mock_subprocess` — patches `subprocess.run`
- `tmp_agent_dir` — temp dir with `data/`, `downloads/`

**Execution harness** (`tests/execution_harness.py`): `ScriptedLLM`, `RecordingExecutor`, `run_react()` for deterministic multi-step ReAct testing without network, Telegram, or graph DB.

**Graph memory tests:** `test_graph_memory_e2e.py` uses `pytest.importorskip("ladybug")` — entire module skips when ladybug not installed. `test_graph_memory_integration.py` tests wiring without real DB. **Ladybug IS installed** in the project venv (`.venv/`, Python 3.14, ladybug 0.18.3) — the e2e tests run and pass (6/6). They do NOT skip in this environment.

**Full suite baseline:** `make check` (lint + test) yields **1627 passed, 1 skipped**. The single skip is `test_access_control.py::TestIsContained::test_normcase_case_insensitive` — a platform-specific test that skips on macOS because `os.path.normcase` is a no-op on case-insensitive filesystems. It is unrelated to any dependency or code change. No tests skip due to missing packages.

**Mocking:** Tests use `unittest.mock` (MagicMock, patch). No external mocking frameworks.

## Conventions & Gotchas

- **`vulture_whitelist.py`** must be updated when adding new public API symbols that vulture flags as unused (Protocol methods, dataclass fields, logging overrides, backfill API).
- **`prompt_builder.py`** re-exports `estimate_tokens`/`estimate_messages_tokens` from `token_estimator.py`. New token-estimation code goes in `token_estimator.py`; keep the re-export in `prompt_builder.py` for backward compat.
- **`react_loop.py`** is the canonical loop logic. `agent_controller.py` delegates to it. Don't add loop logic to the controller.
- **Config migration** is incremental. New code should use `app_cfg` (typed `AppConfig`); old code may still access `cfg` dict. Both are passed through `_run()`.
- **Graph memory** is opt-in. Always guard with `if graph_memory_store is not None:` or check `app_cfg.graph_memory.enabled`. The `memory_graph_*` built-in tools return graceful errors when unavailable.
- **Sub-agents** have depth limit 1 — they cannot spawn further sub-agents.
- **PID file locking** in `main.py` uses `fcntl.flock()` — the OS releases the lock on process exit, so stale PID files from crashes are handled automatically.
- **Logging:** `structlog` integrated with stdlib, one processor chain → dual sink under `~/.local/state/<agent_name>/logs/`: `agent.jsonl` (structured, primary) + `agent.log` (prose `[label trace] message` with source tags like `[main]`, `[sa-<id>]`, or a scheduled job's `[<job-tag>]`, secondary). Events carry structured identity (`trace` and `agent`, the run label) and an `event_type` taxonomy (`TOOL_START/END/FAILED`, `LLM_CALL/FAILED`, `STEP_BEGIN/END`, `RUN_BEGIN/END`, `ERROR`). Daily gzip date-suffixed rotation, 30 backups. The `log_query` built-in tool filters the active `agent.jsonl` (trace-scoped) for self-analysis.
- **`tomli`** is used for TOML parsing (fallback for Python < 3.11 where `tomllib` is stdlib).
- **Prompt registry** uses a dual-write persistence pattern (ADR-0014): `prompts.jsonl` (append-only event log, crash-safe) + `prompts_archive.jsonl` (one self-contained snapshot line per finalized prompt, for search/show). In-memory records are capped at 100 finalized (`MAX_IN_MEMORY`); running records are never evicted. `search()` snapshots in-memory under lock then scans the archive lock-free — the only method that does file I/O outside the lock. `get()`/`by_trace()` stay in-memory-only for the hot path; `show()` is the archive-aware lookup.
