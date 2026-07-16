# Proposal: split-telegram-callbacks

## Why

`telegram_commands.py` has grown to 1 364 lines, violating the project's "avoid large files" guideline. The final ~380 lines are inline-button callback handlers (`cb_*`) that belong to a different lifecycle than slash-command handlers (`cmd_*`): commands are user-initiated, callbacks are reactions to bot-posted keyboards. Mixing them makes the file hard to navigate and the two groups harder to reason about independently.

## What Changes

- Extract all `cb_*` handler functions from `telegram_commands.py` into a new `telegram_callbacks.py` module (no logic changes).
- Update `telegram_interface.py` to import `cb_*` symbols from `telegram_callbacks` instead of `telegram_commands`.
- Command-only helper functions (`_require_auth`, `_truncate_desc`, `_redact_env_var`, `_tool_entry`, `_fmt_stat`) remain in `telegram_commands.py`.
- `_ack_query` is callback-only and also moves to `telegram_callbacks.py`.

Functions moving to `telegram_callbacks.py`:
- `cb_confirm` (line 983)
- `cb_extend` (line 1043)
- `cb_tool_create` (line 1085)
- `cb_model_switch` (line 1126)
- `cb_mode_switch` (line 1160)
- `cb_deferred` (line 1190)
- `cb_subagent_confirm` (line 1299)

## Capabilities

### New Capabilities

_(none — pure refactor, no behaviour introduced)_

### Modified Capabilities

_(none — no spec-level behaviour changes)_

## Impact

- **`telegram_commands.py`**: loses ~380 lines; `cb_*` symbols removed.
- **`telegram_callbacks.py`**: new module; imports `TelegramInterface` type hint and shared helpers from `telegram_commands`.
- **`telegram_interface.py`**: import line updated to pull `cb_*` from `telegram_callbacks`.
- **Tests**: any test that patches or imports `telegram_commands.cb_*` must be updated to target `telegram_callbacks.cb_*`.
- **No public API or runtime behaviour change.**
