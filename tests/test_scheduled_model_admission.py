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
import html
import json
import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import MagicMock

import httpx
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
    INVALID_SAVED,
    UNCONFIGURED_ID,
    MAIN_ID,
    OTHER_ID,
    SCHEDULED_TAG,
    SECRETS,
    RootHarness,
    ScheduledRootProbe,
    run_scheduled_root,
    scheduled_job_toml,
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
    main_id: str | None = MAIN_ID,
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
    host, path, header, credential = _WIRE[MAIN_ID if pin_id.startswith("acme/") else pin_id]
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
    assert after.view == probe.view_before and after.view.active_model_id == main_id
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


_UPSTREAM_PADDED = "acme/ claude-pad "
_ROOT_SELECTORS = [
    pytest.param(None, _ABSENT, MAIN_ID, id="absent"),
    pytest.param(f'"{OTHER_ID}"', OTHER_ID, OTHER_ID, id="known"),
    # A configured connection's uncached model is pinned exactly (task 4.1); classification
    # never consults the cache. Only the connection is whitespace-exact; the upstream is verbatim.
    pytest.param('"acme/claude-zzz"', "acme/claude-zzz", "acme/claude-zzz", id="configured-uncached"),
    pytest.param(f'"{_UPSTREAM_PADDED}"', _UPSTREAM_PADDED, _UPSTREAM_PADDED, id="upstream-whitespace-verbatim"),
]


_ROOT_SELECTOR_RUNS = [
    pytest.param(*param.values, state, id=f"{param.id}-{state}")
    for param in _ROOT_SELECTORS
    for state in ("valid-main", "no-main")
    if not (param.id == "absent" and state == "no-main")  # inheriting needs Main: see failure tests
]


@pytest.mark.parametrize(("literal", "raw", "pin_id", "main_state"), _ROOT_SELECTOR_RUNS)
def test_root_scheduled_selector_matrix(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    literal: str | None, raw: object, pin_id: str, main_state: str,
) -> None:
    """Absent inherits Main quietly and a saved selector is pinned exactly, with no
    warning, no Main (so Main need not exist) and no cache membership."""
    scheduled_root.serve_provider_chat()
    saved = MAIN_ID if main_state == "valid-main" else INVALID_SAVED

    probe = run_scheduled_root(scheduled_root, monkeypatch, literal, saved_default=saved)

    _assert_scheduled_root_run(
        scheduled_root, probe, raw=raw, pin_id=pin_id,
        main_id=MAIN_ID if main_state == "valid-main" else None,
    )
    assert _selector_warnings(caplog) == []


_ROOT_INVALID_SELECTORS = [
    pytest.param('"ghost/claude-zzz"', "ghost/claude-zzz", "unknown_connection", "ghost/claude-zzz", id="unknown"),
    pytest.param(f'"{UNCONFIGURED_ID}"', UNCONFIGURED_ID, "unknown_connection", UNCONFIGURED_ID, id="obsolete"),
    pytest.param(f'" {OTHER_ID} "', f" {OTHER_ID} ", "unknown_connection", f" {OTHER_ID} ", id="padded"),
    pytest.param(f'"{OTHER_ID.upper()}"', OTHER_ID.upper(), "unknown_connection", OTHER_ID.upper(), id="case-variant"),
    pytest.param('"gem-1"', "gem-1", "malformed_selector", "gem-1", id="no-slash"),
    pytest.param('"gem/"', "gem/", "malformed_selector", "gem/", id="empty-upstream"),
    pytest.param('"/gem-1"', "/gem-1", "malformed_selector", "/gem-1", id="empty-connection"),
    pytest.param("42", 42, "malformed_selector", "malformed (not a string)", id="integer"),
    pytest.param("true", True, "malformed_selector", "malformed (not a string)", id="bool"),
    pytest.param("1.5", 1.5, "malformed_selector", "malformed (not a string)", id="float"),
    pytest.param('["QZXlist"]', ["QZXlist"], "malformed_selector", "malformed (not a string)", id="list"),
    pytest.param('{ QZXkey = "QZXvalue" }', {"QZXkey": "QZXvalue"}, "malformed_selector",
                 "malformed (not a string)", id="table"),
    pytest.param('""', "", "malformed_selector", "blank", id="blank"),
    pytest.param('"   "', "   ", "malformed_selector", "blank", id="whitespace"),
]


