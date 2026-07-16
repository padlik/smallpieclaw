# ADR: Extract telegram_callbacks.py from telegram_commands.py

## Status
Proposed

## Context

`telegram_commands.py` grew to 1 364 lines by accumulating two logically distinct groups:
- `cmd_*` slash-command handlers (lines 87–952): user-initiated, one-shot request-response
- `cb_*` inline-button callback handlers (lines 983–1364): reactions to bot-posted keyboards

The project guideline says to avoid large files. The two groups also have different lifecycles, making the combined file harder to navigate.

## Decision

Extract all 7 `cb_*` handlers and the callback-only helper `_ack_query` into a new `telegram_callbacks.py` module.

Keep in `telegram_commands.py`:
- 5 command-only helpers: `_require_auth`, `_truncate_desc`, `_redact_env_var`, `_tool_entry`, `_fmt_stat`
- 2 genuinely shared symbols: `_apply_mode`, `_MODE_DESCRIPTIONS` (used by both `cmd_mode` and `cb_mode_switch`)
- All `cmd_*` handlers

`telegram_callbacks.py` imports `_apply_mode` and `_MODE_DESCRIPTIONS` from `telegram_commands` at runtime. Dependency direction is one-way (`telegram_callbacks → telegram_commands`). No circular import.

## Alternatives Considered

**Split further into per-domain cmd groups** — rejected. The shared helpers would need a third module and the domain groupings within `cmd_*` are not stable enough to justify the topology change.

**Move shared symbols to a third module** — rejected. Only two symbols are shared; a third module adds complexity disproportionate to the benefit.

## Consequences

- `telegram_commands.py` shrinks from ~1 364 to ~985 lines.
- `telegram_callbacks.py` is ~380 lines.
- `telegram_interface.py` gains a second import statement (`from telegram_callbacks import ...`).
- Two test files require import-path updates; no logic changes.
- No public API or runtime behaviour change.
