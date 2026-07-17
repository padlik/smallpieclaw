"""
config_schema.py
----------------
Typed configuration dataclasses with validation.

Usage:
    raw = tomllib.loads(config_text)
    app_config = parse_config(raw)
    # Now use app_config.agent.max_iterations instead of cfg["agent"]["max_iterations"]
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

try:
    import tomli as _tomllib  # type: ignore[import]
except ImportError:
    import tomllib as _tomllib  # type: ignore[no-redef]  # Python 3.11+

from exceptions import ConfigError


# ---------------------------------------------------------------------------
# Vault-backed secret loader
# ---------------------------------------------------------------------------

def parse_vault_content(
    content: str, path: str, *, require_all_strings: bool = True
) -> dict:
    """Parse vault file *content* (TOML format) into a ``{key: value}`` dict.

    The vault is a TOML file where every top-level key maps to a string
    secret, for example::

        # ~/.local/share/<agent_name>/secrets.toml
        openai_key = "sk-..."
        bot_token  = "123456:ABC"

    When *require_all_strings* is ``True`` (the default, used for fail-fast
    startup validation) every top-level value must be a string.  Nested
    tables, arrays, integers, booleans, floats, and other non-string types
    are rejected with a :class:`ConfigError` that names the offending key.

    When *require_all_strings* is ``False`` the per-value string check is
    skipped and the parsed table is returned as-is, so a non-string sibling
    key does not break the whole file.  Callers that need a string (e.g. the
    runtime ``secret_get`` tool) must validate the specific key they read.
    TOML-format checks (parse errors, non-table top level) are always
    enforced regardless of this flag.

    Args:
        content: Raw text content of the vault file.
        path: File path used only in error messages.
        require_all_strings: When ``True`` (default), validate that every
            top-level value is a string.  When ``False``, skip that check and
            return the parsed table unmodified.

    Returns:
        A ``dict`` of vault entries.  Values are all ``str`` when
        *require_all_strings* is ``True``; otherwise they may be any TOML type.

    Raises:
        ConfigError: On TOML parse failure, on a non-table top level, or —
            when *require_all_strings* is ``True`` — if any top-level value is
            not a string.
    """
    try:
        data = _tomllib.loads(content)
    except _tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Invalid TOML in vault file at {path!r}: {exc}"
        ) from None

    if not isinstance(data, dict):
        # Defensive: TOML always produces a table at the top level.
        raise ConfigError(f"Vault file at {path!r} is not a TOML table.")
    if require_all_strings:
        for key, value in data.items():
            if not isinstance(value, str):
                raise ConfigError(
                    f"Vault file at {path!r}: value for key '{key}' must be a string, "
                    f"got {type(value).__name__}. All vault values must be strings."
                )
    return data


def _load_vault(path: str) -> dict:
    """Load a TOML vault file into a plain ``{key: value}`` dict.

    The vault is a TOML file where every top-level key maps to a string
    secret.  Non-string values (nested tables, arrays, integers, booleans,
    etc.) are rejected with a :class:`ConfigError` that names the offending
    key.

    Delegates format parsing to :func:`parse_vault_content`.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        raise ConfigError(f"Cannot read vault file at {path!r}: {exc}") from None
    return parse_vault_content(content, path)


# ---------------------------------------------------------------------------
# Environment-variable and vault-secret expansion
# ---------------------------------------------------------------------------

# Strict variable-name rule: must start with letter/underscore, then alphanumeric.
_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Prefixes that mark whole-string references.
_ENV_PREFIX = "env:"
_SEC_PREFIX = "sec:"


