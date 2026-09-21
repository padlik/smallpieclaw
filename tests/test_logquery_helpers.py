"""Unit tests for ``builtin_tools/logquery_helpers.py``.

Covers the ``before_id`` filter part added to ``LogQueryFilters`` and
``_log_query_compile_where``: default behavior is unchanged, and the filter
composes correctly with other parts.
"""

from __future__ import annotations

import pytest

from builtin_tools.logquery_helpers import (
    LogQueryFilters,
    _log_query_compile_where,
)


def test_default_filters_no_before_id():
    """A default LogQueryFilters (before_id=None) compiles to no id < ? part,
    identical to a LogQueryFilters constructed without the before_id kwarg."""
    default = LogQueryFilters(trace="r-aaaaaaaa")
    where, params = _log_query_compile_where(default)

    assert "id < ?" not in where
    no_before = LogQueryFilters(trace="r-aaaaaaaa")
    where_no, params_no = _log_query_compile_where(no_before)
    assert (where, params) == (where_no, params_no)


def test_before_id_appended_last_and_composes():
    """before_id adds ``id < ?`` at the end with its param in the right place."""
    filters = LogQueryFilters(trace="r-aaaaaaaa", event_type="TOOL_FAILED", before_id=500)
    where, params = _log_query_compile_where(filters)

    assert "event_type = ?" in where
    assert "id < ?" in where
    # id < ? must appear after event_type = ? in the AND-joined clause.
    assert where.index("event_type = ?") < where.index("id < ?")
    assert params == ["r-aaaaaaaa", "r-aaaaaaaa", "TOOL_FAILED", 500]


def test_non_positive_before_id_omitted():
    """before_id <= 0 omits the id < ? part."""
    for bad in (0, -5):
        filters = LogQueryFilters(trace="r-aaaaaaaa", before_id=bad)
        where, params = _log_query_compile_where(filters)
        assert "id < ?" not in where
        no_bad = LogQueryFilters(trace="r-aaaaaaaa")
        where_no, params_no = _log_query_compile_where(no_bad)
        assert (where, params) == (where_no, params_no)


def test_multiple_parts_order_with_before_id():
    """Params for trace, event_type, prompt_id, text come before the before_id param."""
    filters = LogQueryFilters(
        trace="r-aaaaaaaa",
        event_type="TOOL_START",
        prompt_id="01JARYN6R0",
        text="shell",
        before_id=999,
    )
    where, params = _log_query_compile_where(filters)

    assert params == ["r-aaaaaaaa", "r-aaaaaaaa", "TOOL_START", "01JARYN6R0", "shell", 999]
    assert where.count("?") == len(params)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
