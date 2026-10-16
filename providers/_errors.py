"""
providers/_errors.py
--------------------
LLM provider error hierarchy shared across provider backends.

These exception types classify failures from LLM API calls so the retry helper
(``_with_retry``) and callers can distinguish transient, permanent, empty, and
cancelled conditions. Moved verbatim from ``llm_client.py`` during the provider
extraction refactor; ``llm_client`` re-imports them for backward compatibility.
"""


class LLMEmptyResponseError(RuntimeError):
    """Raised when the LLM provider returns an empty or whitespace-only response."""


class LLMError(RuntimeError):
    """Raised when the LLM provider returns an API-level error (HTTP 200 with error body)."""


class LLMPermanentError(LLMError):
    """
    Raised for API-level errors that should never be retried — e.g. content filter
    violations, invalid API keys, bad request parameters.  Propagates immediately out
    of _with_retry without consuming any retry attempts.
    """


def _safe_token_count(value: object) -> int | None:
    """Keep only a positive plain integer token count; drop anything else."""
    if type(value) is int and value > 0:
        return value
    return None


class LLMContextOverflowError(LLMPermanentError):
    """A request exceeds context capacity, locally or as reported by the provider.

    ``input_tokens`` and ``repair_input_ceiling`` are set only when the local
    budget found a recoverable oversize input: the estimated input and the
    largest input that could fit. They carry numbers only, never request,
    model, endpoint, or credential text. Both are ``None`` for provider-reported
    overflow, which gives no safe repair target. ``status_code`` is the plain
    integer HTTP status of a provider-reported overflow, else ``None``.
    """

    def __init__(
        self,
        message: str = "Request exceeds the model context capacity",
        *,
        input_tokens: int | None = None,
        repair_input_ceiling: int | None = None,
        status_code: int | None = None,
    ) -> None:
        """Create the error, keeping the optional repair numbers only as a pair."""
        super().__init__(message)
        self.status_code = (
            status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        )
        tokens = _safe_token_count(input_tokens)
        ceiling = _safe_token_count(repair_input_ceiling)
        if tokens is None or ceiling is None or tokens <= ceiling:
            tokens = ceiling = None
        self.input_tokens = tokens
        self.repair_input_ceiling = ceiling


# Error codes (from OpenAI / OpenRouter / Anthropic) that are permanent — retrying
# them wastes quota, burns time, and can trigger duplicate billing on some providers.
_PERMANENT_ERROR_CODES = frozenset({
    # OpenAI / OpenRouter
    "content_filter",
    "content_policy_violation",
    "invalid_request_error",
    "invalid_api_key",
    "authentication_error",
    "model_not_found",
    # Anthropic
    "invalid_api_key",
    "permission_error",
    "not_found_error",
    "invalid_request",
})


class LLMCancelledError(RuntimeError):
    """Raised when the LLM request is cancelled via cancel_event."""
