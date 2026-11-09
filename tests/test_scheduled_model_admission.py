"""
Raw scheduled-job ``model`` input on add and modify (task 12.21, ADR-0038).

Add and modify own the raw ``model`` value: no catalog query and no
admission check. On add, an omitted or ``None`` model silently inherits (no
``model`` key is persisted); on modify, an omitted or ``None`` model changes
nothing (there is no clear operation). The discriminator is absence or
``is None``, never truthiness: every TOML-representable value — a blank or
whitespace string, a boolean, a number, a list, an inline table, or an
unknown model string — is stored exactly as supplied, never trimmed,
stringified, or rewritten, both through the ``schedule`` tool and the
scheduler API. An explicit model equal to the current one is an ordinary
verbatim store. A value scheduler.toml cannot represent is refused before
the attempt, with no change.

Not duplicated here: the scheduler API's unrepresentable-model refusal
(``tests/test_scheduler_toml_encoder.py::test_add_refuses_unrepresentable_model_before_attempt``
and ``test_modify_refuses_unrepresentable_model_before_attempt``) and load-time
silence (``tests/test_scheduler_load_containment.py``).

Only external boundaries (the file system via ``tmp_path``, notify, agent,
the ``schedule`` library's global scheduler) are substituted; the scheduler,
its encoder, and the builtin ``schedule`` tool are real. The saved file is
parsed with the standard-library ``tomllib``, independent of the scheduler.

Section G (task 7.3) proves execution-time classification through the actual
production root: ``main._run`` → real ``Scheduler._run_job`` → root
``BuiltinExecutor`` spawn/supervisor → main-produced factory →
``ModelRegistry.admit_scheduled`` → runtime/runner/client, driven by
``tests/root_startup_harness.py`` in its opt-in real-scheduler mode.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import MagicMock

import pytest
import schedule

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_mod
from agent_runtime import RuntimeProfile
from builtin_tools.context_io import _context_path, scheduled_context_key
from builtin_tools.schedule import exec_schedule
from model_registry import bounded_selector_evidence
from outcome_utils import read_execution
from scheduler import Scheduler
from sub_agent_registry import SOURCE_SCHEDULED
from tests.root_startup_harness import (
    ACME_HOST,
    GEM_HOST,
    UNCONFIGURED_ID,
    MAIN_ID,
    OTHER_ID,
    SCHEDULED_TAG,
    SECRETS,
    RootHarness,
    ScheduledRootProbe,
    run_scheduled_root,
)
from xdg import XDGPaths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paths_for(state_dir: Path) -> XDGPaths:
    """Build an XDGPaths rooted at *state_dir* without touching real XDG env vars."""
    logs_dir = state_dir / "logs"
    return XDGPaths(
        config_home=state_dir, data_home=state_dir, state_home=state_dir,
        cache_home=state_dir, runtime_dir=state_dir,
        config_file=state_dir / "config.toml",
        model_selection_file=state_dir / "model-selection.toml",
        models_cache_file=state_dir / "models-cache.json",
        models_config_file=state_dir / "models.toml",
        scheduler_config=state_dir / "scheduler.toml",
        memory_file=state_dir / "memory.json",
        graph_memory_db=state_dir / "graph_memory",
        tool_index_file=state_dir / "tool_index.json",
        pid_file=state_dir / "agent.pid",
        secrets_file=state_dir / "secrets.toml",
        mcp_tokens_dir=state_dir / "mcp_tokens",
        logs_dir=logs_dir,
        log_file=logs_dir / "agent.log",
        log_store=logs_dir / "agent_logs.sqlite",
        skills_dir=state_dir / "skills",
        scheduler_state=state_dir / "scheduler_state",
        scheduler_commands=state_dir / "scheduler_commands",
        scheduler_jobs=state_dir / "scheduler_jobs",
        job_execution_log=state_dir / "job_execution_log",
    )


def _strict_equal(left: object, right: object) -> bool:
    """Type-strict equality: a boolean is not an integer, an int is not a float."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _strict_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(map(_strict_equal, left, right))
    return left == right


