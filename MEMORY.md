# Agent Memory Architecture

This document describes how memory is organised in `smallpieclaw`, which files
and databases are used, and how the `/reset` command interacts with the memory
subsystem.

---

## Overview

The agent uses **five distinct memory layers** at different scopes and lifetimes:

| Layer | Scope | Persistence | Location |
|---|---|---|---|
| MemoryStore | Per-agent singleton | JSON file, across sessions | `data/memory.json` |
| ShortTermMemory | Per-conversation | In-memory ring buffer; persisted for sub-agents | `data/job_contexts/<key>.json` |
| WorkingMemory | Per-task (in-flight) | In-memory only | — |
| ResultsMemory | Cross-session, per-agent | JSON file | `data/results_memory.json` |
| GraphMemoryStore | Cross-session, semantic | LadybugDB graph + HNSW vector index | `data/graph_memory` (default) |

---

## Layer 1 — MemoryStore (`data/memory.json`)

**File:** `memory_store.py` → `MemoryStore`

A simple JSON-backed key-value store. Written atomically (temp-file + `os.replace`)
with exponential-backoff retry to avoid partial writes.

**Contents:**
- Operator-managed facts: `known_services`, `notes`, and any key set via the
  `memory_write` tool.
- An `_event_log` list: the last 50 timestamped events (user requests, agent
  completions, cancellations, and max-iteration events). Capped automatically.
  `/reset` does not append to the event log.

**Thread safety:** all mutations use a `threading.RLock`, so concurrent
sub-agents can call `memory_write` safely without data races.

**Prompt injection:** `MemoryStore.as_prompt_text()` formats only **public**
keys as a flat `key: value` block injected into the system prompt at every turn.
Internal bookkeeping keys (those prefixed with `_`, such as `_event_log`) are
deliberately excluded — they add prompt noise and token cost with no reasoning
value. If only internal keys exist, the block renders as "No persistent memory
entries."

---

## Layer 2 — ShortTermMemory (conversation ring buffer)

**File:** `memory_store.py` → `ShortTermMemory`

A bounded `deque` that stores the last `max_turns` (default: 20) conversation
turns as `{role, content}` dicts. This is the agent's **chat history**.

**Lifetime:**
- The main agent holds one `ShortTermMemory` instance for the lifetime of the
  process. Turns accumulate until `/reset` clears them.
- Sub-agents spawned with a `context_key` load their own `ShortTermMemory` from
  `data/job_contexts/<key>.json` so they resume a previous conversation thread.
  On exit, the sub-agent persists its history back to the same file.

**How it feeds the prompt:** at the start of each `react_loop` call,
`messages.extend(ctx.short_term.get_messages())` prepends the full ring buffer
to the LLM message list before the new user goal is appended.

**`as_prompt_text()`** shows the last 10 turns (truncated to 200 chars each),
but it is not used by the current system-prompt path. Runtime chat history is
supplied as prior LLM messages via `get_messages()`, not as text in the system
prompt.

---

## Layer 3 — WorkingMemory (in-flight task context)

**File:** `memory_store.py` → `WorkingMemory`

An in-memory object tracking the **currently running task**:
- `goal` — the user's request text.
- `steps` — list of `{action, details, timestamp}` dicts appended after each
  tool call.
- `started_at` — ISO timestamp.

`WorkingMemory` is **not persisted** and is **not injected into the system
prompt**. The model already sees what it has done this turn because each tool
call and its result are part of the `messages` list (assistant / tool-result
turns). `WorkingMemory` exists for two purposes:
1. Step tracking during a run (`start_task()` / `add_step()`), used for logging
   and progress.
2. On task finish via `reset_task(save=True)`, its `to_summary_text()` output is
   condensed by the LLM into a 2–3 sentence summary that is stored into
   `ResultsMemory`. `to_summary_text()` is **only** used on this `/reset` path.

`WorkingMemory` is cleared after every successful `finish` action or when
`/reset` is called.

---

## Layer 4 — ResultsMemory (`data/results_memory.json`)

**File:** `memory_store.py` → `ResultsMemory`