def _expand_value(value: Any, path: str = "", vault: dict | None = None) -> Any:
    """Recursively resolve ``env:VAR`` and ``sec:KEY`` references in raw config values.

    A string value that is **exactly** ``env:VAR`` is replaced with the value
    of environment variable ``VAR``.  A string value that is exactly ``sec:KEY``
    is replaced with the corresponding value from *vault*.

    Any other string is returned unchanged.  Non-string scalars (int, float,
    bool) are always returned unchanged.

    Rules:
    - ``env:VAR`` — whole-string reference; raises :class:`ConfigError` when
      ``VAR`` is not set in the environment.
    - ``sec:KEY`` — whole-string reference; raises :class:`ConfigError` when
      no vault is loaded, when ``KEY`` is missing, or when ``vault`` is not a
      dict.
    - ``env:`` or ``sec:`` with an empty or invalid key raises :class:`ConfigError`.
    - Strings that are not exactly ``env:VAR``/``sec:KEY`` (e.g.
      ``Bearer env:TOKEN``, ``sec:`` at start of longer text, plain values)
      are kept literal.

    The *path* argument is used only in error messages.
    """
    if isinstance(value, str):
        if value.startswith(_ENV_PREFIX):
            var = value[len(_ENV_PREFIX):]
            loc = f" (at {path})" if path else ""
            if not _ENV_VAR_NAME.match(var):
                raise ConfigError(
                    f"empty or invalid variable name in env reference '{value}' in config{loc}: "
                    f"name must match [A-Za-z_][A-Za-z0-9_]*"
                )
            resolved = os.environ.get(var)
            if resolved is None:
                raise ConfigError(
                    f"Environment variable '{var}' referenced in config{loc} is not set. "
                    f"Export it before starting: export {var}=<value>"
                )
            return resolved

        if value.startswith(_SEC_PREFIX):
            key = value[len(_SEC_PREFIX):]
            loc = f" (at {path})" if path else ""
            if vault is None:
                raise ConfigError(
                    f"Vault secret '{value}' referenced in config{loc} but no vault loaded. "
                    f"Set a vault path or add the secret to the config."
                )
            if not _ENV_VAR_NAME.match(key):
                raise ConfigError(
                    f"empty or invalid key name in sec reference '{value}' in config{loc}: "
                    f"name must match [A-Za-z_][A-Za-z0-9_]*"
                )
            if key not in vault:
                raise ConfigError(
                    f"Vault secret key '{key}' referenced in config{loc} is missing. "
                    f"Add it to the vault."
                )
            return vault[key]

        return value

    if isinstance(value, dict):
        return {k: _expand_value(v, path=f"{path}.{k}" if path else k, vault=vault) for k, v in value.items()}

    if isinstance(value, list):
        return [_expand_value(item, path=f"{path}[{i}]", vault=vault) for i, item in enumerate(value)]

    return value


def expand_env(raw: dict, vault: dict | None = None) -> dict:
    """Resolve all ``env:VAR`` and ``sec:KEY`` references in a raw config dict.

    Returns a new dict where every string value that is exactly ``env:VAR``
    or ``sec:KEY`` has been replaced with the corresponding value.  All other
    values (strings, ints, floats, booleans) are returned unchanged.

    Call this once at load time before passing *raw* to :func:`parse_config`.
    Raises :class:`ConfigError` if any referenced variable or vault key is
    missing, or if the key name is invalid.
    """
    return _expand_value(raw, vault=vault)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------

