"""
agent_logging.py
----------------
structlog-based structured-primary logging for the agent.

Provides a dual-sink logging setup: a primary machine-readable JSONL sink
(``agent.jsonl``) and a secondary human prose sink (``agent.log`` + stdout),
both rendered from one structlog processor chain so their content cannot drift.

Run identity — the trace id (``r-<hex>``), agent label, and source tag — is
carried via ``structlog.contextvars`` and merged into every event as structured
fields (observability-only ambient state; correctness-critical trace
propagation remains explicit elsewhere). A closed :class:`LogEvent` taxonomy
gives the agent a stable, enumerable vocabulary for runtime log introspection
(see the ``log_query`` built-in tool). Known secret values are redacted from all
fields before either sink serializes.

Integration with stdlib ``logging`` is via ``structlog.stdlib.ProcessorFormatter``:
existing ``logging.getLogger(__name__)`` call sites keep working (their records
flow through ``foreign_pre_chain``), while hot-set call sites use
``structlog.get_logger()`` with structured key-values.
"""

from __future__ import annotations

import enum
import gzip
import logging
import logging.handlers
import os
import re
import shutil
import sys
from typing import Any, Iterable, cast

import structlog
from structlog.typing import EventDict, WrappedLogger

_REDACTION_PLACEHOLDER = "***REDACTED***"
# Minimum length for a vault value to be used as a redaction needle. Short/common
# values (e.g. "prod") would otherwise mangle unrelated log text via substring match.
_MIN_REDACTION_LEN = 6


# ---------------------------------------------------------------------------
# Closed event taxonomy
# ---------------------------------------------------------------------------
class LogEvent(str, enum.Enum):
    """Closed, enumerable set of structured event types.

    Emitted under the ``event_type`` key (structlog reserves ``event`` for the
    human message). The agent queries by these values rather than by prose
    substrings; the set is intentionally small and stable.
    """

    TOOL_START = "TOOL_START"
    TOOL_END = "TOOL_END"
    TOOL_FAILED = "TOOL_FAILED"
    LLM_CALL = "LLM_CALL"
    LLM_FAILED = "LLM_FAILED"
    STEP_BEGIN = "STEP_BEGIN"
    STEP_END = "STEP_END"
    RUN_BEGIN = "RUN_BEGIN"
    RUN_END = "RUN_END"
    ERROR = "ERROR"

    def __str__(self) -> str:  # so f-strings / str() yield the bare value
        return self.value


# ---------------------------------------------------------------------------
# Secret redaction processor (Option A: exact vault-value match)
# ---------------------------------------------------------------------------
def _make_redactor(secret_values: frozenset[str]):
    """Return a structlog processor that scrubs known secret values.

    Exact-substring match against known vault values across every string field
    (including the message). Deterministic, with no false positives; it is
    defense-in-depth, not a guarantee — emit sites should keep secrets out of
    structured fields by convention.
    """

    # Only values long enough to be genuine secrets are used as needles; scans
    # only top-level string fields (defense-in-depth, not a guarantee).
    needles = frozenset(s for s in secret_values if len(s) >= _MIN_REDACTION_LEN)

    def redact(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
        if not needles:
            return event_dict
        for key, value in list(event_dict.items()):
            if isinstance(value, str) and value:
                for secret in needles:
                    if secret in value:
                        value = value.replace(secret, _REDACTION_PLACEHOLDER)
                event_dict[key] = value
        return event_dict

    return redact


# ---------------------------------------------------------------------------
# Human prose renderer — reproduces "[label trace] message key=value"
# ---------------------------------------------------------------------------
class _ProseRenderer:
    """Render an event dict as the legacy human line, preserving grep habits.

    Shape: ``<ts> [<LEVEL>] <logger>: [<label> <trace>] <message> k=v ...`` —
    matching the previous ``%(asctime)s [%(levelname)s] %(name)s: %(message)s``
    format with the run-identity prefix restored from structured fields.
    """

    def __call__(self, _logger: WrappedLogger, _name: str, event_dict: EventDict) -> str:
        ts = event_dict.pop("ts", "")
        level = str(event_dict.pop("level", "")).upper()
        logger_name = event_dict.pop("logger", "")
        message = event_dict.pop("msg", event_dict.pop("event", ""))
        agent = event_dict.pop("agent", "")
        trace = event_dict.pop("trace", "")

        ident = " ".join(part for part in (agent, trace) if part)
        prefix = f"[{ident}] " if ident else ""
        extras = " ".join(f"{k}={v}" for k, v in event_dict.items())

        line = f"{ts} [{level}] {logger_name}: {prefix}{message}"
        if extras:
            line = f"{line} {extras}"
        return line


# ---------------------------------------------------------------------------
# Rotation — daily, date-suffixed, gzip-compressed backups
# ---------------------------------------------------------------------------
def _gzip_rotator(source: str, dest: str) -> None:
    """Compress the rotated file to ``<dest>.gz`` and remove the plain source."""
    with open(source, "rb") as sf, gzip.open(f"{dest}.gz", "wb") as df:
        shutil.copyfileobj(sf, df)
    try:
        os.remove(source)
    except OSError:
        pass


class _GzipTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Daily rotation with Linux-style date suffixes and gzip compression.

    Rotated files look like ``agent.log.2026-07-05.gz``. ``extMatch`` is widened
    so the handler's retention pruning still recognizes gzipped backups and
    honors ``backupCount``.
    """

    def __init__(self, filename: str, *, backup_count: int = 30) -> None:
        super().__init__(
            filename,
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
            utc=False,
        )
        self.rotator = _gzip_rotator
        # Default midnight extMatch is r"^\d{4}-\d{2}-\d{2}(\.\w+)?$"; allow a
        # trailing ".gz" so getFilesToDelete() prunes compressed backups too.
        self.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}(\.\w+)?(\.gz)?$", re.ASCII)


# ---------------------------------------------------------------------------
# Shared processor chain
# ---------------------------------------------------------------------------
def _shared_processors(secret_values: frozenset[str]) -> list:
    """Build the processor chain shared by native and foreign records.

    Order: contextvars identity → level → logger name → ISO timestamp →
    rename ``event``→``msg`` → redact secrets. Renderers run afterwards, per
    handler.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", key="ts"),
        structlog.processors.EventRenamer("msg"),
        _make_redactor(secret_values),
    ]