A JSON-backed vector index of completed task summaries. Each entry records:
- `goal` — original request.
- `summary` — task outcome. Both write paths now produce bounded, 2–3 sentence
  summaries:
  - On a `finish` action (`react_loop`), the final `result` is summarized by the
    LLM. If the LLM call fails or returns an empty/overlong response, a
    deterministic fallback (goal + bounded head/tail of the raw result) is stored.
  - On `/reset save` (`reset_task`), the LLM condenses the working-memory step
    log into a 2–3 sentence summary (falling back to a truncated step log if the
    LLM call fails).
- `tools_used` — list of tool names used.
- `timestamp` — ISO timestamp.
- `vector` — embedding of `"Goal: <goal>\nResult: <summary>"` (if an embedder
  is available).

**Search:** `search(query, top_k)` embeds the query and ranks entries by cosine
similarity. Falls back to recency order when no embedder is available.

**Prompt injection:** `build_system_prompt()` injects up to **2** (`top_k=2`)
recent/relevant past results so the model can recall how similar requests were
handled previously. When **graph memory is enabled and returns context for the
turn**, `react_loop` suppresses this injection (`results_top_k=0`) to avoid
redundant/overlapping recall — graph memory already provides richer, ranked
semantic recall in that case. `ResultsMemory` remains the default recall layer
whenever graph memory is disabled (the default configuration).

**Note:** `LongTermMemory` (`data/longterm_memory.json`) is a **legacy backfill
shim** (`is_migration_only = True`). It is not wired into any runtime
controller/scheduler and should not be written by normal runtime code. The
legacy `long_term` / `long_term_memory` runtime parameters have been removed
from `AgentController`, `SubAgentRunner`, and `Scheduler`. The only supported use
case is one-time migration into graph memory via `backfill_graph_memory.py`.

---

## Layer 5 — GraphMemoryStore (semantic graph + vector index)

**Files:** `graph_memory.py` → `GraphMemoryStore` and `GraphMemoryWriter`

An opt-in, session-persistent knowledge graph backed by **LadybugDB**
(embedded Kuzu-compatible graph DB) with an HNSW vector index. Enabled via
`[graph_memory] enabled = true` in `config.toml`. Off by default.

**Storage:** single file at `graph_memory.db_path` (default: `data/graph_memory`).

### Schema

Three-layer property graph:

| Node/Edge | Purpose | Key fields |
|---|---|---|
| `Entity` | Semantic concept (person, tool, concept, preference) | `id`, `name`, `entity_type`, `normalized_name`, `embedding`, `mention_count`, `first_seen`, `last_seen` |
| `Episode` | Timestamped conversation event | `id`, `content`, `source`, `user_id`, `admission_status`, `confidence`, `created_at`, `embedding` |
| `RELATES_TO` | Directed fact edge between entities | `relation_type`, `fact`, `valid_at`, `invalid_at`, `admission_status`, `confidence` |
| `MENTIONED_IN` | Entity→Episode link | `confidence` |

### Admission and Confidence System

Every edge and episode carries:

| Status | Meaning | Default confidence |
|---|---|---|
| `confirmed` | Operator explicitly approved via confirmation flow | 0.95 |
| `observed` | Auto-extracted from chat / legacy backfill | 0.6 / 0.5 |
| `proposed` | Suggested by model/sub-agent, not yet approved | — |
| `rejected` | Explicitly denied (never stored) | — |

On `add_relation` **MATCH** (duplicate edge), a `confirmed` edge is **never
downgraded** by a later `observed` write — the CASE guard in the Cypher
`ON MATCH SET` clause preserves the operator-approved fact text and timestamp.
An `observed` edge *can* be promoted to `confirmed` when the operator later
approves it.

Recalled facts are always injected as explicitly untrusted informational context
(delimited with `--- begin/end recalled facts ---`) so adversarial content stored
in past conversations cannot override system prompt or tool-safety rules.

### Background Extraction (GraphMemoryWriter)

Graph writes never block the agent turn. Two sources are enqueued:

1. **User messages** — after each user goal, `react_loop` calls
   `graph_memory_writer.enqueue(user_goal, source="chat")` for general fact
   extraction.
2. **Task outcomes** — when a task finishes (via `finish` action or `/reset
   save`) and graph memory is enabled, a bounded text containing the goal,
   the final summary, and the tools used is enqueued with
   `source="task_outcome"`. Only the already-summarized outcome is persisted;
   raw tool output and chat history are not sent to graph memory.