def _assert_strict_equal(actual: object, expected: object) -> None:
    """Assert type-strict equality with a readable failure message."""
    assert _strict_equal(actual, expected), f"\nactual:   {actual!r}\nexpected: {expected!r}"


def _saved_jobs(toml_path: Path) -> dict:
    """Parse the saved scheduler file with the standard library, independently."""
    return tomllib.loads(toml_path.read_text(encoding="utf-8"))["jobs"]


def _listed(sched: Scheduler, tag: str) -> dict:
    """Return the public list entry for *tag*."""
    return next(job for job in sched.list_jobs() if job["tag"] == tag)


# Far-future cron; the disabled scheduler config keeps the loop stopped.
_BASE_TOML = """\
[jobs.healthy]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "healthy job"
notify = false
model = "provider/model-a"
"""

_CURRENT_MODEL = "provider/model-a"


def _tool_add(sched: Scheduler, model: object) -> dict:
    """Add the ``fresh`` job through the real ``schedule`` tool with *model*."""
    return exec_schedule(sched, {
        "action": "add", "tag": "fresh", "task": "fresh task",
        "cron": "0 3 1 1 *", "notify": False, "model": model,
    })


def _tool_modify(sched: Scheduler, model: object) -> dict:
    """Modify the ``healthy`` job's model through the real ``schedule`` tool."""
    return exec_schedule(sched, {"action": "modify", "tag": "healthy", "model": model})


def _core_add(sched: Scheduler, **model_kwargs: object) -> dict:
    """Add the ``fresh`` job through the scheduler API; *model_kwargs* may omit model."""
    return sched.add_job(
        tag="fresh", schedule_type="cron", task="fresh task", cron="0 3 1 1 *",
        notify=False, _on_attempt=MagicMock(), **model_kwargs,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_schedule(monkeypatch: pytest.MonkeyPatch) -> schedule.Scheduler:
    """Replace the external library's global scheduler (and its ``jobs`` alias)."""
    isolated = schedule.Scheduler()
    monkeypatch.setattr(schedule, "default_scheduler", isolated)
    monkeypatch.setattr(schedule, "jobs", isolated.jobs)
    return isolated


@pytest.fixture
def make_scheduler(
    tmp_path: Path, isolated_schedule: schedule.Scheduler,
) -> Iterator[Callable[[], tuple[Scheduler, Path]]]:
    """Construct a real disabled Scheduler over ``_BASE_TOML`` in ``tmp_path``."""
    assert schedule.default_scheduler is isolated_schedule
    created: list[Scheduler] = []

    def _make() -> tuple[Scheduler, Path]:
        toml_path = tmp_path / "scheduler.toml"
        toml_path.write_text(_BASE_TOML, encoding="utf-8")
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        sched = Scheduler(
            config={"scheduler": {"enabled": False}, "agent": {"long_run_warn_minutes": 0}},
            notify_fn=MagicMock(),
            agent_fn=MagicMock(),
            paths=_paths_for(state_dir),
            scheduler_config_path=str(toml_path),
        )
        created.append(sched)
        return sched, toml_path

    yield _make
    for sched in created:
        if sched._thread is not None:
            sched.stop()


# ---------------------------------------------------------------------------
# A. Tool add stores the raw model exactly
# ---------------------------------------------------------------------------

_TOOL_ADD_MODELS = [
    pytest.param("", id="blank_string"),
    pytest.param("   ", id="whitespace_string"),
    pytest.param(42, id="integer"),
    pytest.param(3.14, id="float"),
    pytest.param(True, id="boolean"),
    pytest.param([1, 2], id="list"),
    pytest.param({"provider": "x"}, id="inline_table"),
    pytest.param("no-such-provider/no-such-model", id="unknown_model_string"),
]


@pytest.mark.parametrize("model", _TOOL_ADD_MODELS)
def test_tool_add_stores_raw_model(
    make_scheduler: Callable[[], tuple[Scheduler, Path]], model: object,
) -> None:
    """The saved table and the runtime view carry the supplied value and type."""
    sched, toml_path = make_scheduler()

    out = _tool_add(sched, copy.deepcopy(model))

    assert out["success"] is True, out
    assert read_execution(out) == "executed"
    assert "model" in _saved_jobs(toml_path)["fresh"]
    _assert_strict_equal(_saved_jobs(toml_path)["fresh"]["model"], model)
    _assert_strict_equal(_listed(sched, "fresh")["model"], model)
    assert _listed(sched, "fresh")["config_error"] is None


# ---------------------------------------------------------------------------
# B. Tool modify stores the raw model exactly
# ---------------------------------------------------------------------------

_TOOL_MODIFY_MODELS = [
    pytest.param("", id="blank_string"),
    pytest.param(0, id="zero"),
    pytest.param(False, id="false"),
    pytest.param(42, id="integer"),
    pytest.param([1, 2], id="list"),
    pytest.param({"k": 1}, id="inline_table"),
]


@pytest.mark.parametrize("model", _TOOL_MODIFY_MODELS)
def test_tool_modify_stores_raw_model(
    make_scheduler: Callable[[], tuple[Scheduler, Path]], model: object,
) -> None:
    """A falsy or non-string model replaces the current one verbatim."""
    sched, toml_path = make_scheduler()

    out = _tool_modify(sched, copy.deepcopy(model))

    assert out["success"] is True, out
    assert read_execution(out) == "executed"
    _assert_strict_equal(_saved_jobs(toml_path)["healthy"]["model"], model)
    _assert_strict_equal(sched.show_job("healthy")["model"], model)


# ---------------------------------------------------------------------------
# C. Scheduler add stores a falsy model instead of inheriting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [pytest.param("", id="blank_string"), pytest.param(0, id="zero"),
     pytest.param(False, id="false")],
)
def test_core_add_stores_falsy_model(
    make_scheduler: Callable[[], tuple[Scheduler, Path]], model: object,
) -> None:
    """Only omission or ``None`` inherits; a falsy value is persisted as supplied."""
    sched, toml_path = make_scheduler()

    result = _core_add(sched, model=model)

    assert result == {"success": True}
    assert "model" in _saved_jobs(toml_path)["fresh"]
    _assert_strict_equal(_saved_jobs(toml_path)["fresh"]["model"], model)
    _assert_strict_equal(sched.show_job("fresh")["model"], model)


