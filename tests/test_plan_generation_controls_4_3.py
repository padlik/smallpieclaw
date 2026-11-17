"""Task 4.3: per-call generation controls are rejected in plan arguments.

``max_tokens``/``temperature``/``top_p`` are model settings, not per-call plan
fields.  A plan carrying any of them as a top-level plan or step key (by key
presence, whatever the value) must fail as a whole through the existing
failed-plan containment BEFORE any runner/client is created.  Opaque step
``args`` stay opaque.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import react_loop
from tests.test_execution_disposition import _ctx, _factory

KEYS = ("max_tokens", "temperature", "top_p")
VALUES = (None, 0, False, "", 0.5, "canary-secret-7f3a")


def _run(plan_data: dict):
    calls: list = []
    ctx = _ctx()
    ctx.builtin_executor = SimpleNamespace(_sub_agent_factory=_factory({}, calls))
    flags: list[str] = []
    msg, _, ok = react_loop._run_plan(
        ctx, plan_data, 8, lambda _m: None, on_disposition=flags.append,
    )
    return msg, ok, calls


def _plan(step_extra: dict | None = None, plan_extra: dict | None = None,
          args: dict | None = None) -> dict:
    step = {"id": "s1", "tool": "shell", "args": args if args is not None else {"command": "x"}}
    step.update(step_extra or {})
    plan = {"description": "p", "steps": [step], "timeout": 30}
    plan.update(plan_extra or {})
    return plan


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", VALUES, ids=repr)
@pytest.mark.parametrize("where", ("step", "plan"))
def test_generation_key_presence_fails_plan_before_any_runner(key, value, where):
    data = _plan(step_extra={key: value}) if where == "step" else _plan(plan_extra={key: value})
    msg, ok, calls = _run(data)
    assert ok is False
    assert calls == []
    assert msg.startswith("Plan execution failed")
    assert key in msg
    assert "canary-secret-7f3a" not in msg


def test_all_three_names_reported_together():
    msg, ok, calls = _run(_plan(step_extra={k: None for k in KEYS}))
    assert ok is False and calls == []
    for key in KEYS:
        assert key in msg


def test_valid_plan_still_admitted_control():
    msg, ok, calls = _run(_plan())
    assert ok is True
    assert len(calls) == 1


def test_opaque_step_args_are_not_scanned():
    """Tool args stay opaque (4.2: PlanStep has no model channel)."""
    msg, ok, calls = _run(_plan(args={"command": "x", "temperature": 0.2, "top_p": 1}))
    assert ok is True
    assert len(calls) == 1
