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