# ---------------------------------------------------------------------------
# D. Scheduler characterizations: truthy raw values, inherit, no clear
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [pytest.param(42, id="integer"), pytest.param(True, id="boolean"),
     pytest.param([1, 2], id="list")],
)
def test_core_add_stores_truthy_raw_model(
    make_scheduler: Callable[[], tuple[Scheduler, Path]], model: object,
) -> None:
    """A truthy non-string model is persisted without stringification."""
    sched, toml_path = make_scheduler()

    result = _core_add(sched, model=copy.deepcopy(model))

    assert result == {"success": True}
    _assert_strict_equal(_saved_jobs(toml_path)["fresh"]["model"], model)


@pytest.mark.parametrize(
    "model_kwargs",
    [pytest.param({}, id="omitted"), pytest.param({"model": None}, id="none")],
)
def test_core_add_without_model_inherits_silently(
    make_scheduler: Callable[[], tuple[Scheduler, Path]], model_kwargs: dict,
) -> None:
    """An omitted or ``None`` model persists no ``model`` key."""
    sched, toml_path = make_scheduler()

    result = _core_add(sched, **model_kwargs)

    assert result == {"success": True}
    assert "model" not in _saved_jobs(toml_path)["fresh"]
    assert sched.show_job("fresh")["model"] is None


@pytest.mark.parametrize(
    "updates",
    [pytest.param({"task": "new task"}, id="omitted"),
     pytest.param({"task": "new task", "model": None}, id="none")],
)
def test_core_modify_without_model_keeps_current_model(
    make_scheduler: Callable[[], tuple[Scheduler, Path]], updates: dict,
) -> None:
    """An omitted or ``None`` model is no change; there is no clear operation."""
    sched, toml_path = make_scheduler()

    result = sched.modify_job("healthy", updates, _on_attempt=MagicMock())

    assert result == {"success": True}
    assert _saved_jobs(toml_path)["healthy"]["task"] == "new task"
    _assert_strict_equal(_saved_jobs(toml_path)["healthy"]["model"], _CURRENT_MODEL)
    _assert_strict_equal(sched.show_job("healthy")["model"], _CURRENT_MODEL)