def _has_sec_reference(value: Any) -> bool:
    """Return True if *value* contains any ``sec:`` reference."""
    if isinstance(value, str):
        return value.startswith(_SEC_PREFIX)
    if isinstance(value, dict):
        return any(_has_sec_reference(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_sec_reference(item) for item in value)
    return False


def vault_path(raw: dict) -> str:
    """Return the vault file path for *raw*.

    Uses ``$SPC_VAULT_FILE`` when set, otherwise the default location under
    ``~/.local/share/<agent_name>/secrets.toml``.
    """
    env_path = os.environ.get("SPC_VAULT_FILE")
    if env_path:
        return env_path
    agent_name = (raw.get("agent") or {}).get("agent_name") or "piclaw"
    return os.path.expanduser(f"~/.local/share/{agent_name}/secrets.toml")


def log_dir(raw: dict) -> str:
    """Return the XDG state log directory for *raw*.

    Uses ``$SPC_LOG_DIR`` when set, otherwise the default location under
    ``~/.local/state/<agent_name>/logs``. Resolved from ``agent_name`` only and
    is therefore independent of ``agent_home`` (mirrors :func:`vault_path`).
    """
    env_dir = os.environ.get("SPC_LOG_DIR")
    if env_dir:
        return os.path.expanduser(env_dir)
    agent_name = (raw.get("agent") or {}).get("agent_name") or "piclaw"
    return os.path.expanduser(f"~/.local/state/{agent_name}/logs")


def log_path(raw: dict) -> str:
    """Resolve the active log file path for *raw*.

    An absolute ``[paths] log_file`` overrides and is returned as-is. Otherwise
    the configured filename (default ``agent.log``) is placed under
    :func:`log_dir`, so logs never land inside the source checkout.
    """
    configured = (raw.get("paths") or {}).get("log_file", "agent.log")
    if configured and os.path.isabs(configured):
        return configured
    filename = os.path.basename(configured) if configured else "agent.log"
    return os.path.join(log_dir(raw), filename or "agent.log")


def _parse_bool(value: Any, field_path: str) -> bool:
    """Return *value* as bool, rejecting strings to prevent env refs or
    ``"false"`` from being silently coerced to ``True``."""
    if isinstance(value, bool):
        return value
    raise ConfigError(
        f"Config field '{field_path}' must be a boolean (true/false), "
        f"got {type(value).__name__} {value!r}"
    )


def _parse_int(value: Any, default: int, field_path: str) -> int:
    """Return *value* as int, falling back to *default* when absent.

    Rejects strings to prevent env-resolved values (e.g. ``"4096"``) from
    silently coercing into numeric fields — env references must only be used
    on string config fields.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(
            f"Config field '{field_path}' must be an integer, got bool {value!r}"
        )
    if isinstance(value, str):
        raise ConfigError(
            f"Config field '{field_path}' must be an integer, got string {value!r}. "
            f"env:VAR references are only allowed on string config fields."
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"Config field '{field_path}' must be an integer, "
            f"got {type(value).__name__} {value!r}"
        ) from None


def _parse_float(value: Any, default: float, field_path: str) -> float:
    """Return *value* as float, falling back to *default* when absent.

    Rejects strings for the same reason as :func:`_parse_int`.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(
            f"Config field '{field_path}' must be a number, got bool {value!r}"
        )
    if isinstance(value, str):
        raise ConfigError(
            f"Config field '{field_path}' must be a number, got string {value!r}. "
            f"env:VAR references are only allowed on string config fields."
        )
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"Config field '{field_path}' must be a number, "
            f"got {type(value).__name__} {value!r}"
        ) from None


