"""Tests for per-prompt approval TTL and sub-agent headless auto-approval."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from builtin_executor import BuiltinExecutor
from confirmation import ConfirmationManager


@pytest.fixture
def executor(tmp_path):
    """Return a fresh BuiltinExecutor with a wired prompt callback."""
    return BuiltinExecutor(data_dir=str(tmp_path))


class TestGrantExpiresAtRunEnd:
    def test_auto_approve_cleared_in_run_finally(self, executor):
        confirmation = ConfirmationManager()
        confirmation.auto_approve_tools.add("file_read")
        assert "file_read" in confirmation.auto_approve_tools

        # Simulate what AgentController.run() finally does.
        confirmation.clear_auto_approve()
        assert confirmation.auto_approve_tools == set()


class TestHeadlessConfirmBridge:
    def test_sub_agent_auto_approves_when_tool_in_set(self, executor):
        executor._prompt_approval_set = {"file_read"}
        executor._subagent_confirm_prompt_fn = MagicMock()

        executor._headless_confirm_bridge(
            "file_read", {"path": "/tmp/test.txt"}, "read test file", caller_tag="sa-1"
        )
        # The bridge auto-approves by delegating to confirm(); the actual file read
        # may fail in this minimal fixture, but the important signal is that no
        # Telegram prompt was sent.
        assert executor._subagent_confirm_prompt_fn.called is False

    def test_sub_agent_prompts_when_tool_not_in_set(self, executor):
        executor._prompt_approval_set = set()
        executor._subagent_confirm_prompt_fn = MagicMock()

        # The bridge blocks for the operator timeout; patch it so tests are fast.
        with patch.object(executor, "_subagent_confirm_timeout", 0):
            result = executor._headless_confirm_bridge(
                "file_read", {"path": "/tmp/test.txt"}, "read test file", caller_tag="sa-1"
            )
        assert result.get("success") is False
        assert executor._subagent_confirm_prompt_fn.called is True

    def test_fail_closed_when_set_is_none(self, executor):
        executor._prompt_approval_set = None
        executor._subagent_confirm_prompt_fn = MagicMock()

        with patch.object(executor, "_subagent_confirm_timeout", 0):
            result = executor._headless_confirm_bridge(
                "file_read", {"path": "/tmp/test.txt"}, "read test file", caller_tag="sa-1"
            )
        assert result.get("success") is False
        assert executor._subagent_confirm_prompt_fn.called is True


class TestCallerTagSplitLookup:
    """C1 regression: caller_tag is 'sa-1 r-abc'; bare id must be used for the registry lookup."""

    def test_compound_caller_tag_auto_approves_matching_prompt(self, executor):
        """Registry is queried with the bare agent_id, not the full 'sa-1 r-abc' string."""
        from unittest.mock import MagicMock

        prompt_id = "01JARYN6R0ABCDEFGHJKMNPQRS"
        executor._prompt_approval_set = {"file_read"}
        executor._current_prompt_id = prompt_id
        executor._subagent_confirm_prompt_fn = MagicMock()

        mock_rec = MagicMock()
        mock_rec.prompt_id = prompt_id
        mock_reg = MagicMock()
        mock_reg.get.return_value = mock_rec

        with patch("sub_agent_registry.get_registry", return_value=mock_reg):
            executor._headless_confirm_bridge(
                "file_read", {"path": "/tmp/x.txt"}, "read x",
                caller_tag="sa-1 r-deadbeef",
            )

        # Bare id was used — NOT the compound tag
        mock_reg.get.assert_called_once_with("sa-1")
        # No Telegram prompt was sent
        assert executor._subagent_confirm_prompt_fn.called is False

    def test_mismatched_prompt_id_falls_through_to_telegram(self, executor):
        """Caller from a stale/different prompt must not auto-approve."""
        from unittest.mock import MagicMock

        executor._prompt_approval_set = {"file_read"}
        executor._current_prompt_id = "01JARYZ3W2ABCDEFGHJKMNPQRS"
        executor._subagent_confirm_prompt_fn = MagicMock()

        mock_rec = MagicMock()
        mock_rec.prompt_id = "01JARYZ3W2DIFFERENTULIDSTR"  # different prompt
        mock_reg = MagicMock()
        mock_reg.get.return_value = mock_rec

        with patch("sub_agent_registry.get_registry", return_value=mock_reg):
            with patch.object(executor, "_subagent_confirm_timeout", 0):
                result = executor._headless_confirm_bridge(
                    "file_read", {"path": "/tmp/x.txt"}, "read x",
                    caller_tag="sa-stale r-deadbeef",
                )

        assert result.get("success") is False
        assert executor._subagent_confirm_prompt_fn.called is True