@pytest.mark.parametrize("main_state", ["valid-main", "no-main"])
@pytest.mark.parametrize(("literal", "raw", "code", "evidence"), _ROOT_INVALID_SELECTORS)
def test_root_scheduled_invalid_selector_fails_only_that_job_without_substitution(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    literal: str, raw: object, code: str, evidence: str, main_state: str,
) -> None:
    """A blank, non-string, malformed or unknown-connection saved selector is a normal
    launch failure of that job (record + direct alert, bounded evidence): no warning,
    no Main inheritance, no credential/runtime/client/provider work, nothing rewritten."""
    scheduled_root.serve_provider_chat()
    saved = MAIN_ID if main_state == "valid-main" else INVALID_SAVED

    probe = run_scheduled_root(scheduled_root, monkeypatch, literal, saved_default=saved)

    after = probe.after
    _assert_strict_equal(probe.admits, {"admit": [], "admit_scheduled": [raw]})
    assert probe.codes == [code] and probe.resolved == [] and probe.pins == []
    assert probe.creates == [] and probe.registered == [] and probe.loop_calls == []
    assert _selector_warnings(caplog) == []
    (entry,) = probe.at_admission
    assert entry.running is True and len(entry.run_ids) == 1 and entry.records == []
    (run_id,) = entry.run_ids
    (record,) = after.records
    assert (record["run_id"], record["status"], record["success"], record["model"]) == (
        run_id, "failed", False, evidence,
    )
    (alert,) = after.notices  # notify = false: the direct launch-failure alert still goes out
    assert f"Job {SCHEDULED_TAG} failed to spawn" in alert and f"Model: {html.escape(evidence)}" in alert
    # Non-string content is never stringified: only the typed label is shown, and the
    # canary payloads inside list/table values appear nowhere (record, alert, logs).
    (model_line,) = [line for line in alert.splitlines() if line.startswith("Model: ")]
    assert model_line == f"Model: {html.escape(evidence)}"
    for text in (record["result"], alert, caplog.text, json.dumps(after.state)):
        assert "QZXlist" not in text and "QZXvalue" not in text and "QZXkey" not in text
    assert after.state["last_error"] == record["result"]
    assert after.tracked is False and after.active_children == []
    assert after.llm_clients == 0 and after.http_clients == 0 and after.provider_requests == []
    assert after.view == probe.view_before and after.settings == probe.settings_before
    assert after.view.active_model_id == (MAIN_ID if main_state == "valid-main" else None)
    assert after.toml == probe.toml_before  # the raw saved value is never rewritten
    assert SCHEDULED_TAG in scheduled_root.scheduler._jobs_meta, "the job must stay registered"
    assert scheduled_root.unexpected == [] and scheduled_root.open_clients_at_exit == []