def _parse_int_list(value: Any, field_path: str) -> list[int]:
    """Return *value* as a list of ints, rejecting env-resolved string items."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(
            f"Config field '{field_path}' must be a list of integers, "
            f"got {type(value).__name__} {value!r}"
        )
    return [_parse_int(item, 0, f"{field_path}[{i}]") for i, item in enumerate(value)]


# ---------------------------------------------------------------------------
# Sub-config dataclasses (all frozen — no accidental mutation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = field(repr=False)
    security_mode: str = "allowlist"
    allowed_user_ids: list[int] = field(default_factory=list)
    pairing_timeout: int = 300


@dataclass(frozen=True)
class ModelConfig:
    name: str
    provider: str
    model: str
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    max_tokens: int = 1024
    temperature: float = 0.2
    top_p: float | None = None
    request_timeout: int = 120
    max_retries: int = 5
    retry_delay: int = 2
    vision: bool = False
    reasoning: bool = False
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EmbeddingsConfig:
    provider: str = "openai"
    api_key: str = field(default="", repr=False)
    model: str = "text-embedding-3-small"
    base_url: str = ""


@dataclass(frozen=True)
class ProviderConfig:
    """Configuration for a named LLM provider (e.g. openai, anthropic).

    Provides credential and transport defaults that individual models can
    inherit when those fields are not explicitly set at the model level.
    """

    name: str
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    request_timeout: int = 120
    max_retries: int = 5
    retry_delay: int = 2


@dataclass(frozen=True)
class AgentConfig:
    agent_name: str = "piclaw"
    agent_home: str = ""
    max_iterations: int = 8
    scheduled_max_iterations: int = 100
    tool_timeout: int = 10
    max_output_size: int = 4000  # chars: stdout/stderr per-stream limit for shell/tools
    top_tools: int = 3
    ctx_max_tokens: int = 90_000
    max_subagents: int = 6
    subagent_result_timeout: int = 300
    long_run_warn_minutes: int = 30
    diagnose_empty_responses: bool = False
    default_model: str = ""
    background_model: str = ""
    fallback_models: list[str] = field(default_factory=list)
    # Shell execution backend — "subprocess" (default, cross-platform) or "pty"
    # (POSIX-only; gives commands a real TTY, enabling line buffering, color
    # output, and progress indicators from tools like pytest, git, npm, etc.)
    shell_backend: str = "subprocess"
    shell_pty_cols: int = 220  # terminal width reported to the child (wide → fewer wraps)
    shell_pty_rows: int = 50
    # Stream live shell output to the progress panel in real-time (PTY backend only).
    # When enabled, each chunk of output is forwarded to the UI as it arrives;
    # the panel shows a rolling tail. Requires shell_backend = "pty".
    shell_streaming: bool = False
    # Creativity mode for prompt assembly — default/planner/explorer/resilient
    creativity_mode: str = "default"
    # Maximum iterations for plan execution (higher than normal max_iterations
    # to let multi-step plans complete without artificial interruption)
    plan_max_iterations: int = 50
    # Minutes of inactivity before a soft "still working?" prompt is injected
    inactivity_warn_minutes: int = 15


@dataclass(frozen=True)
class GraphMemoryConfig:
    enabled: bool = False
    db_path: str = "data/graph_memory"
    buffer_pool_mb: int = 256
    extraction_model: str = ""
    extract_every_n_turns: int = 3
    min_message_length: int = 100
    max_context_entries: int = 10


@dataclass(frozen=True)
class VaultConfig:
    type: str = "file"


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = True


@dataclass(frozen=True)
class PathsConfig:
    tools_dir: str = "tools"
    generated_tools_dir: str = "tools_generated"
    data_dir: str = "data"
    tool_index_file: str = "data/tool_index.json"
    memory_file: str = "data/memory.json"
    longterm_memory_file: str = "data/longterm_memory.json"
    results_memory_file: str = "data/results_memory.json"
    scheduler_config: str = "scheduler.toml"
    skills_dir: str = "skills"
    downloads_dir: str = "downloads"
    log_file: str = "agent.log"
    log_backup_count: int = 30
    pid_file: str = "data/agent.pid"
    tmp_dir: str = ""
    prompts_dir: str = "prompts"
    workspace_dir: str = "~/Documents"


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str  # "stdio" | "http"
    command: list[str] = field(default_factory=list)
    url: str = ""
    enabled: bool = True
    timeout: int = 30
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration — the single source of truth."""
    telegram: TelegramConfig
    agent: AgentConfig
    models: list[ModelConfig]
    embeddings: EmbeddingsConfig
    scheduler: SchedulerConfig
    paths: PathsConfig
    vault: VaultConfig = field(default_factory=VaultConfig)
    graph_memory: GraphMemoryConfig = field(default_factory=GraphMemoryConfig)
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)

    # Keep reference to raw dict for incremental migration — consumers that
    # haven't been updated yet can use this temporarily.
    _raw: dict = field(default_factory=dict, repr=False, compare=False)


def _parse_vault(raw: dict) -> VaultConfig:
    """Parse the [vault] config section into a VaultConfig."""
    section = raw.get("vault") or {}
    return VaultConfig(type=section.get("type", "file"))


# ---------------------------------------------------------------------------
# Parser / validator
# ---------------------------------------------------------------------------

def _require(d: dict, key: str, section: str) -> Any:
    """Raise ConfigError if a required key is missing."""
    if key not in d:
        raise ConfigError(f"Missing required config key '{key}' in [{section}]")
    return d[key]


