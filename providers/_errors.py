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


class LLMContextOverflowError(LLMPermanentError):
    """The provider reported that the request exceeds its context capacity.

    ``status_code`` is the plain integer HTTP status of the provider response,
    else ``None``. No request, model, endpoint or credential text is retained.
    """

    def __init__(
        self,
        message: str = "Request exceeds the model context capacity",
        *,
        status_code: int | None = None,
    ) -> None:
        """Create the error, keeping only a plain in-range HTTP status."""
        super().__init__(message)
        self.status_code = (
            status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        )


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
