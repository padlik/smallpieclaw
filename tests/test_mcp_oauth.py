"""Tests for mcp_oauth.py — OAuth helpers for MCP HTTP servers."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import threading
from pathlib import Path
import pytest

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_oauth import CallbackServer, FileTokenStorage, OAuthProviderFactory, make_redirect_handler


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
