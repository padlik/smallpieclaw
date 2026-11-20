"""
Narrow lossless scheduler TOML value encoder (task 12.3, ADR-0037).

Explicit operator mutations patch only the requested fields of the parsed raw
scheduler document and re-emit it through a scheduler-local encoder that covers
exactly the value types ``tomli`` returns. The only preservation guarantee is
parsed-value equality, NaN-aware and type-strict (a boolean is not an integer;
NaN equals NaN); comments and layout are never asserted. Values the encoder
cannot represent are refused before ``_on_attempt`` with no change.

The encoder entry point is the PROPOSED private ``scheduler._encode_toml_document``
(raises ``ValueError`` for an unrepresentable value); it is looked up when each
test runs, so its absence fails tests instead of breaking collection.

"Load never rewrites" stays owned by the 12.2 suite and is not duplicated here:
``tests/test_scheduler_load_containment.py::test_unparseable_file_bytes_survive_startup``,
``test_legacy_schedule_load_leaves_file_untouched``,
``test_reload_of_edited_legacy_schedule_leaves_file_untouched`` and
``test_legacy_schedule_with_unencodable_sibling_keeps_file_bytes``.

Only external boundaries (the file system via ``tmp_path``, notify, agent) are
substituted; the scheduler, its parser, and its encoder are real.
"""

from __future__ import annotations

import copy
import math
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler as scheduler_module
from scheduler import Scheduler
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


def _parse(text: str) -> dict:
    """Parse TOML text with the scheduler's own parser."""
    return scheduler_module.tomli.loads(text)


def _encode(document: dict) -> str:
    """Encode *document* with the scheduler-local encoder (resolved at call time)."""
    return scheduler_module._encode_toml_document(document)


