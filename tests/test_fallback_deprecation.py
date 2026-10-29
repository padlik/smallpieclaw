"""Unsupported legacy ``fallback_models`` settings (provider-model-catalog 1.2/12.21/12.23).

Automatic model substitution is gone, so the old "warn and keep" / "warn and
drop" contracts are superseded:

- ``agent.fallback_models`` in config.toml is rejected whenever the key is
  present (non-empty or empty) with a ``ConfigError`` naming the field; an
  absent key parses quietly.
- A per-job ``fallback_models`` key in scheduler.toml loads and reloads
  silently and stays raw in the document; when the job fires, one bounded
  warning names the job and the key (never the value) and the spawn call
  never carries the key or any model derived from it.

Only external boundaries are substituted: the file system (``tmp_path``), the
XDG environment, the ``schedule`` library's global scheduler, notify, agent,
and the spawn-capable executor. The scheduler is real; private state is read
only for observation.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest
import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_schema import parse_config
from exceptions import ConfigError
from scheduler import Scheduler
from xdg import xdg_paths


_SECRET = "sk-fake-legacy-fallback-3c9e"
_TAG = "legacy_job"
_FALLBACK_VALUE = ["provider/alt-model", _SECRET]
_TOML = (
    f"[jobs.{_TAG}]\nenabled = true\nschedule = \"cron\"\ncron = \"0 4 1 1 *\"\n"
    f"task = \"legacy task\"\nnotify = false\n"
    f"fallback_models = {json.dumps(_FALLBACK_VALUE)}\n"
)
_MISSING = object()

_VALID_CONFIG: dict[str, Any] = {
    "telegram": {
        "bot_token": "123456:ABC-DEF",
        "allowed_user_ids": [12345],
        "security_mode": "allowlist",
    },
    "agent": {"max_iterations": 8, "scheduled_max_iterations": 100},
    "providers": {
        "openai-main": {
            "type": "openai",
            "api_key": "test-provider-key",
            "base_url": "https://api.openai.com/v1",
        }
    },
    "paths": {},
}


# The field as a standalone token: pytest tmp directory names derived from test
# names (``test_job_fallback_models_...``) embed the word inside a path segment.
_FIELD_TOKEN = re.compile(r"\bfallback_models\b")


def _fallback_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return every captured record whose message names the ``fallback_models`` field."""
    return [r for r in caplog.records if _FIELD_TOKEN.search(r.getMessage())]


def test_field_token_matches_field_messages_not_embedded_identifiers() -> None:
    """The observation matcher counts field-naming messages, not paths or identifiers."""
    assert _FIELD_TOKEN.search("Job x: legacy fallback_models is ignored; remove the key.")
    assert _FIELD_TOKEN.search("agent.fallback_models is no longer supported")
    assert not _FIELD_TOKEN.search(
        "Loaded 1 jobs from /tmp/test_job_fallback_models_load_0/scheduler.toml"
    )


# ---------------------------------------------------------------------------
# agent.fallback_models (config.toml)
# ---------------------------------------------------------------------------


def test_valid_base_config_without_fallback_parses_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With the key absent the valid provider/Telegram config parses with no fallback log."""
    caplog.set_level(logging.DEBUG)

    app_cfg = parse_config(copy.deepcopy(_VALID_CONFIG))

    assert _fallback_records(caplog) == [], [r.getMessage() for r in caplog.records]
    assert app_cfg.agent.fallback_models == [], "a fallback list appeared without config"
    assert "openai-main" in app_cfg.providers


@pytest.mark.parametrize(
    "value",
    [pytest.param(["provider/other"], id="nonempty"), pytest.param([], id="empty")],
)
def test_agent_fallback_models_present_is_rejected(value: list[str]) -> None:
    """Presence alone (even ``[]``) is a ConfigError naming ``agent.fallback_models``."""
    raw = copy.deepcopy(_VALID_CONFIG)
    raw["agent"]["fallback_models"] = value

    with pytest.raises(ConfigError, match=r"agent\.fallback_models is no longer supported"):
        parse_config(raw)


# ---------------------------------------------------------------------------
# Per-job fallback_models (scheduler.toml) through a real Scheduler
# ---------------------------------------------------------------------------


@pytest.fixture
def legacy_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> Iterator[tuple[Scheduler, Path, list[tuple[dict, object]]]]:
    """Build a real disabled Scheduler over the legacy job; capture logs and spawn calls.

    Logging is captured from before construction so the initial load is observed.
    """
    caplog.set_level(logging.DEBUG)
    isolated = schedule.Scheduler()
    monkeypatch.setattr(schedule, "default_scheduler", isolated)
    monkeypatch.setattr(schedule, "jobs", isolated.jobs)
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    paths = xdg_paths("fallback-deprecation-test")
    assert str(paths.scheduler_config).startswith(str(tmp_path))
    paths.config_home.mkdir(parents=True, exist_ok=True)
    paths.state_home.mkdir(parents=True, exist_ok=True)
    paths.scheduler_config.write_text(_TOML, encoding="utf-8")

    sched = Scheduler(
        config={"scheduler": {"enabled": False}, "agent": {"long_run_warn_minutes": 0}},
        notify_fn=MagicMock(),
        agent_fn=MagicMock(),
        paths=paths,
        scheduler_config_path=str(paths.scheduler_config),
    )
    spawns: list[tuple[dict, object]] = []

    def spawn(args: dict, options: object) -> dict:
        spawns.append((dict(args), options))
        return {"success": True, "agent_id": f"sa-{len(spawns)}"}

    sched.builtin_executor = MagicMock()
    sched.builtin_executor.spawn_agent = MagicMock(side_effect=spawn)
    yield sched, paths.scheduler_config, spawns
    if sched._thread is not None:
        sched.stop()


def test_job_fallback_models_load_and_reload_are_silent(
    legacy_scheduler: tuple[Scheduler, Path, list[tuple[dict, object]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Load and reload log nothing, set no config_error, keep the raw value and file bytes."""
    sched, toml_path, _spawns = legacy_scheduler

    result = sched.reload()

    assert "error" not in result, result
    assert _fallback_records(caplog) == [], [r.getMessage() for r in caplog.records]
    assert _SECRET not in caplog.text
    assert (sched.show_job(_TAG) or {}).get("config_error") is None
    assert sched._load_result.document["jobs"][_TAG]["fallback_models"] == _FALLBACK_VALUE
    assert toml_path.read_text(encoding="utf-8") == _TOML


def test_job_fallback_models_fire_warns_once_and_spawns_without_key(
    legacy_scheduler: tuple[Scheduler, Path, list[tuple[dict, object]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One bounded secret-free warning; exactly one spawn with no fallback or alternate model."""
    sched, toml_path, spawns = legacy_scheduler
    before = toml_path.read_bytes()
    caplog.clear()

    sched._run_job(_TAG)

    warnings = [r for r in _fallback_records(caplog) if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    message = warnings[0].getMessage()
    assert _TAG in message and "ignored" in message, message
    assert _SECRET not in caplog.text
    assert "provider/alt-model" not in caplog.text

    assert len(spawns) == 1, spawns
    args, options = spawns[0]
    assert args == {"task": "legacy task"}, "spawn args gained a fallback-derived field"
    assert getattr(options, "fallback_models", _MISSING) is _MISSING
    assert _SECRET not in repr(options)
    sched.agent.assert_not_called()
    assert [call.args[0] for call in sched.notify.call_args_list] == []

    assert toml_path.read_bytes() == before
    assert sched._load_result.document["jobs"][_TAG]["fallback_models"] == _FALLBACK_VALUE