# ---------------------------------------------------------------------------
# Public setup
# ---------------------------------------------------------------------------
def setup_bootstrap() -> None:
    """Minimal stdout-only prose logging before config (and the XDG path) load."""
    shared = _shared_processors(frozenset())
    structlog.configure(
        processors=shared + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=cast(Any, [structlog.stdlib.ProcessorFormatter.remove_processors_meta, _ProseRenderer()]),
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    _reset_handlers(root)
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def setup_logging(
    log_file: str,
    *,
    backup_count: int = 30,
    secret_values: Iterable[str] = (),
    stream=None,
) -> str:
    """Configure structlog dual-sink logging and return the JSONL path.

    Args:
        log_file: Resolved absolute prose log path (see ``config_schema.log_path``).
        backup_count: Number of daily rotated backups to retain per sink.
        secret_values: Known secret strings to redact from every field.
        stream: Console stream for the prose sink (defaults to ``sys.stdout``).

    Returns:
        The path to the primary JSONL sink (``<log_file stem>.jsonl``).
    """
    if stream is None:
        stream = sys.stdout
    os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
    json_file = f"{os.path.splitext(log_file)[0]}.jsonl"
    secrets = frozenset(s for s in secret_values if isinstance(s, str) and s)

    shared = _shared_processors(secrets)
    structlog.configure(
        processors=shared + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    json_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    prose_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=cast(
            Any,
            [structlog.stdlib.ProcessorFormatter.remove_processors_meta, _ProseRenderer()],
        ),
    )

    json_handler = _GzipTimedRotatingFileHandler(json_file, backup_count=backup_count)
    json_handler.setFormatter(json_formatter)
    prose_handler = _GzipTimedRotatingFileHandler(log_file, backup_count=backup_count)
    prose_handler.setFormatter(prose_formatter)
    stream_handler = logging.StreamHandler(stream)
    stream_handler.setFormatter(prose_formatter)

    root = logging.getLogger()
    _reset_handlers(root)
    root.setLevel(logging.INFO)
    root.addHandler(json_handler)
    root.addHandler(prose_handler)
    root.addHandler(stream_handler)

    # Suppress high-volume INFO noise from HTTP/Telegram internals.
    for noisy in ("httpx", "telegram", "telegram.ext"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return json_file


def _reset_handlers(root: logging.Logger) -> None:
    """Remove and close any existing root handlers (idempotent reconfigure)."""
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Run identity + emit helpers
# ---------------------------------------------------------------------------
def bind_run_context(*, trace: str = "", agent: str = "", prompt_id: str = "") -> dict:
    """Bind run-identity fields into the context-local logging state.

    Call at each run entry point (ReAct run start — the common chokepoint for
    main, sub-agent, and scheduled runs). ``agent`` is the run label (e.g.
    ``main`` or ``sa-<id>``). ``prompt_id`` is the operator-facing prompt number.
    Only non-empty values are bound.

    Returns the token mapping to pass to :func:`reset_run_context` on exit
    (empty dict if nothing was bound), so nested runs on one thread restore the
    parent's identity rather than wiping it.
    """
    data: dict[str, str] = {}
    if trace:
        data["trace"] = trace
    if agent:
        data["agent"] = agent
    if prompt_id:
        data["prompt_id"] = prompt_id
    if data:
        return dict(structlog.contextvars.bind_contextvars(**data))
    return {}


def reset_run_context(tokens: dict) -> None:
    """Restore context-local identity to the values captured before binding.

    Nesting-safe counterpart to :func:`bind_run_context`; unlike
    :func:`clear_run_context` it does not wipe an enclosing run's identity.
    """
    if tokens:
        structlog.contextvars.reset_contextvars(**tokens)


def clear_run_context() -> None:
    """Clear all context-local run-identity fields (call on run/thread exit)."""
    structlog.contextvars.clear_contextvars()


def get_logger(name: str | None = None):
    """Return a structlog logger, optionally named (usually ``__name__``)."""
    return structlog.get_logger(name)


def log_event(
    event: LogEvent,
    message: str,
    *,
    level: int = logging.INFO,
    logger=None,
    **fields: Any,
) -> None:
    """Emit a taxonomy event with structured key-values.

    Emits ``message`` at ``level`` with ``event_type`` set to the enum value and
    any additional structured ``fields`` (e.g. ``tool``, ``exit``, ``dur_ms``,
    ``err``).
    """
    log = logger if logger is not None else structlog.get_logger()
    log.log(level, message, event_type=str(event), **fields)
