# Tasks: split-telegram-callbacks

## Task List

- [x] **T1** — Create `telegram_callbacks.py` with module header
  - New file at repo root alongside `telegram_commands.py`
  - Add module docstring
  - Add `from __future__ import annotations`
  - Add `TYPE_CHECKING` block importing `TelegramInterface` from `telegram_interface`
  - Copy all standard `python-telegram-bot` imports that `cb_*` handlers need (`Update`, `ContextTypes`, `ParseMode`, `InlineKeyboardMarkup`, `InlineKeyboardButton`, `html`, `logging`, etc.)
  - Add runtime import: `from telegram_commands import _apply_mode, _MODE_DESCRIPTIONS`

- [x] **T2** — Move `_ack_query` into `telegram_callbacks.py`
  - Cut `_ack_query` (line 42) from `telegram_commands.py`
  - Paste into `telegram_callbacks.py` after the imports section

- [x] **T3** — Move all 7 `cb_*` handlers into `telegram_callbacks.py`
  - Cut in order: `cb_confirm` (983), `cb_extend` (1043), `cb_tool_create` (1085), `cb_model_switch` (1126), `cb_mode_switch` (1160), `cb_deferred` (1190), `cb_subagent_confirm` (1299)
  - Paste into `telegram_callbacks.py` after `_ack_query`
  - Verify `telegram_commands.py` ends cleanly after the last `cmd_*` function

- [x] **T4** — Update imports in `telegram_interface.py`
  - Split the `from telegram_commands import (...)` block (lines 41–48) into two:
    - Keep `cmd_*` symbols in the existing `from telegram_commands import (...)` line
    - Add `from telegram_callbacks import cb_confirm, cb_extend, cb_tool_create, cb_model_switch, cb_mode_switch, cb_deferred, cb_subagent_confirm`
  - Handler registration block (lines 247–253) is unchanged

- [x] **T5** — Update test import paths
  - `tests/test_deferred_message.py`: change the six `from telegram_commands import cb_deferred` statements to `from telegram_callbacks import cb_deferred`
  - `tests/test_telegram_mode_selector.py`: split the combined `from telegram_commands import (..., _MODE_DESCRIPTIONS, cb_mode_switch)` block — keep `_MODE_DESCRIPTIONS` importing from `telegram_commands`; add `from telegram_callbacks import cb_mode_switch`

- [x] **T6** — Run lint and tests
  - `make check` (`ruff check . && vulture . vulture_whitelist.py --min-confidence 80 && pytest tests/ -v --tb=short`)
  - Result: 1134 passed, 1 skipped, 0 failed ✓

## Done Criteria

- `telegram_commands.py` contains no `cb_*` functions and no `_ack_query`
- `telegram_callbacks.py` contains exactly `_ack_query` + 7 `cb_*` handlers, imports `_apply_mode`/`_MODE_DESCRIPTIONS` from `telegram_commands` at runtime
- `make check` passes clean
