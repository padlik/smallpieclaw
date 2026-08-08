"""Tests for the proactive OAuth 401-probe step in ``_run_oauth_flow``.

Covers the scenarios from the ``proactive-oauth-401-probe`` change:
- Probe triggers 401 → redirect_handler called → callback completes → token
  file created → session connects.
- Probe returns 200 → no redirect_handler → no token file → session connects
  → INFO log "server did not require OAuth" → no WARNING.
- Probe triggers 401 but callback times out / fails → no token file → WARNING
  emitted (probe saw auth challenge).  Also covers 403 insufficient-scope.
- Probe returns non-401/200/403 (e.g. 500) → no auth challenge → WARNING for
  unexpected status → session connection still attempted as fallback.
- Operator cancels during probe → probe task cancelled → flow returns
  ``{"success": False, "error": "Cancelled by operator"}`` → callback server
  closed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import mcp_oauth
from mcp_client import MCPManager, _SdkClientWrapper
from mcp_oauth import CallbackServer


def _base_cfg(tmp_path: Path) -> tuple[dict, dict]:
    """Return a minimal (cfg, oauth_cfg) pair for testing."""
    cfg = {"name": "srv", "transport": "http", "url": "https://example.com"}
    oauth_cfg = {
        "callback_port": 0,
        "callback_bind": "127.0.0.1",
        "cert_path": "unused",
        "key_path": "unused",
    }
    return cfg, oauth_cfg


def _patch_callback_server(monkeypatch) -> None:
    """Patch CallbackServer.start/stop to no-ops."""
    monkeypatch.setattr(CallbackServer, "start", AsyncMock(return_value=None))
    monkeypatch.setattr(CallbackServer, "stop", AsyncMock(return_value=None))


def _patch_provider_factory(monkeypatch) -> None:
    """Patch OAuthProviderFactory.build to return a sentinel object."""
    monkeypatch.setattr(
        mcp_oauth.OAuthProviderFactory, "build", lambda *a, **k: object()
    )


def _patch_probe(
    monkeypatch,
    *,
    saw_challenge: bool,
    final_status: int | None,
    error: str | None = None,
) -> None:
    """Patch _probe_oauth_challenge to return a fixed result."""

    async def fake_probe(self, name, server_url, provider, oauth_cfg):  # noqa: ARG001
        return (saw_challenge, final_status, error)

    monkeypatch.setattr(MCPManager, "_probe_oauth_challenge", fake_probe)


def _patch_session_runner(
    monkeypatch,
    tmp_path: Path,
    *,
    needs_auth: bool = False,
    write_token: bool = False,
    tools: list | None = None,
) -> None:
    """Patch _session_runner to simulate a ready session."""

    async def fake_session_runner(self) -> None:
        self.needs_auth = needs_auth
        self._tools = tools or []
        if write_token:
            (tmp_path / "srv.json").write_text("{}", encoding="utf-8")
        self._ready_future.set_result(True)

    monkeypatch.setattr(_SdkClientWrapper, "_session_runner", fake_session_runner)


def _run_flow(manager: MCPManager, cfg: dict, oauth_cfg: dict) -> dict:
    """Run _run_oauth_flow on the manager's event loop."""

    async def _run() -> dict:
        manager._loop = asyncio.get_running_loop()
        return await manager._run_oauth_flow("srv", cfg, oauth_cfg)

    return asyncio.run(_run())


class TestProbeTriggers401:
    """Task 4.1: probe triggers 401 → redirect_handler → callback → token → session."""

    def test_probe_401_token_created_session_connects(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)
        _patch_probe(monkeypatch, saw_challenge=True, final_status=401)
        _patch_session_runner(
            monkeypatch, tmp_path, needs_auth=False, write_token=True
        )

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        result = _run_flow(manager, cfg, oauth_cfg)

        assert result == {"success": True}
        assert (tmp_path / "srv.json").exists()


