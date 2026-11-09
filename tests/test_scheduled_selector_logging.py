"""Bounded rendering of marked scheduled-selector warnings (task 12.9).

Records logged with ``extra={"scheduled_selector_warning": True}`` must reach
both rendered sinks (structured JSON and human prose) with a ``msg`` of at most
256 characters, truncation indicator included, and only *after* the shared
secret redactor has removed known secret values. Unmarked records keep today's
behavior: full message, redaction only.

Everything here is in-memory: real ``agent_logging._shared_processors`` and
``_ProseRenderer`` behind stdlib ``StreamHandler``s on an isolated logger. No
SQLite store, no global ``structlog.configure`` and no vault reads; secrets are
fabricated literals. The sink-only tests log a raw value directly, so their
``LogRecord`` legitimately holds it and only the formatted output is asserted.
The registry's own secret-bearing warning must already be safe when emitted:
those cases also observe the record before any handler runs.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, cast

import pytest
import structlog

import agent_logging
from builtin_tools.text_utils import redact_vault_secrets
from config_schema import ProviderConfig, ResolvedProviderConnection
from model_registry import CatalogDocument, ModelRecord, ModelRegistry, ProviderState
from model_settings import ModelSettings

_MAX_MSG = 256
_MARKER = {"scheduled_selector_warning": True}
_KIND = "Scheduled selector unknown; inheriting Main: "
_PLACEHOLDER = "***REDACTED***"

# Fabricated secrets with a distinctive sentinel start (never an ordinary word).
_SPANNING_SECRET = "QZXSENTINEL7f3aSPAN" + "k9" * 95  # 209 chars
_HUGE_SECRET = "QZXSENTINEL7f3aHUGE" + "m4" * 503  # 1025 chars
_UNMARKED_SECRET = "QZXSENTINEL7f3aPLAIN-token"
_FAKE_SECRETS = frozenset({_SPANNING_SECRET, _HUGE_SECRET, _UNMARKED_SECRET})


@dataclass
class _Sinks:
    """An isolated logger wired to a JSON sink and a prose sink."""

    logger: logging.Logger
    json_stream: io.StringIO
    prose_stream: io.StringIO

    def emit_warning(self, selector: str, *, marked: bool) -> None:
        """Log one selector warning with the raw, untruncated selector."""
        extra = dict(_MARKER) if marked else None
        saved_ctx = structlog.contextvars.get_contextvars()
        structlog.contextvars.clear_contextvars()
        try:
            self.logger.warning(_KIND + "%s", selector, extra=extra)
        finally:
            structlog.contextvars.clear_contextvars()
            structlog.contextvars.bind_contextvars(**saved_ctx)

    def json_msg(self) -> str:
        """Return ``msg`` of the single structured record emitted."""
        lines = self.json_stream.getvalue().splitlines()
        assert len(lines) == 1, lines
        return json.loads(lines[0])["msg"]

    def prose_line(self) -> str:
        """Return the single prose line emitted."""
        lines = self.prose_stream.getvalue().splitlines()
        assert len(lines) == 1, lines
        return lines[0]

    def prose_msg(self) -> str:
        """Return the message part of the prose line (after ``<logger>: ``)."""
        line = self.prose_line()
        marker = f"[WARNING] {self.logger.name}: "
        assert marker in line, line
        return line.split(marker, 1)[1]


@pytest.fixture
def sinks() -> Iterator[_Sinks]:
    """Attach both real formatters to a unique logger; restore it afterwards."""
    shared = agent_logging._shared_processors(_FAKE_SECRETS)
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
            [
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                agent_logging._ProseRenderer(),
            ],
        ),
    )
    json_stream, prose_stream = io.StringIO(), io.StringIO()
    json_handler = logging.StreamHandler(json_stream)
    json_handler.setFormatter(json_formatter)
    prose_handler = logging.StreamHandler(prose_stream)
    prose_handler.setFormatter(prose_formatter)

    logger = logging.getLogger(f"test_scheduled_selector_logging.{uuid.uuid4().hex}")
    saved_handlers, saved_level, saved_propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers = [json_handler, prose_handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield _Sinks(logger, json_stream, prose_stream)
    finally:
        for handler in (json_handler, prose_handler):
            logger.removeHandler(handler)
            handler.close()
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def _assert_same_msg_in_both(sinks: _Sinks) -> str:
    """Both sinks carry the same rendered message; return it."""
    msg = sinks.json_msg()
    assert sinks.prose_msg() == msg
    return msg


def test_marked_short_selector_kept_whole(sinks: _Sinks) -> None:
    """A short marked selector is rendered unchanged with its kind prefix."""
    sinks.emit_warning("provider/not-known", marked=True)

    msg = _assert_same_msg_in_both(sinks)
    assert msg == _KIND + "provider/not-known"
    assert len(msg) <= _MAX_MSG


def test_marked_long_benign_selector_bounded(sinks: _Sinks) -> None:
    """A >=1000-char marked selector renders at most 256 chars, prefix kept."""
    selector = "provider/" + "benign-selector-segment-" * 50
    assert len(selector) >= 1000
    sinks.emit_warning(selector, marked=True)

    msg = _assert_same_msg_in_both(sinks)
    assert len(msg) <= _MAX_MSG
    assert msg.startswith(_KIND)
    assert msg != _KIND + selector


@pytest.mark.parametrize(
    ("head", "secret"),
    [
        pytest.param("provider/" + "b" * 141, _SPANNING_SECRET, id="secret-spans-cutoff"),
        pytest.param("", _HUGE_SECRET, id="secret-longer-than-bound"),
    ],
)
def test_marked_secret_redacted_before_bound(sinks: _Sinks, head: str, secret: str) -> None:
    """Redaction runs before the bound: no secret fragment survives truncation."""
    selector = head + secret + "-public-suffix" * 200
    start = len(_KIND) + len(head)
    assert start < _MAX_MSG < start + len(secret)
    # Even after redaction the message still exceeds the bound (old code: RED).
    assert len(_KIND + head + _PLACEHOLDER + "-public-suffix" * 200) > _MAX_MSG
    sinks.emit_warning(selector, marked=True)

    msg = _assert_same_msg_in_both(sinks)
    assert len(msg) <= _MAX_MSG
    assert msg.startswith(_KIND + head)
    assert _PLACEHOLDER in msg
    prose = sinks.prose_line()
    for fragment in (secret, secret[:16], secret[:8]):
        assert fragment not in msg
        assert fragment not in prose
        assert fragment not in sinks.json_stream.getvalue()


def test_unmarked_long_message_unchanged(sinks: _Sinks) -> None:
    """Unmarked records are never truncated."""
    selector = "provider/" + "benign-selector-segment-" * 50
    sinks.emit_warning(selector, marked=False)

    msg = _assert_same_msg_in_both(sinks)
    assert msg == _KIND + selector
    assert len(msg) > _MAX_MSG


def test_unmarked_secret_still_redacted(sinks: _Sinks) -> None:
    """The existing redactor still scrubs unmarked records, without truncation."""
    sinks.emit_warning(f"provider/{_UNMARKED_SECRET}/tail", marked=False)

    msg = _assert_same_msg_in_both(sinks)
    assert msg == f"{_KIND}provider/{_PLACEHOLDER}/tail"
    assert _UNMARKED_SECRET not in sinks.prose_line()
    assert _UNMARKED_SECRET not in sinks.json_stream.getvalue()


# ---------------------------------------------------------------------------
# Integration: the real ModelRegistry.admit_scheduled warning through both sinks
# ---------------------------------------------------------------------------
_REGISTRY_LOGGER = "model_registry"
_PROVIDER = ProviderConfig("p", "openai", base_url="https://example.invalid/v1")
_CONNECTION = ResolvedProviderConnection(
    "p", "openai", base_url="https://example.invalid/v1", request_timeout=120, api_key="fake-key"
)


@contextlib.contextmanager
def _route_registry_logger(sinks: _Sinks) -> Iterator[None]:
    """Send the real ``model_registry`` stdlib logger to the in-memory sinks.

    Saves and restores every mutated stdlib attribute and the structlog
    contextvars so no state leaks into other tests.
    """
    registry_logger = logging.getLogger(_REGISTRY_LOGGER)
    saved = (
        registry_logger.handlers[:],
        registry_logger.filters[:],
        registry_logger.level,
        registry_logger.propagate,
        registry_logger.disabled,
    )
    saved_ctx = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    registry_logger.handlers = sinks.logger.handlers[:]
    registry_logger.filters = []
    registry_logger.setLevel(logging.WARNING)
    registry_logger.propagate = False
    registry_logger.disabled = False
    try:
        yield
    finally:
        registry_logger.handlers, registry_logger.filters = saved[0], saved[1]
        registry_logger.setLevel(saved[2])
        registry_logger.propagate, registry_logger.disabled = saved[3], saved[4]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**saved_ctx)


def _rendered(sinks: _Sinks) -> tuple[list[str], list[str]]:
    """Return the JSON lines and prose lines rendered so far."""
    return sinks.json_stream.getvalue().splitlines(), sinks.prose_stream.getvalue().splitlines()


def _registry_msg(sinks: _Sinks) -> str:
    """Return the one registry warning message, identical in both sinks."""
    json_lines, prose_lines = _rendered(sinks)
    assert len(json_lines) == 1 and len(prose_lines) == 1, (json_lines, prose_lines)
    record = json.loads(json_lines[0])
    assert record["logger"] == _REGISTRY_LOGGER
    marker = f"[WARNING] {_REGISTRY_LOGGER}: "
    assert marker in prose_lines[0], prose_lines[0]
    assert prose_lines[0].split(marker, 1)[1] == record["msg"]
    return record["msg"]


@dataclass
class _Registry:
    """A real registry seeded with an active Main ``p/main`` plus its probes."""

    registry: ModelRegistry
    settings_path: Path
    resolved: list[ProviderConfig]
    rendered_at_resolve: list[tuple[list[str], list[str]]]


def _seeded_registry(
    tmp_path: Path, sinks: _Sinks, *, redactor: Callable[[str], str] | None = None,
) -> _Registry:
    """Persist a one-model catalog, select Main, and record resolver entries.

    ``redactor`` is the registry's optional ``selector_redactor`` (default none).
    """
    settings = ModelSettings(tmp_path / "models.toml")
    settings.write(
        CatalogDocument(
            providers=(ProviderState("p", "openai", "2026-10-22T00:00:00+00:00", "fresh"),),
            models=(ModelRecord("p/main", "p", "main", "Main"),),
        )
    )
    resolved: list[ProviderConfig] = []
    rendered_at_resolve: list[tuple[list[str], list[str]]] = []

    def resolver(config: ProviderConfig) -> ResolvedProviderConnection:
        """Record what both sinks already hold when credentials are resolved."""
        resolved.append(config)
        rendered_at_resolve.append(_rendered(sinks))
        return _CONNECTION

    discovery = cast(Any, SimpleNamespace())  # never called by admission
    registry = ModelRegistry({"p": _PROVIDER}, settings, discovery, resolver, selector_redactor=redactor)
    registry.select("p/main")
    return _Registry(registry, settings.path, resolved, rendered_at_resolve)


def _assert_one_warning_before_resolve(seeded: _Registry) -> None:
    """Exactly one Main resolution, entered after one warning in each sink."""
    assert seeded.resolved == [_PROVIDER]
    json_lines, prose_lines = seeded.rendered_at_resolve[0]
    assert len(json_lines) == 1 and len(prose_lines) == 1


def test_registry_unknown_short_selector_warns_before_resolve(tmp_path: Path, sinks: _Sinks) -> None:
    """A benign unconfigured-connection selector inherits Main; warning renders first."""
    seeded = _seeded_registry(tmp_path, sinks)
    with _route_registry_logger(sinks):
        run = seeded.registry.admit_scheduled("ghost/not-known")

    assert run.model.id == "p/main"
    _assert_one_warning_before_resolve(seeded)
    assert _registry_msg(sinks) == _KIND + repr("ghost/not-known")


class _EmissionObserver(logging.Filter):
    """Delegating pre-handler observer: snapshots each record and never drops one."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[object, object, str]] = []

    def filter(self, record: logging.LogRecord) -> bool:
        """Snapshot ``msg``/``args``/formatted text as emitted, then pass it on."""
        self.records.append((record.msg, record.args, record.getMessage()))
        return True