def _parse_telegram(raw: dict) -> TelegramConfig:
    section = raw.get("telegram") or {}

    return TelegramConfig(
        bot_token=_require(section, "bot_token", "telegram"),
        security_mode=section.get("security_mode", "allowlist"),
        allowed_user_ids=_parse_int_list(section.get("allowed_user_ids"), "telegram.allowed_user_ids"),
        pairing_timeout=_parse_int(section.get("pairing_timeout"), 300, "telegram.pairing_timeout"),
    )


def _parse_model(entry: dict, index: int) -> ModelConfig:
    name = entry.get("name") or f"model-{index}"
    provider = entry.get("provider", "")
    if not provider:
        raise ConfigError(f"[[models]] entry '{name}' is missing 'provider'")
    model_id = entry.get("model", "")
    if not model_id:
        raise ConfigError(f"[[models]] entry '{name}' is missing 'model'")
    return ModelConfig(
        name=name,
        provider=provider,
        model=model_id,
        api_key=entry.get("api_key", ""),
        base_url=entry.get("base_url", ""),
        max_tokens=_parse_int(entry.get("max_tokens"), 1024, f"models.{name}.max_tokens"),
        temperature=_parse_float(entry.get("temperature"), 0.2, f"models.{name}.temperature"),
        top_p=_parse_float(entry["top_p"], 0.0, f"models.{name}.top_p") if "top_p" in entry else None,
        request_timeout=_parse_int(entry.get("request_timeout"), 120, f"models.{name}.request_timeout"),
        max_retries=_parse_int(entry.get("max_retries"), 5, f"models.{name}.max_retries"),
        retry_delay=_parse_int(entry.get("retry_delay"), 2, f"models.{name}.retry_delay"),
        vision=_parse_bool(entry.get("vision", False), f"models.{name}.vision"),
        reasoning=_parse_bool(entry.get("reasoning", False), f"models.{name}.reasoning"),
        aliases=list(entry.get("aliases") or []),
    )


def _parse_embeddings(raw: dict) -> EmbeddingsConfig:
    section = raw.get("embeddings") or {}
    return EmbeddingsConfig(
        provider=section.get("provider", "openai"),
        api_key=section.get("api_key", ""),
        model=section.get("model", "text-embedding-3-small"),
        base_url=section.get("base_url", ""),
    )


def _parse_agent(raw: dict) -> AgentConfig:
    section = raw.get("agent") or {}
    agent_name = section.get("agent_name", "piclaw")
    # Derive agent_home from agent_name when not set or set to empty string.
    agent_home_raw = section.get("agent_home", "")
    agent_home = agent_home_raw if agent_home_raw else os.path.expanduser(f"~/{agent_name}")
    return AgentConfig(
        agent_name=agent_name,
        agent_home=agent_home,
        max_iterations=_parse_int(section.get("max_iterations"), 8, "agent.max_iterations"),
        scheduled_max_iterations=_parse_int(section.get("scheduled_max_iterations"), 100, "agent.scheduled_max_iterations"),
        tool_timeout=_parse_int(section.get("tool_timeout"), 10, "agent.tool_timeout"),
        max_output_size=_parse_int(section.get("max_output_size"), 4000, "agent.max_output_size"),
        top_tools=_parse_int(section.get("top_tools"), 3, "agent.top_tools"),
        ctx_max_tokens=_parse_int(section.get("ctx_max_tokens"), 90_000, "agent.ctx_max_tokens"),
        max_subagents=_parse_int(section.get("max_subagents"), 6, "agent.max_subagents"),
        subagent_result_timeout=_parse_int(section.get("subagent_result_timeout"), 300, "agent.subagent_result_timeout"),
        long_run_warn_minutes=_parse_int(section.get("long_run_warn_minutes"), 30, "agent.long_run_warn_minutes"),
        diagnose_empty_responses=_parse_bool(section.get("diagnose_empty_responses", False), "agent.diagnose_empty_responses"),
        default_model=section.get("default_model", ""),
        background_model=section.get("background_model", ""),
        fallback_models=list(section.get("fallback_models") or []),
        shell_backend=str(section.get("shell_backend", "subprocess")),
        shell_pty_cols=_parse_int(section.get("shell_pty_cols"), 220, "agent.shell_pty_cols"),
        shell_pty_rows=_parse_int(section.get("shell_pty_rows"), 50, "agent.shell_pty_rows"),
        shell_streaming=_parse_bool(section.get("shell_streaming", False), "agent.shell_streaming"),
        creativity_mode=section.get("creativity_mode", "default"),
        plan_max_iterations=_parse_int(section.get("plan_max_iterations"), 50, "agent.plan_max_iterations"),
        inactivity_warn_minutes=_parse_int(section.get("inactivity_warn_minutes"), 15, "agent.inactivity_warn_minutes"),
    )


