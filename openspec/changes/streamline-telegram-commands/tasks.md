## 1. Telegram Command Surface

- [ ] 1.1 Remove `cmd_health` imports and the `/health` `BotCommand` entry from `telegram_interface.py`.
- [ ] 1.2 Remove the `/health` `CommandHandler` registration so Telegram no longer accepts it as a slash command.
- [ ] 1.3 Remove the now-unused `cmd_health` function from `telegram_commands.py`.
- [ ] 1.4 Remove `/compress` from Telegram `BotCommand` registration while keeping its `CommandHandler` registration intact.
- [ ] 1.5 Remove `/compress` from `cmd_help()` output while keeping `/status` and `/reset` visible.

## 2. Documentation Updates

- [ ] 2.1 Update README feature bullets to remove `/health` as a named command and describe health diagnosis as natural-language or scheduled-job driven.
- [ ] 2.2 Update the README Telegram command table to remove `/health` and `/compress` from the primary public command list.
- [ ] 2.3 Update README context-compaction wording so automatic compaction is the normal mechanism and `/compress` is not presented as a public command.
- [ ] 2.4 Update `MEMORY.md` context-command documentation to clarify that `/compress` is hidden/advanced while `/reset` remains public.

## 3. Tests and Verification

- [ ] 3.1 Add or update tests asserting Telegram command discovery omits `health` and `compress` while keeping `reset`.
- [ ] 3.2 Add or update tests/assertions showing `/compress` remains registered as a handler if command-handler coverage exists.
- [ ] 3.3 Search the repository for stale `/health` public-command references and remove or reword them.
- [ ] 3.4 Run `ruff check .`.
- [ ] 3.5 Run `vulture . vulture_whitelist.py --min-confidence 80`.
