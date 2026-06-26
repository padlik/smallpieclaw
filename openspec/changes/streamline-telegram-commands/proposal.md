## Why

The Telegram command surface has accumulated overlapping and low-value commands, making the bot harder to operate and document. `/health` duplicates a natural-language diagnostic workflow while `/status` already covers quick operational state, and `/compress` is now mostly an advanced escape hatch because automatic compaction protects model calls near the context limit.

## What Changes

- Remove `/health` completely from the Telegram command surface and documentation.
- Preserve health diagnosis as a natural-language capability and scheduled-job pattern rather than a dedicated slash command.
- Hide `/compress` from user-facing command discovery and help text while keeping the handler available for advanced/manual use.
- Keep `/reset` visible because it has distinct task-lifecycle semantics: save/discard current task context and start fresh.
- Update documentation so `/status`, `/reset`, automatic context compaction, and hidden `/compress` behavior are described accurately.

## Capabilities

### New Capabilities
- `telegram-command-surface`: Defines the supported user-visible Telegram slash command surface and behavior for hidden/advanced commands.

### Modified Capabilities

None.

## Impact

- Affected code: `telegram_interface.py`, `telegram_commands.py`, `README.md`, and potentially command-related tests.
- User-facing API: `/health` will stop being accepted as a slash command. Users can still ask the agent in natural language to analyze logs or check health.
- Documentation: command list, feature bullets, and context-compaction guidance need to reflect the new public/hidden command split.
- Dependencies: no new runtime dependencies.