def _parse_scheduler(raw: dict) -> SchedulerConfig:
    section = raw.get("scheduler") or {}
    return SchedulerConfig(
        enabled=_parse_bool(section.get("enabled", True), "scheduler.enabled"),
    )


def _parse_paths(raw: dict) -> PathsConfig:
    section = raw.get("paths") or {}
    return PathsConfig(
        tools_dir=section.get("tools_dir", "tools"),
        generated_tools_dir=section.get("generated_tools_dir", "tools_generated"),
        data_dir=section.get("data_dir", "data"),
        tool_index_file=section.get("tool_index_file", "data/tool_index.json"),
        memory_file=section.get("memory_file", "data/memory.json"),
        longterm_memory_file=section.get("longterm_memory_file", "data/longterm_memory.json"),
        results_memory_file=section.get("results_memory_file", "data/results_memory.json"),
        scheduler_config=section.get("scheduler_config", "scheduler.toml"),
        skills_dir=section.get("skills_dir", "skills"),
        downloads_dir=section.get("downloads_dir", "downloads"),
        log_file=section.get("log_file", "agent.log"),
        log_backup_count=_parse_int(section.get("log_backup_count"), 30, "paths.log_backup_count"),
        pid_file=section.get("pid_file", "data/agent.pid"),
        tmp_dir=section.get("tmp_dir", ""),
        prompts_dir=section.get("prompts_dir", "prompts"),
        workspace_dir=section.get("workspace_dir", "~/Documents"),
    )


def _parse_graph_memory(raw: dict) -> GraphMemoryConfig:
    section = raw.get("graph_memory") or {}
    return GraphMemoryConfig(
        enabled=_parse_bool(section.get("enabled", False), "graph_memory.enabled"),
        db_path=section.get("db_path", "data/graph_memory"),
        buffer_pool_mb=_parse_int(section.get("buffer_pool_mb"), 256, "graph_memory.buffer_pool_mb"),
        extraction_model=section.get("extraction_model", ""),
        extract_every_n_turns=_parse_int(section.get("extract_every_n_turns"), 3, "graph_memory.extract_every_n_turns"),
        min_message_length=_parse_int(section.get("min_message_length"), 100, "graph_memory.min_message_length"),
        max_context_entries=_parse_int(section.get("max_context_entries"), 10, "graph_memory.max_context_entries"),
    )


def _parse_mcp_server(entry: dict, index: int) -> MCPServerConfig:
    name = entry.get("name", "")
    if not name:
        raise ConfigError(f"[[mcp_servers]] entry #{index} is missing 'name'")
    transport = entry.get("transport", "")
    if transport not in ("stdio", "http"):
        raise ConfigError(
            f"[[mcp_servers]] '{name}': transport must be 'stdio' or 'http', got '{transport}'"
        )
    if transport == "stdio" and not entry.get("command"):
        raise ConfigError(f"[[mcp_servers]] '{name}': stdio transport requires 'command' list")
    if transport == "http" and not entry.get("url"):
        raise ConfigError(f"[[mcp_servers]] '{name}': http transport requires 'url'")
    return MCPServerConfig(
        name=name,
        transport=transport,
        command=list(entry.get("command") or []),
        url=entry.get("url", ""),
        enabled=_parse_bool(entry.get("enabled", True), f"mcp_servers.{name}.enabled"),
        timeout=_parse_int(entry.get("timeout"), 30, f"mcp_servers.{name}.timeout"),
        env=dict(entry.get("env") or {}),
        headers=dict(entry.get("headers") or {}),
    )


