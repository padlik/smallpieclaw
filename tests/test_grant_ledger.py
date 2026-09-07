"""Tests for the GrantLedger and its integration with ConfirmationManager.

Covers grant hit/miss by tool and directory, prompt/session lifetimes,
sink-side vetoes for shell/secret_get, and sub-agent scope containment.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from confirmation import (
    ConfirmationManager,
    GrantLedger,
    GrantLifetime,
    NO_STANDING_GRANT_TOOLS,
    grant_covers,
)


@pytest.fixture
def ledger() -> GrantLedger:
    return GrantLedger()


class TestGrantCovers:
    def test_grant_covers_exact_dir(self) -> None:
        assert grant_covers("/data", "/data") is True

    def test_grant_covers_nested_path(self) -> None:
        assert grant_covers("/data", "/data/sub/deep/file.txt") is True

    def test_grant_does_not_cover_sibling_dir(self) -> None:
        assert grant_covers("/data/reports", "/data/other.txt") is False

    def test_grant_does_not_cover_prefix_collision(self) -> None:
        assert grant_covers("/data/repo", "/data/repository/file.txt") is False


class TestGrantHitMiss:
    def test_recursive_coverage_hit(self, ledger: GrantLedger) -> None:
        assert ledger.add("file_write", "/data", GrantLifetime.PROMPT) is True
        assert ledger.check("file_write", "/data/sub/deep/f.txt") is True

    def test_sibling_exclusion(self, ledger: GrantLedger) -> None:
        assert ledger.add("file_write", "/data/reports", GrantLifetime.PROMPT) is True
        assert ledger.check("file_write", "/data/other.txt") is False

    def test_per_tool_isolation(self, ledger: GrantLedger) -> None:
        assert ledger.add("file_write", "/data", GrantLifetime.PROMPT) is True
        assert ledger.check("file_read", "/data/file.txt") is False


class TestGrantLifetimes:
    def test_prompt_grant_cleared_for_main_scope(self, ledger: GrantLedger) -> None:
        assert ledger.add("file_write", "/data", GrantLifetime.PROMPT) is True
        assert ledger.check("file_write", "/data/x.txt") is True
        ledger.clear_prompt_scope(None)
        assert ledger.check("file_write", "/data/x.txt") is False

    def test_session_grant_survives_prompt_clear(self, ledger: GrantLedger) -> None:
        assert ledger.add("file_write", "/data", GrantLifetime.SESSION) is True
        ledger.clear_prompt_scope(None)
        assert ledger.check("file_write", "/data/x.txt") is True

    def test_clear_all_clears_both_lifetimes(self, ledger: GrantLedger) -> None:
        assert ledger.add("file_write", "/data", GrantLifetime.PROMPT) is True
        assert ledger.add("file_read", "/data", GrantLifetime.SESSION) is True
        ledger.clear_all()
        assert ledger.check("file_write", "/data/x.txt") is False
        assert ledger.check("file_read", "/data/x.txt") is False


class TestSinkVeto:
    @pytest.mark.parametrize("tool", ["shell", "secret_get"])
    def test_may_hold_grant_false_for_vetoed_tools(self, tool: str) -> None:
        assert tool in NO_STANDING_GRANT_TOOLS
        assert GrantLedger.may_hold_grant(tool) is False

    @pytest.mark.parametrize("tool", ["shell", "secret_get"])
    def test_add_refuses_vetoed_tool(self, tool: str, ledger: GrantLedger) -> None:
        assert ledger.add(tool, "/data", GrantLifetime.SESSION) is False
        assert ledger.check(tool, "/data/x.txt") is False

    def test_other_tools_can_hold_grants(self, ledger: GrantLedger) -> None:
        assert ledger.add("file_write", "/data", GrantLifetime.SESSION) is True
        assert ledger.check("file_write", "/data/x.txt") is True


class TestSubAgentScope:
    def test_sub_agent_grant_covers_only_same_scope(self, ledger: GrantLedger) -> None:
        assert ledger.add(
            "file_write", "/data", GrantLifetime.PROMPT, scope_owner="sa-123"
        ) is True
        assert ledger.check("file_write", "/data/x.txt", scope_owner="sa-123") is True
        assert ledger.check("file_write", "/data/x.txt", scope_owner=None) is False
        assert ledger.check("file_write", "/data/x.txt", scope_owner="sa-456") is False

    def test_clear_prompt_scope_only_target_scope(self, ledger: GrantLedger) -> None:
        assert ledger.add(
            "file_write", "/data", GrantLifetime.PROMPT, scope_owner="sa-123"
        ) is True
        assert ledger.add(
            "file_write", "/data", GrantLifetime.PROMPT, scope_owner="sa-456"
        ) is True
        ledger.clear_prompt_scope("sa-123")
        assert ledger.check("file_write", "/data/x.txt", scope_owner="sa-123") is False
        assert ledger.check("file_write", "/data/x.txt", scope_owner="sa-456") is True

    def test_session_grant_survives_prompt_scope_clear(self, ledger: GrantLedger) -> None:
        assert ledger.add(
            "file_write", "/data", GrantLifetime.SESSION, scope_owner="sa-123"
        ) is True
        ledger.clear_prompt_scope("sa-123")
        assert ledger.check("file_write", "/data/x.txt", scope_owner="sa-123") is True

    def test_clear_prompt_scope_keeps_other_scopes_and_lifetimes(self, ledger: GrantLedger) -> None:
        """clear_prompt_scope removes only the target scope's prompt grants."""
        assert ledger.add(
            "file_write", "/data", GrantLifetime.PROMPT, scope_owner="sa-abc"
        ) is True
        assert ledger.add(
            "file_write", "/data2", GrantLifetime.PROMPT, scope_owner="sa-abc"
        ) is True
        assert ledger.add(
            "file_write", "/data", GrantLifetime.PROMPT, scope_owner="sa-xyz"
        ) is True
        assert ledger.add(
            "file_write", "/data", GrantLifetime.PROMPT, scope_owner=None
        ) is True
        assert ledger.add(
            "file_write", "/data", GrantLifetime.SESSION, scope_owner=None
        ) is True
        ledger.clear_prompt_scope("sa-abc")
        # sa-abc scoped prompt grants are gone.
        assert ledger.check("file_write", "/data2/x.txt", scope_owner="sa-abc") is False
        # sa-xyz scoped prompt grant survives.
        assert ledger.check("file_write", "/data/x.txt", scope_owner="sa-xyz") is True
        # The main-scoped prompt grant survives (different scope).
        assert ledger.check("file_write", "/data/x.txt", scope_owner=None) is True
        # The session-wide grant also survives prompt scope clearing.
        assert any(
            g.scope_owner is None and g.lifetime == GrantLifetime.SESSION
            for g in ledger._grants
        )

    def test_scope_free_session_grant_covers_every_scope(self, ledger: GrantLedger) -> None:
        # "Till /reset" consent is session-wide (approval-grants spec): a
        # scope-free SESSION grant covers the main agent and any sub-agent.
        assert ledger.add("file_write", "/data", GrantLifetime.SESSION) is True
        assert ledger.check("file_write", "/data/x.txt", scope_owner=None) is True
        assert ledger.check("file_write", "/data/x.txt", scope_owner="sa-123") is True
        assert ledger.check("file_write", "/data/x.txt", scope_owner="sa-456") is True

    def test_scoped_session_grant_does_not_cover_main(self, ledger: GrantLedger) -> None:
        # Legacy/explicitly-scoped session grants stay scope-exact.
        assert ledger.add(
            "file_write", "/data", GrantLifetime.SESSION, scope_owner="sa-123"
        ) is True
        assert ledger.check("file_write", "/data/x.txt", scope_owner="sa-123") is True
        assert ledger.check("file_write", "/data/x.txt", scope_owner=None) is False


class TestDirectoryNormalization:
    def test_realpath_expansion(self, ledger: GrantLedger) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_dir = os.path.realpath(tmp)
            assert ledger.add("file_write", tmp, GrantLifetime.SESSION) is True
            assert ledger.check("file_write", real_dir) is True
            nested = os.path.join(real_dir, "a.txt")
            assert ledger.check("file_write", nested) is True

    def test_tilde_expansion(self, ledger: GrantLedger, monkeypatch) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = tmp
            monkeypatch.setenv("HOME", fake_home)
            subdir = os.path.join(fake_home, "data")
            os.makedirs(subdir, exist_ok=True)
            assert ledger.add("file_write", "~/data", GrantLifetime.SESSION) is True
            assert ledger.check("file_write", "~/data/file.txt") is True


class TestConfirmationManagerDelegation:
    def test_add_grant_and_check_grant_delegates(self) -> None:
        cm = ConfirmationManager()
        assert cm.add_grant("file_write", "/data", GrantLifetime.SESSION) is True
        assert cm.check_grant("file_write", "/data/sub/file.txt") is True
        assert cm.check_grant("file_read", "/data/sub/file.txt") is False