# ---------------------------------------------------------------------------
# E. The tool refuses an unrepresentable model before the attempt
# ---------------------------------------------------------------------------

_UNREPRESENTABLE_MODELS = [
    pytest.param([1, None], id="none_in_array"),
    pytest.param({"k": None}, id="none_in_inline_table"),
]


@pytest.mark.parametrize("model", _UNREPRESENTABLE_MODELS)
def test_tool_add_refuses_unrepresentable_model(
    make_scheduler: Callable[[], tuple[Scheduler, Path]], model: object,
) -> None:
    """The add is refused (no attempt), the job is absent, and the file is unchanged."""
    sched, toml_path = make_scheduler()
    before = toml_path.read_bytes()
    jobs_before = copy.deepcopy(sched.list_jobs())

    out = _tool_add(sched, model)

    assert out["success"] is False, out
    assert read_execution(out) == "refused"
    assert isinstance(out["error"], str) and out["error"]
    assert toml_path.read_bytes() == before
    assert sched.list_jobs() == jobs_before


@pytest.mark.parametrize("model", _UNREPRESENTABLE_MODELS)
def test_tool_modify_refuses_unrepresentable_model(
    make_scheduler: Callable[[], tuple[Scheduler, Path]], model: object,
) -> None:
    """The modify is refused (no attempt); the file and the current model are unchanged."""
    sched, toml_path = make_scheduler()
    before = toml_path.read_bytes()
    jobs_before = copy.deepcopy(sched.list_jobs())

    out = _tool_modify(sched, model)

    assert out["success"] is False, out
    assert read_execution(out) == "refused"
    assert isinstance(out["error"], str) and out["error"]
    assert toml_path.read_bytes() == before
    assert sched.list_jobs() == jobs_before
    _assert_strict_equal(sched.show_job("healthy")["model"], _CURRENT_MODEL)


# ---------------------------------------------------------------------------
# F. List/show displays render the bounded selector evidence (task 12.22)
# ---------------------------------------------------------------------------


def test_tool_show_renders_label_for_non_string_model(
    make_scheduler: Callable[[], tuple[Scheduler, Path]],
) -> None:
    """``schedule show`` prints the classification label; the document keeps 123."""
    sched, toml_path = make_scheduler()
    assert _tool_modify(sched, 123)["success"] is True

    out = exec_schedule(sched, {"action": "show", "tag": "healthy"})

    assert out["success"] is True, out
    lines = out["output"].splitlines()
    assert "model: malformed (not a string)" in lines
    assert "model: 123" not in lines
    _assert_strict_equal(_saved_jobs(toml_path)["healthy"]["model"], 123)
    _assert_strict_equal(sched.show_job("healthy")["model"], 123)


def test_jobs_list_renders_label_for_non_string_model(
    make_scheduler: Callable[[], tuple[Scheduler, Path]],
) -> None:
    """The ``/jobs`` rendering of list metadata shows the label, contents never."""
    from telegram_formatter import format_jobs_list

    sched, toml_path = make_scheduler()
    assert _tool_modify(sched, {"QZXkey": "QZXvalue"})["success"] is True

    rendered = format_jobs_list(sched.list_jobs())

    assert "malformed (not a string)" in rendered
    assert "QZX" not in rendered
    _assert_strict_equal(_saved_jobs(toml_path)["healthy"]["model"], {"QZXkey": "QZXvalue"})
    _assert_strict_equal(_listed(sched, "healthy")["model"], {"QZXkey": "QZXvalue"})


# ---------------------------------------------------------------------------
# G. Task 7.3: the actual root classifies a fired job's raw selector
# ---------------------------------------------------------------------------
# Valid Main is acme/claude-a. ``VAULT_SECRET`` is a synthetic startup-known
# vault value supplied at the vault-read boundary (``main._read_vault_secrets``)
# before the root builds its real selector redactor; the harness's env-only
# provider keys are NOT vault values and so are not redaction needles.