# The same startup-known secrets the sinks redact, injected the way the root
# injects its selector redactor (production dependency, not a sink-policy change).
_REGISTRY_REDACTOR = functools.partial(
    redact_vault_secrets, secrets=tuple(sorted(_FAKE_SECRETS)), placeholder=_PLACEHOLDER,
)


@pytest.mark.parametrize(
    ("head", "secret"),
    [
        pytest.param("provider/" + "b" * 141, _SPANNING_SECRET, id="secret-spans-cutoff"),
        pytest.param("", _HUGE_SECRET, id="secret-longer-than-bound"),
    ],
)
def test_registry_unknown_secret_selector_redacted_and_bounded(
    tmp_path: Path, sinks: _Sinks, head: str, secret: str
) -> None:
    """The real warning carries redacted-then-bounded quoted evidence when emitted,
    and both sinks stay bounded and secret-free."""
    seeded = _seeded_registry(tmp_path, sinks, redactor=_REGISTRY_REDACTOR)
    selector = head + secret + "-public-suffix" * 200
    original_selector = "".join([head, secret, "-public-suffix" * 200])
    view_before = seeded.registry.view()
    settings_before = seeded.settings_path.read_bytes()
    observer = _EmissionObserver()
    with _route_registry_logger(sinks):
        registry_logger = logging.getLogger(_REGISTRY_LOGGER)
        registry_logger.addFilter(observer)
        try:
            run = seeded.registry.admit_scheduled(selector)
        finally:
            registry_logger.removeFilter(observer)

    # The emitted record itself is safe before any handler or sink processor runs.
    ((emitted_msg, emitted_args, emitted_text),) = observer.records
    assert emitted_args == ()
    assert isinstance(emitted_msg, str) and emitted_text == emitted_msg
    assert emitted_msg.startswith(_KIND + "'" + head) and emitted_msg.endswith("'")
    assert len(emitted_msg) <= len(_KIND) + 2 + _MAX_MSG
    assert _PLACEHOLDER in emitted_msg
    for fragment in (secret, secret[:16], secret[:8]):
        assert fragment not in emitted_msg

    assert run.model.id == "p/main"
    _assert_one_warning_before_resolve(seeded)
    msg = _registry_msg(sinks)
    assert len(msg) <= _MAX_MSG
    assert msg.startswith(_KIND + "'" + head)
    assert _PLACEHOLDER in msg
    json_text, prose_text = sinks.json_stream.getvalue(), sinks.prose_stream.getvalue()
    for fragment in (secret, secret[:16], secret[:8]):
        assert fragment not in json_text
        assert fragment not in prose_text
    assert selector == original_selector
    assert seeded.registry.view() == view_before
    assert seeded.settings_path.read_bytes() == settings_before


