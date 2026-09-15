# Delta Spec: telegram-progress-panel

## MODIFIED Requirements

### Requirement: Tool-call brief on Running line

The progress panel SHALL display a short per-tool brief on the Running line so the operator can see what the tool is doing, not just the tool name. The brief SHALL be extracted from the tool's arguments by a per-tool formatter (`fmt_tool_brief`) that selects the semantically meaningful argument for each tool. The brief SHALL be truncated to approximately 35 characters with an ellipsis suffix when it exceeds that length. The `schedule` formatter SHALL render `modify` and `show` calls in the same `action + tag` style as the existing schedule actions.

Feature: Telegram progress panel
Rule: Every tool call shows a one-line brief of what it's doing, not just the tool name.

#### Scenario: Shell command shows stripped command text
- **GIVEN** the agent calls the `shell` tool with `command = "sh -c \"python3 -c 'import json, sys; ...'\"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows the shell wrapper stripped (`sh -c` removed) and the inner command truncated to ~35 chars
- **AND** the line does not show the raw `sh -c` wrapper

#### Scenario: file_read shows basename
- **GIVEN** the agent calls `file_read` with `path = "/home/paul/projects/foo/config.yaml"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `file_read config.yaml`
- **AND** the line does not show the full directory path

#### Scenario: file_patch shows basename and line counts
- **GIVEN** the agent calls `file_patch` with `path = "app.py"`, `old_str` containing 3 lines, and `new_str` containing 12 lines
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `file_patch app.py +12 -3`
- **AND** the line does not show the patch content

#### Scenario: file_diff shows both basenames with arrow
- **GIVEN** the agent calls `file_diff` with `path_a = "src/app.py"` and `path_b = "tests/app.py"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `file_diff app.py ↔ app.py`
- **AND** the line shows both file basenames connected by `↔`
- **AND** the full directory paths are not shown

#### Scenario: file_write shows basename and content length
- **GIVEN** the agent calls `file_write` with `path = "app.py"` and `content` of 340 characters
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `file_write app.py (340)`
- **AND** the line does not show the content itself

#### Scenario: secret_get shows key only
- **GIVEN** the agent calls `secret_get` with `key = "API_KEY"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `secret_get API_KEY`
- **AND** the line does not show the secret value

#### Scenario: spawn_agent shows truncated task description
- **GIVEN** the agent calls `spawn_agent` with `task = "research React 19 breaking changes and summarize"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `spawn_agent "research React 19…`
- **AND** the task is truncated to ~30 characters

#### Scenario: wait_for_any_agent shows count when more than two
- **GIVEN** the agent calls `wait_for_any_agent` with `agent_ids = ["sa-abc", "sa-def", "sa-ghi"]`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `wait_for_any_agent [3 agents]`
- **AND** the individual agent IDs are not listed

#### Scenario: wait_for_any_agent shows IDs when two or fewer
- **GIVEN** the agent calls `wait_for_any_agent` with `agent_ids = ["sa-abc", "sa-def"]`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `wait_for_any_agent sa-abc, sa-def`

#### Scenario: schedule shows action and tag
- **GIVEN** the agent calls `schedule` with `action = "add"`, `tag = "daily-report"`, `cron = "*/8 * * * *"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `schedule add "daily-report" */8 * * * *`

#### Scenario: shell_env_set shows key only
- **GIVEN** the agent calls `shell_env_set` with `key = "GITHUB_TOKEN"` and `value = "ghp_abc123"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `shell_env_set GITHUB_TOKEN`
- **AND** the value is not displayed

#### Scenario: shell_env_list shows static label
- **GIVEN** the agent calls `shell_env_list` with no arguments
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `shell_env_list list env vars`

#### Scenario: schedule modify shows action and tag
- **GIVEN** the agent calls `schedule` with `action = "modify"`, `tag = "daily-report"`, `task = "updated report task"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `schedule modify "daily-report"`
- **AND** the task text is not displayed

#### Scenario: schedule show shows action and tag
- **GIVEN** the agent calls `schedule` with `action = "show"`, `tag = "daily-report"`
- **WHEN** the progress panel renders the Running line
- **THEN** the line shows `schedule show "daily-report"`