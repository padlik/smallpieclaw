"""
mcp_oauth.py
------------
OAuth 2.0 helpers for MCP HTTP servers.

Provides local token storage that satisfies the MCP SDK ``TokenStorage``
protocol, a minimal async HTTPS callback server for the OAuth redirect, and a
factory that assembles an ``OAuthClientProvider`` from raw server config.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import ssl
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

logger = logging.getLogger(__name__)


class FileTokenStorage:
    """File-backed token storage implementing the MCP SDK ``TokenStorage`` protocol.

    Tokens and dynamic client registration information are stored in
    ``<mcp_tokens_dir>/<server_name>.json`` with restrictive file permissions.
    Credentials are pre-seeded from config so the SDK can skip dynamic
    client registration when no persisted client info exists yet.
    """

    def __init__(
        self,
        server_name: str,
        mcp_tokens_dir: Path,
        client_id: str,
        client_secret: str,
        token_endpoint_auth_method: str = "client_secret_basic",
    ) -> None:
        self.server_name = server_name
        self.mcp_tokens_dir = mcp_tokens_dir
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_endpoint_auth_method = token_endpoint_auth_method
        self._token_file = mcp_tokens_dir / f"{server_name}.json"

    def _read_file(self) -> dict[str, Any] | None:
        if not self._token_file.exists():
            return None
        try:
            with open(self._token_file, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "MCP token file for %s is unreadable or corrupt: %s",
                self.server_name,
                exc,
            )
            return None

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        """Write payload to the token file atomically with 0600 permissions.

        Writes to a temp file then renames, so a crash mid-write does not
        corrupt the existing token file.
        """
        tmp_path = self._token_file.with_suffix(".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._token_file)
        except BaseException as exc:  # broad catch intentional: ensure tmp cleanup on KeyboardInterrupt/SystemExit
            logger.warning(
                "MCP [%s] failed to write token file %s: %s",
                self.server_name,
                self._token_file,
                exc,
            )
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def get_tokens(self) -> OAuthToken | None:
        """Read stored OAuth tokens, or ``None`` if no file exists."""
        data = self._read_file()
        if data is None:
            logger.debug("MCP [%s] token: no file on disk", self.server_name)
            return None
        token_data = data.get("token") or data
        try:
            token = OAuthToken(
                access_token=token_data["access_token"],
                token_type=token_data.get("token_type", "Bearer"),
                expires_in=token_data.get("expires_in"),
                scope=token_data.get("scope"),
                refresh_token=token_data.get("refresh_token"),
            )
        except (KeyError, TypeError) as exc:
            logger.warning(
                "MCP token data for %s is malformed: %s",
                self.server_name,
                exc,
            )
            return None

        has_refresh = token.refresh_token is not None
        scope = token.scope or "unknown"
        remaining: int | str = "unknown"
        issued_at = token_data.get("issued_at")
        if token.expires_in is not None and issued_at is not None:
            try:
                remaining = int(issued_at + token.expires_in - time.time())
            except (TypeError, ValueError, OverflowError):
                # Diagnostic only — a corrupt ``issued_at`` (wrong type, inf, nan)
                # must never prevent an otherwise valid token from loading.
                remaining = "unknown"
        logger.debug(
            "MCP [%s] token: found (scope=%s, has_refresh=%s, remaining=%s)",
            self.server_name,
            scope,
            has_refresh,
            remaining,
        )
        return token

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Persist ``tokens`` to disk, preserving any existing ``client_info``."""
        existing = self._read_file() or {}
        token_dict = tokens.model_dump(mode="json", exclude_none=True)
        token_dict["issued_at"] = time.time()
        payload: dict[str, Any] = {
            "token": token_dict,
        }
        if "client_info" in existing:
            payload["client_info"] = existing["client_info"]
        self._atomic_write(payload)
        has_refresh = tokens.refresh_token is not None
        scope = tokens.scope or "unknown"
        logger.info(
            "MCP [%s] tokens written to disk (scope=%s, has_refresh=%s)",
            self.server_name,
            scope,
            has_refresh,
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Return OAuth client information.

        Prefer a previously persisted ``client_info`` block, unless the
        configured ``client_secret`` has since been rotated, in which case the
        current config takes precedence so a secret rotation actually takes
        effect. Falls back to constructing one from the pre-seeded credentials
        so the SDK skips dynamic client registration.
        """
        data = self._read_file()
        if data and "client_info" in data:
            info = data["client_info"]
            try:
                cached = OAuthClientInformationFull(**info)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "MCP client_info for %s is malformed, using pre-seed: %s",
                    self.server_name,
                    exc,
                )
            else:
                if cached.client_secret == self.client_secret:
                    logger.debug("MCP [%s] client_info: using cached", self.server_name)
                    return cached
                logger.info(
                    "MCP client_secret for %s changed in config; "
                    "ignoring cached client_info",
                    self.server_name,
                )
        logger.debug("MCP [%s] client_info: using pre-seeded credentials", self.server_name)
        return OAuthClientInformationFull(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uris=None,
            token_endpoint_auth_method=self.token_endpoint_auth_method,
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """Persist ``client_info`` to disk, merging with any existing token data."""
        existing = self._read_file() or {}
        existing["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._atomic_write(existing)


class CallbackServer:
    """Local async HTTPS callback server that receives the OAuth redirect.

    ``expected_state`` should be set after the SDK generates the authorization
    URL and before the browser is directed to it.
    """

    def __init__(
        self,
        port: int,
        bind: str,
        cert_path: str,
        key_path: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.port = port
        self.bind = bind
        self.cert_path = cert_path
        self.key_path = key_path
        self.loop = loop
        self._server: asyncio.base_events.Server | None = None
        self._future: asyncio.Future[tuple[str, str | None]] = loop.create_future()
        self.expected_state: str | None = None

    def set_expected_state(self, state: str) -> None:
        """Set the expected ``state`` value for defense-in-depth validation."""
        self.expected_state = state

    def _build_ssl_context(self) -> ssl.SSLContext:
        if not Path(self.cert_path).is_file():
            raise RuntimeError(f"TLS cert not found: {self.cert_path}")
        if not Path(self.key_path).is_file():
            raise RuntimeError(f"TLS key not found: {self.key_path}")
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.cert_path, self.key_path)
        return ctx

    async def start(self) -> None:
        """Start the HTTPS callback server, if not already started.

        Idempotent so callers that may not know whether an earlier code path
        already started the server (e.g. a lazily-triggered redirect handler)
        can call it unconditionally.

        Raises:
            RuntimeError: If the certificate or key file is missing.
            OSError: If the port is already in use or otherwise unavailable.
        """
        if self._server is not None:
            return
        if self._future.done():
            # A previous flow resolved or cancelled the future. Replace it so
            # this flow waits on its own callback instead of instantly reusing
            # the old result (or raising the old CancelledError).
            self._future = self.loop.create_future()
        ssl_context = self._build_ssl_context()
        try:
            self._server = await asyncio.start_server(
                self._handle,
                host=self.bind,
                port=self.port,
                ssl=ssl_context,
            )
            logger.info("MCP OAuth callback server started on %s:%d", self.bind, self.port)
        except OSError as exc:
            raise OSError(
                f"Cannot start OAuth callback server on {self.bind}:{self.port}: {exc}"
            ) from exc

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Parse a single HTTP callback and resolve the future on success."""
        try:
            request_line = await reader.readline()
            # Drain remaining headers to avoid RSTing the client.
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            parts = request_line.decode("utf-8", errors="replace").split()
            if len(parts) < 2 or "?" not in parts[1]:
                logger.debug("OAuth callback handler error: malformed request")
                writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nMalformed request")
                await writer.drain()
                return

            # Accept any path (e.g. /, /callback, /cb) — the OAuth redirect_uri
            # may use a non-root path.  Parse the query string after the first ?.
            query = parts[1].split("?", 1)[1]
            params = parse_qs(query)
            code = params.get("code", [None])[0]
            state = params.get("state", [None])[0]

            if code is None:
                logger.debug("OAuth callback handler error: missing code")
                writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nMissing code")
                await writer.drain()
                return

            # Defense-in-depth state validation.  The SDK also validates state
            # internally via ``secrets.compare_digest``; this early-reject avoids
            # resolving the future with a bad code.  When ``expected_state`` is
            # set, the incoming state MUST match — a missing state is rejected.
            validated_state = self.expected_state is not None
            if validated_state:
                if state is None or not secrets.compare_digest(self.expected_state, state):
                    logger.warning(
                        "MCP OAuth callback state mismatch: expected=%s... got=%s... — "
                        "possible CSRF or misdirected redirect",
                        self.expected_state[:8] if self.expected_state else "",
                        state[:8] if state else "",
                    )
                    writer.write(
                        b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nState mismatch"
                    )
                    await writer.drain()
                    return

            if not self._future.done():
                self._future.set_result((code, state))
            # A mismatch returns above, so reaching here means state either
            # matched or was never validated — there is no "no" case to report.
            logger.info(
                "MCP OAuth callback received (code=yes, state_match=%s)",
                "yes" if validated_state else "n/a",
            )

            body = "Auth complete, close this tab"
            response = (
                f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"
            )
            writer.write(response.encode("utf-8"))
            await writer.drain()
        except Exception as exc:  # noqa: BLE001 - resilience for malformed callbacks
            logger.warning("OAuth callback handler error: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def wait_for_callback(self, timeout: float = 300.0) -> tuple[str, str | None]:
        """Wait for the OAuth callback or raise ``asyncio.TimeoutError``."""
        return await asyncio.wait_for(self._future, timeout=timeout)

    async def stop(self) -> None:
        """Close the server socket and cancel any still-pending future.

        Idempotent, and leaves the instance restartable: ``_server`` is reset to
        ``None`` so a later :meth:`start` binds a fresh socket rather than
        silently no-opping. Safe to call while a callback result is already
        resolved — a completed future is left intact so a caller that has not yet
        read it still can.
        """
        server, self._server = self._server, None
        if server is not None:
            # close() is synchronous and stops accepting immediately; clear the
            # handle first so a CancelledError during wait_closed() cannot leave
            # the instance believing it is still listening.
            server.close()
            logger.info("MCP OAuth callback server stopped on %s:%d", self.bind, self.port)
            await server.wait_closed()
        if not self._future.done():
            self._future.cancel()


class OAuthProviderFactory:
    """Factory that assembles an ``OAuthClientProvider`` from raw config."""

    @staticmethod
    def build(
        server_cfg: dict,
        mcp_tokens_dir: Path,
        cb_server: CallbackServer,
        tg_iface: object | None = None,
        chat_id: int | None = None,
    ) -> OAuthClientProvider:
        """Build an ``OAuthClientProvider`` for an HTTP MCP server.

        Args:
            server_cfg: Raw MCP server dict. Must contain ``name``, ``url``,
                and an ``oauth`` subsection with the OAuth fields.
            mcp_tokens_dir: Directory for persisting tokens.
            cb_server: A ``CallbackServer`` that will receive the OAuth redirect.
                May be unstarted — the redirect handler starts it lazily, and the
                callback handler stops it as soon as the code arrives, so the
                caller need not stop it. A caller that starts it up front should
                still stop it in a ``finally`` as a backstop, for flows that abort
                before the redirect ever fires.
            tg_iface: Optional Telegram interface used to forward the auth URL.
            chat_id: Chat ID to send the authorization prompt to. Required when
                ``tg_iface`` is provided for interactive flows.

        Returns:
            A configured ``OAuthClientProvider``.
        """
        name = server_cfg["name"]
        server_url = server_cfg["url"]
        oauth_cfg = server_cfg["oauth"]

        client_metadata = OAuthClientMetadata(
            redirect_uris=[oauth_cfg["redirect_uri"]],
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method="client_secret_basic",
            scope=oauth_cfg["scope"],
            client_name="smallpieclaw",
        )

        storage = FileTokenStorage(
            server_name=name,
            mcp_tokens_dir=mcp_tokens_dir,
            client_id=oauth_cfg["client_id"],
            client_secret=oauth_cfg["client_secret"],
        )

        trace = oauth_cfg.get("trace", False)
        timeout = int(oauth_cfg.get("timeout", 300))
        redirect_handler = make_redirect_handler(
            tg_iface, name, cb_server, chat_id=chat_id, trace=trace, timeout=timeout
        )
        callback_handler = make_callback_handler(cb_server, timeout=timeout)

        return OAuthClientProvider(
            server_url=server_url,
            client_metadata=client_metadata,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=float(timeout),
        )


def make_redirect_handler(
    tg_iface: object | None,
    server_name: str,
    cb_server: CallbackServer,
    chat_id: int | None = None,
    trace: bool = False,
    timeout: int = 300,
) -> Callable[[str], Awaitable[None]]:
    """Return an async handler that forwards the auth URL to Telegram.

    Also wires the OAuth ``state`` from the auth URL into ``cb_server`` so
    the callback handler can validate it as defense-in-depth.

    Args:
        tg_iface: Optional Telegram interface used to forward the auth URL.
        server_name: Human-readable name of the MCP server being authorized.
        cb_server: Callback server that will receive the OAuth redirect.
        chat_id: Chat ID to send the authorization prompt to. Required when
            ``tg_iface`` is provided for interactive flows.
        trace: When ``True``, log the full authorization URL at INFO. The
            URL contains ``client_id`` and ``scope`` — diagnostic-only.
        timeout: OAuth flow timeout in seconds, surfaced in the prompt text.
    """
    async def _handler(auth_url: str) -> None:
        # Lazily start the callback server here so a fallback full-redirect
        # flow (e.g. a stored refresh token turning out to be invalid during
        # a non-interactive reconnect) has a listener ready for the callback.
        # Idempotent: a no-op if an interactive flow already started it.
        await cb_server.start()

        # Extract state from the auth URL and wire it into the callback server
        # for defense-in-depth CSRF validation (the SDK also validates state).
        parsed = urlparse(auth_url)
        params = parse_qs(parsed.query)
        state = params.get("state", [""])[0]
        if state:
            cb_server.set_expected_state(state)

        logger.info(
            "MCP [%s] redirect_handler called (state=%s...)",
            server_name,
            state[:8] if state else "",
        )
        if trace:
            logger.info("MCP [%s] auth URL: %s", server_name, auth_url)

        if tg_iface is None or chat_id is None:
            # Degraded path: no Telegram chat in context (e.g. non-interactive
            # reconnect with a stored refresh token that turned out invalid).
            # This is a legitimate manual-authorize fallback — the operator
            # can visit the URL from the logs.  Do NOT raise here; raising
            # would abort the SDK flow, which is wrong for this path since
            # the operator may still complete authorization manually.
            logger.warning(
                "MCP [%s] needs re-authorization but no Telegram chat is in "
                "context for this flow; visit this URL to authorize: %s",
                server_name,
                auth_url,
            )
            return

        send_fn = getattr(tg_iface, "send_oauth_prompt", None)
        if send_fn is None:
            logger.warning(
                "MCP [%s] Telegram interface has no send_oauth_prompt; "
                "cannot send auth URL to chat. URL: %s",
                server_name,
                auth_url,
            )
            raise RuntimeError(
                f"Telegram interface has no send_oauth_prompt; cannot deliver "
                f"auth URL for MCP [{server_name}]"
            )
        try:
            future = send_fn(chat_id, server_name, auth_url, timeout)
            if future is None:
                raise RuntimeError(
                    f"Could not schedule OAuth prompt delivery for MCP "
                    f"[{server_name}] — Telegram app not built or loop not running"
                )
            # Await the delivery result on the MCP loop via wrap_future.
            # If send_message fails on the Telegram loop, this re-raises
            # here, aborting the SDK's auth flow so the operator isn't
            # stranded waiting for a callback that will never come.
            await asyncio.wrap_future(future)
            logger.info(
                "MCP [%s] auth URL delivered to Telegram (chat=%s)",
                server_name,
                chat_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "MCP [%s] failed to deliver auth URL via Telegram: %s",
                server_name,
                exc,
            )
            raise

    return _handler


def make_callback_handler(
    cb_server: CallbackServer,
    timeout: float = 300.0,
) -> Callable[[], Awaitable[tuple[str, str | None]]]:
    """Return an async handler that awaits the callback server result.

    The listener is closed as soon as the callback resolves — before token
    exchange and session initialization — so the port is exposed only for the
    window it is actually needed. Closing here also covers the lazily-started
    fallback flow, whose callback server has no other owner to stop it.

    Args:
        cb_server: Callback server that will receive the OAuth redirect.
        timeout: Maximum seconds to wait for the OAuth callback.
    """
    async def _handler() -> tuple[str, str | None]:
        try:
            return await cb_server.wait_for_callback(timeout=timeout)
        finally:
            # Also runs on timeout and cancellation. stop() is idempotent, so the
            # owning flow's own finally-block stop() remains a harmless backstop.
            await cb_server.stop()

    return _handler