class TestProbeInternalLogging:
    """Task 3.1/3.2/3.3 + C2: verify _probe_oauth_challenge with a real httpx client.

    These tests drive the *real* ``_probe_oauth_challenge`` method through a
    real ``httpx.AsyncClient`` with ``MockTransport`` — pinning the async
    response-event-hook contract that a sync hook would violate (C1).
    """

    def test_probe_start_and_completion_logs_200(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """The probe emits start + completion INFO logs for a 200 response."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "result": {"tools": [{"name": "noop"}]},
                    },
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "noop"
            assert body["params"]["arguments"] == {}
            return httpx.Response(200, text="ok")

        original_async_client = httpx.AsyncClient

        def _fake_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            # Drop auth — MockTransport doesn't need a real httpx.Auth
            kwargs.pop("auth", None)
            return original_async_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _fake_client)

        caplog.set_level(logging.INFO, logger="mcp_client")
        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert any(
            "proactive OAuth probe starting" in rec.message
            for rec in caplog.records
        )
        assert any(
            "server did not require OAuth" in rec.message
            for rec in caplog.records
        )

    def test_probe_401_logs_handshake_triggered(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """When the event hook sees a 401, the probe logs 'handshake triggered'.

        Uses a real ``httpx.AsyncClient`` with ``MockTransport`` returning 401
        to verify the async response event hook fires correctly (C1/C2).
        """
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        original_async_client = httpx.AsyncClient

        def _fake_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            kwargs.pop("auth", None)
            return original_async_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _fake_client)

        caplog.set_level(logging.INFO, logger="mcp_client")
        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert any(
            "probe triggered OAuth handshake" in rec.message
            for rec in caplog.records
        )

    def test_probe_403_logs_handshake_triggered(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """A 403 insufficient-scope also sets the auth-challenge flag."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="forbidden")

        original_async_client = httpx.AsyncClient

        def _fake_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            kwargs.pop("auth", None)
            return original_async_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _fake_client)

        caplog.set_level(logging.INFO, logger="mcp_client")
        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 403
        assert error is None
        assert any(
            "probe triggered OAuth handshake" in rec.message
            for rec in caplog.records
        )


class TestProbeReturns200:
    """Task 4.2: probe returns 200 → no redirect_handler → no warning."""

    def test_probe_200_no_warning_no_token(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)
        _patch_probe(monkeypatch, saw_challenge=False, final_status=200)
        _patch_session_runner(
            monkeypatch, tmp_path, needs_auth=False, write_token=False
        )

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        caplog.set_level(logging.INFO, logger="mcp_client")
        result = _run_flow(manager, cfg, oauth_cfg)

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
        # Probe completion log (no handshake)
        assert any(
            "server did not require OAuth" in rec.message
            for rec in caplog.records
        )


