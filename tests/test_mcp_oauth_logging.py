"""Targeted tests for MCP OAuth logging behavior (mcp-oauth-auth-logging change).

Covers the two new observable behaviors introduced by this change:
1. Post-flow token verification WARNING when the flow reports success but no
   token file was persisted (false-positive detection).
2. Trace-gated authorization URL logging in ``make_redirect_handler``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from unittest.mock import MagicMock

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
        """`_run_oauth_flow` must warn if it reports success but wrote no token file.

        The probe saw an auth challenge (401) so the warning is appropriate —
        the OAuth flow was attempted but no token was stored.
        """

        async def fake_start(self) -> None:
            return None

        async def fake_stop(self) -> None:
            return None

        monkeypatch.setattr(CallbackServer, "start", fake_start)
        monkeypatch.setattr(CallbackServer, "stop", fake_stop)
        monkeypatch.setattr(
            mcp_oauth.OAuthProviderFactory, "build", lambda *a, **k: object()
        )

        async def fake_probe(self, name, server_url, provider, oauth_cfg):  # noqa: ARG001
            return (True, 401, None)

        monkeypatch.setattr(MCPManager, "_probe_oauth_challenge", fake_probe)

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

        assert result == {
            "success": False,
            "error": (
                "OAuth flow completed but no token was stored — "
                "the authorization link may not have been delivered. "
                "Retry /mcp auth srv."
            ),
        }
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

        async def fake_probe(self, name, server_url, provider, oauth_cfg):  # noqa: ARG001
            return (True, 401, None)

        monkeypatch.setattr(MCPManager, "_probe_oauth_challenge", fake_probe)

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

    def test_run_oauth_flow_no_warning_when_probe_saw_no_auth_challenge(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """No WARNING should fire when the probe returned 200 (no auth challenge).

        The server did not require OAuth on the probe, so no token file is
        expected.  An INFO log should be emitted instead of a WARNING.
        """

        async def fake_start(self) -> None:
            return None

        async def fake_stop(self) -> None:
            return None

        monkeypatch.setattr(CallbackServer, "start", fake_start)
        monkeypatch.setattr(CallbackServer, "stop", fake_stop)
        monkeypatch.setattr(
            mcp_oauth.OAuthProviderFactory, "build", lambda *a, **k: object()
        )

        async def fake_probe(self, name, server_url, provider, oauth_cfg):  # noqa: ARG001
            return (False, 200, None)

        monkeypatch.setattr(MCPManager, "_probe_oauth_challenge", fake_probe)

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

        caplog.set_level(logging.INFO, logger="mcp_client")
        result = asyncio.run(_run())

        assert result == {"success": True}
        assert not (tmp_path / "srv.json").exists()
        # No WARNING about "no token file found"
        assert not any(
            "no token file found" in rec.message for rec in caplog.records
        )
        # INFO log about server not requiring OAuth
        assert any(
            "did not require OAuth" in rec.message for rec in caplog.records
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

    def _make_tg_iface(self):
        class _TgIface:
            def __init__(self):
                self.send_oauth_prompt = MagicMock()

        return _TgIface()

    def _run_handler(self, tmp_path: Path, monkeypatch, *, trace: bool, url: str) -> None:
        cb_server = self._make_cb_server(tmp_path)

        async def fake_start(self) -> None:
            return None

        monkeypatch.setattr(CallbackServer, "start", fake_start)

        tg_iface = self._make_tg_iface()
        # The handler now awaits the future returned by send_oauth_prompt,
        # so stub it with a real completed future.
        future: concurrent.futures.Future = concurrent.futures.Future()
        future.set_result(None)
        tg_iface.send_oauth_prompt = MagicMock(return_value=future)
        handler = make_redirect_handler(
            tg_iface, "srv", cb_server, chat_id=123, trace=trace
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

    def test_no_send_oauth_prompt_logs_warning_with_url(
        self, tmp_path: Path, caplog, monkeypatch
    ) -> None:
        """When tg_iface has no send_oauth_prompt, the URL is logged as a warning."""
        auth_url = "https://auth.example.com/authorize?client_id=abc&state=s3"
        cb_server = self._make_cb_server(tmp_path)

        async def fake_start(self) -> None:
            return None

        monkeypatch.setattr(CallbackServer, "start", fake_start)

        # tg_iface without send_oauth_prompt
        class _BareTgIface:
            pass

        handler = make_redirect_handler(
            _BareTgIface(), "srv", cb_server, chat_id=123, trace=False
        )
        caplog.set_level(logging.WARNING, logger="mcp_oauth")
        with pytest.raises(RuntimeError):
            asyncio.run(handler(auth_url))

        assert any(
            "no send_oauth_prompt" in rec.message and auth_url in rec.message
            for rec in caplog.records
        )
