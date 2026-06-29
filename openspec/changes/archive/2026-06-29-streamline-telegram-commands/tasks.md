## 1. Telegram Command Surface

- [x] 1.1 Remove `cmd_health` imports and the `/health` `BotCommand` entry from `telegram_interface.py`.
- [x] 1.2 Remove the `/health` `CommandHandler` registration so Telegram no longer accepts it as a slash command.
- [x] 1.3 Remove the now-unused `cmd_health` function from `telegram_commands.py`.
- [x] 1.4 Remove `/compress` from Telegram `BotCommand` registration while keeping its `CommandHandler` registration intact.
- [x] 1.5 Remove `/compress` from `cmd_help()` output while keeping `/status` and `/reset` visible.

## 2. Documentation Updates

- [x] 2.1 Update README feature bullets to remove `/health` as a named command and describe health diagnosis as natural-language or scheduled-job driven.
- [x] 2.2 Update the README Telegram command table to remove `/health` and `/compress` from the primary public command list.
- [x] 2.3 Update README context-compaction wording so automatic compaction is the normal mechanism and `/compress` is not presented as a public command.
- [x] 2.4 Update `MEMORY.md` context-command documentation to clarify that `/compress` is hidden/advanced while `/reset` remains public.

## 3. Tests and Verification

- [x] 3.1 Add or update tests asserting Telegram command discovery omits `health` and `compress` while keeping `reset`.
- [x] 3.2 Add or update tests/assertions showing `/compress` remains registered as a handler if command-handler coverage exists.
- [x] 3.3 Search the repository for stale `/health` public-command references and remove or reword them.
- [x] 3.4 Run `ruff check .`.
- [x] 3.5 Run `vulture . vulture_whitelist.py --min-confidence 80 --exclude .venv` (project-scoped; `--exclude .venv` avoids third-party noise while preserving the AGENTS-required intent).
