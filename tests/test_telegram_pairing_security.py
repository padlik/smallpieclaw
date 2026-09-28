"""Security behavior for Telegram pairing tokens."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from telegram_commands import cmd_pair, cmd_unpair


def _call(command, iface, user_id: int, args: list[str] | None = None) -> list[str]:
    """Invoke a pairing command and capture replies."""
    replies: list[str] = []
    message = SimpleNamespace(reply_text=AsyncMock(side_effect=lambda text, **_: replies.append(text)))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=message)
    context = SimpleNamespace(args=args or [])
    asyncio.run(command(iface, update, context))
    return replies


def _iface(timeout: int = 300) -> Any:
    iface = SimpleNamespace(
        security_mode="pairing",
        allowed_ids={1},
        _pending_pairs={},
        _pairing_timeout=timeout,
        _send_unauthorized=AsyncMock(),
    )
    iface._is_authorized = lambda user_id: user_id in iface.allowed_ids
    return iface


def _real_iface(timeout: int) -> Any:
    """Construct the real interface without building a Telegram application."""
    from telegram_interface import TelegramInterface

    return TelegramInterface(
        config={
            "telegram": {
                "bot_token": "fake:token",
                "security_mode": "pairing",
                "allowed_user_ids": [1],
                "pairing_timeout": timeout,
            }
        },
        agent_handler=lambda *_: None,
    )


def _token_from_reply(reply: str) -> str:
    """Extract generated token from command response."""
    match = re.search(r"<code>([0-9a-f]+)</code>", reply)
    assert match is not None
    return match.group(1)


def test_pair_token_expires_at_timeout(monkeypatch):
    import telegram_commands

    monkeypatch.setattr(telegram_commands.time, "monotonic", lambda: 310.0)
    iface = _iface()
    iface._pending_pairs["token"] = (1, 310.0)

    replies = _call(cmd_pair, iface, 2, ["token"])

    assert "Invalid or expired" in replies[0]
    assert 2 not in iface.allowed_ids


def test_unpair_revokes_tokens_issued_by_target():
    iface = _iface()
    iface._pending_pairs = {"target": (2, 500.0), "other": (1, 500.0)}

    _call(cmd_unpair, iface, 1, ["2"])

    assert iface._pending_pairs == {"other": (1, 500.0)}


def test_pair_token_is_single_use(monkeypatch):
    import telegram_commands

    monkeypatch.setattr(telegram_commands.time, "monotonic", lambda: 100.0)
    iface = _iface()
    iface._pending_pairs["token"] = (1, 500.0)

    first = _call(cmd_pair, iface, 2, ["token"])
    second = _call(cmd_pair, iface, 3, ["token"])

    assert "Pairing successful" in first[0]
    assert "Invalid or expired" in second[0]
    assert iface.allowed_ids == {1, 2}


def test_pair_token_rejected_after_issuer_is_unpaired(monkeypatch):
    import telegram_commands

    monkeypatch.setattr(telegram_commands.time, "monotonic", lambda: 100.0)
    iface = _iface()
    iface._pending_pairs["token"] = (1, 500.0)
    iface.allowed_ids.discard(1)

    replies = _call(cmd_pair, iface, 2, ["token"])

    assert "Invalid or expired" in replies[0]
    assert 2 not in iface.allowed_ids


def test_unpair_revokes_tokens_issued_by_self():
    iface = _iface()
    iface._pending_pairs = {"self": (1, 500.0), "other": (2, 500.0)}

    _call(cmd_unpair, iface, 1, ["1"])

    assert iface._pending_pairs == {"other": (2, 500.0)}


def test_unauthorized_user_cannot_issue_pair_token():
    iface = _iface()

    replies = _call(cmd_pair, iface, 2)

    iface._send_unauthorized.assert_awaited_once()
    assert replies == []
    assert iface._pending_pairs == {}


def test_real_interface_token_redeems_before_configured_timeout(monkeypatch):
    import telegram_commands

    now = [10.0]
    monkeypatch.setattr(telegram_commands.time, "monotonic", lambda: now[0])
    iface = _real_iface(timeout=5)

    issued = _call(cmd_pair, iface, 1)
    token = _token_from_reply(issued[0])
    now[0] = 14.999
    redeemed = _call(cmd_pair, iface, 2, [token])

    assert "expires in 5 seconds" in issued[0]
    assert "Pairing successful" in redeemed[0]
    assert 2 in iface.allowed_ids


def test_real_interface_token_rejected_at_configured_timeout(monkeypatch):
    import telegram_commands

    now = [20.0]
    monkeypatch.setattr(telegram_commands.time, "monotonic", lambda: now[0])
    iface = _real_iface(timeout=5)

    issued = _call(cmd_pair, iface, 1)
    token = _token_from_reply(issued[0])
    now[0] = 25.0
    redeemed = _call(cmd_pair, iface, 2, [token])

    assert "Invalid or expired" in redeemed[0]
    assert 2 not in iface.allowed_ids


def test_real_interface_self_unpair_revokes_issued_token(monkeypatch):
    import telegram_commands

    monkeypatch.setattr(telegram_commands.time, "monotonic", lambda: 30.0)
    iface = _real_iface(timeout=5)

    issued = _call(cmd_pair, iface, 1)
    token = _token_from_reply(issued[0])
    _call(cmd_unpair, iface, 1, ["1"])
    redeemed = _call(cmd_pair, iface, 2, [token])

    assert "Invalid or expired" in redeemed[0]
    assert 1 not in iface.allowed_ids
    assert 2 not in iface.allowed_ids
