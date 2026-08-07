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
        """GET 405 → POST 401 should fire the event hook and trigger OAuth."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(405, text="method not allowed")
            # Verify the POST body matches design D2 (JSON-RPC tools/call).
            body = json.loads(request.content)
            assert body["jsonrpc"] == "2.0"
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "_oauth_probe"
            assert body["params"]["arguments"] == {}
            assert "id" in body
            # Verify the POST headers match design D3 (MCP streamable-http transport).
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
        """GET 405 → POST 200 should report that the server did not require OAuth."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(405, text="method not allowed")
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
        """GET 405 → POST 405 should not log the OAuth-success or 200 messages."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
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

    def test_get_200_no_post_issued(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 200 should short-circuit; no POST retry must be issued."""
        manager = MCPManager([], mcp_tokens_dir=tmp_path)
        requests_made: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, text="ok")
            raise AssertionError("POST should not be issued when GET returns 200")

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
        assert requests_made == ["GET"]

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

    def test_get_405_post_raises_no_stale_status(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """GET 405 → POST raises should report final_status=None, not the stale GET 405.

        The POST retry resets ``final_status`` to None before issuing the request,
        so if the POST raises (network error, timeout), the returned status is None
        rather than the intermediate GET 405.
        """
        manager = MCPManager([], mcp_tokens_dir=tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(405, text="method not allowed")
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
