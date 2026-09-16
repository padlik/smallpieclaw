"""Tests for mcp_oauth.py — OAuth helpers for MCP HTTP servers."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.exceptions import OAuthTokenError
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

from mcp_oauth import (
    CallbackServer,
    FileTokenStorage,
    OAuthProviderFactory,
    RefreshPreservingOAuthProvider,
    make_redirect_handler,
)


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


def _insecure_ssl_context() -> ssl.SSLContext:
    """Client context that accepts the tests' self-signed cert."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _port_is_listening(host: str, port: int) -> bool:
    """Return True if a TCP connect to ``host:port`` is accepted.

    Used to assert the OAuth callback listener is really gone, not just that the
    server object's handle was cleared.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


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

    def test_token_file_payload_shape(self, tmp_path: Path):
        """The written file nests the grant under ``token`` and stamps ``issued_at``.

        Asserted on the raw JSON rather than through ``get_tokens()``, which reads
        ``data.get("token") or data`` and accepts a flat layout too — a read-back
        test cannot tell the two shapes apart.
        """
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        asyncio.run(
            storage.set_tokens(
                OAuthToken(
                    access_token="a",
                    refresh_token="r",
                    scope="read",
                    expires_in=3600,
                )
            )
        )

        payload = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        assert set(payload) == {"token"}, "a normal flow writes no client_info block"
        token = payload["token"]
        assert token["access_token"] == "a"
        assert token["token_type"] == "Bearer"
        assert token["refresh_token"] == "r"
        assert token["scope"] == "read"
        assert token["expires_in"] == 3600
        assert isinstance(token["issued_at"], float)

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

    def test_refresh_grant_preserves_stored_refresh_token(self, tmp_path: Path):
        """A refresh-grant response (no refresh_token) must not wipe the stored one.

        Google-style refresh grants omit ``refresh_token`` from the response
        body. Asserting on the raw JSON (not ``get_tokens()``) pins the exact
        disk contract: the token object keeps ``refresh_token`` while
        ``access_token``/``expires_in``/``scope``/``issued_at`` all take the new
        grant's values.
        """
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        asyncio.run(
            storage.set_tokens(
                OAuthToken(
                    access_token="A1",
                    refresh_token="R1",
                    scope="scope-old",
                    expires_in=3600,
                )
            )
        )
        first_issued_at = json.loads(
            (tmp_path / "srv.json").read_text(encoding="utf-8")
        )["token"]["issued_at"]

        time.sleep(0.01)  # guarantee issued_at moves forward between writes
        asyncio.run(
            storage.set_tokens(
                OAuthToken(
                    access_token="A2",
                    token_type="Bearer",
                    # refresh_token deliberately omitted — refresh-grant shape
                    scope="scope-new",
                    expires_in=1800,
                )
            )
        )

        token = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))["token"]
        assert token["refresh_token"] == "R1", (
            "refresh-grant persistence must retain the stored refresh_token"
        )
        assert token["access_token"] == "A2"
        assert token["scope"] == "scope-new"
        assert token["expires_in"] == 1800
        assert token["issued_at"] > first_issued_at

    def test_incoming_refresh_token_overwrites_stored_one(self, tmp_path: Path):
        """Real rotation / full re-auth replaces the stored refresh_token."""
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        asyncio.run(
            storage.set_tokens(OAuthToken(access_token="A1", refresh_token="R1"))
        )
        asyncio.run(
            storage.set_tokens(OAuthToken(access_token="A2", refresh_token="R2"))
        )
        token = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))["token"]
        assert token["refresh_token"] == "R2"

    def test_no_stored_refresh_token_and_none_granted_stays_absent(self, tmp_path: Path):
        """Merge-preserve never invents a refresh_token that was never stored."""
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        asyncio.run(storage.set_tokens(OAuthToken(access_token="A1")))
        asyncio.run(storage.set_tokens(OAuthToken(access_token="A2")))
        token = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))["token"]
        assert "refresh_token" not in token

    def test_token_storage_preserves_client_info(self, tmp_path: Path):
        """A persisted client_info block survives a later token write and is reused.

        The cached ``client_id`` deliberately differs from the storage's configured
        one (as a real DCR response would), so the assertions cannot pass via the
        pre-seed fallback. ``redirect_uris`` must be non-None: ``set_client_info``
        persists with ``exclude_none=True``, and a dropped ``redirect_uris`` makes
        the block unparseable on reload, silently falling through to the pre-seed.
        """
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        info = OAuthClientInformationFull(
            client_id="dcr-cid",
            client_secret="csec",
            redirect_uris=["https://localhost:8765/callback"],
        )
        asyncio.run(storage.set_client_info(info))
        asyncio.run(storage.set_tokens(OAuthToken(access_token="a")))
        reloaded = asyncio.run(storage.get_client_info())
        assert reloaded is not None
        assert reloaded.client_id == "dcr-cid", "expected the cached path, not pre-seed"
        assert reloaded.client_secret == "csec"

    def test_pre_seed_includes_token_endpoint_auth_method(self, tmp_path: Path):
        """Pre-seed path carries the auth method so prepare_token_auth() sends the secret.

        Configures ``client_secret_post`` — deliberately *not* the constructor
        default — so the assertion fails if the parameter is ignored and the
        default is emitted instead. ``test_token_endpoint_auth_method_defaults_to_basic``
        covers the default separately.
        """
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
            token_endpoint_auth_method="client_secret_post",
        )
        info = asyncio.run(storage.get_client_info())
        assert info is not None
        assert info.token_endpoint_auth_method == "client_secret_post"

    def test_malformed_cached_client_info_falls_back_to_pre_seed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """An unparseable client_info block degrades to the pre-seed, not an exception.

        The bad block is written straight to the file rather than round-tripped
        through ``set_client_info``, so this covers the ``except (TypeError,
        ValueError)`` handler independently of how client_info is persisted.
        """
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        # redirect_uris is required by the SDK model, so omitting it fails validation.
        (tmp_path / "srv.json").write_text(
            json.dumps({"client_info": {"client_id": "dcr-cid", "client_secret": "csec"}}),
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger="mcp_oauth"):
            info = asyncio.run(storage.get_client_info())

        assert info is not None, "malformed client_info must not abort the OAuth flow"
        assert info.client_id == "cid", "expected the pre-seed, not the malformed block"
        assert info.token_endpoint_auth_method == "client_secret_basic"
        assert "malformed" in caplog.text

    def test_token_endpoint_auth_method_defaults_to_basic(self, tmp_path: Path):
        """Existing construction sites that omit the param keep working."""
        storage = FileTokenStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        assert storage.token_endpoint_auth_method == "client_secret_basic"
        info = asyncio.run(storage.get_client_info())
        assert info is not None
        assert info.token_endpoint_auth_method == "client_secret_basic"


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

    def test_callback_handler_closes_listener_on_success(self, tmp_path: Path):
        """The port must close the moment the code arrives, not when the flow ends."""
        from mcp_oauth import make_callback_handler

        server = self._make_server(tmp_path)
        _run_async(self.loop, server.start())
        host, port = server._server.sockets[0].getsockname()[:2]
        handler = make_callback_handler(server, timeout=5.0)

        async def _drive() -> tuple[str, str | None]:
            waiting = asyncio.ensure_future(handler())
            await asyncio.sleep(0)
            reader, writer = await asyncio.open_connection(
                host, port, ssl=_insecure_ssl_context()
            )
            writer.write(b"GET /?code=c1&state=s1 HTTP/1.1\r\n\r\n")
            await writer.drain()
            _ = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            return await waiting

        code, state = _run_async(self.loop, _drive())
        assert (code, state) == ("c1", "s1"), "the result must survive the early close"
        assert server._server is None, "listener must be closed once the code arrives"
        assert not _port_is_listening(host, port), f"port {port} still accepting"

    def test_bind_all_interfaces_delivers_success_page_before_close(self, tmp_path: Path):
        """A remote browser must still get the full success page despite the early close.

        The approval sequence can run on a different host, so ``callback_bind``
        defaults to ``0.0.0.0`` and must keep working. ``_handle`` resolves the
        future (mcp_oauth.py:323) *before* writing the success page, so the
        handler's close races the response flush. That is safe because
        ``Server.close()`` closes only listening sockets and leaves accepted
        connections to finish — this test pins that guarantee.
        """
        from mcp_oauth import make_callback_handler

        cert_path, key_path = _make_self_signed_cert(tmp_path)
        server = CallbackServer(
            port=0,
            bind="0.0.0.0",
            cert_path=cert_path,
            key_path=key_path,
            loop=self.loop,
        )
        _run_async(self.loop, server.start())
        bound = [sk.getsockname() for sk in server._server.sockets]
        assert any(addr == "0.0.0.0" for addr, *_ in bound), (
            f"must bind all interfaces for off-box approval, got {bound}"
        )
        port = bound[0][1]
        server.set_expected_state("st8")
        handler = make_callback_handler(server, timeout=5.0)

        async def _drive() -> tuple[bytes, tuple[str, str | None]]:
            waiting = asyncio.ensure_future(handler())
            await asyncio.sleep(0)
            # 0.0.0.0 covers loopback, so this exercises the all-interfaces bind
            # without depending on a routable LAN address being present.
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=_insecure_ssl_context()
            )
            writer.write(b"GET /?code=remote1&state=st8 HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            payload = await reader.read(8192)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return payload, await waiting

        payload, (code, state) = _run_async(self.loop, _drive())

        assert b"200 OK" in payload
        assert b"Auth complete, close this tab" in payload, (
            "early close truncated the response to the remote browser"
        )
        assert (code, state) == ("remote1", "st8")
        assert server._server is None
        assert not _port_is_listening("127.0.0.1", port)

    def test_callback_handler_closes_listener_on_timeout(self, tmp_path: Path):
        """A flow that never receives a callback must not leak the port either."""
        from mcp_oauth import make_callback_handler

        server = self._make_server(tmp_path)
        _run_async(self.loop, server.start())
        host, port = server._server.sockets[0].getsockname()[:2]
        handler = make_callback_handler(server, timeout=0.05)

        with pytest.raises(asyncio.TimeoutError):
            _run_async(self.loop, handler())

        assert server._server is None
        assert not _port_is_listening(host, port), f"port {port} leaked after timeout"

    def test_callback_server_is_restartable_after_stop(self, tmp_path: Path):
        """stop() must leave the instance reusable, not silently dead.

        Closing early is only safe if a second flow on the same instance really
        rebinds: start() previously no-opped because stop() left ``_server`` set,
        and the once-created future stayed cancelled.
        """
        server = self._make_server(tmp_path)
        _run_async(self.loop, server.start())
        assert server._server is not None
        _run_async(self.loop, server.stop())
        assert server._server is None

        _run_async(self.loop, server.start())
        assert server._server is not None, "start() after stop() must rebind"
        assert not server._future.done(), "a reused instance needs a fresh future"

        host, port = server._server.sockets[0].getsockname()[:2]

        async def _send() -> None:
            reader, writer = await asyncio.open_connection(
                host, port, ssl=_insecure_ssl_context()
            )
            writer.write(b"GET /?code=second&state=s2 HTTP/1.1\r\n\r\n")
            await writer.drain()
            _ = await reader.read(4096)
            writer.close()
            await writer.wait_closed()

        try:
            _run_async(self.loop, _send())
            code, _ = _run_async(self.loop, server.wait_for_callback(timeout=5.0))
            assert code == "second", "the second flow must resolve its own callback"
        finally:
            _run_async(self.loop, server.stop())

    def test_stop_is_idempotent(self, tmp_path: Path):
        """The owning flow's finally-block stop() must stay a harmless backstop."""
        server = self._make_server(tmp_path)
        _run_async(self.loop, server.start())
        _run_async(self.loop, server.stop())
        _run_async(self.loop, server.stop())
        assert server._server is None

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
        assert provider.context.storage.token_endpoint_auth_method == "client_secret_basic"

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

    def test_redirect_handler_non_interactive_callback(self, tmp_path: Path) -> None:
        """With on_non_interactive, degraded path signals and raises."""
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
        asyncio.set_event_loop(loop)
        loop.run_until_complete(cb_server.start())  # Real listener so we can verify stop()

        callback_ran: dict[str, bool] = {"ran": False}

        def _on_non_interactive() -> None:
            callback_ran["ran"] = True

        handler = make_redirect_handler(
            None, "srv", cb_server, chat_id=None,
            on_non_interactive=_on_non_interactive,
        )

        async def _run() -> None:
            with pytest.raises(RuntimeError, match="non-interactive OAuth redirect"):
                await handler("https://example.com?state=abc123")

        try:
            loop.run_until_complete(_run())
            assert callback_ran["ran"] is True
            assert cb_server._server is None, "callback server must be stopped"
        finally:
            loop.run_until_complete(cb_server.stop())
            loop.close()

    def test_redirect_handler_non_interactive_callback_exception_still_raises(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If on_non_interactive raises, the handler still logs, stops, and raises."""
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
        asyncio.set_event_loop(loop)
        loop.run_until_complete(cb_server.start())

        def _on_non_interactive() -> None:
            raise RuntimeError("boom")

        handler = make_redirect_handler(
            None, "srv", cb_server, chat_id=None,
            on_non_interactive=_on_non_interactive,
        )

        async def _run() -> None:
            with pytest.raises(RuntimeError, match="non-interactive OAuth redirect"):
                await handler("https://example.com?state=abc123")

        with caplog.at_level(logging.ERROR, logger="mcp_oauth"):
            try:
                loop.run_until_complete(_run())
            finally:
                loop.run_until_complete(cb_server.stop())
                loop.close()

        assert "on_non_interactive callback raised" in caplog.text
        assert cb_server._server is None


# ---------------------------------------------------------------------------
# RefreshPreservingOAuthProvider (refresh-token lifecycle contract)
# ---------------------------------------------------------------------------


class TestRefreshPreservingOAuthProvider:
    """Contract tests for the SDK refresh-response hook.

    The SDK (mcp 1.29.1, ``oauth2.py:_handle_refresh_response``) replaces
    ``context.current_tokens`` wholesale after a successful refresh grant, so a
    refresh response that omits ``refresh_token`` (as Google's do) makes
    ``can_refresh_token()`` false after the first refresh. These tests drive
    the provider's ``_handle_refresh_response`` directly with constructed
    ``httpx.Response`` objects — no network — on a provider built via the
    factory with a real ``FileTokenStorage`` rooted at ``tmp_path``.
    """

    def _build_provider(
        self, tmp_path: Path, server_name: str = "srv"
    ) -> OAuthClientProvider:
        """Build a provider the same way TestOAuthProviderFactory.build does.

        ``build`` is synchronous, so no loop is needed here; tests that drive
        async hooks create and close their own loop in a ``finally``.
        """
        cert_path, key_path = _make_self_signed_cert(tmp_path)
        server_cfg = {
            "name": server_name,
            "url": "https://mcp.example.com/sse",
            "oauth": {
                "client_id": "cid",
                "client_secret": "csec",
                "redirect_uri": "https://localhost/callback",
                "scope": "read",
                "cert_path": cert_path,
                "key_path": key_path,
                "callback_port": 8123,
                "callback_bind": "127.0.0.1",
            },
        }
        cb_server = CallbackServer(
            port=server_cfg["oauth"]["callback_port"],
            bind=server_cfg["oauth"]["callback_bind"],
            cert_path=cert_path,
            key_path=key_path,
            loop=asyncio.new_event_loop(),
        )
        mcp_tokens_dir = tmp_path / "tokens"
        # Production creates the tokens dir at startup (_create_xdg_dirs in
        # main.py) before any provider exists; mirror that so seeded writes land.
        mcp_tokens_dir.mkdir(parents=True, exist_ok=True)

        return OAuthProviderFactory.build(server_cfg, mcp_tokens_dir, cb_server)

    def _seed_tokens(self, provider: OAuthClientProvider, tmp_path: Path) -> None:
        """Load context.client_info and an RT-bearing token via the public API.

        ``async_auth_flow`` would do this on first use; driving the hook
        directly needs it seeded up front. Seeding through ``_initialize``
        exercises the real storage round trip.
        """
        storage = provider.context.storage
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                storage.set_tokens(
                    OAuthToken(
                        access_token="A1",
                        refresh_token="R1",
                        scope="read",
                        expires_in=3600,
                    )
                )
            )
            loop.run_until_complete(provider._initialize())
        finally:
            loop.close()

    def test_successful_refresh_reattaches_refresh_token(self, tmp_path: Path):
        """A 200 RT-less refresh response must leave the RT re-attached in memory.

        ``can_refresh_token()`` must stay true after the refresh so the *next*
        expiry refreshes silently instead of falling back to a full
        authorization flow (defect B).
        """
        provider = self._build_provider(tmp_path)
        self._seed_tokens(provider, tmp_path)
        assert provider.context.current_tokens is not None
        captured_rt = provider.context.current_tokens.refresh_token
        assert captured_rt == "R1", "fixture: seed must carry a refresh token"
        assert provider.context.can_refresh_token() is True

        response = httpx.Response(
            200,
            json={
                "access_token": "A2",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "read",
            },
        )

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                provider._handle_refresh_response(response)
            )
        finally:
            loop.close()

        assert result is True, "successful refresh must return True unchanged"
        current = provider.context.current_tokens
        assert current is not None
        assert current.refresh_token == "R1", (
            "in-memory refresh_token must survive an RT-less refresh grant"
        )
        assert current.access_token == "A2", "new access token must take effect"
        assert provider.context.can_refresh_token() is True, (
            "provider must stay refreshable after an RT-less refresh grant"
        )

    def test_failed_refresh_keeps_cleared_state(self, tmp_path: Path):
        """A non-200 refresh response clears tokens and the override must not re-attach.

        A revoked RT failing the grant is the *correct* moment to clear; the
        failure path is a strict no-op for the re-attachment logic (otherwise
        a revoked RT would be resurrected and re-arm a doomed refresh loop).
        """
        provider = self._build_provider(tmp_path)
        self._seed_tokens(provider, tmp_path)

        response = httpx.Response(400, json={"error": "invalid_grant"})

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                provider._handle_refresh_response(response)
            )
        finally:
            loop.close()

        assert result is False, "failed refresh must return False unchanged"
        assert provider.context.current_tokens is None, (
            "cleared state must be preserved — no resurrection of a dead RT"
        )
        assert provider.context.can_refresh_token() is False

    def test_successive_refreshes_keep_rt_on_disk_and_in_memory(self, tmp_path: Path):
        """Two consecutive RT-less refresh cycles never lose the refresh token.

        Mirrors the delta scenario's 6h-cadence case: expiry → silent refresh →
        expiry → silent refresh. The RT must survive on disk (via the storage
        merge) and in memory (via the re-attachment) after BOTH cycles.
        """
        provider = self._build_provider(tmp_path)
        self._seed_tokens(provider, tmp_path)
        token_file = tmp_path / "tokens" / "srv.json"

        def _read_file_rt() -> str | None:
            payload = json.loads(token_file.read_text(encoding="utf-8"))
            return payload["token"].get("refresh_token")

        assert _read_file_rt() == "R1", "fixture: seeded RT must be on disk"

        for expected_access in ("A2", "A3"):
            response = httpx.Response(
                200,
                json={
                    "access_token": expected_access,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "read",
                },
            )
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    provider._handle_refresh_response(response)
                )
            finally:
                loop.close()

            assert result is True
            current = provider.context.current_tokens
            assert current is not None
            assert current.refresh_token == "R1", (
                f"in-memory RT lost after refresh to {expected_access}"
            )
            assert current.access_token == expected_access
            assert _read_file_rt() == "R1", (
                f"disk RT lost after refresh to {expected_access}"
            )
            assert provider.context.can_refresh_token() is True

    def test_factory_builds_refresh_preserving_subclass(self, tmp_path: Path):
        """Both interactive and non-interactive sites get the preserving subclass."""
        from mcp_oauth import RefreshPreservingOAuthProvider

        provider = self._build_provider(tmp_path)
        assert isinstance(provider, RefreshPreservingOAuthProvider), (
            "OAuthProviderFactory.build must construct RefreshPreservingOAuthProvider"
        )

    def test_sdk_still_invokes_handle_refresh_response(self) -> None:
        """Drift guard for the private-API coupling in RefreshPreservingOAuthProvider.

        ``RefreshPreservingOAuthProvider._handle_refresh_response`` overrides an
        underscore-prefixed internal of the installed MCP SDK
        (``mcp.client.auth.oauth2``). The override is only reached if the SDK's
        refresh flow still awaits ``self._handle_refresh_response(...)``. A future
        SDK bump could rename or remove that method, and the override would then
        silently stop being invoked. This test pins that call site in the SDK
        source so an upgrade past the verified version (mcp 1.29.1) fails loudly
        and forces a re-verification of the refresh-token lifecycle wiring
        (design.md D2 risk note).
        """
        import mcp.client.auth.oauth2 as oauth2_module

        sdk_source = Path(oauth2_module.__file__).read_text(encoding="utf-8")
        assert "await self._handle_refresh_response(" in sdk_source, (
            "SDK refresh flow no longer calls _handle_refresh_response; "
            "RefreshPreservingOAuthProvider override is likely orphaned"
        )


# ---------------------------------------------------------------------------
# Extra auth params
# ---------------------------------------------------------------------------


class _FakeTgIface:
    """Minimal Telegram interface that captures the auth URL it receives."""

    def __init__(self) -> None:
        self.captured_url: str | None = None

    def send_oauth_prompt(
        self,
        chat_id: int,
        server_name: str,
        auth_url: str,
        timeout: int,
    ) -> asyncio.Future[None]:
        """Capture ``auth_url`` and return an immediately resolved future."""
        self.captured_url = auth_url
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        future.set_result(None)
        return future


class TestExtraAuthParams:
    """Tests for the ``extra_auth_params`` hook in ``make_redirect_handler``."""

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

    def test_extra_auth_params_appended_to_url(self, tmp_path: Path) -> None:
        """Configured Google params are appended to a standard SDK auth URL."""
        cb_server = self._make_server(tmp_path)
        tg_iface = _FakeTgIface()
        handler = make_redirect_handler(
            tg_iface,
            "srv",
            cb_server,
            chat_id=123,
            extra_auth_params={"access_type": "offline", "prompt": "consent"},
        )

        async def _run() -> None:
            await handler(
                "https://accounts.google.com/o/oauth2/auth?response_type=code"
                "&client_id=cid&redirect_uri=https://localhost/callback"
                "&state=st123&code_challenge=ch&code_challenge_method=S256&scope=openid"
            )

        _run_async(self.loop, _run())
        url = tg_iface.captured_url
        assert url is not None
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "state=st123" in url
        assert "client_id=cid" in url
        assert "response_type=code" in url

    def test_existing_params_not_overwritten(self, tmp_path: Path) -> None:
        """Params already present in the URL are preserved, not overwritten."""
        cb_server = self._make_server(tmp_path)
        tg_iface = _FakeTgIface()
        handler = make_redirect_handler(
            tg_iface,
            "srv",
            cb_server,
            chat_id=123,
            extra_auth_params={"access_type": "offline", "prompt": "consent"},
        )

        async def _run() -> None:
            await handler(
                "https://accounts.google.com/o/oauth2/auth?access_type=online&state=s"
            )

        _run_async(self.loop, _run())
        url = tg_iface.captured_url
        assert url is not None
        assert "access_type=online" in url
        assert "access_type=offline" not in url
        assert "prompt=consent" in url

    def test_custom_extra_auth_params(self, tmp_path: Path) -> None:
        """Explicit ``extra_auth_params`` replace the default (empty) injection set."""
        cb_server = self._make_server(tmp_path)
        tg_iface = _FakeTgIface()
        handler = make_redirect_handler(
            tg_iface, "srv", cb_server, chat_id=123, extra_auth_params={"foo": "bar"}
        )

        async def _run() -> None:
            await handler("https://example.com/auth?state=s")

        _run_async(self.loop, _run())
        url = tg_iface.captured_url
        assert url is not None
        assert "foo=bar" in url
        assert "access_type=offline" not in url
        assert "prompt=consent" not in url

    def test_no_query_string_uses_question_mark(self, tmp_path: Path) -> None:
        """URLs without an existing query string receive a ``?`` separator."""
        cb_server = self._make_server(tmp_path)
        tg_iface = _FakeTgIface()
        handler = make_redirect_handler(
            tg_iface,
            "srv",
            cb_server,
            chat_id=123,
            extra_auth_params={"foo": "bar"},
        )

        async def _run() -> None:
            await handler("https://example.com/auth")

        _run_async(self.loop, _run())
        url = tg_iface.captured_url
        assert url is not None
        assert "?foo=bar" in url

    def test_empty_extra_auth_params_disables_injection(self, tmp_path: Path) -> None:
        """An empty dict disables injection entirely."""
        cb_server = self._make_server(tmp_path)
        tg_iface = _FakeTgIface()
        handler = make_redirect_handler(
            tg_iface, "srv", cb_server, chat_id=123, extra_auth_params={}
        )

        async def _run() -> None:
            await handler("https://example.com/auth?state=s")

        _run_async(self.loop, _run())
        url = tg_iface.captured_url
        assert url is not None
        assert "access_type" not in url
        assert "prompt" not in url

    def test_none_extra_auth_params_is_no_injection(self, tmp_path: Path) -> None:
        """``None`` (the function default) behaves the same as an empty dict."""
        cb_server = self._make_server(tmp_path)
        tg_iface = _FakeTgIface()
        handler = make_redirect_handler(
            tg_iface, "srv", cb_server, chat_id=123, extra_auth_params=None
        )

        async def _run() -> None:
            await handler("https://example.com/auth?state=s")

        _run_async(self.loop, _run())
        url = tg_iface.captured_url
        assert url is not None
        assert "access_type" not in url
        assert "prompt" not in url

    def test_state_still_extracted_with_extra_params(self, tmp_path: Path) -> None:
        """State extraction continues to work alongside param injection."""
        cb_server = self._make_server(tmp_path)
        tg_iface = _FakeTgIface()
        handler = make_redirect_handler(
            tg_iface,
            "srv",
            cb_server,
            chat_id=123,
            extra_auth_params={"access_type": "offline", "prompt": "consent"},
        )

        async def _run() -> None:
            await handler("https://example.com/auth?state=expected-state-xyz")

        _run_async(self.loop, _run())
        assert cb_server.expected_state == "expected-state-xyz"

    def test_default_is_no_injection(self, tmp_path: Path) -> None:
        """Default ``make_redirect_handler`` call does not inject any params."""
        cb_server = self._make_server(tmp_path)
        tg_iface = _FakeTgIface()
        handler = make_redirect_handler(tg_iface, "srv", cb_server, chat_id=123)

        async def _run() -> None:
            await handler("https://example.com/auth?state=s")

        _run_async(self.loop, _run())
        url = tg_iface.captured_url
        assert url is not None
        assert url == "https://example.com/auth?state=s"
        assert "access_type" not in url
        assert "prompt" not in url

    def test_factory_wires_extra_auth_params(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``OAuthProviderFactory.build`` passes extra_auth_params from config.

        The degraded path (tg_iface=None) logs the full auth_url at WARNING
        (mcp_oauth.py:521–526).  We capture that log line and assert the
        injected params are present — proving the factory wired the config
        through to the handler.  Without the wiring, the URL would contain
        only the original ``state=st`` and no ``access_type``.
        """
        import logging

        mcp_tokens_dir = tmp_path / "tokens"
        cert_path, key_path = _make_self_signed_cert(tmp_path)
        server_cfg = {
            "name": "myserver",
            "url": "https://mcp.example.com/sse",
            "oauth": {
                "client_id": "cid",
                "client_secret": "csec",
                "redirect_uri": "https://localhost/callback",
                "scope": "read",
                "cert_path": cert_path,
                "key_path": key_path,
                "callback_port": 8123,
                "callback_bind": "127.0.0.1",
                "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
            },
        }

        cb_server = CallbackServer(
            port=server_cfg["oauth"]["callback_port"],
            bind=server_cfg["oauth"]["callback_bind"],
            cert_path=cert_path,
            key_path=key_path,
            loop=self.loop,
        )

        async def _build() -> OAuthClientProvider:
            return OAuthProviderFactory.build(server_cfg, mcp_tokens_dir, cb_server)

        provider = _run_async(self.loop, _build())
        assert provider is not None
        redirect_handler = provider.context.redirect_handler
        assert redirect_handler is not None

        # Provider's handler has tg_iface=None/chat_id=None, so it logs the
        # full (mutated) auth_url at WARNING in the degraded path.  Capture
        # that log line and assert the injected params are present.
        with caplog.at_level(logging.WARNING, logger="mcp_oauth"):
            _run_async(
                self.loop,
                redirect_handler(
                    "https://example.com/auth?response_type=code&state=st"
                ),
            )
        assert cb_server.expected_state == "st"
        # The WARNING log line contains the full URL with injected params.
        joined = " ".join(r.message for r in caplog.records)
        assert "access_type=offline" in joined, (
            "factory must wire extra_auth_params — URL should contain access_type=offline"
        )
        assert "prompt=consent" in joined, (
            "factory must wire extra_auth_params — URL should contain prompt=consent"
        )


# ---------------------------------------------------------------------------
# SDK drift guards (b9b3a24 executable-assertion pattern; design D5)
# ---------------------------------------------------------------------------


class TestSdkDriftGuards:
    """Pin the SDK couplings the restore overrides depend on (mcp 1.29.1).

    ``RefreshPreservingOAuthProvider`` extends three private SDK internals.
    If an SDK upgrade changes them, the overrides silently stop working —
    these guards fail loudly instead, forcing a re-verification:

    - (a) if upstream #1784 lands (``_initialize`` writing
      ``token_expiry_time``), the D1 expiry restore becomes a double-write
      and must be removed;
    - (b) if upstream #2492/#2875 land (``_refresh_token`` no longer reading
      ``oauth_metadata.token_endpoint`` with the ``/token`` fallback), the
      metadata restore must be re-verified;
    - (c) if the flow stops awaiting ``_handle_token_response``, the D3
      metadata persist is orphaned.
    """

    def test_sdk_initialize_does_not_write_token_expiry_time(self) -> None:
        """Guard (a): the upstream expiry-restore gap is still present."""
        source = inspect.getsource(OAuthClientProvider._initialize)
        assert "token_expiry_time" not in source, (
            "SDK _initialize now writes token_expiry_time (upstream #1784?) — "
            "remove the RefreshPreservingOAuthProvider expiry restore"
        )

    def test_sdk_refresh_token_still_reads_metadata_endpoint_with_fallback(self) -> None:
        """Guard (b): refresh grants aim at oauth_metadata.token_endpoint."""
        source = inspect.getsource(OAuthClientProvider._refresh_token)
        assert "oauth_metadata.token_endpoint" in source, (
            "SDK _refresh_token no longer reads oauth_metadata.token_endpoint "
            "(upstream #2492/#2875?) — re-verify the metadata restore"
        )
        assert 'urljoin(auth_base_url, "/token")' in source, (
            "SDK _refresh_token lost the <server-base>/token fallback — "
            "re-verify the metadata restore and the pre-migration degradation"
        )

    def test_sdk_flow_still_invokes_handle_token_response(self) -> None:
        """Guard (c): orphan-detection for the metadata-persist override."""
        source = inspect.getsource(OAuthClientProvider.async_auth_flow)
        assert "await self._handle_token_response(" in source, (
            "SDK flow no longer calls _handle_token_response; the metadata "
            "persist override is likely orphaned"
        )


# ---------------------------------------------------------------------------
# Storage: read_issued_at / oauth_metadata (tasks 3.2, 3.3)
# ---------------------------------------------------------------------------


class TestReadIssuedAt:
    """Matrix for ``FileTokenStorage.read_issued_at`` (delta spec scenario
    "issued_at readable for expiry restoration")."""

    def _storage(self, tmp_path: Path) -> FileTokenStorage:
        return FileTokenStorage(
            server_name="srv", mcp_tokens_dir=tmp_path, client_id="cid", client_secret="csec"
        )

    def _write(self, tmp_path: Path, payload: dict) -> None:
        (tmp_path / "srv.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_returns_stored_value(self, tmp_path: Path):
        self._write(tmp_path, {"token": {"access_token": "A1", "issued_at": 1757985041.5}})
        assert self._storage(tmp_path).read_issued_at() == 1757985041.5

    def test_integer_stamp_is_coerced_to_float(self, tmp_path: Path):
        self._write(tmp_path, {"token": {"access_token": "A1", "issued_at": 1757985041}})
        assert self._storage(tmp_path).read_issued_at() == 1757985041.0

    def test_no_file_returns_none(self, tmp_path: Path):
        assert self._storage(tmp_path).read_issued_at() is None

    def test_no_token_block_returns_none(self, tmp_path: Path):
        self._write(tmp_path, {"client_info": {"client_id": "cid"}})
        assert self._storage(tmp_path).read_issued_at() is None

    def test_no_issued_at_key_returns_none(self, tmp_path: Path):
        self._write(tmp_path, {"token": {"access_token": "A1"}})
        assert self._storage(tmp_path).read_issued_at() is None

    def test_non_numeric_string_returns_none(self, tmp_path: Path):
        self._write(tmp_path, {"token": {"access_token": "A1", "issued_at": "yesterday"}})
        assert self._storage(tmp_path).read_issued_at() is None

    def test_bool_stamp_returns_none(self, tmp_path: Path):
        self._write(tmp_path, {"token": {"access_token": "A1", "issued_at": True}})
        assert self._storage(tmp_path).read_issued_at() is None

    def test_synchronous_no_event_loop_required(self, tmp_path: Path):
        """Mirrors read_status: a plain call must not need an event loop."""
        self._write(tmp_path, {"token": {"access_token": "A1", "issued_at": 1757985041.5}})
        storage = self._storage(tmp_path)
        assert inspect.iscoroutinefunction(storage.read_issued_at) is False
        # The direct call itself is the proof: no get_event_loop anywhere.
        assert storage.read_issued_at() == 1757985041.5


class TestOAuthMetadataStorage:
    """``get_oauth_metadata`` / ``set_oauth_metadata`` (delta spec scenarios
    "OAuth metadata persisted after a completed authorization" and
    "Malformed oauth metadata block falls back to None")."""

    _METADATA = {
        "issuer": "https://accounts.example.test/",
        "authorization_endpoint": "https://accounts.example.test/authorize",
        "token_endpoint": "https://tokens.example.test/token",
        "response_types_supported": ["code"],
    }

    def _storage(self, tmp_path: Path) -> FileTokenStorage:
        return FileTokenStorage(
            server_name="srv", mcp_tokens_dir=tmp_path, client_id="cid", client_secret="csec"
        )

    def _make_metadata(self) -> OAuthMetadata:
        return OAuthMetadata.model_validate(dict(self._METADATA))

    def test_round_trip(self, tmp_path: Path):
        storage = self._storage(tmp_path)
        asyncio.run(storage.set_oauth_metadata(self._make_metadata()))
        loaded = asyncio.run(storage.get_oauth_metadata())
        assert loaded == self._make_metadata()

    def test_set_merges_into_existing_token_file(self, tmp_path: Path):
        """set_oauth_metadata must not clobber the token block (atomic merge)."""
        storage = self._storage(tmp_path)
        asyncio.run(
            storage.set_tokens(
                OAuthToken(access_token="A1", refresh_token="R1", scope="read", expires_in=3600)
            )
        )
        asyncio.run(storage.set_oauth_metadata(self._make_metadata()))
        payload = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        assert payload["token"]["access_token"] == "A1", "merge clobbered the token block"
        assert payload["oauth_metadata"]["token_endpoint"] == self._METADATA["token_endpoint"]

    def test_malformed_block_warns_and_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        storage = self._storage(tmp_path)
        (tmp_path / "srv.json").write_text(
            json.dumps({"token": {"access_token": "A1"}, "oauth_metadata": {"issuer": "not-a-url"}}),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="mcp_oauth"):
            loaded = asyncio.run(storage.get_oauth_metadata())
        assert loaded is None, "malformed oauth_metadata must not raise"
        assert "srv" in caplog.text, "the warning must name the server"
        assert "oauth_metadata" in caplog.text

    def test_malformed_block_of_wrong_type_returns_none(self, tmp_path: Path):
        """A schema-drifted non-dict block degrades to None, not an exception."""
        storage = self._storage(tmp_path)
        (tmp_path / "srv.json").write_text(
            json.dumps({"token": {"access_token": "A1"}, "oauth_metadata": "flat-string"}),
            encoding="utf-8",
        )
        assert asyncio.run(storage.get_oauth_metadata()) is None

    def test_missing_block_returns_none_without_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        storage = self._storage(tmp_path)
        asyncio.run(storage.set_tokens(OAuthToken(access_token="A1")))
        with caplog.at_level(logging.WARNING, logger="mcp_oauth"):
            loaded = asyncio.run(storage.get_oauth_metadata())
        assert loaded is None
        assert "malformed" not in caplog.text, (
            "a merely-absent block must not be reported as malformed"
        )


# ---------------------------------------------------------------------------
# Shared fixtures for the restore-contract tests (ADR-0031)
# ---------------------------------------------------------------------------

_OAUTH_METADATA = {
    "issuer": "https://accounts.example.test/",
    "authorization_endpoint": "https://accounts.example.test/authorize",
    "token_endpoint": "https://tokens.example.test/token",
    "response_types_supported": ["code"],
}


def _make_storage(tmp_path: Path) -> FileTokenStorage:
    """A FileTokenStorage rooted at tmp_path, like the factory builds."""
    return FileTokenStorage(
        server_name="srv", mcp_tokens_dir=tmp_path, client_id="cid", client_secret="csec"
    )


def _make_test_provider(storage: FileTokenStorage) -> RefreshPreservingOAuthProvider:
    """Build a provider directly (no factory, no certs) with aborting handlers.

    The redirect handler raises like the non-interactive session abort
    (ADR-0030), so a degradation that falls back to the full authorization
    flow surfaces as RuntimeError instead of hanging on a callback.
    """

    async def _redirect(url: str) -> None:
        raise RuntimeError("non-interactive abort")

    async def _callback() -> tuple[str, str | None]:
        raise AssertionError("callback handler must not run in this flow")

    return RefreshPreservingOAuthProvider(
        server_url="https://mcp.example.com/sse",
        client_metadata=OAuthClientMetadata(
            redirect_uris=["https://localhost/callback"],
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method="client_secret_basic",
            scope="read",
            client_name="smallpieclaw",
        ),
        storage=storage,
        redirect_handler=_redirect,
        callback_handler=_callback,
        timeout=5.0,
    )


def _seed_token_file(
    tmp_path: Path, token: dict, metadata: dict | None = None
) -> dict:
    """Write a raw token file (as a pre-change deploy would have on disk)."""
    payload: dict = {"token": token}
    if metadata is not None:
        payload["oauth_metadata"] = metadata
    (tmp_path / "srv.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


class _RecordingStorage(FileTokenStorage):
    """FileTokenStorage that records the order of persist calls."""

    def __init__(
        self, server_name: str, mcp_tokens_dir: Path, client_id: str, client_secret: str
    ) -> None:
        super().__init__(server_name, mcp_tokens_dir, client_id, client_secret)
        self.calls: list[str] = []

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.calls.append("set_tokens")
        await super().set_tokens(tokens)

    async def set_oauth_metadata(self, metadata: OAuthMetadata) -> None:
        self.calls.append("set_oauth_metadata")
        await super().set_oauth_metadata(metadata)


class _PersistFailsStorage(FileTokenStorage):
    """FileTokenStorage whose metadata persist always fails (disk-error double)."""

    async def set_oauth_metadata(self, metadata: OAuthMetadata) -> None:
        raise OSError("simulated disk failure")


# ---------------------------------------------------------------------------
# Storage: set_tokens carry-forward (task 3.4b — ADR-0031 on-disk contract)
# ---------------------------------------------------------------------------


class TestSetTokensCarryForward:
    """``set_tokens`` carries ``oauth_metadata`` forward alongside ``client_info``.

    The ADR-0029 merge-preserve lesson generalized to the new key: a block
    present on disk before a token write survives it, so the first silent
    refresh never wipes the persisted metadata (delta scenario "Silent
    refresh preserves the stored oauth metadata").
    """

    def test_silent_refresh_carries_metadata_and_client_info(self, tmp_path: Path):
        storage = _make_storage(tmp_path)
        _seed_token_file(
            tmp_path,
            token={
                "access_token": "A1",
                "refresh_token": "R1",
                "expires_in": 3600,
                "issued_at": 1000.0,
            },
            metadata=dict(_OAUTH_METADATA),
        )
        # A persisted client_info block rides along the same path.
        payload_path = tmp_path / "srv.json"
        with payload_path.open(encoding="utf-8") as fh:
            seeded = json.load(fh)
        seeded["client_info"] = {
            "client_id": "dcr-cid",
            "client_secret": "csec",
            "redirect_uris": ["https://localhost:8765/callback"],
        }
        payload_path.write_text(json.dumps(seeded), encoding="utf-8")

        # Refresh-grant shape: new access token, no incoming refresh_token.
        asyncio.run(storage.set_tokens(OAuthToken(access_token="A2", expires_in=1800)))

        after = json.loads(payload_path.read_text(encoding="utf-8"))
        assert after["token"]["access_token"] == "A2"
        assert after["token"]["refresh_token"] == "R1", "ADR-0029 merge-preserve"
        assert after["token"]["issued_at"] > 1000.0, "issued_at must re-stamp"
        assert after["client_info"]["client_id"] == "dcr-cid"
        assert (
            after["oauth_metadata"]["token_endpoint"]
            == "https://tokens.example.test/token"
        ), "oauth_metadata block must survive a silent-refresh write"

    def test_metadata_survives_when_no_client_info_exists(self, tmp_path: Path):
        """Carry-forward never invents a client_info block either."""
        storage = _make_storage(tmp_path)
        _seed_token_file(
            tmp_path,
            token={"access_token": "A1", "expires_in": 3600, "issued_at": 1000.0},
            metadata=dict(_OAUTH_METADATA),
        )

        asyncio.run(storage.set_tokens(OAuthToken(access_token="A2", expires_in=1800)))

        after = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        assert "client_info" not in after, "no client_info block was stored"
        assert after["oauth_metadata"]["token_endpoint"] == (
            "https://tokens.example.test/token"
        )


# ---------------------------------------------------------------------------
# Provider: _initialize restore matrix (task 3.5 — design D1/D2)
# ---------------------------------------------------------------------------


class TestInitializeRestore:
    """Matrix for the ``_initialize`` override over a real FileTokenStorage.

    Pins the restore contract and the pre-migration skips: a file written
    before this change (missing ``issued_at`` or ``oauth_metadata``) loads
    exactly as before.
    """

    def test_expiry_restored_from_issued_at_plus_expires_in(self, tmp_path: Path):
        storage = _make_storage(tmp_path)
        _seed_token_file(
            tmp_path,
            token={"access_token": "A1", "expires_in": 3600, "issued_at": 1000.0},
        )
        provider = _make_test_provider(storage)

        asyncio.run(provider._initialize())

        assert provider.context.current_tokens is not None
        assert provider.context.current_tokens.access_token == "A1"
        assert provider.context.token_expiry_time == 1000.0 + 3600, (
            "expiry must be restored from the stored stamp, not from now()"
        )

    def test_missing_issued_at_leaves_expiry_unset(self, tmp_path: Path):
        storage = _make_storage(tmp_path)
        _seed_token_file(tmp_path, token={"access_token": "A1", "expires_in": 3600})
        provider = _make_test_provider(storage)

        asyncio.run(provider._initialize())

        assert provider.context.current_tokens is not None
        assert provider.context.token_expiry_time is None, (
            "a pre-migration file without issued_at must not restore an expiry"
        )

    def test_missing_expires_in_leaves_expiry_unset(self, tmp_path: Path):
        storage = _make_storage(tmp_path)
        _seed_token_file(tmp_path, token={"access_token": "A1", "issued_at": 1000.0})
        provider = _make_test_provider(storage)

        asyncio.run(provider._initialize())

        assert provider.context.current_tokens is not None
        assert provider.context.token_expiry_time is None

    def test_no_token_file_is_a_no_op(self, tmp_path: Path):
        provider = _make_test_provider(_make_storage(tmp_path))

        asyncio.run(provider._initialize())

        assert provider.context.current_tokens is None
        assert provider.context.token_expiry_time is None
        assert provider.context.oauth_metadata is None

    def test_metadata_restored_when_present(self, tmp_path: Path):
        storage = _make_storage(tmp_path)
        _seed_token_file(
            tmp_path,
            token={"access_token": "A1", "expires_in": 3600, "issued_at": 1000.0},
            metadata=dict(_OAUTH_METADATA),
        )
        provider = _make_test_provider(storage)

        asyncio.run(provider._initialize())

        assert provider.context.oauth_metadata is not None
        assert str(provider.context.oauth_metadata.token_endpoint) == (
            "https://tokens.example.test/token"
        )

    def test_malformed_metadata_restores_none(self, tmp_path: Path):
        """Malformed block degrades to None — the value, not 'unchanged'."""
        storage = _make_storage(tmp_path)
        _seed_token_file(
            tmp_path,
            token={"access_token": "A1", "expires_in": 3600, "issued_at": 1000.0},
            metadata={"issuer": "not-a-url"},
        )
        provider = _make_test_provider(storage)

        asyncio.run(provider._initialize())

        assert provider.context.oauth_metadata is None, (
            "a fresh provider with malformed metadata must start without it"
        )
        assert provider.context.token_expiry_time == 1000.0 + 3600, (
            "the expiry restore is independent of the metadata block"
        )

    def test_metadata_not_restored_without_tokens(self, tmp_path: Path):
        """With no token block there is nothing to restore for.

        A provider reconstructed from persisted *tokens* is the restore
        contract's scope (ADR-0031); a metadata-only file is unreachable via
        the public flow (design D4). The early return skips the metadata
        restore there — the 401 branch re-discovers metadata anyway.
        """
        storage = _make_storage(tmp_path)
        (tmp_path / "srv.json").write_text(
            json.dumps({"oauth_metadata": dict(_OAUTH_METADATA)}), encoding="utf-8"
        )
        provider = _make_test_provider(storage)

        asyncio.run(provider._initialize())

        assert provider.context.current_tokens is None
        assert provider.context.token_expiry_time is None
        assert provider.context.oauth_metadata is None, (
            "no persisted tokens means no restore — nothing to serve"
        )


# ---------------------------------------------------------------------------
# Provider: _handle_token_response metadata-persist path (task 3.6 — design D3)
# ---------------------------------------------------------------------------


class TestHandleTokenResponseMetadataPersist:
    """The metadata-persist ordering (delta scenario "OAuth metadata
    persisted after a completed authorization").

    A persist bug here would ship the exact production defect this change
    fixes with the suite green, so both clauses are pinned: the block is
    written only after a completed exchange, and a failed exchange writes
    nothing.
    """

    def _make_provider(
        self, tmp_path: Path, *, with_metadata: bool = True
    ) -> tuple[RefreshPreservingOAuthProvider, _RecordingStorage]:
        storage = _RecordingStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        provider = _make_test_provider(storage)
        if with_metadata:
            provider.context.oauth_metadata = OAuthMetadata.model_validate(
                dict(_OAUTH_METADATA)
            )
        return provider, storage

    def test_successful_exchange_persists_metadata_after_tokens(
        self, tmp_path: Path
    ):
        provider, storage = self._make_provider(tmp_path)
        response = httpx.Response(
            200,
            json={
                "access_token": "A2",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "read",
            },
        )

        asyncio.run(provider._handle_token_response(response))

        assert storage.calls == ["set_tokens", "set_oauth_metadata"], (
            "metadata must be persisted strictly after the exchange completed"
        )
        payload = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        assert set(payload) == {"token", "oauth_metadata"}, (
            "a metadata-only file state is unreachable: set_tokens ran first"
        )
        assert (
            payload["oauth_metadata"]["token_endpoint"]
            == "https://tokens.example.test/token"
        )
        assert "issued_at" in payload["token"], "set_tokens must stamp issued_at"

    def test_failed_exchange_writes_no_metadata(self, tmp_path: Path):
        provider, storage = self._make_provider(tmp_path)
        response = httpx.Response(400, json={"error": "invalid_grant"})

        with pytest.raises(OAuthTokenError):
            asyncio.run(provider._handle_token_response(response))

        assert storage.calls == [], "a failed exchange must persist nothing"
        assert not (tmp_path / "srv.json").exists()

    def test_metadata_persist_failure_does_not_abort_completed_exchange(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """A cosmetic metadata-write failure must not fail a completed auth.

        The exchange has already succeeded and ``set_tokens`` has persisted
        the new tokens; a subsequent ``set_oauth_metadata`` disk error
        (full disk, permission flip) must degrade to a warning — next
        session falls back to the ``/token`` endpoint until re-auth —
        rather than surfacing as an auth failure to the operator.
        """
        storage = _PersistFailsStorage(
            server_name="srv",
            mcp_tokens_dir=tmp_path,
            client_id="cid",
            client_secret="csec",
        )
        provider = _make_test_provider(storage)
        provider.context.oauth_metadata = OAuthMetadata.model_validate(
            dict(_OAUTH_METADATA)
        )
        response = httpx.Response(
            200,
            json={
                "access_token": "A2",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "read",
            },
        )

        with caplog.at_level(logging.WARNING, logger="mcp_oauth"):
            asyncio.run(provider._handle_token_response(response))

        payload = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        assert payload["token"]["access_token"] == "A2", (
            "the completed exchange's tokens must be on disk"
        )
        assert provider.context.current_tokens is not None
        assert provider.context.current_tokens.access_token == "A2"
        assert "srv" in caplog.text, "the warning must name the server"

    def test_exchange_without_metadata_persists_no_metadata_block(
        self, tmp_path: Path
    ):
        """No discovery metadata in context → the override is a passthrough."""
        provider, storage = self._make_provider(tmp_path, with_metadata=False)
        response = httpx.Response(
            200, json={"access_token": "A2", "token_type": "Bearer", "expires_in": 3600}
        )

        asyncio.run(provider._handle_token_response(response))

        assert storage.calls == ["set_tokens"]
        payload = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        assert set(payload) == {"token"}


# ---------------------------------------------------------------------------
# Provider: end-to-end reconstructed-provider silent refresh (tasks 4.1–4.5)
# ---------------------------------------------------------------------------


class TestE2EReconstructedProviderRefresh:
    """Silent refresh through the real SDK flow for a provider reconstructed
    from disk.

    Fresh, UN-INITIALIZED providers over a real ``FileTokenStorage`` — the
    SDK calls ``_initialize`` only when ``not self._initialized``
    (oauth2.py:494), so these drives exercise the restore overrides for
    real; a pre-initialized provider would pass vacuously. The generator
    harness feeds scripted ``httpx`` responses; the redirect handler raises,
    mirroring the non-interactive session abort (ADR-0030) that any
    degradation must fall back to.
    """

    def _make_provider(
        self,
        tmp_path: Path,
        *,
        issued_at: float | None = None,
        include_issued_at: bool = True,
        with_metadata: bool = True,
    ) -> RefreshPreservingOAuthProvider:
        storage = _make_storage(tmp_path)
        token: dict = {
            "access_token": "A1",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "R1",
        }
        if include_issued_at:
            # Default: issued two hours ago → long expired for expires_in=3600.
            token["issued_at"] = time.time() - 7200.0 if issued_at is None else issued_at
        _seed_token_file(
            tmp_path, token=token, metadata=dict(_OAUTH_METADATA) if with_metadata else None
        )
        return _make_test_provider(storage)

    def _drive(
        self, provider: RefreshPreservingOAuthProvider, responses: list[httpx.Response]
    ) -> tuple[list[httpx.Request], Exception | None]:
        """Drive async_auth_flow with scripted responses; return (requests, exc)."""

        async def _run() -> tuple[list[httpx.Request], Exception | None]:
            flow = provider.async_auth_flow(
                httpx.Request("GET", "https://mcp.example.com/sse")
            )
            seen: list[httpx.Request] = []
            response: httpx.Response | None = None
            exc: Exception | None = None
            try:
                while True:
                    try:
                        request = await flow.asend(response)
                    except StopAsyncIteration:
                        break
                    seen.append(request)
                    response = responses[min(len(seen), len(responses)) - 1]
            except Exception as caught:  # harness reports; assertions decide
                exc = caught
            finally:
                await flow.aclose()
            return seen, exc

        return asyncio.run(_run())

    @staticmethod
    def _refresh_ok() -> httpx.Response:
        """A Google-shaped refresh grant: 200, no refresh_token in the body."""
        return httpx.Response(
            200,
            json={
                "access_token": "A2",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "read",
            },
        )

    @staticmethod
    def _not_found() -> httpx.Response:
        return httpx.Response(404)

    def test_expired_token_refreshes_proactively_to_restored_endpoint(
        self, tmp_path: Path
    ):
        """The FIRST yielded request is a refresh POST to the restored endpoint.

        Not ``<server-base>/token`` (the SDK fallback), and not an on-401
        refresh — the refresh fires before any request carries the stale
        access token (delta scenario "Silent token refresh on restored
        expiry").
        """
        provider = self._make_provider(tmp_path)

        seen, exc = self._drive(provider, [self._refresh_ok(), httpx.Response(200)])

        assert exc is None
        assert len(seen) == 2, f"expected refresh + retry, got {seen}"
        refresh = seen[0]
        assert refresh.method == "POST"
        assert str(refresh.url) == "https://tokens.example.test/token", (
            "the refresh grant must aim at the restored token endpoint"
        )
        assert str(refresh.url) != "https://mcp.example.com/token", (
            "the SDK <server-base>/token fallback must not be used"
        )
        body = parse_qs(refresh.content.decode("utf-8"))
        assert body["grant_type"] == ["refresh_token"]
        assert body["refresh_token"] == ["R1"]

    def test_successful_rtless_refresh_persists_and_retries_with_new_bearer(
        self, tmp_path: Path
    ):
        """A 200 RT-less refresh (Google shape) keeps the RT on disk and in
        memory, carries the metadata forward, and the retried request
        carries the new Bearer token (delta scenario "Successive silent
        refreshes never lose the refresh token")."""
        provider = self._make_provider(tmp_path)
        seeded = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))

        seen, exc = self._drive(provider, [self._refresh_ok(), httpx.Response(200)])

        assert exc is None
        retried = seen[1]
        assert str(retried.url) == "https://mcp.example.com/sse"
        assert retried.headers.get("Authorization") == "Bearer A2", (
            "the server never received the stale access token"
        )
        after = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        assert after["token"]["access_token"] == "A2"
        assert after["token"]["refresh_token"] == "R1", (
            "ADR-0029: the refresh token must be merge-preserved on disk"
        )
        assert after["token"]["issued_at"] > seeded["token"]["issued_at"]
        assert after["oauth_metadata"] == seeded["oauth_metadata"], (
            "task 1.3: the metadata block must be carried forward across the refresh write"
        )
        assert provider.context.current_tokens is not None
        assert provider.context.current_tokens.refresh_token == "R1", (
            "ADR-0029: the in-memory RT must be re-attached for the next expiry"
        )

    def test_pre_migration_file_degrades_to_fallback_endpoint(
        self, tmp_path: Path
    ):
        """No oauth_metadata block → refresh POSTs to ``<server-base>/token``,
        the non-200 clears in-memory tokens only, and the on-disk RT
        survives (delta scenario "Pre-migration token file degrades to
        today's behavior")."""
        provider = self._make_provider(tmp_path, with_metadata=False)
        before = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        responses = [
            httpx.Response(400, json={"error": "invalid_grant"}),
            httpx.Response(401),
            *[self._not_found() for _ in range(6)],
        ]

        seen, exc = self._drive(provider, responses)

        assert isinstance(exc, RuntimeError), (
            "degradation must end in the non-interactive abort, not a crash"
        )
        assert str(seen[0].url) == "https://mcp.example.com/token", (
            "without restored metadata the SDK fallback endpoint is used"
        )
        body = parse_qs(seen[0].content.decode("utf-8"))
        assert body["grant_type"] == ["refresh_token"]
        assert provider.context.current_tokens is None, (
            "the failed refresh clears in-memory tokens only"
        )
        after = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        assert after["token"]["refresh_token"] == "R1", (
            "the on-disk refresh token must survive the degradation"
        )
        assert after["token"]["issued_at"] == before["token"]["issued_at"]
        assert "oauth_metadata" not in after, (
            "the degradation path must never invent a metadata block"
        )

    def test_pre_migration_file_without_issued_at_skips_restore(self, tmp_path: Path):
        """No issued_at stamp → expiry restore skipped → the stale token goes
        out as-is, the 401 runs the full flow, and the abort degrades
        exactly as before this change (delta scenario "Token file without
        issued_at degrades to today's behavior")."""
        provider = self._make_provider(tmp_path, include_issued_at=False)

        seen, exc = self._drive(
            provider, [httpx.Response(401), *[self._not_found() for _ in range(6)]]
        )

        assert isinstance(exc, RuntimeError)
        assert seen[0].method == "GET", (
            "without a restored expiry the SDK treats the token as valid — "
            "the first request is the original call, not a refresh"
        )
        assert seen[0].headers.get("Authorization") == "Bearer A1"
        assert not any(request.method == "POST" for request in seen), (
            "no refresh grant may be attempted without a restored expiry"
        )

    def test_server_side_401_does_not_refresh_and_leaves_disk_untouched(
        self, tmp_path: Path
    ):
        """Clock-valid token rejected server-side → no refresh grant, the
        next request is discovery, and the abort leaves the on-disk file
        untouched (delta scenario "Silent token refresh on 401", rewritten
        post-freeze)."""
        provider = self._make_provider(tmp_path, issued_at=time.time() - 60.0)
        before = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        responses = [httpx.Response(401), *[self._not_found() for _ in range(6)]]

        seen, exc = self._drive(provider, responses)

        assert isinstance(exc, RuntimeError), (
            "degradation must end in the non-interactive abort"
        )
        assert seen[0].headers.get("Authorization") == "Bearer A1", (
            "the clock-valid token went out and the server rejected it"
        )
        assert not any(request.method == "POST" for request in seen), (
            "the SDK's 401 branch must not attempt a refresh grant"
        )
        first_after_401 = seen[1]
        assert first_after_401.method == "GET"
        assert "/.well-known/" in str(first_after_401.url), (
            "the request after the 401 is metadata discovery, not a refresh"
        )
        after = json.loads((tmp_path / "srv.json").read_text(encoding="utf-8"))
        assert after["token"]["refresh_token"] == before["token"]["refresh_token"]
        assert after["token"]["issued_at"] == before["token"]["issued_at"]
        assert after == before, (
            "clear_tokens() clears only in-memory context — the disk file "
            "must be byte-for-byte untouched"
        )