def test_root_scheduled_failing_selector_does_not_disturb_a_healthy_sibling(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """One job's invalid selector fails only that job: the sibling still completes on
    Main with its own success record and no spurious alert; Main and the TOML stay put."""
    scheduled_root.serve_provider_chat()
    probes: list[ScheduledRootProbe] = []
    toml = scheduled_job_toml('"ghost/claude-zzz"', tag="badjob") + "\n" + scheduled_job_toml(None, tag="goodjob")

    def hold(started: RootHarness) -> None:
        """Fire the failing job, then the healthy one, each once."""
        started.registry.refresh()
        for tag in ("badjob", "goodjob"):
            probe = ScheduledRootProbe(started, monkeypatch, tag=tag)
            probes.append(probe)
            probe.fire()

    scheduled_root.start(saved_default=MAIN_ID, hold=hold, scheduler_toml=toml)

    bad, good = probes
    (bad_record,) = bad.after.records
    assert (bad_record["status"], bad_record["model"]) == ("failed", "ghost/claude-zzz")
    assert len(bad.after.notices) == 1
    (good_record,) = good.after.records
    assert (good_record["status"], good_record["success"], good_record["model"]) == ("done", True, MAIN_ID)
    assert good.after.notices == [] and good.after.tracked is False
    assert [pin.model.id for pin in good.pins] == [MAIN_ID]
    assert good.after.view == good.view_before and good.view_before.active_model_id == MAIN_ID
    assert good.after.toml == bad.toml_before
    assert _selector_warnings(caplog) == []
    assert scheduled_root.unexpected == [] and scheduled_root.open_clients_at_exit == []


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


def test_root_scheduled_unknown_selector_failure_evidence_is_bounded_and_redacted(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The failed record and the direct alert carry the bounded evidence redacted by the
    root's own redactor (and the alert escapes HTML); the raw value stays verbatim in the
    human-edited scheduler file."""
    raw = "ghost/<b>" + "b" * 200 + VAULT_SECRET + "-public-suffix" * 50 + "-TAILQZX"
    scheduled_root.serve_provider_chat()

    probe = run_scheduled_root(scheduled_root, monkeypatch, f'"{raw}"')

    redactor = scheduled_root.scheduler.selector_redactor
    assert redactor is not None and VAULT_SECRET not in redactor(raw), "root redactor lacks the vault value"
    evidence = bounded_selector_evidence(raw, redact=redactor)
    assert len(evidence) <= 256 and VAULT_SECRET not in evidence and "<b>" in evidence
    assert probe.codes == ["unknown_connection"] and probe.resolved == []
    (record,) = probe.after.records
    (alert,) = probe.after.notices
    assert record["model"] == evidence
    (model_line,) = [line for line in alert.splitlines() if line.startswith("Model: ")]
    assert model_line == f"Model: {html.escape(evidence)}"
    assert "<b>" not in model_line and "&lt;b&gt;" in model_line
    # The 256 bound applies to the evidence; only a short fixed text surrounds it in the alert.
    assert len(alert) < 2 * len(html.escape(evidence)) + 512
    for text in (record["model"], record["result"], alert, caplog.text, json.dumps(probe.after.state)):
        for fragment in (VAULT_SECRET, VAULT_SECRET[:12], "TAILQZX"):
            assert fragment not in text
    assert _selector_warnings(caplog) == []
    assert raw in scheduled_root.paths.scheduler_config.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# H. Task 8.2: one root admission, no live recheck, no fallback, per-job failure isolation
# ---------------------------------------------------------------------------

_PROVIDER_CANARY = "QZXprovider-body-canary-8-2"
_ACME_CHAT_PATH = "/v1/messages"
_UNCACHED_ID = "acme/claude-zzz"


def test_root_scheduled_pin_survives_main_switch_and_catalog_refresh_after_admission(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """After the one root admission, switching Main and refreshing a changed catalog while the run
    is in flight cannot re-resolve, re-admit or redirect it: the one request still goes to the
    pinned model's own endpoint with its own credential, through the one pinned client."""
    scheduled_root.serve_provider_chat()
    probes: list[ScheduledRootProbe] = []
    seen: dict[str, object] = {}

    def hold(started: RootHarness) -> None:
        """Fire once; inside the run (after admission) change Main and the catalog."""
        started.registry.refresh()
        probe = ScheduledRootProbe(started, monkeypatch)
        probes.append(probe)
        chat_loop = probe._loop

        def loop(ctx: object, goal: str, progress_callback: object = None, images: object = None, **kw: object) -> str:
            """Disturb Main, the catalog and the credentials, then send the pinned request."""
            registry = started.registry
            seen["admissions_at_run"] = (list(probe.admits["admit"]), list(probe.admits["admit_scheduled"]), list(probe.resolved))
            registry.select(MAIN_ID.replace("claude-a", "claude-b"))  # Main moves to another model
            report = registry.refresh()  # a deliberate /models refresh while the run is in flight
            seen["refreshed"] = report.refreshed_provider_ids
            monkeypatch.setenv("KEY_GEM", "rotated-after-admission")  # the run keeps its pinned credential
            seen["main_after_switch"] = registry.view().active_model_id
            seen["resolved_after_disturbance"] = list(probe.resolved)  # discovery resolves its own credentials
            return chat_loop(ctx, goal, progress_callback, images, **kw)

        monkeypatch.setattr(probe, "_loop", loop)
        probe.fire()

    scheduled_root.start(saved_default=MAIN_ID, hold=hold, scheduler_toml=scheduled_job_toml(f'"{OTHER_ID}"'))

    (probe,) = probes
    assert seen["admissions_at_run"] == ([], [OTHER_ID], ["gem"])
    assert seen["refreshed"] and seen["main_after_switch"] == "acme/claude-b"
    # Still exactly one admission and one credential resolution, and the pin was not replaced.
    _assert_strict_equal(probe.admits, {"admit": [], "admit_scheduled": [OTHER_ID]})
    # The run itself resolved nothing after the disturbance (the refresh's own discovery resolutions
    # precede it); the pinned client sent the request with the credential resolved at admission.
    assert probe.resolved == seen["resolved_after_disturbance"] and probe.resolved[0] == "gem"
    assert len(probe.pins) == 1 and len(probe.creates) == 1
    (post,) = probe.after.provider_posts
    assert (post.url.host, post.url.path, post.headers["x-goog-api-key"]) == (
        GEM_HOST, "/v1beta/models/gem-1:generateContent", SECRETS["KEY_GEM"],
    )
    (record,) = probe.after.records
    assert (record["status"], record["success"], record["model"]) == ("done", True, OTHER_ID)
    assert probe.after.llm_clients == 1 and probe.after.tracked is False
    assert probe.after.view.active_model_id == "acme/claude-b"  # the operator's change, not the run's
    assert _selector_warnings(caplog) == []


def _reject_provider_requests(root: RootHarness, predicate: Callable[[httpx.Request], bool], response: httpx.Response) -> None:
    """Answer chat requests selected by ``predicate`` with ``response``; delegate everything else."""
    served = root._route

    def route(request: httpx.Request) -> httpx.Response:
        """Reject the selected provider chat request, recording it like the harness does."""
        if request.method == "POST" and request.url.host in (ACME_HOST, GEM_HOST):
            request.read()
            if predicate(request):
                root.requests.append(request)
                return response
        return served(request)

    root.monkeypatch.setattr(root, "_route", route)


def _is_acme_upstream(request: httpx.Request, upstream: str) -> bool:
    """True for an acme chat request whose body names ``upstream`` exactly."""
    return request.url.host == ACME_HOST and json.loads(request.content).get("model") == upstream


# Provider-side rejections are proven through the canonical react_loop below; this launch-failure
# (exception) path needs no loop at all because the credential fails before any client exists.
_ISOLATION_CASES = [
    pytest.param(OTHER_ID, "credential-unresolvable", id="credential-unresolvable"),
]


@pytest.mark.parametrize(("selector", "failure"), _ISOLATION_CASES)
def test_root_scheduled_failure_is_recorded_for_that_job_only_and_never_falls_back(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    selector: str, failure: str,
) -> None:
    """A provider rejection of an uncached configured upstream, a provider context rejection and an
    unresolvable credential each fail ONLY the pinned job, once, with the pinned selector in its
    record; a healthy sibling on the same Scheduler completes on Main; Main and preferences stay."""
    scheduled_root.serve_provider_chat()
    if failure == "provider-rejects-uncached-upstream":
        _reject_provider_requests(
            scheduled_root, lambda r: _is_acme_upstream(r, "claude-zzz"),
            httpx.Response(404, json={"error": {"message": f"model not found {_PROVIDER_CANARY}"}}),
        )
    elif failure == "provider-context-rejection":
        _reject_provider_requests(
            scheduled_root, lambda r: r.url.host == GEM_HOST,
            httpx.Response(400, json={"error": {"message": f"context_length_exceeded {_PROVIDER_CANARY}"}}),
        )
    probes: list[ScheduledRootProbe] = []
    prefs: dict[str, bytes | None] = {}
    bad_snapshot: dict[str, object] = {}
    toml = scheduled_job_toml(f'"{selector}"', tag="badjob") + "\n" + scheduled_job_toml(None, tag="goodjob")

    def hold(started: RootHarness) -> None:
        """Fire the failing job, then the healthy sibling, each once."""
        started.registry.refresh()
        pref = started.paths.model_selection_file
        prefs["before"] = pref.read_bytes() if pref.exists() else None
        if failure == "credential-unresolvable":
            monkeypatch.delenv("KEY_GEM")
        for tag in ("badjob", "goodjob"):
            probe = ScheduledRootProbe(started, monkeypatch, tag=tag)
            probes.append(probe)
            probe.fire()
            if tag == "badjob":  # the sibling's probe wraps the same spies: snapshot first
                bad_snapshot.update(
                    admits={k: list(v) for k, v in probe.admits.items()}, resolved=list(probe.resolved),
                    codes=list(probe.codes), pins=list(probe.pins), creates=list(probe.creates),
                )
        prefs["after"] = pref.read_bytes() if pref.exists() else None

    scheduled_root.start(saved_default=MAIN_ID, hold=hold, scheduler_toml=toml)

    bad, good = probes
    provider = selector.split("/", 1)[0]
    # The failing job: one admission of the exact saved selector, never Main.
    _assert_strict_equal(bad_snapshot["admits"], {"admit": [], "admit_scheduled": [selector]})
    assert bad_snapshot["resolved"] == [provider]
    entry = bad.at_admission[0]  # the sibling's later probe also observes this registry
    (run_id,) = entry.run_ids
    (record,) = bad.after.records
    assert (record["run_id"], record["status"], record["success"], record["model"]) == (run_id, "failed", False, selector)
    assert bad.after.tracked is False and bad.after.active_children == []
    if failure == "credential-unresolvable":
        assert bad_snapshot["codes"] == ["credential_resolution_failed"] and bad_snapshot["pins"] == [] and bad_snapshot["creates"] == []
        assert bad.after.llm_clients == 0 and bad.after.provider_requests == []
        assert len(bad.after.notices) == 1 and f"Model: {selector}" in bad.after.notices[0]
    else:
        assert bad_snapshot["codes"] == [] and [pin.model.id for pin in bad_snapshot["pins"]] == [selector] and len(bad_snapshot["creates"]) == 1
        assert bad.after.llm_clients == 1 and bad.after.llm_closed == [True] and bad.after.http_open == []
        assert bad.after.notices == []  # notify = false: a run failure alerts only through its record
        (post,) = bad.after.provider_posts  # exactly one attempt, on the pinned model's provider
        assert post.url.host == (ACME_HOST if provider == "acme" else GEM_HOST)
        if provider == "acme":
            assert json.loads(post.content)["model"] == "claude-zzz"  # the exact upstream, not Main's
            assert post.headers["x-api-key"] == SECRETS["KEY_ACME"]
        assert _PROVIDER_CANARY not in record["result"] and _PROVIDER_CANARY not in caplog.text
    assert record["result"] and _PROVIDER_CANARY not in json.dumps(bad.after.state)
    # The healthy sibling: completes on Main with its own record; no alert; nothing of Main changed.
    (good_record,) = good.after.records
    assert (good_record["status"], good_record["success"], good_record["model"]) == ("done", True, MAIN_ID)
    assert good.after.notices == [] and good.after.tracked is False
    assert [pin.model.id for pin in good.pins][-1] == MAIN_ID and good.resolved[-1] == "acme"
    assert good.after.view == good.view_before and good.view_before.active_model_id == MAIN_ID
    assert good.after.settings == bad.settings_before and good.after.toml == bad.toml_before
    assert prefs["before"] == prefs["after"]
    assert scheduled_root.unexpected == [] and scheduled_root.open_clients_at_exit == []


_FINISH = "canonical-finish-8-2"


def _finish_acme_chats(root: RootHarness) -> None:
    """Give every successful acme chat a valid finish action (transport shape only)."""
    served = root._route

    def route(request: httpx.Request) -> httpx.Response:
        """Answer acme chat with the real finish-action schema; delegate everything else."""
        if request.method == "POST" and request.url.host == ACME_HOST and request.url.path == _ACME_CHAT_PATH:
            response = served(request)
            if response.status_code == 200:
                return httpx.Response(200, json={
                    "content": [{"type": "text", "text": json.dumps({"action": "finish", "result": _FINISH})}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                })
            return response
        return served(request)

    root.monkeypatch.setattr(root, "_route", route)


def _canonical_probe(started: RootHarness, monkeypatch: pytest.MonkeyPatch, tag: str) -> ScheduledRootProbe:
    """A probe whose fire runs the REAL ``react_loop`` (the harness patches ``self._loop`` in)."""
    from react_loop import react_loop as canonical_react_loop

    probe = ScheduledRootProbe(started, monkeypatch, tag=tag)
    monkeypatch.setattr(probe, "_loop", canonical_react_loop)
    return probe


_CANONICAL_REJECTIONS = [
    pytest.param(
        _UNCACHED_ID, lambda r: _is_acme_upstream(r, "claude-zzz"), 404, "model not found", "Provider rejected", id="uncached-upstream-404",
    ),
    pytest.param(
        OTHER_ID, lambda r: r.url.host == GEM_HOST, 400, "context_length_exceeded", "Context too long", id="context-400",
    ),
]


@pytest.mark.parametrize(("selector", "predicate", "status", "provider_text", "sanitized"), _CANONICAL_REJECTIONS)
def test_root_scheduled_provider_rejection_through_canonical_react_loop_fails_only_that_job(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    selector: str, predicate: Callable[[httpx.Request], bool], status: int, provider_text: str, sanitized: str,
) -> None:
    """The REAL react_loop turns a provider rejection into a sanitized ``❌`` NORMAL return (no exception
    reaches the supervisor); the scheduler must still classify it as THAT job's failed run: one pinned
    attempt, no fallback to Main, run released, healthy sibling finishing through the same loop, and
    Main/preferences/TOML/settings unchanged."""
    root = scheduled_root
    root.serve_provider_chat()
    _finish_acme_chats(root)
    _reject_provider_requests(
        root, predicate, httpx.Response(status, json={"error": {"message": f"{provider_text} {_PROVIDER_CANARY}"}}),
    )
    probes: list[ScheduledRootProbe] = []
    prefs: dict[str, bytes | None] = {}
    bad_snapshot: dict[str, object] = {}
    toml = scheduled_job_toml(f'"{selector}"', tag="badjob") + "\n" + scheduled_job_toml(None, tag="goodjob")

    def hold(started: RootHarness) -> None:
        """Fire the rejected job, then the healthy sibling, each once through the canonical loop."""
        started.registry.refresh()
        pref = started.paths.model_selection_file
        prefs["before"] = pref.read_bytes() if pref.exists() else None
        for tag in ("badjob", "goodjob"):
            probe = _canonical_probe(started, monkeypatch, tag)
            probes.append(probe)
            probe.fire()
            if tag == "badjob":  # the sibling's probe wraps the same spies: snapshot first
                bad_snapshot.update(
                    admits={k: list(v) for k, v in probe.admits.items()}, resolved=list(probe.resolved),
                    codes=list(probe.codes), pins=list(probe.pins), creates=list(probe.creates),
                )
        prefs["after"] = pref.read_bytes() if pref.exists() else None

    root.start(saved_default=MAIN_ID, hold=hold, scheduler_toml=toml)

    bad, good = probes
    provider = selector.split("/", 1)[0]
    # The loop really ran and returned (the harness chat boundary never did): nothing but production code.
    assert bad.loop_calls == [] and good.loop_calls == []
    # One admission of the exact saved selector, one pin, one runtime, one pinned client; no Main fallback.
    _assert_strict_equal(bad_snapshot["admits"], {"admit": [], "admit_scheduled": [selector]})
    assert bad_snapshot["codes"] == [] and bad_snapshot["resolved"] == [provider]
    assert [pin.model.id for pin in bad_snapshot["pins"]] == [selector] and len(bad_snapshot["creates"]) == 1
    entry = bad.at_admission[0]
    (run_id,) = entry.run_ids
    (post,) = bad.after.provider_posts  # exactly one provider attempt; the sibling's request is separate
    assert post.url.host == (ACME_HOST if provider == "acme" else GEM_HOST)
    if provider == "acme":
        assert json.loads(post.content)["model"] == "claude-zzz" and post.headers["x-api-key"] == SECRETS["KEY_ACME"]
    # NORMAL-RETURN classification: the done result starts with the sanitized ❌, the record is failed.
    (record,) = bad.after.records
    assert (record["run_id"], record["status"], record["success"], record["model"]) == (run_id, "failed", False, selector)
    assert record["result"].startswith("❌") and sanitized in record["result"]
    assert _PROVIDER_CANARY not in record["result"] and _PROVIDER_CANARY not in caplog.text
    assert _PROVIDER_CANARY not in json.dumps(bad.after.state)
    assert bad.after.state["last_run"] == entry.persisted_last_run
    assert bad.after.notices == []  # notify = false
    assert bad.after.tracked is False and bad.after.active_children == []
    assert bad.after.llm_clients == 1 and bad.after.llm_closed == [True] and bad.after.http_open == []
    # The healthy sibling finishes through the same canonical loop on Main with a real finish action.
    (good_record,) = good.after.records
    assert (good_record["status"], good_record["success"], good_record["model"]) == ("done", True, MAIN_ID)
    assert good_record["result"] == _FINISH and good.after.notices == [] and good.after.tracked is False
    (good_post,) = good.after.provider_posts
    assert json.loads(good_post.content)["model"] == "claude-a" and good_post.url.host == ACME_HOST
    assert good.after.view == good.view_before and good.view_before.active_model_id == MAIN_ID
    assert good.after.settings == bad.settings_before and good.after.toml == bad.toml_before
    assert prefs["before"] == prefs["after"]
    assert root.unexpected == [] and root.open_clients_at_exit == []


def test_root_scheduled_provider_rejection_sends_one_sanitized_notice_without_main(
    scheduled_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``notify = true`` the canonical loop's sanitized ``❌`` normal return still produces exactly one
    failure notice for that job (no provider body text, no Main identity); one request, no second attempt."""
    root = scheduled_root
    root.serve_provider_chat()
    _reject_provider_requests(
        root, lambda r: _is_acme_upstream(r, "claude-zzz"),
        httpx.Response(404, json={"error": {"message": f"model not found {_PROVIDER_CANARY}"}}),
    )
    toml = scheduled_job_toml(f'"{_UNCACHED_ID}"').replace("notify = false\n", "notify = true\n")
    probes: list[ScheduledRootProbe] = []

    def hold(started: RootHarness) -> None:
        """Fire the notifying job once through the canonical loop."""
        started.registry.refresh()
        probe = _canonical_probe(started, monkeypatch, SCHEDULED_TAG)
        probes.append(probe)
        probe.fire()

    root.start(saved_default=MAIN_ID, hold=hold, scheduler_toml=toml)

    (probe,) = probes
    (record,) = probe.after.records
    assert (record["status"], record["success"], record["model"]) == ("failed", False, _UNCACHED_ID)
    assert record["result"].startswith("❌")
    (notice,) = probe.after.notices
    assert "Scheduled job failed: nightly" in notice and "Provider rejected" in notice
    assert MAIN_ID not in notice and _PROVIDER_CANARY not in notice
    (post,) = probe.after.provider_posts
    assert json.loads(post.content)["model"] == "claude-zzz"
    assert probe.after.tracked is False and probe.after.llm_clients == 1
