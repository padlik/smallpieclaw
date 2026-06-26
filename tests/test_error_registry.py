"""Unit tests for the error_registry module.

These tests cover the :class:`ErrorTypeInfo` dataclass defaults and the
:class:`ErrorTypeRegistry` registration and lookup behaviour.
"""

from __future__ import annotations

from error_registry import ErrorTypeInfo, ErrorTypeRegistry


class TestErrorTypeInfo:
    """Tests for the :class:`ErrorTypeInfo` frozen dataclass."""

    def test_dataclass_fields(self):
        """Frozen dataclass exposes the expected attributes with correct defaults."""
        info = ErrorTypeInfo("custom_error", True)

        assert info.error_type == "custom_error"
        assert info.recoverable is True
        assert info.max_retries == 0
        assert info.backoff_base == 2.0
        assert info.requires_complex_recovery is False

    def test_default_max_retries_zero(self):
        """Default ``max_retries`` is 0."""
        info = ErrorTypeInfo("default_retries", False)

        assert info.max_retries == 0

    def test_default_backoff_base(self):
        """Default ``backoff_base`` is 2.0."""
        info = ErrorTypeInfo("default_backoff", False)

        assert info.backoff_base == 2.0


class TestErrorTypeRegistry:
    """Tests for the :class:`ErrorTypeRegistry` lookup and defaults."""

    def test_register_and_get(self):
        """A custom type can be registered and retrieved."""
        registry = ErrorTypeRegistry()
        info = ErrorTypeInfo("custom_error", True, 2, 1.5)

        registry.register(info)

        assert registry.get("custom_error") is info

    def test_get_unknown_returns_none(self):
        """Looking up an unknown error type returns None."""
        registry = ErrorTypeRegistry()

        assert registry.get("not_a_real_error") is None

    def test_default_types_loaded(self):
        """All 9 default error types are registered on construction."""
        registry = ErrorTypeRegistry()

        expected = {
            "tool_timeout",
            "network_error",
            "syntax_error",
            "permission_denied",
            "file_not_found",
            "command_not_found",
            "wrong_model_for_task",
            "fundamentally_wrong_approach",
            "impossible_with_current_tools",
        }
        assert set(registry._types.keys()) == expected
        assert len(registry._types) == 9

    def test_transient_types_are_recoverable(self):
        """Transient errors are marked recoverable."""
        registry = ErrorTypeRegistry()

        for error_type in ("tool_timeout", "network_error", "syntax_error"):
            info = registry.get(error_type)
            assert info is not None
            assert info.recoverable is True
            assert info.requires_complex_recovery is False

    def test_fatal_types_are_not_recoverable(self):
        """Fatal errors are not recoverable.

        ``permission_denied`` and ``file_not_found`` require complex recovery,
        while ``command_not_found`` does not.
        """
        registry = ErrorTypeRegistry()

        for error_type in ("permission_denied", "file_not_found", "command_not_found"):
            info = registry.get(error_type)
            assert info is not None
            assert info.recoverable is False

        assert registry.get("permission_denied").requires_complex_recovery is True
        assert registry.get("file_not_found").requires_complex_recovery is True
        assert registry.get("command_not_found").requires_complex_recovery is False

    def test_planning_types_require_complex_recovery(self):
        """Planning errors are not recoverable and require complex recovery."""
        registry = ErrorTypeRegistry()

        for error_type in (
            "wrong_model_for_task",
            "fundamentally_wrong_approach",
            "impossible_with_current_tools",
        ):
            info = registry.get(error_type)
            assert info is not None
            assert info.recoverable is False
            assert info.requires_complex_recovery is True

    def test_retry_counts(self):
        """Verify ``max_retries`` values for each error category."""
        registry = ErrorTypeRegistry()

        # Transient errors allow retries.
        assert registry.get("tool_timeout").max_retries == 2
        assert registry.get("network_error").max_retries == 2
        assert registry.get("syntax_error").max_retries == 2

        # Fatal and planning errors do not retry.
        for error_type in (
            "permission_denied",
            "file_not_found",
            "command_not_found",
            "wrong_model_for_task",
            "fundamentally_wrong_approach",
            "impossible_with_current_tools",
        ):
            assert registry.get(error_type).max_retries == 0