def test_registry_malformed_selector_never_stringified(tmp_path: Path, sinks: _Sinks) -> None:
    """A non-string selector inherits Main with a fixed label, unformatted."""

    class _Poison:
        """Non-string selector whose stringification must never happen."""

        def __init__(self) -> None:
            self.fake_secret = _SPANNING_SECRET
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            raise AssertionError("must not stringify")

        def __repr__(self) -> str:
            self.calls += 1
            raise AssertionError("must not stringify")

    poison = _Poison()
    seeded = _seeded_registry(tmp_path, sinks)
    with _route_registry_logger(sinks):
        run = seeded.registry.admit_scheduled(poison)

    assert run.model.id == "p/main"
    _assert_one_warning_before_resolve(seeded)
    msg = _registry_msg(sinks)
    assert msg.startswith("Scheduled selector malformed")
    assert len(msg) <= _MAX_MSG
    assert poison.calls == 0
    assert poison.fake_secret == _SPANNING_SECRET
    for fragment in (_SPANNING_SECRET, _SPANNING_SECRET[:16], _SPANNING_SECRET[:8], "_Poison"):
        assert fragment not in sinks.json_stream.getvalue()
        assert fragment not in sinks.prose_stream.getvalue()


# ---------------------------------------------------------------------------
# Task 12.22: shared bounded selector evidence (labels stay verbatim in WARN)
# ---------------------------------------------------------------------------
_INHERIT_LABEL = "inherit"
_MALFORMED_LABEL = "malformed (not a string)"
_BLANK_LABEL = "blank"


