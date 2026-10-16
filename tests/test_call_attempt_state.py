"""Tests for the bounded state of one logical provider request."""

from __future__ import annotations

import copy

import pytest

from call_attempt_state import CallAttemptState, CallAttemptStateError


def run_transient_request(
    state: CallAttemptState,
    *,
    failures: int,
    sleeper: list[float],
) -> int:
    """Drive synthetic transient failures without a provider or real sleep."""
    calls = 0
    while True:
        state.begin_outbound_attempt()
        calls += 1
        if calls > failures:
            return calls
        if not state.can_retry_automatically():
            return calls
        sleeper.append(state.next_retry_delay())


def test_default_budget_allows_five_calls_with_exponential_delays() -> None:
    state = CallAttemptState()
    delays: list[float] = []

    assert run_transient_request(state, failures=5, sleeper=delays) == 5
    assert delays == [2.0, 4.0, 8.0, 16.0]
    assert state.outbound_attempts == 5
    assert state.automatic_retries_used == 4
    assert state.remaining_retries == 0


def test_zero_retry_budget_still_allows_one_initial_call() -> None:
    state = CallAttemptState(max_retries=0)
    delays: list[float] = []

    assert run_transient_request(state, failures=10, sleeper=delays) == 1
    assert delays == []
    assert state.outbound_attempts == 1


def test_local_conversion_and_control_continuations_retain_shared_budget() -> None:
    state = CallAttemptState()
    # A local native-to-JSON conversion does not begin an outbound attempt.
    assert state.outbound_attempts == 0
    state.begin_outbound_attempt("native")
    state.begin_outbound_attempt("json")
    state.begin_outbound_attempt()

    assert state.outbound_attempts == 3
    assert state.automatic_retries_used == 2
    assert state.mode == "json"
    assert state.remaining_retries == 2
    assert state.next_retry_delay() == 8.0


def test_manual_retry_is_one_pending_grant_without_refilling_budget() -> None:
    state = CallAttemptState(max_retries=0)
    state.begin_outbound_attempt()
    with pytest.raises(CallAttemptStateError, match="exhausted"):
        state.begin_outbound_attempt()

    state.authorize_manual_retry()
    with pytest.raises(CallAttemptStateError, match="already"):
        state.authorize_manual_retry()
    state.begin_outbound_attempt(manual=True)
    assert state.outbound_attempts == 2
    assert state.automatic_retries_used == 0
    assert state.remaining_retries == 0
    assert not state.can_retry_automatically()
    with pytest.raises(CallAttemptStateError, match="authorized"):
        state.begin_outbound_attempt(manual=True)


def test_each_deliberate_manual_retry_is_one_call_and_roundtrips_counts() -> None:
    state = CallAttemptState(max_retries=0)
    state.begin_outbound_attempt()
    for _ in range(3):
        state.authorize_manual_retry()
        state.begin_outbound_attempt(manual=True)

    resumed = CallAttemptState.from_dict(state.to_dict())
    assert resumed.manual_retries_used == 3
    assert resumed.outbound_attempts == 4
    assert resumed.automatic_retries_used == 0
    assert resumed.remaining_retries == 0

    resumed.authorize_manual_retry()
    with pytest.raises(CallAttemptStateError, match="already authorized"):
        resumed.authorize_manual_retry()
    saved_pending_retry = CallAttemptState.from_dict(resumed.to_dict())
    saved_pending_retry.begin_outbound_attempt(manual=True)
    assert saved_pending_retry.manual_retries_used == 4
    assert saved_pending_retry.outbound_attempts == 5
    assert saved_pending_retry.remaining_retries == 0


@pytest.mark.parametrize("marker", ["partial", "effect"])
def test_partial_output_or_effect_vetoes_automatic_retry(marker: str) -> None:
    state = CallAttemptState()
    state.begin_outbound_attempt()
    getattr(state, "mark_partial_output" if marker == "partial" else "mark_effect")()

    assert not state.can_retry_automatically()
    with pytest.raises(CallAttemptStateError, match="partial|effect"):
        state.next_retry_delay()


def test_checkpoint_roundtrip_preserves_budget_and_current_call_flags() -> None:
    original = CallAttemptState(retry_delay=1.5)
    original.begin_outbound_attempt("json")
    original.begin_outbound_attempt()
    original.authorize_manual_retry()
    resumed = CallAttemptState.from_dict(original.to_dict())

    assert resumed.to_dict() == original.to_dict()
    assert resumed.remaining_retries == 3
    assert resumed.mode == "json"
    resumed.begin_outbound_attempt("json", manual=True)
    assert resumed.outbound_attempts == 3
    assert resumed.automatic_retries_used == 1
    assert resumed.remaining_retries == 3


@pytest.mark.parametrize(
    "patch,category",
    [
        ({"max_retries": True}, "invalid_checkpoint"),
        ({"retry_delay": float("nan")}, "invalid_checkpoint"),
        ({"mode": "other"}, "invalid_checkpoint"),
        ({"automatic_retries_used": 5}, "invalid_checkpoint"),
        ({"unexpected": 1}, "invalid_checkpoint"),
        ({"manual_retry_authorized": True}, "invalid_checkpoint"),
    ],
)
def test_checkpoint_rejects_tampered_or_invalid_state(patch: dict, category: str) -> None:
    data = CallAttemptState().to_dict()
    data.update(patch)

    with pytest.raises(CallAttemptStateError) as error:
        CallAttemptState.from_dict(data)
    assert error.value.category == category


@pytest.mark.parametrize("field", ["max_retries", "retry_delay", "mode", "outbound_attempts"])
def test_checkpoint_requires_all_critical_fields(field: str) -> None:
    data = CallAttemptState().to_dict()
    del data[field]
    with pytest.raises(CallAttemptStateError) as error:
        CallAttemptState.from_dict(data)
    assert error.value.category == "invalid_checkpoint"


def test_checkpoint_rejects_inconsistent_counters() -> None:
    data = CallAttemptState().to_dict()
    data["outbound_attempts"] = 1
    data["automatic_retries_used"] = 1
    with pytest.raises(CallAttemptStateError, match="counter"):
        CallAttemptState.from_dict(data)


@pytest.mark.parametrize(
    "field,value",
    [("automatic_retries_used", 2), ("manual_retries_used", 1)],
)
def test_checkpoint_rejects_retry_counts_without_initial_outbound_attempt(
    field: str, value: int
) -> None:
    data = CallAttemptState().to_dict()
    data[field] = value

    with pytest.raises(CallAttemptStateError, match="counter"):
        CallAttemptState.from_dict(data)


def test_serialized_state_contains_no_non_state_payload() -> None:
    state = CallAttemptState()
    serialized = state.to_dict()
    assert set(serialized) == {
        "max_retries",
        "retry_delay",
        "mode",
        "outbound_attempts",
        "automatic_retries_used",
        "manual_retries_used",
        "manual_retry_authorized",
        "partial_output",
        "effect",
    }
    assert copy.deepcopy(serialized) == serialized
