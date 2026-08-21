# Use disk-persisted checkpoints for LLM error recovery

## Status

Accepted

## Date

2026-08-21

## Context

When an LLM call fails after provider-level retries are exhausted, the ReAct loop (`react_loop.py`) exits immediately, discarding all accumulated state — the conversation history with tool results, step count, and loop metadata (`_LoopState`). The user has no way to retry without retyping the entire prompt and re-executing all prior tool calls. If the process crashes during a retry prompt, the state is permanently lost.

The existing `ConfirmationManager` pattern (`confirmation.py`) provides thread-blocking user interaction via inline buttons, and the prompt registry (`prompt_registry.py`, ADR-0014) demonstrates atomic disk writes (tmp → `os.replace`) for crash-safe persistence. The checkpoint design combines both: write `_LoopState` to disk on LLM error, block the agent thread with a retry/cancel prompt, and resume from the checkpoint on retry or `/resume`.

The checkpoint store is a new persistence layer at `data/run_checkpoints/{trace_id}.json` with a specific lifecycle contract: write-on-error only, delete-on-success-or-explicit-cancel, survive-retry-prompt-timeout, no automatic cleanup. This lifecycle is intentionally simpler than the prompt registry's dual-write archive pattern — checkpoints are ephemeral recovery units, not historical records.

## Decision

Use disk-persisted checkpoints stored at `data/run_checkpoints/{trace_id}.json` as the recovery unit for LLM error handling. Each checkpoint contains the full `_LoopState` (messages, step, goal_idx, max_steps, json_fail_streak) plus metadata (trace_id, user_goal, model, created_at, error_info). Writes are atomic (write to `.tmp` → `os.replace`), mirroring ADR-0014's crash-safety technique.

The checkpoint lifecycle is:
- **Write**: only when an LLM error occurs in `_request_turn()` (before the retry prompt)
- **Delete**: on successful run completion or when the user explicitly presses Cancel
- **Survive**: when the 120s retry-prompt timeout expires without user action (checkpoint stays for `/resume`)
- **No automatic cleanup**: no TTL, no max-files eviction, no startup purge. Stale files are small, few, and visible via `/resume` listing.

The resumed run reuses the checkpoint's stored `trace_id` for log correlation and correct checkpoint deletion on success.

## Consequences

- **Crash recovery**: checkpoints survive process crashes/restarts. The startup scan in `main.py` notifies the operator about recoverable runs, and `/resume` loads the checkpoint to resume from the saved state without re-executing prior tools.
- **No cleanup infrastructure needed**: the lifecycle contract (delete-on-success/cancel) means most checkpoints are short-lived. Stale files from abandoned retry prompts are harmless (~10-100KB each) and manageable via `/resume` listing.
- **Atomic write guarantee**: a crash during checkpoint write leaves no partial `.json` file (the `.tmp` file is garbage, the `.json` is either the previous version or absent). This is the same guarantee ADR-0014 provides for the prompt registry.
- **New storage directory**: `data/run_checkpoints/` is created on first use. No data migration is needed — the directory is new and empty. The `data/` path is resolved relative to the agent's XDG data directory (per ADR-0019), consistent with existing `data/` stores (`data/tool_index.json`, `data/longterm_memory.json`, `data/prompts.jsonl`).
- **Checkpoint write failure is non-fatal**: if the disk is full or permissions prevent writing, `CheckpointStore.save()` catches `OSError`, logs a warning, and the retry prompt proceeds without a checkpoint. Inline retry still works (in-memory state); only `/resume` and crash recovery are unavailable.
- **Future features** that need to preserve run state across failures can reuse the `CheckpointStore` infrastructure. The store is a general-purpose `_LoopState` persistence layer, not specific to LLM errors.