def resolve_model_id(selected: str, configured_models: list[dict]) -> str:
    """Resolve a model name/alias to the canonical ``model`` ID.

    The LLM (or a user) may supply the model's ``name`` or a configured
    ``alias`` instead of the full ``model`` identifier
    (e.g. ``kimi-k2.5`` vs ``kimi-k2.5:cloud``).  Resolution order:

    1. Exact match on the ``model`` field — already correct, return as-is.
    2. Case-insensitive match on the ``name`` field.
    3. Case-insensitive match on any entry in the ``aliases`` list.

    Works with both raw ``dict`` entries from the TOML config and with
    :class:`ModelConfig` dataclass instances (accessed via attribute or key).

    Returns the canonical ``model`` ID on success, or ``""`` on failure.
    """
    if not selected:
        return ""

    def _get(m, key: str, default: str = "") -> str:
        if isinstance(m, dict):
            return m.get(key, default) or default
        return getattr(m, key, default) or default

    def _aliases(m) -> list:
        if isinstance(m, dict):
            return m.get("aliases") or []
        return getattr(m, "aliases", None) or []

    selected_lower = selected.lower()
    for m in configured_models:
        if _get(m, "model") == selected:
            return selected
    for m in configured_models:
        if _get(m, "name").lower() == selected_lower:
            return _get(m, "model")
    for m in configured_models:
        for alias in _aliases(m):
            if alias.lower() == selected_lower:
                return _get(m, "model")
    return ""


# ---------------------------------------------------------------------------
# Provider parsing and credential-inheritance helpers
# ---------------------------------------------------------------------------

def _parse_providers(raw: dict) -> dict[str, ProviderConfig]:
    """Parse ``[providers.<name>]`` sections from *raw*."""
    providers_raw = raw.get("providers") or {}
    result: dict[str, ProviderConfig] = {}

    for name, entry in providers_raw.items():
        if not isinstance(entry, dict):
            continue

        result[name] = ProviderConfig(
            name=name,
            api_key=entry.get("api_key", ""),
            base_url=entry.get("base_url", ""),
            request_timeout=_parse_int(
                entry.get("request_timeout"), 120, f"providers.{name}.request_timeout"
            ),
            max_retries=_parse_int(
                entry.get("max_retries"), 5, f"providers.{name}.max_retries"
            ),
            retry_delay=_parse_int(
                entry.get("retry_delay"), 2, f"providers.{name}.retry_delay"
            ),
        )

    return result


def _normalize_models(raw: dict, providers: dict[str, ProviderConfig]) -> None:
    """Apply provider-level defaults for models that omit credential / transport fields.

    Mutates *raw* in-place: writes inherited values back into each
    ``raw["models"][i]`` entry so that ``_raw`` mirrors what the typed config
    actually uses at runtime.

    Cross-level precedence:
    - Model-level ``api_key`` beats provider ``api_key``.
    - Provider values fill in only when the model omits the field entirely.
    """
    for entry in (raw.get("models") or []):
        if not providers:
            continue
        provider_name = entry.get("provider", "")
        prov = providers.get(provider_name)
        if prov is None:
            continue

        if "api_key" not in entry and prov.api_key:
            entry["api_key"] = prov.api_key

        if "base_url" not in entry and prov.base_url:
            entry["base_url"] = prov.base_url

        if "request_timeout" not in entry:
            entry["request_timeout"] = prov.request_timeout

        if "max_retries" not in entry:
            entry["max_retries"] = prov.max_retries

        if "retry_delay" not in entry:
            entry["retry_delay"] = prov.retry_delay