def _toml_equal(left: object, right: object) -> bool:
    """Independent NaN-aware, type-strict equality over parsed TOML values."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _toml_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(map(_toml_equal, left, right))
    if isinstance(left, float):
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    if isinstance(left, datetime):
        return (
            left.utcoffset() == right.utcoffset()
            and left.replace(tzinfo=None) == right.replace(tzinfo=None)
        )
    return left == right


def _assert_toml_equal(actual: object, expected: object) -> None:
    """Assert type-strict NaN-aware equality with a readable failure message."""
    assert _toml_equal(actual, expected), f"\nactual:   {actual!r}\nexpected: {expected!r}"


# A healthy far-future job; disabled scheduler config keeps the loop stopped.
_SIMPLE = """
[jobs.healthy]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "healthy job"
model = "provider/model-a"
"""

# Every tomli value type, unknown fields, legacy fields, raw selectors, quoted
# keys, and a config_error sibling. Comments and layout are not asserted.
_RICH = r'''
# operator comment (not preserved, never asserted)
title = "operator scheduler"
schema_hint = 3

[meta]
owner = "ops"
ratio = nan
ceiling = inf
floor = -inf
since = 1979-05-27

[jobs.healthy]
enabled = true
schedule = "cron"
cron = "0 2 1 1 *"
task = "healthy job"
notify = false
model = "provider/model-a"
custom_int = 1
custom_float = 1.0
weights = [1, 2.5, true, "x", [nan]]
extras = { "dotted.key" = "v", "spaced key" = 'C:\literal', nested = { deep = [1, [2, 3]] } }

[[jobs.healthy.runs]]
at = 1979-05-27T07:32:00Z

[[jobs.healthy.runs]]
at = 1979-05-27T00:32:00.999999-07:00

[jobs."ops.backup job"]
enabled = true
schedule = "daily"
time = "02:00"
task = """line1
line2\ttab \u0001 \u007f \U0001F600"""
model = 42
fallback_models = ["model-b", "model-c"]
local_at = 1979-05-27T07:32:00
wake = 07:32:00.5

[jobs.broken]
enabled = 1
schedule = "cron"
cron = "not a cron"
task = "broken"
model = ["a", "b"]
'''


@pytest.fixture
def make_scheduler(tmp_path: Path) -> Iterator[Callable[[str], tuple[Scheduler, Path]]]:
    """Construct a real disabled Scheduler over a scheduler.toml with given text."""
    created: list[Scheduler] = []

    def _make(text: str) -> tuple[Scheduler, Path]:
        toml_path = tmp_path / "scheduler.toml"
        toml_path.write_text(text, encoding="utf-8")
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        sched = Scheduler(
            config={"scheduler": {"enabled": False}, "agent": {}},
            notify_fn=MagicMock(),
            agent_fn=MagicMock(),
            paths=_paths_for(state_dir),
            scheduler_config_path=str(toml_path),
        )
        created.append(sched)
        return sched, toml_path

    yield _make
    for sched in created:
        sched.stop()


# ---------------------------------------------------------------------------
# Test oracle sanity (guards against a vacuous equality helper)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(True, 1, id="bool_vs_int"),
        pytest.param(1, 1.0, id="int_vs_float"),
        pytest.param(datetime(1979, 5, 27), date(1979, 5, 27), id="datetime_vs_date"),
    ],
)
def test_oracle_rejects_cross_type_equal_values(left: object, right: object) -> None:
    """Python-equal values of different TOML types are not parsed-value equal."""
    assert not _toml_equal(left, right)


def test_oracle_treats_nan_as_equal() -> None:
    """NaN equals NaN under the parsed-value equality."""
    assert _toml_equal([float("nan")], [float("nan")])


# ---------------------------------------------------------------------------
# Encoder roundtrip, type by type
# ---------------------------------------------------------------------------


class TestEncoderRoundtrip:
    """Encoded documents reparse type-strictly equal to the input."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param('say "hi" \\ C:\\path', id="quote_backslash"),
            pytest.param("".join(chr(i) for i in range(32)), id="all_c0_controls"),
            pytest.param("a\x7fb", id="del_control"),
            pytest.param("Grüße 漢字 \U0001F600 \u2028", id="unicode_astral"),
            pytest.param("\"\"\" '''", id="triple_quotes"),
            pytest.param(True, id="bool_true"),
            pytest.param(False, id="bool_false"),
            pytest.param(2**63 - 1, id="int_max64"),
            pytest.param(-(2**63), id="int_min64"),
            pytest.param(0.1, id="float_tenth"),
            pytest.param(-0.0, id="float_negative_zero"),
            pytest.param(1e16, id="float_exponent"),
            pytest.param(float("inf"), id="float_inf"),
            pytest.param(float("-inf"), id="float_neg_inf"),
            pytest.param(float("nan"), id="float_nan"),
            pytest.param(
                datetime(1979, 5, 27, 7, 32, 0, 999999, tzinfo=timezone(timedelta(hours=-8))),
                id="offset_datetime",
            ),
            pytest.param(datetime(1979, 5, 27, 7, 32), id="local_datetime"),
            pytest.param(date(1979, 5, 27), id="local_date"),
            pytest.param(time(7, 32, 0, 500000), id="local_time"),
            pytest.param([[], [1, [2.5, "x"]], [True]], id="nested_arrays"),
            pytest.param([{"a": 1}, {"b": {"c": [float("nan")]}}], id="array_of_tables"),
            pytest.param({"a": {"b": 1}, "": {}, "c.d": "e"}, id="nested_inline_table"),
        ],
    )
    def test_value_roundtrips_type_strictly(self, value: object) -> None:
        """A tomli-returnable value in a job table survives encode then parse."""
        document = {"jobs": {"job": {"field": value}}}
        _assert_toml_equal(_parse(_encode(document)), document)

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param("a.b", id="dot"),
            pytest.param("a b", id="space"),
            pytest.param('say "hi"\t\\', id="quote_tab_backslash"),
            pytest.param("ключ \U0001F600", id="unicode"),
            pytest.param("", id="empty"),
        ],
    )
    def test_quoted_key_roundtrips(self, key: str) -> None:
        """A key needing quotes survives as table name, field key, and value."""
        document = {"jobs": {key: {key: key}}}
        _assert_toml_equal(_parse(_encode(document)), document)

    @pytest.mark.parametrize(
        "document",
        [
            pytest.param(
                {"jobs": {"a": {"task": "t"}}, "version": 1}, id="scalar_after_table",
            ),
            pytest.param(
                {"jobs": {"a": {"extras": {"x": 1}, "task": "t", "runs": [{"at": 1}]}}},
                id="scalar_after_subtable",
            ),
        ],
    )
    def test_document_shape_roundtrips(self, document: dict) -> None:
        """Table/scalar ordering in the parsed dict does not break the document."""
        _assert_toml_equal(_parse(_encode(document)), document)

    def test_parsed_rich_document_roundtrips(self) -> None:
        """A parsed operator file with every tomli value type re-emits equal."""
        document = _parse(_RICH)
        _assert_toml_equal(_parse(_encode(document)), document)


# ---------------------------------------------------------------------------
# Encoder refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(object(), id="arbitrary_object"),
        pytest.param(b"bytes", id="bytes"),
        pytest.param((1, 2), id="tuple"),
        pytest.param([1, None], id="none_in_array"),
        pytest.param([{"k": None}], id="none_in_inline_table"),
        pytest.param({1: "x"}, id="non_string_key"),
    ],
)
def test_encoder_refuses_unrepresentable_value(value: object) -> None:
    """A value tomli could never return is refused, not coerced."""
    with pytest.raises(ValueError):
        _encode({"jobs": {"job": {"field": value}}})