def test_registry_blank_selector_warns_with_verbatim_label(tmp_path: Path, sinks: _Sinks) -> None:
    """Consolidation keeps the blank WARN message exactly."""
    seeded = _seeded_registry(tmp_path, sinks)
    with _route_registry_logger(sinks):
        seeded.registry.admit_scheduled(" \t ")

    assert _registry_msg(sinks) == "Scheduled selector blank; inheriting Main"


def test_registry_non_string_selector_warns_with_verbatim_label(
    tmp_path: Path, sinks: _Sinks
) -> None:
    """Consolidation keeps the malformed WARN message exactly."""
    seeded = _seeded_registry(tmp_path, sinks)
    with _route_registry_logger(sinks):
        seeded.registry.admit_scheduled(123)

    assert _registry_msg(sinks) == "Scheduled selector malformed (not a string); inheriting Main"


def _evidence(raw: object, **kwargs: Any) -> str:
    """Render *raw* through the shared helper (imported lazily: absent today)."""
    from model_registry import bounded_selector_evidence

    return bounded_selector_evidence(raw, **kwargs)


def test_selector_evidence_absent_is_inherit() -> None:
    assert _evidence(None) == _INHERIT_LABEL


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(123, id="int"),
        pytest.param(True, id="bool"),
        pytest.param(["QZXlist"], id="list"),
        pytest.param({"QZXkey": "QZXvalue"}, id="dict"),
        pytest.param(__import__("datetime").datetime(2026, 1, 2, 3, 4), id="datetime"),
    ],
)
def test_selector_evidence_non_string_is_label_only(raw: object) -> None:
    rendered = _evidence(raw)

    assert rendered == _MALFORMED_LABEL
    for fragment in ("123", "True", "QZX", "2026"):
        assert fragment not in rendered