VAULT_SECRET = "QZXvault7f3aSELECTOR-secret"
_ABSENT = object()
_WIRE = {
    MAIN_ID: (ACME_HOST, "/v1/messages", "x-api-key", SECRETS["KEY_ACME"]),
    OTHER_ID: (GEM_HOST, "/v1beta/models/gem-1:generateContent", "x-goog-api-key", SECRETS["KEY_GEM"]),
}


@pytest.fixture
def scheduled_root(
    tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> Iterator[RootHarness]:
    """Real-scheduler root with a synthetic vault value; production assertions precede hygiene."""
    monkeypatch.setattr(main_mod, "_read_vault_secrets", lambda _vault_file: [VAULT_SECRET])
    caplog.set_level(logging.WARNING, logger="model_registry")
    root = RootHarness(tmp_xdg, monkeypatch, real_scheduler=True)
    try:
        yield root
    finally:
        root.release_leftovers()


def _selector_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The registry's emitted scheduled-selector WARN records."""
    return [r for r in caplog.records if getattr(r, "scheduled_selector_warning", False)]


def _assert_scheduled_root_run(
    root: RootHarness, probe: ScheduledRootProbe, *, raw: object, pin_id: str,
) -> None:
    """One admission pinned ``pin_id`` and ran once through the actual root chain."""
    after = probe.after
    forwarded = None if raw is _ABSENT else raw
    _assert_strict_equal(probe.admits, {"admit": [], "admit_scheduled": [forwarded]})
    assert probe.codes == [] and len(probe.pins) == 1
    pin = probe.pins[0]
    assert pin.model.id == pin_id
    assert probe.resolved == [pin.model.provider_id]

    # Admission entry: overlap decided, run ID tracked, last_run persisted, no terminal record.
    (entry,) = probe.at_admission
    assert entry.running is True and len(entry.run_ids) == 1
    assert entry.persisted_last_run and entry.records == []
    (run_id,) = entry.run_ids

    # Scheduled profile, source and job-owned context through the main-produced factory.
    ((profile, options),) = probe.creates
    assert profile is RuntimeProfile.SCHEDULED_AGENT and options.run_model is pin
    key = scheduled_context_key(SCHEDULED_TAG)
    assert (options.label, options.context_key, options.context_payload) == (SCHEDULED_TAG, key, {})
    assert [source for _agent_id, source in probe.registered] == [SOURCE_SCHEDULED]
    (call,) = probe.loop_calls
    assert call.pin is pin
    assert os.path.exists(_context_path(key, root.builtin._data_dir))

    # Wire: one request to the pinned model's own endpoint with its own credential.
    host, path, header, credential = _WIRE[pin_id]
    (post,) = after.provider_posts
    assert after.provider_requests == after.provider_posts
    assert (post.url.host, post.url.path) == (host, path)
    assert post.headers[header] == credential
    if host == ACME_HOST:
        assert json.loads(post.content)["model"] == pin.model.upstream_id

    # Terminal record afterwards, once, under the admitted run ID; tracking released.
    (record,) = after.records
    assert (record["run_id"], record["status"], record["model"]) == (run_id, "done", pin_id)
    assert after.state["last_run"] == entry.persisted_last_run
    assert after.tracked is False and after.active_children == []
    assert after.notices == []

    # Main, settings, revision and the raw scheduler document are unchanged.
    assert after.view == probe.view_before and after.view.active_model_id == MAIN_ID
    assert after.settings == probe.settings_before and after.toml == probe.toml_before
    meta = root.scheduler._jobs_meta[SCHEDULED_TAG]
    if raw is _ABSENT:
        assert meta["model"] is None
        assert "model" not in _saved_jobs(root.paths.scheduler_config)[SCHEDULED_TAG]
    else:
        _assert_strict_equal(meta["model"], raw)

    # Run-owned clients were closed by production before fixture hygiene.
    assert after.llm_clients == 1 and after.llm_closed == [True] and after.http_open == []
    assert root.unexpected == [] and root.open_clients_at_exit == []


_ROOT_SELECTORS = [
    pytest.param(None, _ABSENT, MAIN_ID, None, id="absent"),
    pytest.param(f'"{OTHER_ID}"', OTHER_ID, OTHER_ID, None, id="known"),
    # An unconfigured connection (task 4.1: a configured connection's uncached model is pinned
    # exactly, not substituted; scheduled classification of these inputs is task 8.x).
    pytest.param('"ghost/claude-zzz"', "ghost/claude-zzz", MAIN_ID, "ghost/claude-zzz", id="unknown"),
    pytest.param(f'"{UNCONFIGURED_ID}"', UNCONFIGURED_ID, MAIN_ID, UNCONFIGURED_ID, id="obsolete"),
    pytest.param(f'" {OTHER_ID} "', f" {OTHER_ID} ", MAIN_ID, f" {OTHER_ID} ", id="padded"),
    pytest.param(f'"{OTHER_ID.upper()}"', OTHER_ID.upper(), MAIN_ID, OTHER_ID.upper(), id="case-variant"),
    pytest.param("42", 42, MAIN_ID, "malformed (not a string)", id="integer"),
    pytest.param('""', "", MAIN_ID, "blank", id="blank"),
    pytest.param('"   "', "   ", MAIN_ID, "blank", id="whitespace"),
]


@pytest.mark.parametrize(("literal", "raw", "pin_id", "evidence"), _ROOT_SELECTORS)
def test_root_scheduled_selector_matrix(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    literal: str | None, raw: object, pin_id: str, evidence: str | None,
) -> None:
    """Absent inherits quietly, a known ID pins exactly, and malformed or unknown
    selectors warn once with bounded evidence and inherit Main (no trim/alias)."""
    scheduled_root.serve_provider_chat()

    probe = run_scheduled_root(scheduled_root, monkeypatch, literal)

    _assert_scheduled_root_run(scheduled_root, probe, raw=raw, pin_id=pin_id)
    warnings = _selector_warnings(caplog)
    if evidence is None:
        assert warnings == []
        return
    (warning,) = warnings
    assert warning.levelno == logging.WARNING
    assert evidence in warning.getMessage()
    if not isinstance(raw, str):
        # Non-string content is never stringified, in the text or the arguments.
        assert str(raw) not in warning.getMessage() and str(raw) not in repr(warning.args)


def test_root_scheduled_stale_known_selector_is_pinned_without_warning(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A last-good (stale) catalog entry is still known: pinned exactly, no warning."""
    scheduled_root.serve_provider_chat(failing_discovery_hosts=(GEM_HOST,))

    probe = run_scheduled_root(scheduled_root, monkeypatch, f'"{OTHER_ID}"')

    statuses = {state.provider_id: state.status for state in probe.view_before.providers}
    assert statuses == {"acme": "fresh", "gem": "stale"}
    _assert_scheduled_root_run(scheduled_root, probe, raw=OTHER_ID, pin_id=OTHER_ID)
    assert _selector_warnings(caplog) == []


def test_root_scheduled_unknown_selector_warning_is_bounded_and_redacted(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The emitted WARN uses the bounded representation redacted by the root's own
    redactor; the raw value stays verbatim in the human-edited scheduler file."""
    raw = "ghost/" + "b" * 200 + VAULT_SECRET + "-public-suffix" * 50 + "-TAILQZX"
    scheduled_root.serve_provider_chat()

    probe = run_scheduled_root(scheduled_root, monkeypatch, f'"{raw}"')

    _assert_scheduled_root_run(scheduled_root, probe, raw=raw, pin_id=MAIN_ID)
    redactor = scheduled_root.scheduler.selector_redactor
    assert redactor is not None and VAULT_SECRET not in redactor(raw), "root redactor lacks the vault value"
    evidence = bounded_selector_evidence(raw, redact=redactor)
    assert len(evidence) <= 256 and VAULT_SECRET not in evidence
    (warning,) = _selector_warnings(caplog)
    message = warning.getMessage()
    assert evidence in message
    # The 256 bound applies to the evidence; only a short fixed text may surround it.
    assert len(message) < len(evidence) + 128
    for text in (message, repr(warning.msg), repr(warning.args)):
        for fragment in (VAULT_SECRET, VAULT_SECRET[:12], "TAILQZX"):
            assert fragment not in text
    assert raw in scheduled_root.paths.scheduler_config.read_text(encoding="utf-8")
