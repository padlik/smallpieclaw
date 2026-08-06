"""Targeted tests for MCP OAuth logging behavior (mcp-oauth-auth-logging change).

Covers the two new observable behaviors introduced by this change:
1. Post-flow token verification WARNING when the flow reports success but no
   token file was persisted (false-positive detection).
2. Trace-gated authorization URL logging in ``make_redirect_handler``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import mcp_oauth
from mcp_client import MCPManager, _SdkClientWrapper
from mcp_oauth import CallbackServer, FileTokenStorage, make_redirect_handler


class TestGetTokensRemainingTtlDiagnostic:
    def test_corrupt_issued_at_does_not_discard_valid_token(
        self, tmp_path: Path
    ) -> None:
        """A malformed diagnostic-only ``issued_at`` must not cause a valid
        token to be treated as corrupt and discarded (forcing needless re-auth).
        """
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        token_file = tmp_path / "srv.json"
        token_file.write_text(
            '{"token": {"access_token": "abc", "expires_in": 3600, '
            '"issued_at": "corrupt"}}',
            encoding="utf-8",
        )

        loaded = asyncio.run(storage.get_tokens())

        assert loaded is not None
        assert loaded.access_token == "abc"

    @pytest.mark.parametrize("bad_issued_at", ["1e999", "-1e999", "NaN", '"corrupt"'])
    def test_non_finite_issued_at_does_not_discard_valid_token(
        self, tmp_path: Path, bad_issued_at: str
    ) -> None:
        """``json.load`` accepts Infinity/NaN literals; ``int()`` on them raises
        OverflowError/ValueError, which must not escape ``get_tokens()``.
        """
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        (tmp_path / "srv.json").write_text(
            '{"token": {"access_token": "abc", "expires_in": 3600, '
            f'"issued_at": {bad_issued_at}}}}}',
            encoding="utf-8",
        )

        loaded = asyncio.run(storage.get_tokens())

        assert loaded is not None
        assert loaded.access_token == "abc"


class TestPostFlowTokenVerification:
    def test_run_oauth_flow_warns_when_no_token_file(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """`_run_oauth_flow` must warn if it reports success but wrote no token file."""

        async def fake_start(self) -> None:
            return None

        async def fake_stop(self) -> None:
            return None

        monkeypatch.setattr(CallbackServer, "start", fake_start)
        monkeypatch.setattr(CallbackServer, "stop", fake_stop)
        monkeypatch.setattr(
            mcp_oauth.OAuthProviderFactory, "build", lambda *a, **k: object()
        )

        async def fake_session_runner(self) -> None:
            self.needs_auth = False
            self._tools = []
            self._ready_future.set_result(True)

        monkeypatch.setattr(_SdkClientWrapper, "_session_runner", fake_session_runner)

        cfg = {"name": "srv", "transport": "http", "url": "https://example.com"}
        oauth_cfg = {
            "callback_port": 0,
            "callback_bind": "127.0.0.1",
            "cert_path": "unused",
            "key_path": "unused",
        }
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        async def _run() -> dict:
            manager._loop = asyncio.get_running_loop()
            return await manager._run_oauth_flow("srv", cfg, oauth_cfg)

        caplog.set_level(logging.WARNING, logger="mcp_client")
        result = asyncio.run(_run())

        assert result == {"success": True}
        assert not (tmp_path / "srv.json").exists()
        assert any(
            "no token file found" in rec.message for rec in caplog.records
        )

    def test_run_oauth_flow_no_warning_when_token_file_present(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """No WARNING should fire when the token file was actually written."""

        async def fake_start(self) -> None:
            return None

        async def fake_stop(self) -> None:
            return None

        monkeypatch.setattr(CallbackServer, "start", fake_start)
        monkeypatch.setattr(CallbackServer, "stop", fake_stop)
        monkeypatch.setattr(
            mcp_oauth.OAuthProviderFactory, "build", lambda *a, **k: object()
        )

        async def fake_session_runner(self) -> None:
            self.needs_auth = False
            self._tools = []
            (tmp_path / "srv.json").write_text("{}", encoding="utf-8")
            self._ready_future.set_result(True)

        monkeypatch.setattr(_SdkClientWrapper, "_session_runner", fake_session_runner)

        cfg = {"name": "srv", "transport": "http", "url": "https://example.com"}
        oauth_cfg = {
            "callback_port": 0,
            "callback_bind": "127.0.0.1",
            "cert_path": "unused",
            "key_path": "unused",
        }
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        async def _run() -> dict:
            manager._loop = asyncio.get_running_loop()
            return await manager._run_oauth_flow("srv", cfg, oauth_cfg)

        caplog.set_level(logging.WARNING, logger="mcp_client")
        result = asyncio.run(_run())

        assert result == {"success": True}
        assert not any(
            "no token file found" in rec.message for rec in caplog.records
        )


class TestTraceGatedAuthUrlLogging:
    """The ``trace`` flag gates full-auth-URL logging on the interactive path.

    These tests supply a Telegram interface and ``chat_id`` so the handler takes
    the interactive branch. The non-interactive fallback branch deliberately
    logs the URL at WARNING regardless of ``trace`` — it is the only channel an
    operator has to complete auth when no chat is in context — so asserting the
    gate there would be meaningless.
    """

    def _make_cb_server(self, tmp_path: Path) -> CallbackServer:
        loop = asyncio.new_event_loop()
        return CallbackServer(
            port=0,
            bind="127.0.0.1",
            cert_path=str(tmp_path / "unused.crt"),
            key_path=str(tmp_path / "unused.key"),
            loop=loop,
        )

    def _make_tg_iface(self) -> MagicMock:
        tg_iface = MagicMock()
        tg_iface.app.bot.send_message = AsyncMock()
        return tg_iface

    def _run_handler(self, tmp_path: Path, monkeypatch, *, trace: bool, url: str) -> None:
        cb_server = self._make_cb_server(tmp_path)

        async def fake_start(self) -> None:
            return None

        monkeypatch.setattr(CallbackServer, "start", fake_start)
        handler = make_redirect_handler(
            self._make_tg_iface(), "srv", cb_server, chat_id=123, trace=trace
        )
        asyncio.run(handler(url))

    def test_auth_url_logged_when_trace_true(
        self, tmp_path: Path, caplog, monkeypatch
    ) -> None:
        auth_url = "https://auth.example.com/authorize?client_id=abc&scope=read&state=s1"

        caplog.set_level(logging.DEBUG, logger="mcp_oauth")
        self._run_handler(tmp_path, monkeypatch, trace=True, url=auth_url)

        assert any(
            rec.message.startswith("MCP [srv] auth URL:") and auth_url in rec.message
            for rec in caplog.records
        )

    def test_auth_url_absent_from_all_records_when_trace_false(
        self, tmp_path: Path, caplog, monkeypatch
    ) -> None:
        """With trace off, the URL must not appear in *any* log record."""
        auth_url = "https://auth.example.com/authorize?client_id=abc&scope=read&state=s2"

        caplog.set_level(logging.DEBUG, logger="mcp_oauth")
        self._run_handler(tmp_path, monkeypatch, trace=False, url=auth_url)

        # Handler ran far enough to emit its INFO events, so absence is meaningful.
        assert any("redirect_handler called" in rec.message for rec in caplog.records)
        assert not any(auth_url in rec.message for rec in caplog.records)