def test_selector_evidence_never_stringifies_non_string() -> None:
    class _Poison:
        """Non-string selector whose stringification must never happen."""

        calls = 0

        def __str__(self) -> str:
            type(self).calls += 1
            raise AssertionError("must not stringify")

        def __repr__(self) -> str:
            type(self).calls += 1
            raise AssertionError("must not stringify")

    assert _evidence(_Poison()) == _MALFORMED_LABEL
    assert _Poison.calls == 0


@pytest.mark.parametrize("raw", ["", " \t\n "], ids=["empty", "whitespace"])
def test_selector_evidence_blank_is_label(raw: str) -> None:
    assert _evidence(raw) == _BLANK_LABEL


def test_selector_evidence_short_string_kept_exact() -> None:
    assert _evidence(" p/model ") == " p/model "


def test_selector_evidence_long_string_bounded_with_indicator() -> None:
    raw = "provider/" + "benign-selector-segment-" * 50
    rendered = _evidence(raw)

    assert len(rendered) <= _MAX_MSG
    assert rendered.startswith(raw[:100])
    assert not raw.startswith(rendered)  # an indicator marks the cut


def test_selector_evidence_injected_redactor_runs_before_bound() -> None:
    head = "provider/" + "b" * 200
    raw = head + _SPANNING_SECRET + "-public-suffix" * 50
    rendered = _evidence(raw, redact=lambda text: text.replace(_SPANNING_SECRET, _PLACEHOLDER))

    assert len(rendered) <= _MAX_MSG
    assert rendered.startswith(head + _PLACEHOLDER)
    for fragment in (_SPANNING_SECRET, _SPANNING_SECRET[:16], _SPANNING_SECRET[:8]):
        assert fragment not in rendered


def test_selector_evidence_without_redactor_is_bounded_but_unredacted() -> None:
    raw = f"provider/{_UNMARKED_SECRET}"

    assert _evidence(raw) == raw


# ---------------------------------------------------------------------------
# Task 12.22: the scheduler's "Running scheduled job" prose/structured line
# ---------------------------------------------------------------------------
_SCHEDULER_LOGGER = "scheduler"
_RUNNING = "Running scheduled job nightly | model="
_SCHED_ONLY_SECRET = "QZXSENTINEL7f3aSCHED-token"  # not known to the log redactor