class TestProbeAuthChallengeButNoToken:
    """Task 4.3: probe triggers 401/403 but no token → WARNING emitted."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_probe_auth_challenge_no_token_warns(
        self, tmp_path: Path, monkeypatch, caplog, status: int
    ) -> None:
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)
        _patch_probe(monkeypatch, saw_challenge=True, final_status=status)
        _patch_session_runner(
            monkeypatch, tmp_path, needs_auth=False, write_token=False
        )

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        caplog.set_level(logging.WARNING, logger="mcp_client")
        result = _run_flow(manager, cfg, oauth_cfg)

        assert result == {"success": True}
        assert not (tmp_path / "srv.json").exists()
        # WARNING about "no token file found" — probe saw auth challenge
        assert any(
            "no token file found" in rec.message for rec in caplog.records
        )


class TestProbeUnexpectedStatus:
    """Task 4.4: probe returns non-401/200/403 → warning → session fallback."""

    def test_probe_500_logs_warning_and_proceeds(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)
        _patch_probe(monkeypatch, saw_challenge=False, final_status=500)
        _patch_session_runner(
            monkeypatch, tmp_path, needs_auth=False, write_token=False
        )

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        caplog.set_level(logging.WARNING, logger="mcp_client")
        result = _run_flow(manager, cfg, oauth_cfg)

        # Session still connects (fallback)
        assert result == {"success": True}
        # No "no token file found" WARNING (probe didn't see auth challenge)
        assert not any(
            "no token file found" in rec.message for rec in caplog.records
        )
        # WARNING about the unexpected 500 status (design Risk mitigation)
        assert any(
            "unexpected status 500" in rec.message for rec in caplog.records
        )

    def test_probe_exception_no_challenge_proceeds_to_session(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """Probe fails without auth challenge → WARNING → session fallback."""
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)
        _patch_probe(
            monkeypatch,
            saw_challenge=False,
            final_status=None,
            error="connection refused",
        )
        _patch_session_runner(
            monkeypatch, tmp_path, needs_auth=False, write_token=False
        )

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        caplog.set_level(logging.WARNING, logger="mcp_client")
        result = _run_flow(manager, cfg, oauth_cfg)

        # Session still connects (fallback — no auth challenge seen)
        assert result == {"success": True}
        # Probe failure WARNING
        assert any(
            "OAuth probe failed" in rec.message for rec in caplog.records
        )

    def test_probe_exception_with_challenge_returns_error(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """Probe saw 401 but flow failed → return error, no session fallback.

        Rec 2: when the probe saw an auth challenge (401/403) but the OAuth
        flow failed (e.g. callback timeout), suppress the session-connection
        fallback to avoid a second redirect_handler firing and a dead auth
        link.  Return the error directly.
        """
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)
        _patch_probe(
            monkeypatch,
            saw_challenge=True,
            final_status=None,
            error="callback timed out",
        )
        _patch_session_runner(
            monkeypatch, tmp_path, needs_auth=False, write_token=False
        )

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        caplog.set_level(logging.WARNING, logger="mcp_client")
        result = _run_flow(manager, cfg, oauth_cfg)

        # Flow returns failure — no session fallback
        assert result["success"] is False
        assert "callback timed out" in result["error"]
        # WARNING about the probe failing after auth challenge
        assert any(
            "failed after auth challenge" in rec.message
            for rec in caplog.records
        )


class TestProbeCancellation:
    """Task 4.5: operator cancels during probe → flow returns cancelled."""

    def test_cancel_during_probe_returns_cancelled(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)

        # Probe that blocks until cancelled (simulates waiting for callback)
        probe_started = asyncio.Event()

        async def blocking_probe(self, name, server_url, provider, oauth_cfg):  # noqa: ARG001
            probe_started.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        monkeypatch.setattr(MCPManager, "_probe_oauth_challenge", blocking_probe)

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        async def _run() -> dict:
            manager._loop = asyncio.get_running_loop()
            # Start the flow in a task so we can trigger cancel concurrently
            flow_task = asyncio.ensure_future(
                manager._run_oauth_flow("srv", cfg, oauth_cfg)
            )
            # Wait for the probe to start
            await probe_started.wait()
            # Simulate operator tapping Cancel
            manager._oauth_cancel_requested = True
            return await flow_task

        result = asyncio.run(_run())

        assert result == {"success": False, "error": "Cancelled by operator"}


class TestProbeAuthUrlSentViaTelegram:
    """Task 4.1: verify the auth URL is sent via Telegram when probe triggers 401.

    This integration test drives the real ``_probe_oauth_challenge`` with a
    custom ``httpx.Auth`` that simulates the SDK's ``async_auth_flow``: on a
    401 response, it calls the ``redirect_handler`` (which sends the auth URL
    to Telegram), then retries with a Bearer token.  The test verifies that
    ``tg_iface.bot.send_message`` was called with the auth URL.
    """

    def test_probe_401_sends_auth_url_via_telegram(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        auth_url_sent = "https://auth.example.com/authorize?client_id=abc&state=s1"

        # Build a real OAuth provider with a custom redirect_handler that
        # simulates sending the auth URL to Telegram.
        cb_server = CallbackServer(
            port=0,
            bind="127.0.0.1",
            cert_path=str(tmp_path / "cert.pem"),
            key_path=str(tmp_path / "key.pem"),
            loop=asyncio.new_event_loop(),
        )

        # Patch CallbackServer.start to no-op (we don't need a real listener)
        monkeypatch.setattr(CallbackServer, "start", AsyncMock(return_value=None))
        monkeypatch.setattr(CallbackServer, "stop", AsyncMock(return_value=None))

        tg_iface = MagicMock()
        tg_iface.app.bot.send_message = AsyncMock()

        # Build the redirect handler that sends the auth URL to Telegram
        redirect_handler = mcp_oauth.make_redirect_handler(
            tg_iface, "srv", cb_server, chat_id=123, trace=False
        )

        # Custom httpx.Auth that simulates the SDK's async_auth_flow:
        # - On first request, server returns 401
        # - Auth flow calls redirect_handler (sends URL to Telegram)
        # - On retry, server returns 200 (simulating successful auth)
        class _SimulatedOAuthProvider(httpx.Auth):
            def __init__(self):
                self._first_request = True

            async def async_auth_flow(self, request):
                # First request: server returns 401
                response = yield request
                if response.status_code == 401 and self._first_request:
                    self._first_request = False
                    # Simulate the SDK calling redirect_handler
                    # (in real usage, the SDK does discovery → registration →
                    # redirect_handler → callback_handler → token exchange)
                    await redirect_handler(auth_url_sent)
                    # Retry with a Bearer token
                    request.headers["Authorization"] = "Bearer fake_token"
                    yield request
                else:
                    yield request

        provider = _SimulatedOAuthProvider()

        # MockTransport that returns 401 on first request, 200 on retry
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            if "Authorization" in request.headers:
                return httpx.Response(200, text="ok")
            return httpx.Response(401, text="unauthorized")

        original_async_client = httpx.AsyncClient

        def _fake_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            # Use our simulated provider instead of the one passed in
            kwargs["auth"] = provider
            return original_async_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _fake_client)

        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        async def _run():
            manager._loop = asyncio.get_running_loop()
            return await manager._probe_oauth_challenge(
                "srv", "https://example.com", provider, {"timeout": 30}
            )

        saw_challenge, status, error = asyncio.run(_run())

        # The event hook should have seen the 401
        assert saw_challenge is True
        # The final status should be 200 (after retry with Bearer token)
        assert status == 200
        assert error is None
        # The auth URL should have been sent via Telegram
        tg_iface.app.bot.send_message.assert_awaited_once()
        call_kwargs = tg_iface.app.bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 123
        # The auth URL should be in the InlineKeyboardButton
        reply_markup = call_kwargs["reply_markup"]
        # InlineKeyboardMarkup has inline_keyboard attribute
        keyboard = reply_markup.inline_keyboard
        authorize_button = keyboard[0][0]
        assert authorize_button.url == auth_url_sent


class TestProbePostRetry:
    """MockTransport tests driving the real ``_probe_oauth_challenge`` GET→405→POST retry.

    Verifies that a 405 on the GET probe triggers a POST retry with a JSON-RPC
    ``tools/call`` body, that the response event hook observes auth challenges on
    the POST 401, and that no POST is issued when GET already returns a
    decisive status.
    """

    def _fake_client(self, monkeypatch, handler) -> None:
        """Patch ``httpx.AsyncClient`` to use the supplied MockTransport handler."""
        original_async_client = httpx.AsyncClient

        def _wrapper(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            kwargs.pop("auth", None)
            return original_async_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _wrapper)

    def test_get_405_post_401_triggers_oauth(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 405 -> POST tools/list 200 -> POST tools/call 401 triggers OAuth."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(405, text="method not allowed")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                assert body["jsonrpc"] == "2.0"
                assert "id" in body
                assert request.headers["Accept"] == "application/json, text/event-stream"
                assert request.headers["Content-Type"] == "application/json"
                assert request.headers["MCP-Protocol-Version"] == "2025-11-25"
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "result": {"tools": [{"name": "list_labels"}]},
                    },
                )
            assert body["method"] == "tools/call"
            assert body["jsonrpc"] == "2.0"
            assert "id" in body
            assert body["params"]["name"] == "list_labels"
            assert body["params"]["arguments"] == {}
            assert request.headers["Accept"] == "application/json, text/event-stream"
            assert request.headers["Content-Type"] == "application/json"
            assert request.headers["MCP-Protocol-Version"] == "2025-11-25"
            return httpx.Response(401, text="unauthorized")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert any(
            "probe triggered OAuth handshake (status=401)" in rec.message
            for rec in caplog.records
        )

    def test_get_405_post_200_no_oauth(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 405 -> POST tools/list 200 -> POST tools/call 200: no OAuth."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(405, text="method not allowed")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "result": {"tools": [{"name": "list_labels"}]},
                    },
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "list_labels"
            assert body["params"]["arguments"] == {}
            return httpx.Response(200, text="ok")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert any(
            "probe returned 200 — server did not require OAuth" in rec.message
            for rec in caplog.records
        )

    def test_get_405_post_405_no_oauth_warning(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 405 -> POST tools/list 405: no OAuth, no tools/call."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(405, text="method not allowed")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(405, text="method not allowed")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 405
        assert error is None
        assert not any(
            "server did not require OAuth" in rec.message for rec in caplog.records
        )
        assert not any(
            "probe triggered OAuth handshake" in rec.message for rec in caplog.records
        )

    def test_get_200_post_200_no_oauth(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 → POST tools/list 200 → POST tools/call 200: no OAuth."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "result": {"tools": [{"name": "list_labels"}]},
                    },
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "list_labels"
            assert body["params"]["arguments"] == {}
            return httpx.Response(200, text="ok")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST", "POST"]
        assert any(
            "server did not require OAuth" in rec.message for rec in caplog.records
        )

    def test_get_401_no_post_issued(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 401 should short-circuit; no POST retry must be issued."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(401, text="unauthorized")
            raise AssertionError("POST should not be issued when GET returns 401")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert requests_made == ["GET"]

    def test_get_401_auth_retry_200_no_post_issued(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 401 → auth flow retries GET with token → 200: no POST should be issued.

        The ``not probe_saw_auth_challenge`` guard prevents a spurious POST when
        the GET already triggered the OAuth handshake and the SDK retried with a
        Bearer token (final_status=200, flag=True).  Without this guard, the probe
        would send an unnecessary POST after the OAuth flow already fired.
        """
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        class _RetryAuthProvider(httpx.Auth):
            """Simulates the SDK's async_auth_flow: on 401, add a Bearer token and retry."""

            async def async_auth_flow(self, request):
                response = yield request
                if response.status_code == 401:
                    request.headers["Authorization"] = "Bearer fake_token"
                    yield request

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                if "Authorization" in request.headers:
                    return httpx.Response(200, text="ok")
                return httpx.Response(401, text="unauthorized")
            raise AssertionError("POST should not be issued after GET auth retry")

        original_async_client = httpx.AsyncClient

        def _fake_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            # Use our retry auth provider instead of the passed-in mock
            kwargs["auth"] = _RetryAuthProvider()
            return original_async_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _fake_client)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", _RetryAuthProvider(), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 200
        assert error is None
        # Only GET requests — no POST despite final_status=200
        assert all(r == "GET" for r in requests_made)
        assert len(requests_made) == 2  # original GET (401) + retry GET (200)

    def test_get_200_post_401_triggers_oauth(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 → POST tools/list 200 → POST tools/call 401 triggers OAuth.

        This is the Gmail scenario: the server allows unauthenticated GET but
        returns 401 on unauthenticated tools/call.
        """
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "result": {"tools": [{"name": "list_labels"}]},
                    },
                )
            assert body["method"] == "tools/call"
            assert body["jsonrpc"] == "2.0"
            assert "id" in body
            assert body["params"]["name"] == "list_labels"
            assert body["params"]["arguments"] == {}
            assert request.headers["Accept"] == "application/json, text/event-stream"
            assert request.headers["Content-Type"] == "application/json"
            assert request.headers["MCP-Protocol-Version"] == "2025-11-25"
            return httpx.Response(401, text="unauthorized")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert any(
            "probe triggered OAuth handshake (status=401)" in rec.message
            for rec in caplog.records
        )

    def test_get_200_post_raises_no_stale_status(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 -> POST tools/list raises: final_status=None, not the stale GET 200."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            raise httpx.ConnectError("post failed")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status is None
        assert error is not None
        assert "post failed" in error
        assert any(
            "OAuth probe failed" in rec.message for rec in caplog.records
        )

    def test_get_405_post_raises_no_stale_status(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 405 -> POST tools/list raises: final_status=None, not the stale GET 405.

        The POST retry resets ``final_status`` to None before issuing the request,
        so if the POST raises (network error, timeout), the returned status is None
        rather than the intermediate GET 405.
        """
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(405, text="method not allowed")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            raise httpx.ConnectError("post failed")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status is None
        assert error is not None
        assert "post failed" in error
        assert any(
            "OAuth probe failed" in rec.message for rec in caplog.records
        )


class TestProbePostRetryFlow:
    """Full-flow integration tests for the GET→405→POST retry behavior."""

    def test_get_405_post_401_oauth_fires_token_created(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GET 405 → POST 401 should trigger OAuth and create the token file."""
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)
        _patch_probe(monkeypatch, saw_challenge=True, final_status=401)
        _patch_session_runner(monkeypatch, tmp_path, needs_auth=False, write_token=True)

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        result = _run_flow(manager, cfg, oauth_cfg)

        assert result == {"success": True}
        assert (tmp_path / "srv.json").exists()

    def test_get_405_post_200_no_oauth_no_token(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 405 → POST 200 should complete the flow without creating a token."""
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)
        _patch_probe(monkeypatch, saw_challenge=False, final_status=200)
        _patch_session_runner(monkeypatch, tmp_path, needs_auth=False, write_token=False)

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        caplog.set_level(logging.INFO, logger="mcp_client")
        result = _run_flow(manager, cfg, oauth_cfg)

        assert result == {"success": True}
        assert not (tmp_path / "srv.json").exists()
        assert not any(
            "no token file found" in rec.message for rec in caplog.records
        )
        assert any(
            "did not require OAuth" in rec.message for rec in caplog.records
        )

    def test_get_405_post_405_warning_session_fallback(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 405 → POST 405 should warn about the unexpected status and fall back."""
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)
        _patch_probe(monkeypatch, saw_challenge=False, final_status=405)
        _patch_session_runner(monkeypatch, tmp_path, needs_auth=False, write_token=False)

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        caplog.set_level(logging.WARNING, logger="mcp_client")
        result = _run_flow(manager, cfg, oauth_cfg)

        assert result == {"success": True}
        assert any(
            "unexpected status 405" in rec.message for rec in caplog.records
        )

    def test_get_200_post_401_oauth_fires_token_created(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GET 200 → POST 401 should trigger OAuth and create the token file."""
        _patch_callback_server(monkeypatch)
        _patch_provider_factory(monkeypatch)
        _patch_probe(monkeypatch, saw_challenge=True, final_status=401)
        _patch_session_runner(monkeypatch, tmp_path, needs_auth=False, write_token=True)

        cfg, oauth_cfg = _base_cfg(tmp_path)
        manager = MCPManager([cfg], mcp_tokens_dir=tmp_path)

        result = _run_flow(manager, cfg, oauth_cfg)

        assert result == {"success": True}
        assert (tmp_path / "srv.json").exists()


class TestProbeTwoStepDiscovery:
    """MockTransport tests driving the real ``_probe_oauth_challenge`` two-step discovery.

    Verifies GET → POST tools/list → POST tools/call flow: auth-challenge detection,
    tool-name extraction, empty tool list handling, SSE parsing, and error cases.
    """

    def _fake_client(self, monkeypatch, handler) -> None:
        """Patch ``httpx.AsyncClient`` to use the supplied MockTransport handler."""
        original_async_client = httpx.AsyncClient

        def _wrapper(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            kwargs.pop("auth", None)
            return original_async_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _wrapper)

    def test_get_200_tools_list_200_tools_call_401_triggers_oauth(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 → tools/list 200 → tools/call 401 triggers OAuth handshake."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "result": {
                            "tools": [
                                {"name": "list_labels"},
                                {"name": "send_email"},
                            ]
                        },
                    },
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "list_labels"
            assert body["params"]["arguments"] == {}
            return httpx.Response(401, text="unauthorized")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert requests_made == ["GET", "POST", "POST"]
        assert any(
            "probe triggered OAuth handshake (status=401)" in rec.message
            for rec in caplog.records
        )

    def test_get_200_tools_list_200_tools_call_200_no_oauth(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 → tools/list 200 → tools/call 200: server did not require OAuth."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "result": {"tools": [{"name": "list_labels"}]},
                    },
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "list_labels"
            assert body["params"]["arguments"] == {}
            return httpx.Response(200, text="ok")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert any(
            "probe returned 200 — server did not require OAuth" in rec.message
            for rec in caplog.records
        )

    def test_get_200_tools_list_200_empty_no_tools_call(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 → tools/list 200 with empty tools: no tools/call issued."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "result": {"tools": []}},
            )

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any(
            "no usable tool name" in rec.message for rec in caplog.records
        )

    def test_get_200_tools_list_401_no_tools_call(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 → tools/list 401: auth challenge detected, no tools/call issued."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(401, text="unauthorized")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any(
            "probe triggered OAuth handshake" in rec.message for rec in caplog.records
        )

    def test_get_401_no_post(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 401 short-circuits; no POST tools/list or tools/call issued."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(401, text="unauthorized")
            raise AssertionError("POST should not be issued when GET returns 401")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert requests_made == ["GET"]

    def test_get_405_tools_list_200_tools_call_401_triggers_oauth(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 405 → tools/list 200 → tools/call 401; final status reflects tools/call."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(405, text="method not allowed")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "result": {"tools": [{"name": "list_labels"}]},
                    },
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "list_labels"
            assert body["params"]["arguments"] == {}
            return httpx.Response(401, text="unauthorized")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert any(
            "probe triggered OAuth handshake (status=401)" in rec.message
            for rec in caplog.records
        )

    def test_get_200_tools_list_sse_parsed(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 → tools/list returns SSE frames; first tool extracted correctly."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                content = b'data: {"jsonrpc": "2.0", "result": {"tools": [{"name": "search"}]}}\n\n'
                return httpx.Response(
                    200,
                    content=content,
                    headers={"content-type": "text/event-stream"},
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "search"
            assert body["params"]["arguments"] == {}
            return httpx.Response(401, text="unauthorized")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None

    def test_get_200_tools_list_500_no_tools_call(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 → tools/list 500: no tools/call, final status 500, warning logged."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(500, text="internal server error")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 500
        assert error is None
        assert requests_made == ["GET", "POST"]
        # The probe logs the non-200/non-challenge status at INFO; the
        # "unexpected status" WARNING is emitted by _run_probe_step, not
        # _probe_oauth_challenge (which this test calls directly).
        assert any(
            "probe returned 500" in rec.message for rec in caplog.records
        )

    def test_get_200_tools_list_connect_error(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 → tools/list raises ConnectError; final_status=None, warning logged."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            raise httpx.ConnectError("tools/list failed")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status is None
        assert error is not None
        assert "tools/list failed" in error
        assert any(
            "OAuth probe failed" in rec.message for rec in caplog.records
        )

    def test_get_200_tools_list_200_tools_call_connect_error(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 → tools/list 200 → tools/call raises ConnectError; status=None."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "result": {"tools": [{"name": "list_labels"}]},
                    },
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "list_labels"
            assert body["params"]["arguments"] == {}
            raise httpx.ConnectError("tools/call failed")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status is None
        assert error is not None
        assert "tools/call failed" in error
        assert requests_made == ["GET", "POST", "POST"]
        assert any(
            "OAuth probe failed" in rec.message for rec in caplog.records
        )


class TestProbeMalformedResponses:
    """MockTransport tests for malformed tools/list responses and edge cases.

    Verifies that the probe extracts the first tool name safely, logs a WARNING
    when extraction fails, and skips the tools/call POST in those cases.
    """

    def _fake_client(self, monkeypatch, handler) -> None:
        """Patch ``httpx.AsyncClient`` to use the supplied MockTransport handler."""
        original_async_client = httpx.AsyncClient

        def _wrapper(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            kwargs.pop("auth", None)
            return original_async_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _wrapper)

    def test_tools_list_200_missing_name_field(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """tools/list returns a tool with no 'name' field -> no tools/call."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "result": {"tools": [{"description": "no name here"}]},
                },
            )

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any(
            "no usable tool name" in rec.message for rec in caplog.records
        )

    def test_tools_list_200_result_not_dict(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """tools/list returns 'result' as a string -> no tools/call."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "result": "not_a_dict"},
            )

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any(
            "no usable tool name" in rec.message for rec in caplog.records
        )

    def test_tools_list_200_tools_not_list(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """tools/list returns 'tools' as a string -> no tools/call."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "result": {"tools": "not_a_list"}},
            )

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any(
            "no usable tool name" in rec.message for rec in caplog.records
        )

    def test_tools_list_200_malformed_json(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """tools/list returns malformed JSON -> no tools/call."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(
                200,
                content=b"not valid json",
                headers={"content-type": "application/json"},
            )

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any(
            "no usable tool name" in rec.message for rec in caplog.records
        )

    def test_tools_list_sse_no_space_prefix(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """SSE data: without space prefix is parsed correctly."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                content = b'data:{"jsonrpc": "2.0", "result": {"tools": [{"name": "search"}]}}\n\n'
                return httpx.Response(
                    200,
                    content=content,
                    headers={"content-type": "text/event-stream"},
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "search"
            assert body["params"]["arguments"] == {}
            return httpx.Response(401, text="unauthorized")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert requests_made == ["GET", "POST", "POST"]
        assert any(
            "probe triggered OAuth handshake" in rec.message for rec in caplog.records
        )

    def test_tools_list_sse_malformed(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """SSE frame contains malformed JSON -> no tools/call."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            content = b'data: not json\n\n'
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": "text/event-stream"},
            )

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any(
            "no usable tool name" in rec.message for rec in caplog.records
        )

    def test_tools_list_sse_empty(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """SSE body has no data frames -> no tools/call."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(
                200,
                content=b"\n\n",
                headers={"content-type": "text/event-stream"},
            )

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any(
            "no usable tool name" in rec.message for rec in caplog.records
        )

    def test_tools_list_403_triggers_oauth(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """tools/list returns 403 -> auth challenge, no tools/call issued."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(403, text="forbidden")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 403
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any(
            "probe triggered OAuth handshake" in rec.message for rec in caplog.records
        )

    def test_tools_list_response_too_large_content_length(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """tools/list with Content-Length > 1MB -> WARNING, no tools/call."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            return httpx.Response(
                200,
                content=b'{"jsonrpc": "2.0", "result": {"tools": [{"name": "list"}]}}',
                headers={"content-length": str(2_000_000)},
            )

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any("too large" in rec.message for rec in caplog.records)

    def test_tools_list_response_too_large_body(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """tools/list with body > 1MB -> WARNING, no tools/call."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            big = b'{"jsonrpc":"2.0","result":{"tools":[{"name":"list"}]}}' + b"x" * 1_100_000
            return httpx.Response(200, content=big)

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any("too large" in rec.message for rec in caplog.records)

    def test_tools_list_all_mutating_tools_no_tools_call(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """tools/list returns only mutating tools -> no tools/call, WARNING."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "result": {
                        "tools": [
                            {"name": "send_email"},
                            {"name": "delete_message"},
                        ]
                    },
                },
            )

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.WARNING, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is False
        assert status == 200
        assert error is None
        assert requests_made == ["GET", "POST"]
        assert any(
            "no usable tool name" in rec.message for rec in caplog.records
        )

    def test_tools_list_prefers_non_mutating_tool(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """tools/list returns mutating + non-mutating -> probe uses non-mutating."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "result": {
                            "tools": [
                                {"name": "send_email"},
                                {"name": "list_labels"},
                            ]
                        },
                    },
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "list_labels"
            return httpx.Response(401, text="unauthorized")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert requests_made == ["GET", "POST", "POST"]

    def test_sse_multi_event_skips_heartbeat(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """SSE stream with heartbeat before tools event -> parser skips heartbeat."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                sse_body = (
                    b'data: {"type": "ping"}\n\n'
                    b'data: {"jsonrpc": "2.0", "result": {"tools": [{"name": "search"}]}}\n\n'
                )
                return httpx.Response(
                    200,
                    content=sse_body,
                    headers={"content-type": "text/event-stream"},
                )
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "search"
            return httpx.Response(401, text="unauthorized")

        self._fake_client(monkeypatch, handler)
        caplog.set_level(logging.INFO, logger="mcp_client")

        saw, status, error = asyncio.run(
            manager._probe_oauth_challenge(
                "srv", "https://example.com", MagicMock(spec=httpx.Auth), {"timeout": 30}
            )
        )

        assert saw is True
        assert status == 401
        assert error is None
        assert requests_made == ["GET", "POST", "POST"]


class TestExtractFirstToolName:
    """Unit tests for the ``_extract_first_tool_name`` helper."""

    def test_json_with_tools(self) -> None:
        resp = httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {"tools": [{"name": "list_labels"}, {"name": "send"}]},
            },
        )
        assert MCPManager._extract_first_tool_name(resp) == "list_labels"

    def test_json_empty_tools(self) -> None:
        resp = httpx.Response(
            200, json={"jsonrpc": "2.0", "result": {"tools": []}}
        )
        assert MCPManager._extract_first_tool_name(resp) is None

    def test_json_missing_result(self) -> None:
        resp = httpx.Response(200, json={"jsonrpc": "2.0"})
        assert MCPManager._extract_first_tool_name(resp) is None

    def test_json_result_not_dict(self) -> None:
        resp = httpx.Response(
            200, json={"jsonrpc": "2.0", "result": "bad"}
        )
        assert MCPManager._extract_first_tool_name(resp) is None

    def test_json_tools_not_list(self) -> None:
        resp = httpx.Response(
            200, json={"jsonrpc": "2.0", "result": {"tools": "bad"}}
        )
        assert MCPManager._extract_first_tool_name(resp) is None

    def test_json_tool_missing_name(self) -> None:
        resp = httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {"tools": [{"desc": "no name"}]},
            },
        )
        assert MCPManager._extract_first_tool_name(resp) is None

    def test_json_malformed_body(self) -> None:
        resp = httpx.Response(
            200,
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert MCPManager._extract_first_tool_name(resp) is None

    def test_sse_with_space(self) -> None:
        body = b'data: {"jsonrpc": "2.0", "result": {"tools": [{"name": "search"}]}}\n\n'
        resp = httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )
        assert MCPManager._extract_first_tool_name(resp) == "search"

    def test_sse_without_space(self) -> None:
        body = b'data:{"jsonrpc": "2.0", "result": {"tools": [{"name": "search"}]}}\n\n'
        resp = httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )
        assert MCPManager._extract_first_tool_name(resp) == "search"

    def test_sse_malformed(self) -> None:
        body = b'data: not json\n\n'
        resp = httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )
        assert MCPManager._extract_first_tool_name(resp) is None

    def test_sse_empty(self) -> None:
        body = b"\n\n"
        resp = httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )
        assert MCPManager._extract_first_tool_name(resp) is None

    def test_sse_tool_missing_name(self) -> None:
        body = b'data: {"jsonrpc": "2.0", "result": {"tools": [{"desc": "no name"}]}}\n\n'
        resp = httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )
        assert MCPManager._extract_first_tool_name(resp) is None

    def test_prefers_non_mutating_tool(self) -> None:
        resp = httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {
                    "tools": [{"name": "send_email"}, {"name": "list_labels"}]
                },
            },
        )
        assert MCPManager._extract_first_tool_name(resp) == "list_labels"

    def test_all_mutating_tools_returns_none(self) -> None:
        resp = httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {
                    "tools": [
                        {"name": "send_email"},
                        {"name": "delete_message"},
                        {"name": "write_file"},
                    ]
                },
            },
        )
        assert MCPManager._extract_first_tool_name(resp) is None

    def test_first_non_mutating_selected_even_if_later(self) -> None:
        resp = httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {
                    "tools": [
                        {"name": "delete_item"},
                        {"name": "update_record"},
                        {"name": "get_status"},
                        {"name": "list_all"},
                    ]
                },
            },
        )
        assert MCPManager._extract_first_tool_name(resp) == "get_status"

    def test_sse_multi_event_skips_heartbeat(self) -> None:
        """SSE stream with heartbeat before tools event -> skip heartbeat."""
        body = (
            b'data: {"type": "ping"}\n\n'
            b'data: {"jsonrpc": "2.0", "result": {"tools": [{"name": "search"}]}}\n\n'
        )
        resp = httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )
        assert MCPManager._extract_first_tool_name(resp) == "search"

    def test_sse_response_too_large(self) -> None:
        """SSE response exceeding the size cap returns None."""
        large_body = (
            b'data: {"jsonrpc": "2.0", "result": {"tools": [{"name": "x"}]}}\n\n'
        )
        large_body += b"x" * 1_100_000
        resp = httpx.Response(
            200,
            content=large_body,
            headers={"content-type": "text/event-stream"},
        )
        assert MCPManager._extract_first_tool_name(resp) is None
