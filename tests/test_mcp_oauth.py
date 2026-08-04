"""Tests for mcp_oauth.py — OAuth helpers for MCP HTTP servers."""

from __future__ import annotations

import asyncio
import ssl
import threading
from pathlib import Path
import pytest

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_oauth import CallbackServer, FileTokenStorage, OAuthProviderFactory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_test_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def _stop_test_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)


def _run_async(loop: asyncio.AbstractEventLoop, coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def _make_self_signed_cert(tmp_path: Path) -> tuple[str, str]:
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    # Generate minimal self-signed cert with OpenSSL command-line.
    import subprocess

    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert_path), str(key_path)


# ---------------------------------------------------------------------------
# FileTokenStorage
# ---------------------------------------------------------------------------


class TestFileTokenStorage:
    def test_token_storage_round_trip(self, tmp_path: Path):
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        token = OAuthToken(
            access_token="access",
            token_type="Bearer",
            expires_in=3600,
            scope="read",
            refresh_token="refresh",
        )

        asyncio.run(storage.set_tokens(token))
        loaded = asyncio.run(storage.get_tokens())

        assert loaded is not None
        assert loaded.access_token == "access"
        assert loaded.token_type == "Bearer"
        assert loaded.expires_in == 3600
        assert loaded.scope == "read"
        assert loaded.refresh_token == "refresh"

    def test_token_storage_file_permissions(self, tmp_path: Path):
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        asyncio.run(storage.set_tokens(OAuthToken(access_token="a")))
        mode = (tmp_path / "srv.json").stat().st_mode & 0o777
        assert mode == 0o600

    def test_token_storage_pre_seeded_client_info(self, tmp_path: Path):
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        info = asyncio.run(storage.get_client_info())
        assert info is not None
        assert info.client_id == "cid"
        assert info.client_secret == "csec"
        assert info.redirect_uris is None

    def test_token_storage_missing_file_returns_none(self, tmp_path: Path):
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        assert asyncio.run(storage.get_tokens()) is None

    def test_token_storage_reauth_overwrites(self, tmp_path: Path):
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        asyncio.run(storage.set_tokens(OAuthToken(access_token="first")))
        asyncio.run(storage.set_tokens(OAuthToken(access_token="second")))
        loaded = asyncio.run(storage.get_tokens())
        assert loaded is not None
        assert loaded.access_token == "second"

    def test_token_storage_preserves_client_info(self, tmp_path: Path):
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        info = OAuthClientInformationFull(
            client_id="cid",
            client_secret="csec",
            redirect_uris=None,
        )
        asyncio.run(storage.set_client_info(info))
        asyncio.run(storage.set_tokens(OAuthToken(access_token="a")))
        reloaded = asyncio.run(storage.get_client_info())
        assert reloaded is not None
        assert reloaded.client_id == "cid"


# ---------------------------------------------------------------------------
# CallbackServer
# ---------------------------------------------------------------------------


class TestCallbackServer:
    def setup_method(self):
        self.loop, self.thread = _start_test_loop()

    def teardown_method(self):
        _stop_test_loop(self.loop, self.thread)

    def _make_server(self, tmp_path: Path) -> CallbackServer:
        cert_path, key_path = _make_self_signed_cert(tmp_path)
        return CallbackServer(
            port=0,
            bind="127.0.0.1",
            cert_path=cert_path,
            key_path=key_path,
            loop=self.loop,
        )

    def test_callback_server_start_stop(self, tmp_path: Path):
        server = self._make_server(tmp_path)
        _run_async(self.loop, server.start())
        assert server._server is not None
        _run_async(self.loop, server.stop())

    def test_callback_server_receives_valid_callback(self, tmp_path: Path):
        server = self._make_server(tmp_path)
        _run_async(self.loop, server.start())
        try:
            host, port = server._server.sockets[0].getsockname()[:2]
            ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            async def _send() -> None:
                reader, writer = await asyncio.open_connection(
                    host, port, ssl=ssl_context
                )
                writer.write(b"GET /?code=testcode&state=teststate HTTP/1.1\r\n\r\n")
                await writer.drain()
                _ = await reader.read(4096)
                writer.close()
                await writer.wait_closed()

            _run_async(self.loop, _send())
            code, state = _run_async(self.loop, server.wait_for_callback(timeout=5.0))
            assert code == "testcode"
            assert state == "teststate"
        finally:
            _run_async(self.loop, server.stop())

    def test_callback_server_accepts_non_root_redirect_path(self, tmp_path: Path):
        """Callback server must accept redirect_uris with a real path (e.g. /callback)."""
        server = self._make_server(tmp_path)
        _run_async(self.loop, server.start())
        try:
            host, port = server._server.sockets[0].getsockname()[:2]
            ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            async def _send() -> None:
                reader, writer = await asyncio.open_connection(
                    host, port, ssl=ssl_context
                )
                writer.write(b"GET /callback?code=cbcode&state=cbstate HTTP/1.1\r\n\r\n")
                await writer.drain()
                _ = await reader.read(4096)
                writer.close()
                await writer.wait_closed()

            _run_async(self.loop, _send())
            code, state = _run_async(self.loop, server.wait_for_callback(timeout=5.0))
            assert code == "cbcode"
            assert state == "cbstate"
        finally:
            _run_async(self.loop, server.stop())

    def test_callback_server_rejects_mismatched_state(self, tmp_path: Path):
        server = self._make_server(tmp_path)
        server.set_expected_state("good")
        _run_async(self.loop, server.start())
        try:
            host, port = server._server.sockets[0].getsockname()[:2]
            ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            async def _send() -> None:
                reader, writer = await asyncio.open_connection(
                    host, port, ssl=ssl_context
                )
                writer.write(b"GET /?code=c&state=bad HTTP/1.1\r\n\r\n")
                await writer.drain()
                _ = await reader.read(4096)
                writer.close()
                await writer.wait_closed()

            _run_async(self.loop, _send())
            with pytest.raises(asyncio.TimeoutError):
                _run_async(self.loop, server.wait_for_callback(timeout=0.2))
        finally:
            _run_async(self.loop, server.stop())

    def test_callback_server_timeout(self, tmp_path: Path):
        server = self._make_server(tmp_path)
        _run_async(self.loop, server.start())
        try:
            with pytest.raises(asyncio.TimeoutError):
                _run_async(self.loop, server.wait_for_callback(timeout=0.1))
        finally:
            _run_async(self.loop, server.stop())

    def test_callback_server_cert_validation(self, tmp_path: Path):
        server = CallbackServer(
            port=0,
            bind="127.0.0.1",
            cert_path=str(tmp_path / "missing.crt"),
            key_path=str(tmp_path / "missing.key"),
            loop=self.loop,
        )
        with pytest.raises(RuntimeError):
            _run_async(self.loop, server.start())


