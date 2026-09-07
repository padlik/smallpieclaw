"""Tests for the GrantLedger sink-side veto and ConfirmationManager delegate path.

The NO_STANDING_GRANT_TOOLS set ("shell", "secret_get") must be rejected both
when adding a grant and when checking one, even if a grant object was injected
straight into the ledger's internal set.  The public ConfirmationManager helpers
must enforce the same veto, which is the path a crafted Telegram callback would
reach.
"""

from __future__ import annotations

from confirmation import (
    NO_STANDING_GRANT_TOOLS,
    ConfirmationManager,
    Grant,
    GrantLedger,
    GrantLifetime,
)


class TestGrantLedgerSinkVeto:
    """Veto applied inside GrantLedger.add and GrantLedger.check."""

    def test_add_refuses_shell_grant(self) -> None:
        """add() returns False and stores nothing for shell."""
        ledger = GrantLedger()
        assert ledger.add("shell", "/tmp", GrantLifetime.SESSION) is False
        assert not ledger.check("shell", "/tmp")

    def test_add_refuses_secret_get_grant(self) -> None:
        """add() returns False and stores nothing for secret_get."""
        ledger = GrantLedger()
        assert ledger.add("secret_get", "/tmp", GrantLifetime.SESSION) is False
        assert not ledger.check("secret_get", "/tmp")

    def test_injected_shell_grant_rejected_by_check(self) -> None:
        """A shell Grant inserted directly into _grants is still vetoed by check()."""
        ledger = GrantLedger()
        injected = Grant(tool="shell", dir="/tmp", lifetime=GrantLifetime.SESSION)
        ledger._grants.add(injected)
        assert ledger.check("shell", "/tmp") is False

    def test_injected_secret_get_grant_rejected_by_check(self) -> None:
        """A secret_get Grant inserted directly into _grants is still vetoed by check()."""
        ledger = GrantLedger()
        injected = Grant(tool="secret_get", dir="/tmp", lifetime=GrantLifetime.SESSION)
        ledger._grants.add(injected)
        assert ledger.check("secret_get", "/tmp") is False


class TestConfirmationManagerDelegateVeto:
    """Veto propagated through the public ConfirmationManager grant helpers."""

    def test_add_grant_refuses_shell(self) -> None:
        """ConfirmationManager.add_grant refuses to create a shell standing grant."""
        mgr = ConfirmationManager()
        assert mgr.add_grant("shell", "/tmp", GrantLifetime.SESSION) is False
        assert not mgr.check_grant("shell", "/tmp")

    def test_add_grant_refuses_secret_get(self) -> None:
        """ConfirmationManager.add_grant refuses to create a secret_get standing grant."""
        mgr = ConfirmationManager()
        assert mgr.add_grant("secret_get", "/tmp", GrantLifetime.SESSION) is False
        assert not mgr.check_grant("secret_get", "/tmp")

    def test_add_grant_accepts_file_write_positive_control(self) -> None:
        """A non-vetoed tool (file_write) can still obtain a grant through the delegate."""
        mgr = ConfirmationManager()
        assert "file_write" not in NO_STANDING_GRANT_TOOLS
        assert mgr.add_grant("file_write", "/tmp", GrantLifetime.SESSION) is True
        assert mgr.check_grant("file_write", "/tmp") is True
