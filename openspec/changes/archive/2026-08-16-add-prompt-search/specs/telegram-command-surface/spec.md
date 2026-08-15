## MODIFIED Requirements

### Requirement: /prompts command is registered and discoverable

The `/prompts` command SHALL be registered as a Telegram slash command and listed in help text. The command accepts three forms: bare `/prompts` (list recent), `/prompts search <query> [Nd/Nh] [--status=<S>] [--trace=<T>] [--since=<ISO>] [--until=<ISO>] [--page=<N>]` (text search with optional filters), and `/prompts show <id>` (display a single prompt's full record). The content contract for the list view is owned by the `prompt-tracking` capability; the search and show content contracts are also owned by `prompt-tracking`.

Feature: Telegram command surface
Rule: This requirement governs discovery and surface only; the list's fields, search behavior, and show display are specified in `prompt-tracking`.

#### Scenario: /prompts is registered as a command
- **WHEN** the Telegram interface registers bot commands during startup
- **THEN** the registered command list includes `/prompts`

#### Scenario: /prompts appears in help
- **WHEN** an authorized user requests `/help`
- **THEN** the help text includes `/prompts`

#### Scenario: /prompts help mentions search and show subcommands
- **WHEN** an authorized user requests `/help`
- **THEN** the help text mentions `/prompts search <query> [Nd/Nh]` for searching prompt history
- **AND** the help text mentions the filter flags `--status`, `--trace`, `--since`, `--until`, and `--page`
- **AND** the help text mentions `/prompts show <id>` for viewing a single prompt's details