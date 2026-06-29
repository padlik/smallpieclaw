## Context

Telegram commands are registered in `TelegramInterface._post_init()` for Telegram's command menu and in `TelegramInterface._register_handlers()` for actual command handling. User-facing help text is maintained separately in `cmd_help()`, while broader command documentation appears in `README.md` and `MEMORY.md`.

Current behavior separates three related but different operations:

- `/status` returns deterministic operational state directly from the interface and registries.
- `/health` launches an agent task that analyzes `agent.log`; this capability can also be requested in natural language or scheduled via `scheduler.toml`.
- `/compress` rewrites `ShortTermMemory` into one summary, while automatic compaction already protects ReAct model calls at 85% of `ctx_max_tokens`.

## Goals / Non-Goals

**Goals:**

- Remove `/health` as an accepted Telegram slash command.
- Remove `/health` from Telegram command discovery, `/help`, and user-facing documentation.
- Keep log/health diagnosis available through natural-language requests and scheduled jobs.
- Hide `/compress` from Telegram command discovery, `/help`, and primary documentation while preserving the handler for advanced manual use.
- Keep `/reset` visible and documented as the supported task-context lifecycle command.

**Non-Goals:**

- Do not remove automatic context compaction.
- Do not remove `AgentController.compress_context()` or the `/compress` handler in this change.
- Do not change `/status` output semantics beyond documentation if needed.
- Do not change scheduler behavior or the example scheduled health job except documentation wording if necessary.

## Decisions

### Remove `/health` at registration and handler import points

`/health` should be completely cut off as a slash command by removing its `BotCommand` entry and `CommandHandler` registration. The `cmd_health()` function should also be removed if no longer referenced.

Alternative considered: keep `/health` hidden like `/compress`. Rejected because the requested direction is to cut it off completely, and natural-language log analysis already covers the same capability without preserving another alias.

### Preserve natural-language health diagnosis

No replacement command is needed. Users can ask the agent to inspect logs or check system health, and `scheduler.toml.example` can continue showing the recurring health-diagnosis task pattern.

Alternative considered: add a `/diagnose` or `/logs` command. Rejected because it would preserve command-surface clutter under a new name.

### Hide `/compress`, do not remove it yet

`/compress` should be removed from `BotCommand` registration, `/help`, and primary README command tables, but its handler should remain registered. This keeps an expert escape hatch while signaling that normal users should rely on automatic compaction and `/reset`.

Alternative considered: remove `/compress` entirely. Rejected for this change because manual compression has one distinct behavior that autocompress does not guarantee: it permanently rewrites the cross-task `ShortTermMemory` buffer rather than only compacting the current ReAct message list.

### Documentation should describe the public/hidden split

Documentation should avoid presenting hidden commands as normal command-menu choices. It may mention `/compress` only as an advanced/manual context tool, if useful, and must describe automatic compaction as the normal protection mechanism.

## Risks / Trade-offs

- Users who still type `/health` will receive Telegram's unknown-command behavior instead of the old diagnosis task. → Mitigate by documenting natural-language health diagnosis and ensuring `/status` remains visible.
- Hidden `/compress` may become forgotten behavior. → Mitigate by keeping implementation/tests clear and treating future removal as a separate decision if it remains unused.
- Docs and command registration can drift because command menu, help text, and README are maintained separately. → Mitigate by updating all command-surface locations in one task and adding/adjusting tests if existing coverage expects old entries.