# ---------------------------------------------------------------------------
# Public mutations refuse unrepresentable values before the attempt hook
# ---------------------------------------------------------------------------

_UNREPRESENTABLE_MODELS = [
    pytest.param(object(), id="arbitrary_object"),
    pytest.param(["a", None], id="none_in_array"),
    pytest.param({1: "x"}, id="non_string_key"),
]


@pytest.mark.parametrize("model", _UNREPRESENTABLE_MODELS)
def test_add_refuses_unrepresentable_model_before_attempt(
    make_scheduler: Callable[[str], tuple[Scheduler, Path]], model: object,
) -> None:
    """add_job refuses before _on_attempt and leaves the file unchanged."""
    sched, toml_path = make_scheduler(_SIMPLE)
    before = toml_path.read_bytes()
    hook = MagicMock()

    result = sched.add_job(
        tag="fresh", schedule_type="cron", task="t", cron="0 3 1 1 *",
        model=model, _on_attempt=hook,
    )

    assert (result["success"], hook.call_count, toml_path.read_bytes()) == (False, 0, before)


@pytest.mark.parametrize("model", _UNREPRESENTABLE_MODELS)
def test_modify_refuses_unrepresentable_model_before_attempt(
    make_scheduler: Callable[[str], tuple[Scheduler, Path]], model: object,
) -> None:
    """modify_job refuses before _on_attempt and leaves file and job unchanged."""
    sched, toml_path = make_scheduler(_SIMPLE)
    before = toml_path.read_bytes()
    hook = MagicMock()

    result = sched.modify_job("healthy", {"model": model}, _on_attempt=hook)

    assert (
        result["success"], hook.call_count, toml_path.read_bytes(),
        sched.show_job("healthy")["model"],
    ) == (False, 0, before, "provider/model-a")


@pytest.mark.parametrize(
    "context_kwargs",
    [
        pytest.param(
            {"preserve_context": True, "context_max_messages": object()},
            id="context_max_messages_object",
        ),
        pytest.param(
            {"preserve_context": True, "context_max_messages": [1, None]},
            id="context_max_messages_none_in_array",
        ),
        pytest.param({"preserve_context": object()}, id="preserve_context_object"),
        pytest.param({"preserve_context": [1, None]}, id="preserve_context_none_in_array"),
    ],
)
def test_add_refuses_unrepresentable_context_setting_before_attempt(
    make_scheduler: Callable[[str], tuple[Scheduler, Path]], context_kwargs: dict,
) -> None:
    """add_job refuses a raw context setting before _on_attempt with no change."""
    sched, toml_path = make_scheduler(_SIMPLE)
    before = toml_path.read_bytes()
    hook = MagicMock()

    result = sched.add_job(
        tag="fresh", schedule_type="cron", task="t", cron="0 3 1 1 *",
        _on_attempt=hook, **context_kwargs,
    )

    assert (
        result["success"], hook.call_count, toml_path.read_bytes(),
        [job["tag"] for job in sched.list_jobs()], sched.show_job("fresh"),
    ) == (False, 0, before, ["healthy"], None)


def test_add_refuses_unrepresentable_preserve_context_without_truthiness(
    make_scheduler: Callable[[str], tuple[Scheduler, Path]],
) -> None:
    """add_job refuses a raw preserve_context before evaluating its truthiness."""

    class _NoTruthiness:
        """Unsupported raw value whose truthiness must never be evaluated."""

        bool_calls = 0

        def __bool__(self) -> bool:
            type(self).bool_calls += 1
            raise RuntimeError("fake truthiness probe")

    sched, toml_path = make_scheduler(_SIMPLE)
    before = toml_path.read_bytes()
    hook = MagicMock()

    result = sched.add_job(
        tag="fresh", schedule_type="cron", task="t", cron="0 3 1 1 *",
        preserve_context=_NoTruthiness(), _on_attempt=hook,
    )

    assert (
        result["success"], hook.call_count, toml_path.read_bytes(),
        [job["tag"] for job in sched.list_jobs()], _NoTruthiness.bool_calls,
    ) == (False, 0, before, ["healthy"], 0)


# ---------------------------------------------------------------------------
# Explicit patch pipeline: only requested fields change
# ---------------------------------------------------------------------------


