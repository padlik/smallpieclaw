# Agent XDG Launch Specification

## Purpose

Define `--agent-name` as the required CLI argument for agent launch and the XDG startup flow in `main.py`.

## Requirements

### Requirement: --agent-name is a required CLI argument

The application MUST require `--agent-name` at launch with no default value. Omitting it is a startup error caught by `argparse` before any agent logic runs. The `_AGENT_DIR` module-level constant is removed entirely.

Feature: Required agent name argument
Rule: `--agent-name` is declared `required=True` with no default; omitting it causes `argparse` to exit with a non-zero code before XDG paths are resolved.

#### Scenario: Agent launched with --agent-name
- **GIVEN** the agent is invoked with `--agent-name piclaw`
- **WHEN** `main.py` starts
- **THEN** XDG paths are resolved for agent name `"piclaw"` via `xdg_paths("piclaw")`
- **AND** all directories and files used by the agent derive from those resolved paths

#### Scenario: --agent-name omitted — startup fails with error
- **GIVEN** the agent is invoked without `--agent-name`
- **WHEN** `main.py` starts
- **THEN** the process exits with a non-zero exit code
- **AND** the error message indicates that `--agent-name` is required

### Requirement: XDG directory creation at every startup

`_create_xdg_dirs(paths)` MUST be called on every startup and MUST be idempotent. It creates `config_home`, `data_home`, `state_home`, `cache_home`, `logs_dir`, `skills_dir`, and `runtime_dir` using `exist_ok=True`. All directories except `runtime_dir` use `parents=True`. `runtime_dir` uses `parents=False` because its parent (`$XDG_RUNTIME_DIR`) is owned by systemd-logind and must not be created by the agent.

Feature: XDG directory creation
Rule: All XDG dirs use `exist_ok=True`; all except `runtime_dir` use `parents=True`; `runtime_dir` uses `parents=False` so creation fails if the systemd-owned parent does not exist.

#### Scenario: First launch creates all XDG directories
- **GIVEN** the agent is invoked with `--agent-name piclaw`
- **AND** no XDG directories exist for agent `"piclaw"`
- **AND** `XDG_RUNTIME_DIR` is set to `/run/user/1000` and `/run/user/1000` already exists
- **WHEN** `main.py` executes `_create_xdg_dirs(paths)`
- **THEN** `config_home`, `data_home`, `state_home`, `cache_home`, `logs_dir`, `skills_dir`, and `runtime_dir` are all created
- **AND** no error is raised

#### Scenario: Second launch is idempotent — existing dirs cause no error
- **GIVEN** the agent is invoked with `--agent-name piclaw`
- **AND** all XDG directories for `"piclaw"` already exist from a previous launch
- **WHEN** `main.py` executes `_create_xdg_dirs(paths)`
- **THEN** startup completes without error
- **AND** no existing directories are removed or modified

#### Scenario: runtime_dir parent does not exist — startup fails
- **GIVEN** `XDG_RUNTIME_DIR` is set to `/run/user/9999`
- **AND** `/run/user/9999` does not exist on the filesystem
- **WHEN** `main.py` executes `_create_xdg_dirs(paths)`
- **THEN** a `FileNotFoundError` (or equivalent OS error) is raised because `parents=False` for `runtime_dir`
- **AND** the agent does not proceed to load config

### Requirement: Config existence check with helpful error message

After XDG directories are created, `main.py` MUST verify that `paths.config_file` exists. If it does not, the process exits immediately with a message containing the full expected path so the user knows exactly where to create the file.

Feature: Config existence gate
Rule: If `paths.config_file` does not exist after `_create_xdg_dirs`, `sys.exit` is called with a message containing the expected config file path.

#### Scenario: Config absent — exit message shows expected path
- **GIVEN** the agent is invoked with `--agent-name piclaw`
- **AND** all XDG directories are successfully created
- **AND** `paths.config_file` (`~/.config/piclaw/config.toml`) does not exist
- **WHEN** `main.py` reaches the config existence check
- **THEN** the process exits with a non-zero exit code
- **AND** the exit message contains the full path to `paths.config_file`

### Requirement: Relative path warning at startup

`_warn_relative_paths(cfg)` MUST scan all string values in the loaded config and warn on any value that starts with `.`. Values starting with `~` or `/` are valid and MUST NOT trigger a warning.

Feature: Relative path validation
Rule: String config values starting with `.` trigger a startup warning; values starting with `~` or `/` are silently accepted.

#### Scenario: Config contains a dot-relative path — warning logged
- **GIVEN** the agent is invoked with `--agent-name piclaw`
- **AND** `paths.config_file` exists and contains a value such as `workspace_dir = "./projects"`
- **WHEN** `main.py` calls `_warn_relative_paths(cfg)`
- **THEN** a warning is logged indicating the relative path may not resolve as expected
- **AND** the agent continues to start (the warning is non-fatal)
- **AND** a value like `workspace_dir = "~/Documents"` in the same config does not trigger a warning

### Requirement: downloads_dir derived from workspace_dir at startup

`downloads_dir` is no longer a `[paths]` config field. It is derived at startup as `Path(cfg["paths"].get("workspace_dir", "~/Documents")).expanduser() / "downloads"`. `workspace_dir` is the one remaining user-configurable path in `[paths]`; all other path derivation is handled by `xdg.py`.

Feature: downloads_dir derivation
Rule: `downloads_dir` is always `<workspace_dir>/downloads/`; it is not configurable directly and does not appear as a `[paths]` field.

#### Scenario: Default downloads_dir derives from workspace_dir default
- **GIVEN** `[paths]` does not set `workspace_dir`
- **WHEN** the application starts
- **THEN** `downloads_dir` resolves to `~/Documents/downloads/`
- **AND** `downloads_dir` is not a recognised `[paths]` config field

#### Scenario: Custom workspace_dir changes the downloads_dir location
- **GIVEN** `[paths]` sets `workspace_dir = "/data/workspace"`
- **WHEN** the application starts
- **THEN** `downloads_dir` resolves to `/data/workspace/downloads/`
