## Context

The `PromptRegistry` (`prompt_registry.py`) assigns a monotonic integer `prompt_id` to each user-initiated agent run, persisted append-only to `data/prompts.jsonl`. The ID is the operator-facing handle ("Prompt #N") shown in Telegram and used in `log_query` filtering. The `trace_id` (`r-<8 hex>`, from `trace_context.py`) is the high-cardinality join key for logs but is an implementation detail the operator should not need to know.

Three problems motivate this change:

1. **Non-global uniqueness.** The integer counter resets if `prompts.jsonl` is deleted, and a prompt running across midnight writes log records into two different daily `agent.jsonl` files. The same `prompt_id` can refer to different runs depending on log day or registry state.
2. **Unrecognizable `/prompts`.** The command shows only `Prompt #N`, status, elapsed, and sub-agent count — no text, no date. The operator cannot identify which prompt is which.
3. **Operator wants a single ID.** "Summarize actions for prompt 42" must work without the operator knowing `trace_id`. The prompt ID must be the single operator-facing handle and must be globally unique.

In-force ADRs relevant to this design: ADR-0004 (structured logging via structlog — `prompt_id` is an observability field in `agent.jsonl`), ADR-0011 (per-prompt approval scope — `_current_prompt_id` keys the approval set). Neither constrains the ID *format*; both treat `prompt_id` as an opaque token passed through.