class TestExplicitPatchPreservesDocument:
    """Operator mutations re-emit every untouched parsed value unchanged."""

    def test_modify_task_preserves_every_untouched_value(
        self, make_scheduler: Callable[[str], tuple[Scheduler, Path]],
    ) -> None:
        """Modifying one field of a healthy job changes only that field."""
        sched, toml_path = make_scheduler(_RICH)
        expected = copy.deepcopy(_parse(_RICH))
        expected["jobs"]["healthy"]["task"] = "patched task"

        result = sched.modify_job("healthy", {"task": "patched task"})

        assert result["success"] is True
        _assert_toml_equal(_parse(toml_path.read_text(encoding="utf-8")), expected)

    def test_second_modify_keeps_first_patch(
        self, make_scheduler: Callable[[str], tuple[Scheduler, Path]],
    ) -> None:
        """A later patch applies on top of the previously written document."""
        sched, toml_path = make_scheduler(_RICH)
        expected = copy.deepcopy(_parse(_RICH))
        expected["jobs"]["healthy"]["task"] = "patched task"
        expected["jobs"]["healthy"]["notify"] = True

        sched.modify_job("healthy", {"task": "patched task"})
        sched.modify_job("healthy", {"notify": True})

        _assert_toml_equal(_parse(toml_path.read_text(encoding="utf-8")), expected)

    def test_add_preserves_every_untouched_value(
        self, make_scheduler: Callable[[str], tuple[Scheduler, Path]],
    ) -> None:
        """Adding a job leaves every pre-existing parsed value unchanged."""
        sched, toml_path = make_scheduler(_RICH)
        expected = copy.deepcopy(_parse(_RICH))

        result = sched.add_job(tag="fresh", schedule_type="cron", task="t", cron="0 3 1 1 *")

        written = _parse(toml_path.read_text(encoding="utf-8"))
        assert result["success"] is True
        assert "fresh" in written["jobs"]
        del written["jobs"]["fresh"]
        _assert_toml_equal(written, expected)

    def test_remove_preserves_every_untouched_value(
        self, make_scheduler: Callable[[str], tuple[Scheduler, Path]],
    ) -> None:
        """Removing a TOML job drops only that job's table."""
        sched, toml_path = make_scheduler(_RICH)
        expected = copy.deepcopy(_parse(_RICH))
        del expected["jobs"]["healthy"]

        removed = sched.remove_job("healthy")

        assert removed is True
        _assert_toml_equal(_parse(toml_path.read_text(encoding="utf-8")), expected)

    def test_pause_preserves_every_untouched_value(
        self, make_scheduler: Callable[[str], tuple[Scheduler, Path]],
    ) -> None:
        """Pausing a TOML job changes only its enabled flag to false."""
        sched, toml_path = make_scheduler(_RICH)
        expected = copy.deepcopy(_parse(_RICH))
        expected["jobs"]["healthy"]["enabled"] = False

        paused = sched.pause_job("healthy")

        assert paused is True
        _assert_toml_equal(_parse(toml_path.read_text(encoding="utf-8")), expected)

    def test_resume_preserves_every_untouched_value(
        self, make_scheduler: Callable[[str], tuple[Scheduler, Path]],
    ) -> None:
        """Resuming a disabled TOML job changes only its enabled flag to true."""
        healthy_enabled = 'enabled = true\nschedule = "cron"'
        assert _RICH.count(healthy_enabled) == 1
        disabled_rich = _RICH.replace(healthy_enabled, 'enabled = false\nschedule = "cron"')
        sched, toml_path = make_scheduler(disabled_rich)
        expected = copy.deepcopy(_parse(disabled_rich))
        expected["jobs"]["healthy"]["enabled"] = True

        resumed = sched.resume_job("healthy")

        assert resumed is True
        _assert_toml_equal(_parse(toml_path.read_text(encoding="utf-8")), expected)

    @pytest.mark.parametrize(
        "model_kwargs",
        [pytest.param({}, id="absent"), pytest.param({"model": None}, id="none")],
    )
    def test_add_without_model_omits_model_key(
        self, make_scheduler: Callable[[str], tuple[Scheduler, Path]], model_kwargs: dict,
    ) -> None:
        """An absent or None model on add persists no model key (TOML has no null)."""
        sched, toml_path = make_scheduler(_SIMPLE)

        sched.add_job(
            tag="fresh", schedule_type="cron", task="t", cron="0 3 1 1 *", **model_kwargs,
        )

        assert "model" not in _parse(toml_path.read_text(encoding="utf-8"))["jobs"]["fresh"]

    def test_modify_with_none_model_keeps_stored_model(
        self, make_scheduler: Callable[[str], tuple[Scheduler, Path]],
    ) -> None:
        """A None model in a modify is no change, not a clear operation."""
        sched, toml_path = make_scheduler(_SIMPLE)

        sched.modify_job("healthy", {"task": "t2", "model": None})

        written = _parse(toml_path.read_text(encoding="utf-8"))
        assert written["jobs"]["healthy"]["model"] == "provider/model-a"