# ---------------------------------------------------------------------------
# OAuthProviderFactory
# ---------------------------------------------------------------------------


class TestOAuthProviderFactory:
    def test_build_returns_provider(self, tmp_path: Path):
        mcp_tokens_dir = tmp_path / "tokens"
        server_cfg = {
            "name": "myserver",
            "url": "https://mcp.example.com/sse",
            "oauth": {
                "client_id": "cid",
                "client_secret": "csec",
                "redirect_uri": "https://localhost/callback",
                "scope": "read",
                "cert_path": str(tmp_path / "cert.pem"),
                "key_path": str(tmp_path / "key.pem"),
                "callback_port": 8123,
                "callback_bind": "127.0.0.1",
            },
        }

        # Create dummy cert/key so CallbackServer can start later.
        cert_path, key_path = _make_self_signed_cert(tmp_path)
        server_cfg["oauth"]["cert_path"] = cert_path
        server_cfg["oauth"]["key_path"] = key_path

        cb_server = CallbackServer(
            port=server_cfg["oauth"]["callback_port"],
            bind=server_cfg["oauth"]["callback_bind"],
            cert_path=cert_path,
            key_path=key_path,
            loop=asyncio.new_event_loop(),
        )

        async def _build() -> OAuthClientProvider:
            return OAuthProviderFactory.build(server_cfg, mcp_tokens_dir, cb_server)

        loop = asyncio.new_event_loop()
        try:
            provider = loop.run_until_complete(_build())
        finally:
            loop.close()

        assert provider is not None

    def test_redirect_handler_no_telegram_is_noop(self, tmp_path: Path):
        from mcp_oauth import CallbackServer, make_redirect_handler

        cert_path, key_path = _make_self_signed_cert(tmp_path)
        loop = asyncio.new_event_loop()
        cb_server = CallbackServer(
            port=0,
            bind="127.0.0.1",
            cert_path=cert_path,
            key_path=key_path,
            loop=loop,
        )

        async def _run() -> None:
            handler = make_redirect_handler(None, "srv", cb_server)
            await handler("https://example.com?state=s")
            handler_with_chat = make_redirect_handler(None, "srv", cb_server, chat_id=123)
            await handler_with_chat("https://example.com?state=s")

        asyncio.run(_run())

    def test_callback_handler_delegates(self, tmp_path: Path):
        from mcp_oauth import make_callback_handler

        cert_path, key_path = _make_self_signed_cert(tmp_path)
        loop = asyncio.new_event_loop()
        server = CallbackServer(
            port=0,
            bind="127.0.0.1",
            cert_path=cert_path,
            key_path=key_path,
            loop=loop,
        )
        handler = make_callback_handler(server)

        async def _run() -> tuple[str, str | None]:
            return await handler()

        try:
            with pytest.raises(asyncio.TimeoutError):
                loop.run_until_complete(asyncio.wait_for(_run(), timeout=0.1))
        finally:
            loop.run_until_complete(server.stop())
            loop.close()