def _normalize_embeddings(raw: dict, providers: dict[str, ProviderConfig]) -> None:
    """Apply provider defaults to the ``[embeddings]`` section when present.

    Only runs when ``"embeddings"`` already exists in *raw* — an omitted
    embeddings section is left unchanged so that ``_raw`` never gains a
    spurious key.
    """
    if "embeddings" not in raw or not providers:
        return

    emb = raw["embeddings"]
    if not isinstance(emb, dict):
        return

    provider_name = emb.get("provider", "openai")
    prov = providers.get(provider_name)
    if prov is None:
        return

    if "api_key" not in emb and prov.api_key:
        emb["api_key"] = prov.api_key

    if "base_url" not in emb and prov.base_url:
        emb["base_url"] = prov.base_url


def _reject_removed_fields(raw: dict) -> None:
    """Reject legacy removed file-secret fields with clear migration guidance.

    The following fields were removed in favor of vault-backed ``sec:`` references:
    - ``[telegram] bot_token_file``
    - ``[providers.<name>] api_key_file``
    - ``[[models]] api_key_file``
    - ``[embeddings] api_key_file``

    Raises :class:`ConfigError` on the first offending field found.
    """
    _MIGRATION = (
        " Use 'api_key = \"sec:KEY\"' with a vault file instead. "
        "See docs: https://github.com/smallpieclaw/docs/vault"
    )

    telegram = raw.get("telegram") or {}
    if "bot_token_file" in telegram:
        raise ConfigError(
            "[telegram] 'bot_token_file' has been removed. "
            "Use 'bot_token = \"sec:KEY\"' with a vault file instead. "
            "Add your token to the vault and reference it as: bot_token = \"sec:bot_token\""
        )

    providers = raw.get("providers") or {}
    for pname, entry in providers.items():
        if isinstance(entry, dict) and "api_key_file" in entry:
            raise ConfigError(
                f"[providers.{pname}] 'api_key_file' has been removed."
                + _MIGRATION
                + f" Example: api_key = \"sec:{pname}_api_key\""
            )

    for i, entry in enumerate(raw.get("models") or []):
        if isinstance(entry, dict) and "api_key_file" in entry:
            name = entry.get("name") or f"model-{i}"
            raise ConfigError(
                f"[[models]] entry '{name}': 'api_key_file' has been removed."
                + _MIGRATION
                + f" Example: api_key = \"sec:{name}_api_key\""
            )

    embeddings = raw.get("embeddings") or {}
    if isinstance(embeddings, dict) and "api_key_file" in embeddings:
        raise ConfigError(
            "[embeddings] 'api_key_file' has been removed."
            + _MIGRATION
            + " Example: api_key = \"sec:embeddings_api_key\""
        )


def parse_config(raw: dict) -> AppConfig:
    """Parse and validate a raw TOML config dict into a typed AppConfig.

    Environment-variable placeholders (``${VAR}`` and ``${VAR:-default}``) in
    string values are expanded before validation — this is the single place in
    the codebase where config values are resolved against the OS environment.

    Raises ConfigError on missing required fields, invalid values, or
    unset required environment variables.
    """
    _reject_removed_fields(raw)

    vault: dict | None = None
    if _has_sec_reference(raw):
        vault = _load_vault(vault_path(raw))

    # Expand ${VAR} / ${VAR:-default} placeholders and sec: secrets before any other processing.
    raw = expand_env(raw, vault=vault)

    providers = _parse_providers(raw)

    # Apply provider defaults to models/embeddings.
    _normalize_models(raw, providers)
    _normalize_embeddings(raw, providers)

    # Require at least one model
    models_raw = raw.get("models") or []
    if not models_raw:
        raise ConfigError("At least one [[models]] entry is required")

    models = [_parse_model(m, i) for i, m in enumerate(models_raw)]

    mcp_raw = raw.get("mcp_servers") or []
    mcp_servers = [_parse_mcp_server(s, i) for i, s in enumerate(mcp_raw)]

    return AppConfig(
        telegram=_parse_telegram(raw),
        agent=_parse_agent(raw),
        models=models,
        embeddings=_parse_embeddings(raw),
        scheduler=_parse_scheduler(raw),
        paths=_parse_paths(raw),
        vault=_parse_vault(raw),
        graph_memory=_parse_graph_memory(raw),
        mcp_servers=mcp_servers,
        providers=providers,
        _raw=raw,
    )