### Current data flow (C4 component view, ASCII)

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent Process (container)                 │
│                                                              │
│  TelegramInterface                                          │
│    │ start(trace_id, text)                                   │
│    ▼                                                         │
│  PromptRegistry ──append──▶ data/prompts.jsonl (JSONL)       │
│    │ PromptRecord{prompt_id:int, trace_id, text, ...}       │
│    │                                                         │
│    │ prompt_id (int)                                        │
│    ▼                                                         │
│  AgentController.run(task, prompt_id)                        │
│    │ bind_run_context(prompt_id=str(int))                    │
│    │   └─▶ agent.jsonl  (prompt_id field per record)         │
│    │ builtin_executor._current_prompt_id = prompt_id        │
│    ▼                                                         │
│  SubAgentSupervisor ──prompt_id──▶ SubAgentRecord            │
│    │                                                         │
│  log_query tool: filter agent.jsonl by prompt_id field       │
│  /prompts cmd: render Prompt #N, status, elapsed, sub-count │
└─────────────────────────────────────────────────────────────┘
```

## Goals / Non-Goals

**Goals:**
- Make `prompt_id` globally unique and stable forever (across restarts, registry resets, day boundaries) so the operator can reference any historical prompt by one ID.
- Make `/prompts` recognizable: show prompt ID, start timestamp, truncated text, status, elapsed, sub-agent count.
- Keep `log_query`'s `prompt_id` filter working with the new ID format.
- Tolerate legacy integer IDs in `prompts.jsonl` on replay without rewriting history.

**Non-Goals:**
- Changing `trace_id` generation or its role as the log join key.
- Making `log_query` read rotated/compressed log backups (out of scope; the ID uniqueness removes the collision risk even if rotated logs were read later).
- Rewriting or migrating existing `prompts.jsonl` records — legacy int IDs coexist until aged out.
- Adding a new runtime dependency.

## Decisions

### Decision 1: ULID-format string IDs, generated inline (no new dependency)

**Choice:** Replace `int` `prompt_id` with a 26-char ULID string (Crockford base32: 48-bit millisecond timestamp + 80-bit random), generated inline.

**Rationale:**
- Globally unique forever — 80 bits of entropy makes collision effectively impossible.
- Time-sortable lexicographically — ULIDs sort newest-last, so a reverse sort gives newest-first. Note: `list_recent` currently sorts by `prompt_id` keys (`sorted(self._records.keys(), reverse=True)`); this change **requires** `list_recent` to sort by `started_at` descending instead, because legacy int IDs and new ULID str IDs cannot be compared (mixed-type sort raises `TypeError`).
- Short enough for Telegram display and operator copy-paste (26 chars vs 36-char UUID).
- No new dependency — the generator is ~15 lines (6 timestamp bytes + `secrets.token_bytes(10)` random bytes = 16 bytes, Crockford base32 encoded to 26 chars). Avoids the install-approval gate.

**Alternatives considered:**
- *Full UUID (36 chars)* — rejected: too long/ugly for Telegram, not time-sortable.
- *Date-prefixed sequential (`2025-07-23-0001`)* — rejected: still collides on registry reset within a day; date prefix redundant once timestamp is shown in `/prompts`.
- *`ulid-py` library* — rejected: adds a dependency for a trivial generator; install approval required.
- *Short random hex (8 chars)* — rejected: 32-bit entropy risks collision at scale; not time-sortable.

### Decision 2: Full ULID displayed, no truncation

**Choice:** `/prompts` and log references show the full 26-char ULID. No truncation.

**Rationale:** Truncation risks ambiguity (two prompts sharing a prefix). The operator copy-pastes the ID for `log_query`; a truncated ID would not match. Length is acceptable alongside the new text + timestamp context.

### Decision 3: `/prompts` adds timestamp + text

**Choice:** Each `/prompts` entry shows: `<ULID>` · `<start timestamp>` · `<status icon>` · `<truncated text (first ~80 chars)>` · `<elapsed>` · `<sub-agent count>`.

**Rationale:** The ID alone was never a recognition cue. Text + timestamp lets the operator identify prompts regardless of ID format. `text` is already stored truncated to 200 chars in `PromptRecord`; the display truncates further to ~80 for line length.

### Decision 4: Legacy int IDs tolerated on replay

**Choice:** `_replay()` accepts both int and str `prompt_id` values from `prompts.jsonl`. Int IDs are kept as-is in memory (their `str()` form is used for display). New records always get ULIDs. The `_next_id` counter is removed.

**Rationale:** No history rewrite; no migration step. Old records age out naturally. A mixed list is temporarily uglier but correct.

### Decision 5: `prompt_id` type change is a breaking, atomic update

**Choice:** All `prompt_id` fields/params change `Optional[int]` → `Optional[str]` in one atomic change. `bind_run_context` already accepts `str` (it stringifies), so the log path needs no logic change — only the callers stop passing `str(int)` and pass the ULID directly.

**Rationale:** A gradual migration would leave the type ambiguous and error-prone. The blast radius is bounded (~8 source files, ~5 test files) and the change is mechanical (type annotations + display format).

## Risks / Trade-offs

- **[Risk] Mixed int/str IDs in `list_recent` after replay** → Mitigation: `list_recent` is changed to sort by `started_at` descending (not by `prompt_id` keys), so mixed int/str IDs never reach a comparison. This is a **required code change**, not pre-existing behavior — the current implementation sorts by keys and would `TypeError` on mixed types.
- **[Risk] ULID generator bug produces duplicates** → Mitigation: 80-bit random from `secrets.token_bytes(10)` (cryptographic RNG); collision probability is negligible. No uniqueness check needed.
- **[Risk] Operator muscle memory expects "Prompt #42"** → Mitigation: the new format "Prompt `01J...`" is different but unambiguous; the text + timestamp in `/prompts` compensates. Breaking change is intentional.
- **[Risk] Tests break broadly** → Mitigation: expected; tests are updated in the same change. Type change is mechanical.

## Migration Plan

1. Deploy the code change (ULID generator + type changes + `/prompts` rendering).
2. On startup, `_replay()` reads existing `prompts.jsonl` — legacy int IDs load as-is; new prompts get ULIDs.
3. No `prompts.jsonl` rewrite or migration script needed.
4. Rollback: revert the code; legacy int IDs in `prompts.jsonl` are still valid. New ULID records in the file are silently skipped by the old replay logic (`prompt_registry.py:84` guards `if not isinstance(prompt_id, int): continue`) — they become invisible until aged out, but no crash occurs. If full visibility of post-rollback prompts is needed, delete ULID records from `prompts.jsonl` or restore from backup.

## Open Questions

- None remaining. All four open questions from the explore-brief are resolved (inline generator, legacy tolerance, `_next_id` removal, full display).
- No in-force ADR needs supersession. ADR-0004 and ADR-0011 treat `prompt_id` as an opaque token; the format change is compatible. A new ADR will record the ULID decision (produced in the `adr` step).