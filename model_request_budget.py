"""Pure hard-input and completion-cap accounting for a pinned model request."""

from __future__ import annotations

from dataclasses import dataclass

from model_registry import ModelMetadata

_UNKNOWN_OUTPUT_RESERVE = 1_024
_SAFE_BUDGET_ERRORS = frozenset(
    {
        "input_exceeds_budget",
        "invalid_limit",
        "invalid_request",
        "no_input_budget",
        "output_cap_unavailable",
        "output_limit_too_small",
    }
)


@dataclass(frozen=True)
class ModelRequestBudget:
    """Immutable report of the hard input budget and any required wire cap."""

    input_tokens: int
    hard_input_budget: int
    output_cap: int | None
    completion_reserve: int
    combined_context: int | None
    input_limit: int | None
    assumed_context: bool
    assumed_completion_reserve: bool


class ModelRequestBudgetError(ValueError):
    """Safe categorized failure raised before a request can reach a provider."""

    def __init__(self, code: str) -> None:
        """Expose a stable category without retaining model or request content."""
        self.code = code if code in _SAFE_BUDGET_ERRORS else "invalid_request"
        super().__init__(f"model request budget {self.code.replace('_', ' ')}")


def compute_request_budget(
    metadata: ModelMetadata,
    *,
    input_tokens: int,
    local_context_tokens: int,
    output_cap_required: bool = False,
    fixed_output_cap: int | None = None,
    required_output_cap: int | None = None,
) -> ModelRequestBudget:
    """Compute hard input fit from documented combined/input/output limits.

    ``fixed_output_cap`` is a caller-owned policy cap (currently graph memory),
    not a discovered provider limit. It is never lowered to fit the model.
    """
    if not isinstance(metadata, ModelMetadata):
        raise ModelRequestBudgetError("invalid_request")
    if type(input_tokens) is not int or input_tokens < 0:
        raise ModelRequestBudgetError("invalid_limit")
    if type(local_context_tokens) is not int or local_context_tokens <= 0:
        raise ModelRequestBudgetError("invalid_limit")
    if type(output_cap_required) is not bool:
        raise ModelRequestBudgetError("invalid_request")
    if fixed_output_cap is not None and (
        type(fixed_output_cap) is not int or fixed_output_cap <= 0
    ):
        raise ModelRequestBudgetError("invalid_limit")

    context = _positive_optional(metadata.context_tokens)
    input_limit = _positive_optional(metadata.input_tokens)
    output_limit = _positive_optional(metadata.output_tokens)
    has_provider_context = context is not None or input_limit is not None
    assumed_context = not has_provider_context
    combined = context if context is not None else (local_context_tokens if assumed_context else None)

    if fixed_output_cap is not None:
        if output_limit is not None and output_limit < fixed_output_cap:
            raise ModelRequestBudgetError("output_limit_too_small")
        output_cap = fixed_output_cap
        reserve = fixed_output_cap
        assumed_reserve = False
    elif output_cap_required and required_output_cap is not None:
        # Configured mandatory cap: sent as-is, never sized from discovered limits,
        # context or input; the local context guard below still applies to it.
        output_cap = required_output_cap
        reserve = required_output_cap
        assumed_reserve = False
    elif output_cap_required:
        if output_limit is None:
            raise ModelRequestBudgetError("output_cap_unavailable")
        output_cap = output_limit
        reserve = output_cap
        assumed_reserve = False
        if combined is not None:
            remaining = combined - input_tokens
            if remaining <= 0:
                raise ModelRequestBudgetError("no_input_budget")
            output_cap = min(output_cap, remaining)
            reserve = output_cap
    else:
        output_cap = None
        if combined is None:
            reserve = 0
            assumed_reserve = False
        elif output_limit is not None:
            reserve = output_limit
            assumed_reserve = False
        else:
            reserve = _UNKNOWN_OUTPUT_RESERVE
            assumed_reserve = True

    candidates: list[int] = []
    if combined is not None:
        candidates.append(combined - reserve)
    if input_limit is not None:
        candidates.append(input_limit)
    if not candidates:
        raise ModelRequestBudgetError("no_input_budget")
    hard_budget = min(candidates)
    if hard_budget <= 0:
        raise ModelRequestBudgetError("no_input_budget")
    if input_tokens > hard_budget:
        raise ModelRequestBudgetError("input_exceeds_budget")

    return ModelRequestBudget(
        input_tokens=input_tokens,
        hard_input_budget=hard_budget,
        output_cap=output_cap,
        completion_reserve=reserve,
        combined_context=combined,
        input_limit=input_limit,
        assumed_context=assumed_context,
        assumed_completion_reserve=assumed_reserve,
    )


def repair_input_ceiling(
    metadata: ModelMetadata,
    *,
    local_context_tokens: int,
    output_cap_required: bool = False,
    fixed_output_cap: int | None = None,
    required_output_cap: int | None = None,
) -> int:
    """Return the largest input that could still fit; a repair target only.

    It is not a compaction threshold and never authorizes a send: the strict
    preview still decides. A required cap with an unknown output limit, an
    impossible reserve, or a non-positive ceiling raises ``ModelRequestBudgetError``.
    """
    if not isinstance(metadata, ModelMetadata):
        raise ModelRequestBudgetError("invalid_request")
    if type(local_context_tokens) is not int or local_context_tokens <= 0:
        raise ModelRequestBudgetError("invalid_limit")
    if type(output_cap_required) is not bool:
        raise ModelRequestBudgetError("invalid_request")
    if fixed_output_cap is not None and (
        type(fixed_output_cap) is not int or fixed_output_cap <= 0
    ):
        raise ModelRequestBudgetError("invalid_limit")

    context = _positive_optional(metadata.context_tokens)
    input_limit = _positive_optional(metadata.input_tokens)
    output_limit = _positive_optional(metadata.output_tokens)
    combined = context
    if context is None and input_limit is None:
        combined = local_context_tokens

    if fixed_output_cap is not None:
        if output_limit is not None and output_limit < fixed_output_cap:
            raise ModelRequestBudgetError("output_limit_too_small")
        reserve = fixed_output_cap
    elif output_cap_required and required_output_cap is not None:
        reserve = required_output_cap
    elif output_cap_required:
        if output_limit is None:
            raise ModelRequestBudgetError("output_cap_unavailable")
        reserve = 1
    elif combined is None:
        reserve = 0
    else:
        reserve = output_limit if output_limit is not None else _UNKNOWN_OUTPUT_RESERVE

    candidates: list[int] = []
    if combined is not None:
        candidates.append(combined - reserve)
    if input_limit is not None:
        candidates.append(input_limit)
    ceiling = min(candidates) if candidates else 0
    if ceiling <= 0:
        raise ModelRequestBudgetError("no_input_budget")
    return ceiling


def ordinary_output_cap_required(adapter: str) -> bool:
    """Return whether an adapter needs an explicit output cap on ordinary requests."""
    return adapter == "anthropic"


def compaction_threshold(hard_input_budget: int) -> int:
    """Return the legacy 85%/256-token heuristic for a positive hard budget."""
    if type(hard_input_budget) is not int or hard_input_budget <= 0:
        raise ModelRequestBudgetError("invalid_limit")
    return max(int(hard_input_budget * 0.85), 256)


def _positive_optional(value: object) -> int | None:
    """Validate optional catalog token ceilings without accepting booleans."""
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ModelRequestBudgetError("invalid_limit")
    return value