In both cases, texts accumulate in a `_pending` list. Every
`extract_every_n_turns` enqueues (default: 3), the batch is pushed to an internal
`queue.Queue`. A **daemon thread** (`graph-memory-writer`) picks up the batch,
calls the LLM extraction model with `EXTRACTION_PROMPT`, parses the JSON
response into `entities` + `facts`, then writes them to `GraphMemoryStore`.

Extraction prompt instructs the model to produce self-contained facts with
`SCREAMING_SNAKE_CASE` relation types. The result is parsed robustly: fenced
JSON is stripped, partial results are accepted, and unknown fields are ignored.
A failed LLM call or unparseable response is counted and logged; it does not
crash or block the agent.

### Hybrid Retrieval (Three-Phase Search)

`GraphMemoryStore.search(query, k=10)` runs three phases:

1. **HNSW entity vector search** — embeds the query, finds top-`k*2` similar
   entity nodes using the HNSW index (`QUERY_VECTOR_INDEX`).
2. **1-hop graph expansion** — follows `RELATES_TO` edges from the top-5 seed
   entities to retrieve up to `k*2` facts (filters out soft-deleted edges where
   `invalid_at IS NOT NULL`).
3. **Episode vector search** — independently searches the `Episode` HNSW index
   to catch manually stored notes that have no extracted entities.

Results are ranked: `confirmed` entries before `observed`, then by
confidence/similarity. Top `k` entries from each phase are returned.

`format_for_prompt()` formats the result as a multi-line text block (prompt
injection-sanitised via `_sanitize_prompt_field`), which is injected into the
system prompt before each turn.

---

## Context Compaction Algorithm

**File:** `context_manager.py` → `maybe_compact()`

The ReAct loop calls `maybe_compact()` before each LLM call. If the estimated
token count of the current message list exceeds **85 % of `ctx_max_tokens`**, the
algorithm fires:

### Short-history path (≤ 3 messages)

If there are 3 or fewer messages but they already exceed budget (e.g. a single
huge tool result), no summary is possible. Instead:

1. Cap every message's string content to `_RECENT_HEAD + _RECENT_TAIL` chars
   (3000 + 3000 = 6000 chars default) using a head+tail truncation.
2. Halve the cap repeatedly down to `_SHRINK_FLOOR` (400 chars) until within budget.
3. Log a warning and return the capped list.

### Normal compaction path (> 3 messages)

1. **Split:** `first = messages[:1]` (user goal, always kept verbatim),
   `middle = messages[1:-2]` (history to compress), `last = messages[-2:]`
   (two most recent, kept verbatim).

2. **Render middle for LLM:** each message is formatted with
   `_format_middle_message()` — assistant messages are head-capped at 800 chars
   (intent is at the start), tool-result messages keep 1200 chars head + 1200
   chars tail so both the command header and trailing errors survive.

3. **LLM summary call:** the middle text is sent to `llm.chat()` asking for a
   bullet-point summary preserving tool names, key outputs, errors, and
   decisions. If the LLM call fails or returns empty, a **deterministic fallback
   summary** is produced via `_fallback_summary()` — a head+tail truncation of
   the rendered middle text capped at `_SUMMARY_CAP` (6000 chars).

4. **Assemble:** `first` + compaction-summary message + `last`, with the summary
   capped at 6000 chars and the recent/goal messages capped at 6000 chars each.

5. **Budget-tightening loop:** if the post-compaction token count still exceeds
   the 85 % threshold (e.g. a huge goal or enormous recent tool result), the
   algorithm halves `summary_cap`, `recent_cap`, and `first_cap` in lockstep
   until within budget or all reach `_SHRINK_FLOOR` (400 chars). This guarantees
   the model context is never over-budget after compaction.

### Token Estimation

**File:** `token_estimator.py`

Estimation never uses a hard-coded divisor. Instead:

- Text is classified as `cjk`, `code`, or `prose` based on character density
  (CJK fraction ≥ 10 % → CJK; code-indicator density ≥ 12 % → code; else prose).