@contextlib.contextmanager
def _route_scheduler_logger(sinks: _Sinks) -> Iterator[None]:
    """Send the real ``scheduler`` stdlib logger to the sinks; restore everything."""
    sched_logger = logging.getLogger(_SCHEDULER_LOGGER)
    saved = (
        sched_logger.handlers[:], sched_logger.filters[:], sched_logger.level,
        sched_logger.propagate, sched_logger.disabled,
    )
    saved_ctx = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    sched_logger.handlers = sinks.logger.handlers[:]
    sched_logger.filters = []
    sched_logger.setLevel(logging.INFO)
    sched_logger.propagate = False
    sched_logger.disabled = False
    try:
        yield
    finally:
        sched_logger.handlers, sched_logger.filters = saved[0], saved[1]
        sched_logger.setLevel(saved[2])
        sched_logger.propagate, sched_logger.disabled = saved[3], saved[4]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**saved_ctx)


def _fire_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sinks: _Sinks,
    model: dict, redactor: Any = None,
) -> tuple[str, str]:
    """Fire one fail-closed job on a real disabled Scheduler; return its running line.

    Returns the structured ``msg`` and the prose line of the single
    "Running scheduled job" record; every other record is ignored.
    """
    from unittest.mock import MagicMock

    from scheduler import Scheduler
    from xdg import xdg_paths

    config_path = tmp_path / "scheduler.toml"
    config_path.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    sched = Scheduler(
        config={"scheduler": {"enabled": False}, "agent": {}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        scheduler_config_path=str(config_path),
        paths=xdg_paths("test-agent"),
        selector_redactor=redactor,
    )
    sched._jobs_meta["nightly"] = {
        "task": "summarize", "enabled": True, "notify": False,
        "_next_run": "2026-10-04T02:00:00", **model,
    }
    with _route_scheduler_logger(sinks):
        sched._run_job("nightly")
    msgs = [json.loads(line)["msg"] for line in sinks.json_stream.getvalue().splitlines()]
    json_hits = [msg for msg in msgs if msg.startswith(_RUNNING)]
    prose_hits = [line for line in sinks.prose_stream.getvalue().splitlines() if _RUNNING in line]
    assert len(json_hits) == 1 and len(prose_hits) == 1, (json_hits, prose_hits)
    assert prose_hits[0].endswith(json_hits[0])
    return json_hits[0], prose_hits[0]


def test_running_line_redacts_spanning_secret_before_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sinks: _Sinks
) -> None:
    """Injected redaction runs before the 256 bound: no secret prefix survives."""
    head = "provider/" + "b" * 141
    raw = head + _SPANNING_SECRET + "-public-suffix" * 50
    assert len(head + _PLACEHOLDER + "-public-suffix" * 50) > _MAX_MSG
    msg, _ = _fire_logged(
        tmp_path, monkeypatch, sinks, {"model": raw},
        redactor=lambda text: text.replace(_SPANNING_SECRET, _PLACEHOLDER),
    )

    evidence = msg[len(_RUNNING):]
    assert len(evidence) <= _MAX_MSG
    assert evidence.startswith(head + _PLACEHOLDER)
    for fragment in (_SPANNING_SECRET, _SPANNING_SECRET[:16], _SPANNING_SECRET[:8]):
        assert fragment not in sinks.json_stream.getvalue()
        assert fragment not in sinks.prose_stream.getvalue()


def test_running_line_uses_injected_redactor_for_short_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sinks: _Sinks
) -> None:
    """A secret only the scheduler's redactor knows is masked in both sinks."""
    msg, _ = _fire_logged(
        tmp_path, monkeypatch, sinks, {"model": f"p/{_SCHED_ONLY_SECRET}"},
        redactor=lambda text: text.replace(_SCHED_ONLY_SECRET, _PLACEHOLDER),
    )

    assert msg == f"{_RUNNING}p/{_PLACEHOLDER}"
    assert _SCHED_ONLY_SECRET[:8] not in sinks.prose_stream.getvalue()


def test_running_line_empty_model_is_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sinks: _Sinks
) -> None:
    """Evidence reads the saved ``""``, which renders ``blank`` (launch still inherits)."""
    msg, _ = _fire_logged(tmp_path, monkeypatch, sinks, {"model": ""})

    assert msg == f"{_RUNNING}blank"


def test_running_line_absent_model_is_inherit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sinks: _Sinks
) -> None:
    msg, _ = _fire_logged(tmp_path, monkeypatch, sinks, {})

    assert msg == f"{_RUNNING}inherit"
