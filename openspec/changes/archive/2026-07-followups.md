# Follow-ups from July 2026 Archived OpenSpec Changes

Consolidated list of deferred items, future work, and out-of-scope items extracted from the 12 OpenSpec changes archived in July 2026. Each entry cites the source artifact and a category. Priority is "unspecified" unless the source explicitly marked it.

---

## 2026-07-01-vault-secret-manager

- **Encrypted vault backends** (non-file vault types) — *design.md* — security — unspecified
- **Cloud vault backends** — *design.md* — feature — unspecified
- **Vault plugins** — *design.md* — feature — unspecified
- **Vault write/modify operations via the agent** (vault is read-only at runtime) — *design.md* — feature — unspecified
- **Cache vault values in `os.environ`** (rejected for now to avoid leaking to subprocesses) — *design.md* — security — unspecified
- **Per-key approval for `secret_get` replaced by a single "Open vault" request** granting temporary access to read any key — *design.md* — feature — unspecified
- **"Approve-all for vault lookups" option** to reduce friction for skill execution — *design.md* — feature — unspecified

## 2026-07-05-improve-agent-logging

- **SQLite/queryable store for logs** (explicitly deferred) — *proposal.md, design.md, adr.md* — feature — unspecified
- **Retrofit every call site to structured `structlog` loggers** (only hot set migrated) — *proposal.md, design.md* — refactor — unspecified
- **Mid-run querying of rotated `.gz` history** (cross-run learning stays with memory subsystems) — *proposal.md, design.md* — feature — unspecified
- **Broad entropy heuristics for secret redaction** (narrow high-confidence heuristic set as a possible data-driven fast-follow) — *design.md* — security — unspecified

## 2026-07-13-extract-sub-agent-supervisor

- **Introduce `AgentRuntime`, runtime profiles, or a generalized `RunHandle` abstraction** (deferred to `introduce-agent-runtime`) — *design.md, proposal.md* — feature — unspecified
- **Decide whether plan steps, scheduled jobs, or the main agent should appear in `/agents`** (deferred to `unify-running-agent-visibility`) — *design.md* — feature — unspecified
- **Change `max_subagents` semantics for scheduled jobs** (deferred to `unify-running-agent-visibility`) — *design.md* — feature — unspecified
- **Split all built-in tools into a package** (deferred to `split-builtin-executor-modules`) — *design.md* — refactor — unspecified
- **Support nested sub-agents or depth greater than 1** — *design.md* — feature — unspecified
- **Move context payload construction into a runtime builder** (deferred to later AgentRuntime change) — *design.md* — refactor — unspecified
- **`get_agent_result` eventually wait on a `RunHandle` instead of `SubAgentRecord`** (deferred to `introduce-agent-runtime`) — *design.md* — refactor — unspecified
- **`depth` and confirmation mode entirely derived from future runtime profiles** (deferred to `introduce-agent-runtime`) — *design.md* — feature — unspecified

## 2026-07-13-unify-running-agent-visibility

- **Put the main interactive agent into `SubAgentRegistry`** (intentionally excluded — it is not a sub-agent execution) — *design.md* — feature — unspecified
- **Introduce `AgentRuntime`, runtime profiles, or a generalized run registry** (deferred to `introduce-agent-runtime`) — *design.md* — feature — unspecified
- **Whether future `AgentRuntime` profiles should reuse these source names or introduce separate profile names** (deferred to `introduce-agent-runtime`) — *design.md* — refactor — unspecified

## 2026-07-14-cleanup-builtin-executor-facade

- **`ConfirmationCoordinator` extraction** (`_pending`, headless-bridge state, `confirm`/`cancel`) — parked pending two unresolved upstream decisions: whether a planned "unified" approve-all replacement needs to merge with `confirmation.py`'s separate `ConfirmationManager`, and the outcome of a security review of shell's confirmation-gating logic — *proposal.md, design.md* — refactor — unspecified
- **Auditing other façade methods for similar leaf-vs-handler questions** (widening the sweep beyond the four items) — *design.md* — refactor — unspecified
- **ADR-0008's Decision text says `_exec_schedule` stays "inline"** — now factually stale; acknowledged as drift, ADR file untouched — *design.md, tasks.md, review-log.md* — docs — low

## 2026-07-14-introduce-agent-runtime