- Heuristic ratios: prose = 3.6 chars/token, code = 2.8, CJK = 1.0 token/char.
- A per-message framing overhead of 4 tokens is added.
- Images are charged 1000 tokens each (even missing/unreadable paths).
- If the active model is a resolvable OpenAI-compatible name, `tiktoken` is used
  for exact counts; all other providers (Ollama, Google, Anthropic, unknown) fall
  back to the heuristic.
- Estimation never raises — all errors are caught and fall back to the heuristic.

---

## The `/reset` Command

**Source:** `telegram_commands.py` → `cmd_reset()` →
`AgentController.reset_task(save=True|False)`

**Usage:**
```
/reset           — save active task to ResultsMemory, then clear context
/reset discard   — clear context without saving
```

### What `/reset` does, step by step

1. **Save phase** (skipped with `discard`):
   - If `WorkingMemory.has_content()` (there is an active task):
     - Formats the current goal + step log via `to_summary_text()`.
     - Calls the LLM: `"Summarize this task concisely in 2-3 sentences:\n\n..."`.
     - On LLM failure, falls back to the first 300 chars of the task text.
     - Writes the summary into `ResultsMemory.add_result(goal, summary,
       tools_used)` so the result is searchable in future sessions.

2. **Clear phase** (always):
   - `WorkingMemory.clear()` — wipes goal, steps, and `started_at`.
   - `ShortTermMemory.clear()` — empties the conversation ring buffer; the
     next turn starts with a blank chat history.
   - `ConfirmationState.clear_auto_approve()` — clears any tools that were
     auto-approved during the just-reset session.

3. **What is NOT cleared by `/reset`:**
   - `MemoryStore` (`data/memory.json`) — persistent operator facts are untouched.
   - `ResultsMemory` — past results accumulate indefinitely.
   - `GraphMemoryStore` — the knowledge graph is never cleared by `/reset`; it
     accumulates across all sessions.
   - `LongTermMemory` — legacy backfill-only shim, not used at runtime.

4. **Confirmation auto-approvals** are cleared so the operator is re-prompted
   for tool confirmations on the next task.

### Context compress (`/compress` — hidden/advanced)

`AgentController.compress_context()` is a lighter alternative to `/reset`:
it replaces the `ShortTermMemory` buffer **in place** with a single LLM-generated
bullet-point summary, preserving key facts and decisions. Working memory is
**not** cleared and nothing is written to `ResultsMemory`. Use this when you want
to shrink the context window without losing the current task.

`/compress` is a **hidden command** — it is not shown in Telegram's command
menu or in `/help`. Normal users do not need it: automatic context compaction
fires at 85% of `ctx_max_tokens` without any manual trigger. For task lifecycle
management, use `/reset` (save and clear) or `/reset discard` (clear without saving).
`/compress` remains available as an advanced escape hatch by typing it directly.

If the LLM summarization call fails, `/compress` falls back to a **deterministic
head+tail truncation** of the assembled history (rather than leaving the buffer
untouched), so an over-budget context is still reduced. The replacement message
is marked as a deterministic fallback.

---

## Data Files Summary

| File / Path | Class | Created by | Cleared by |
|---|---|---|---|
| `data/memory.json` | `MemoryStore` | First run; `memory_write` tool | Never (operator-managed) |
| `data/results_memory.json` | `ResultsMemory` | Task completion / `/reset` | Never (accumulates) |
| `data/graph_memory` | `GraphMemoryStore` | `[graph_memory] enabled = true` | Never (accumulates) |
| `data/job_contexts/<key>.json` | `ShortTermMemory` | Sub-agent on exit | Next sub-agent run or manual delete |
| In-memory only | `ShortTermMemory` (main) | Start of process | `/reset` |
| In-memory only | `WorkingMemory` | Each task start | Task finish or `/reset` |

---

## Configuration Reference (`config.toml`)

```toml
[paths]
memory_file            = "data/memory.json"
results_memory_file    = "data/results_memory.json"

[graph_memory]
enabled                = false          # opt-in
db_path                = "data/graph_memory"
buffer_pool_mb         = 256
extraction_model       = ""             # defaults to agent.default_model
extract_every_n_turns  = 3              # batch N turns before extracting
min_message_length     = 100            # skip short messages
max_context_entries    = 10             # max items injected into system prompt
```
