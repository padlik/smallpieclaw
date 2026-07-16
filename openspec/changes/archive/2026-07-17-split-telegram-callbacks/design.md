# Design: split-telegram-callbacks

## Approach

Mechanical module split. Move all 7 `cb_*` handler functions, the `_ack_query` helper (callback-only), into a new `telegram_callbacks.py`. No logic changes. Update consumers.

## Module Boundaries

### `telegram_commands.py` (after)
Retains:
- Shared helpers used by both `cmd_*` and `cb_*`: `_apply_mode`, `_MODE_DESCRIPTIONS`
- Helpers used exclusively by `cmd_*`: `_require_auth`, `_truncate_desc`, `_redact_env_var`, `_tool_entry`, `_fmt_stat`
- All `cmd_*` handler functions (lines 87–952, unchanged)

Removes:
- `_ack_query` (callback-only → moves to `telegram_callbacks.py`)
- All 7 `cb_*` handler functions → move to `telegram_callbacks.py`

### `telegram_callbacks.py` (new)
Contains:
- `_ack_query` helper (callback-only, all 10 uses are inside `cb_*` bodies)
- `cb_confirm`, `cb_extend`, `cb_tool_create`, `cb_model_switch`, `cb_mode_switch`, `cb_deferred`, `cb_subagent_confirm`

Runtime imports from `telegram_commands`:
- `_apply_mode` — called by `cb_mode_switch` (line 1177); also used by `cmd_*` → stays in `telegram_commands`, imported here
- `_MODE_DESCRIPTIONS` — read by `cb_mode_switch` (lines 1174, 1180); same rationale

Own imports (declared in `telegram_callbacks.py` itself, same as `telegram_commands.py` already does):
- Standard: `html`, `logging`, `asyncio`, etc. as needed
- PTB: `Update`, `ContextTypes`, `ParseMode`, `InlineKeyboardMarkup`, etc. as needed
- Project: `TelegramInterface` via `TYPE_CHECKING` guard (same pattern as `telegram_commands.py`) to avoid a runtime circular import

**Dependency direction:** `telegram_callbacks` → `telegram_commands` (one-way). No circular import: `telegram_commands.py` does not import from `telegram_callbacks.py`.

## Consumer Updates

### `telegram_interface.py`
The existing single `from telegram_commands import (...)` block at lines 46–47 splits into two:
```python
from telegram_commands import (cmd_start, cmd_help, ...)
from telegram_callbacks import (cb_confirm, cb_extend, ...)
```
The handler registration block (lines 247–253) is unchanged — symbol names are identical.

### Tests
- `tests/test_deferred_message.py` — patches/imports `telegram_commands.cb_deferred`; update to `telegram_callbacks.cb_deferred`
- `tests/test_telegram_mode_selector.py` — imports `telegram_commands.cb_mode_switch`; update to `telegram_callbacks.cb_mode_switch`. The module-level `_apply_mode`/`_MODE_DESCRIPTIONS` used by the mode selector remain in `telegram_commands`, so any test assertions on those symbols continue targeting `telegram_commands`.

## Lint Gate

After the split, run `ruff check . && vulture . vulture_whitelist.py --min-confidence 80` to confirm:
- No unused private symbols in either module
- No broken re-exports

## Non-Decisions

- No `__init__.py` changes needed (flat module layout).
- No `vulture_whitelist.py` updates expected (no new public API symbols).
- No scheduler, MCP, or sub-agent code is affected.