- **Full adoption of `MAIN` profile** (first implementation may only equivalence-test `MAIN` and route through runtime in a later step) — *design.md* — refactor — unspecified
- **Whether `AgentRuntime.create()` should return `SubAgentRunner`, `AgentController`, a wrapper object, or profile-dependent products** — *design.md* — refactor — unspecified
- **Whether LLM client construction should live directly inside `AgentRuntime` or depend on an injected LLM-client provider** — *design.md* — refactor — unspecified
- **Whether confirmation mode should eventually be derived from `RuntimeProfile`, or only preserved through existing `AgentController` wiring** — *design.md* — feature — unspecified

## 2026-07-14-split-builtin-executor-modules

- **Extracting `ConfirmationCoordinator`** (deliberate follow-on change; this change only bakes in three zero-cost seam constraints) — *proposal.md, design.md, adr.md* — refactor — unspecified
- **Repointing `_load_context` in `agent_runtime.py:313`** from re-export to `builtin_tools.context_io` (deferred as later cleanup) — *design.md, review-log.md* — cleanup — unspecified

## 2026-07-15-native-tool-calling

- **Multi-tool calls in a single turn** (single tool call per turn, matching current behavior) — *design.md* — feature — unspecified
- **Schema generation for script tools (`.sh`/`.py`)** — these are being phased out — *design.md* — feature — unspecified
- **Anthropic native API support** (not in use) — *design.md* — feature — unspecified
- **`top_tools` filter for tool definition size** if it becomes an issue (well within OpenAI's 128-function limit for now) — *design.md* — performance — unspecified
- **Config flag to fully disable native tool calling** (not needed for initial deployment given robust fallback) — *design.md* — config — unspecified

## 2026-07-16-decompose-react-loop

- **Split formatters/parsers (`fmt_*`, `parse_json`, `_linearize_native_turns`) into a sibling module** (separate lower-priority change; would rewrite ~20 test imports) — *design.md* — refactor — unspecified
- **Pre-verify whether `build_tool_definitions` exposes `finish` as a native tool** (OQ1 — to be grepped before Tier 3) — *design.md, proposal.md* — other — unspecified
- **Determine whether native tool call arguments ever arrive as a list** (OQ2 — determines whether list-arg normalization moves into `_dispatch_action`) — *design.md* — refactor — unspecified

## 2026-07-16-replace-mcp-transport-with-official-sdk

- **Wire new MCP features (resources, prompts, sampling)** — come free with the SDK but are not explicitly wired — *design.md* — feature — unspecified
- **`close_all` manager-level test coverage** (review-log finding: 6.2 covers wrapper close(), but 6.3 omits close_all) — *review-log.md* — test — low
- **"Disabled server skipped on startup" test assertion** (review-log finding: implemented in 4.2 but not asserted in any test) — *review-log.md* — test — low
- **D7 threading.Lock protection for `_tool_to_server` / `_wrappers` has no explicit task callout** (review-log finding: likely subsumed but a one-line mention would prevent it being dropped) — *review-log.md* — docs — low

## 2026-07-17-split-telegram-callbacks

- No follow-ups found.

## 2026-07-18-file-access-zones

- **Full per-sub-agent `GrantTracker` isolation** (currently shared; sub-agents reuse the main agent's executor, so grant sets are effectively shared) — *design.md* — security — unspecified
- **macOS case-insensitivity handling** (`realpath()` doesn't normalize case — noted as a decision) — *review-log.md* — security — low
- **`add_trusted` dedup/normalization** against duplicate and trailing-slash variants — *review-log.md* — cleanup — low

---

## Cross-cutting observations

- **`ConfirmationCoordinator` extraction** is referenced as a follow-on in two changes (`cleanup-builtin-executor-facade`, `split-builtin-executor-modules`) and is gated on a security review of shell confirmation gating plus the "unified approve-all" decision. It is the most cross-referenced deferred refactor.
- **`AgentRuntime` full adoption** is referenced across three changes (`extract-sub-agent-supervisor`, `unify-running-agent-visibility`, `introduce-agent-runtime`) and remains Phase 2 scaffolding.
- **Per-sub-agent `GrantTracker` isolation** is the only security-flagged follow-up with concrete user-visible impact (concurrent sub-agents can clear each other's per-request grants today).
- **Three low-priority test/docs gaps** from `replace-mcp-transport-with-official-sdk` are quick wins if touched in a single pass.