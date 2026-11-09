"""Unit tests for catalog-derived hard request budgets."""

import pytest

from model_registry import ModelMetadata
from model_request_budget import ModelRequestBudget, ModelRequestBudgetError, compute_request_budget


def budget(
    metadata: ModelMetadata,
    *,
    input_tokens: int = 100,
    local_context_tokens: int = 12_000,
    **kwargs: object,
) -> ModelRequestBudget:
    """Keep test cases focused on the metadata matrix."""
    return compute_request_budget(
        metadata,
        input_tokens=input_tokens,
        local_context_tokens=local_context_tokens,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (ModelMetadata(context_tokens=20_000, output_tokens=4_000), 16_000),
        (ModelMetadata(input_tokens=1_000_000, output_tokens=65_536), 1_000_000),
        (
            ModelMetadata(context_tokens=20_000, input_tokens=18_000, output_tokens=4_000),
            16_000,
        ),
        (ModelMetadata(), 10_976),
        (ModelMetadata(context_tokens=8_192), 7_168),
        (ModelMetadata(context_tokens=8_192, output_tokens=8_192), None),
    ],
)
def test_optional_cap_budget_matrix(metadata: ModelMetadata, expected: int | None) -> None:
    if expected is None:
        with pytest.raises(ModelRequestBudgetError):
            budget(metadata)
        return
    result = budget(metadata)
    assert result.hard_input_budget == expected
    assert result.output_cap is None
    assert result.input_tokens == 100


def test_required_cap_uses_remaining_combined_capacity_as_reserve() -> None:
    result = budget(
        ModelMetadata(context_tokens=10_000, output_tokens=4_000),
        input_tokens=7_000,
        output_cap_required=True,
    )
    assert result.output_cap == 3_000
    assert result.completion_reserve == 3_000
    assert result.hard_input_budget == 7_000


@pytest.mark.parametrize("input_tokens", [10_000, 10_001], ids=["C-I=0", "C-I<0"])
def test_required_cap_without_positive_remaining_combined_capacity_fails(input_tokens: int) -> None:
    with pytest.raises(ModelRequestBudgetError) as raised:
        budget(
            ModelMetadata(context_tokens=10_000, output_tokens=4_000),
            input_tokens=input_tokens,
            output_cap_required=True,
        )
    assert raised.value.code == "no_input_budget"


def test_required_cap_for_input_only_model_does_not_reduce_input_budget() -> None:
    result = budget(
        ModelMetadata(input_tokens=1_000_000, output_tokens=65_536),
        output_cap_required=True,
    )
    assert result.output_cap == 65_536
    assert result.hard_input_budget == 1_000_000


@pytest.mark.parametrize(
    "metadata",
    [ModelMetadata(), ModelMetadata(context_tokens=10_000)],
)
def test_required_cap_rejects_missing_combined_output_metadata(metadata: ModelMetadata) -> None:
    with pytest.raises(ModelRequestBudgetError):
        budget(metadata, output_cap_required=True)


def test_unknown_optional_output_uses_assumed_local_reserve_only_with_combined_capacity() -> None:
    result = budget(ModelMetadata(context_tokens=8_192))
    assert result.hard_input_budget == 7_168
    assert result.completion_reserve == 1_024
    assert result.assumed_completion_reserve is True
    assert result.output_cap is None


def test_graph_fixed_cap_is_not_lowered_and_can_be_used_without_output_metadata() -> None:
    result = budget(ModelMetadata(context_tokens=10_000), fixed_output_cap=1_024)
    assert result.output_cap == 1_024
    assert result.completion_reserve == 1_024
    assert result.hard_input_budget == 8_976


def test_graph_fixed_cap_fails_when_known_output_ceiling_is_too_small() -> None:
    with pytest.raises(ModelRequestBudgetError):
        budget(ModelMetadata(output_tokens=1_000), fixed_output_cap=1_024)


@pytest.mark.parametrize(
    ("metadata", "input_tokens", "local_context_tokens"),
    [
        (ModelMetadata(context_tokens=100, output_tokens=100), 0, 100),
        (ModelMetadata(context_tokens=100, output_tokens=100), 1, 0),
        (ModelMetadata(context_tokens=True), 0, 100),
        (ModelMetadata(context_tokens=100), True, 100),
    ],
)
def test_invalid_counts_and_limits_fail_closed(
    metadata: ModelMetadata, input_tokens: int, local_context_tokens: int
) -> None:
    with pytest.raises(ModelRequestBudgetError):
        budget(metadata, input_tokens=input_tokens, local_context_tokens=local_context_tokens)


def test_input_over_hard_budget_is_rejected_even_when_floor_would_apply() -> None:
    with pytest.raises(ModelRequestBudgetError):
        budget(ModelMetadata(context_tokens=200, output_tokens=100), input_tokens=101)


def test_threshold_helper_preserves_floor_without_claiming_it_is_hard_fit() -> None:
    from model_request_budget import compaction_threshold

    assert compaction_threshold(100) == 256
    assert compaction_threshold(1_000) == 850


# --- user-approved 6.1/6.3 slice under task 4.1: configured mandatory output cap ---------------------

def test_configured_required_cap_needs_no_discovered_output_metadata() -> None:
    result = budget(ModelMetadata(), output_cap_required=True, required_output_cap=1_024)
    assert result.output_cap == 1_024 and result.completion_reserve == 1_024
    assert result.assumed_context is True and result.hard_input_budget == 12_000 - 1_024


@pytest.mark.parametrize("output_tokens", [None, 100, 65_536])
def test_configured_required_cap_is_not_sized_from_discovered_limits_or_remaining_context(
    output_tokens: int | None,
) -> None:
    result = budget(
        ModelMetadata(context_tokens=10_000, output_tokens=output_tokens),
        input_tokens=2_000, output_cap_required=True, required_output_cap=777,
    )
    assert result.output_cap == 777 and result.completion_reserve == 777


def test_configured_required_cap_keeps_the_local_input_guard() -> None:
    with pytest.raises(ModelRequestBudgetError) as raised:
        budget(ModelMetadata(context_tokens=10_000), input_tokens=9_500,
               output_cap_required=True, required_output_cap=1_024)
    assert raised.value.code == "input_exceeds_budget"
    with pytest.raises(ModelRequestBudgetError) as raised:
        budget(ModelMetadata(context_tokens=1_000), output_cap_required=True, required_output_cap=1_024)
    assert raised.value.code == "no_input_budget"


def test_configured_required_cap_is_ignored_when_graph_fixed_cap_is_used() -> None:
    result = budget(ModelMetadata(), fixed_output_cap=512, output_cap_required=True, required_output_cap=9_999)
    assert result.output_cap == 512
