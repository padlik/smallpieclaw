"""Task 4.3: root-produced run clients close on success/error/cancel and reach only their endpoint.

Drives the real ``main._run`` root through ``tests/root_startup_harness.py`` (provider HTTP is the
only replaced boundary; ``agent_controller.react_loop`` is a narrow boundary that sends one chat
through the run's own client).  Ordinary (Main = acme) and on-demand (explicit gem) products are
exercised in succession; every root-produced ``LLMClient``/``httpx.Client`` must be closed, and each
provider request must carry only its owning connection's synthetic canary key.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import agent_controller
from react_loop import ReactContext
from tests.root_startup_harness import ACME_HOST, GEM_HOST, MAIN_ID, OTHER_ID, SECRETS, RootHarness
from sub_agent_registry import SubAgentRecord
from tests.test_spawn_agent import _InlineExecutor

TRACE = "r-43000001"


@pytest.fixture
def root(tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RootHarness]:
    """Root serving provider chat; leftovers closed after assertions."""
    harness = RootHarness(tmp_xdg, monkeypatch)
    harness.serve_provider_chat()
    try:
        yield harness
    finally:
        harness.release_leftovers()


def _loop(mode: str):
    """``react_loop`` boundary: one chat on the run's client, then *mode* ends the run."""
    def run(ctx: ReactContext, goal: str, progress_callback: Any = None, images: Any = None, **kw: Any) -> str:
        """Send one chat; then succeed, raise, or cancel through the run's own event."""
        ctx.llm.chat([{"role": "user", "content": "ping"}])
        if mode == "error":
            raise RuntimeError("run failed after chat")
        if mode == "cancel":
            ctx.cancel_event.set()
            return "cancelled"
        return "ok"
    return run


def _provider_4xx(root: RootHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the gem chat endpoint reject with a genuine 400 (no fallback expected)."""
    original = root._route

    def route(request: httpx.Request) -> httpx.Response:
        """400 for gem chat; delegate the rest."""
        if request.method == "POST" and request.url.host == GEM_HOST:
            request.read()
            root.requests.append(request)
            return httpx.Response(400, json={"error": {"message": "bad request"}})
        return original(request)

    monkeypatch.setattr(root, "_route", route)


def _spawn_gem(
    started: RootHarness, monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], SubAgentRecord | None]:
    """Spawn the explicit gem target through the real executor/supervisor.

    Returns the spawn envelope and the supervisor-retained terminal record of the child
    (read through the public owner lookup; the inline pool has finished the run by then).
    """
    monkeypatch.setattr(started.builtin._supervisor, "_pool", _InlineExecutor())
    result = started.builtin._exec_spawn_agent(
        {"task": "work", "model": OTHER_ID}, caller_depth=0, trace_id=TRACE,
    )
    agent_id = result.get("agent_id")
    record = started.builtin._supervisor.get_owned_record(TRACE, agent_id) if agent_id else None
    return result, record


def _chat_posts(root: RootHarness, host: str) -> list[httpx.Request]:
    """Provider chat POSTs to *host*."""
    return [r for r in root.requests if r.method == "POST" and r.url.host == host]


def _assert_keys_only_for_owner(root: RootHarness) -> None:
    """Every provider request carries only its owning connection's canary."""
    seen = 0
    for request in root.requests:
        blob = repr(dict(request.headers)) + request.url.query.decode()
        if request.url.host == ACME_HOST and request.method == "POST":
            seen += 1
            assert SECRETS["KEY_ACME"] in blob and SECRETS["KEY_GEM"] not in blob
        if request.url.host == GEM_HOST and request.method == "POST":
            seen += 1
            assert SECRETS["KEY_GEM"] in blob and SECRETS["KEY_ACME"] not in blob
    assert seen, "no provider chat request was observed"


@pytest.mark.parametrize("mode", ["success", "error", "cancel"])
def test_ordinary_and_on_demand_clients_close_and_keys_stay_with_owner(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """Both products close their own clients per outcome; keys never cross endpoints."""
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Run an ordinary Main run then an on-demand explicit gem run."""
        monkeypatch.setattr(agent_controller, "react_loop", _loop(mode))
        try:
            started.agent_handler(1, "ordinary", None, trace_id=TRACE)
        except RuntimeError as exc:
            outcome["ordinary_error"] = str(exc)
        outcome["after_ordinary"] = (
            len(started.llm_clients), [c._closed for c in started.llm_clients],
        )
        outcome["spawn"], outcome["record"] = _spawn_gem(started, monkeypatch)

    root.start(saved_default=MAIN_ID, hold=hold)

    if mode == "error":
        assert "run failed after chat" in outcome["ordinary_error"]
    count, closed = outcome["after_ordinary"]
    assert count >= 1 and all(closed), "ordinary run client left open"
    # The ordinary run really executed: exactly one acme chat request.
    assert len(_chat_posts(root, ACME_HOST)) == 1
    # The child was really admitted and ran: accepted submission, one terminal handoff
    # whose status is the mode's actual terminal state, one gem chat request.
    spawn = outcome["spawn"]
    assert spawn["success"] is True and spawn.get("agent_id"), spawn
    done = outcome["record"]
    assert done is not None and done.agent_id == spawn["agent_id"] and done.model == OTHER_ID
    assert len(_chat_posts(root, GEM_HOST)) == 1
    if mode == "success":
        assert (done.status, done.result) == ("done", "ok")
    elif mode == "error":
        assert done.status == "failed" and "run failed after chat" in done.result
    else:
        assert (done.status, done.result) == ("cancelled", "[Cancelled]")
    assert len(root.llm_clients) >= 2, "no child client was produced"
    assert root.unexpected == []
    assert root.open_clients_at_exit == []
    assert all(c._closed for c in root.llm_clients)
    _assert_keys_only_for_owner(root)


def test_provider_4xx_closes_client_without_cross_model_fallback(
    root: RootHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine provider 400 on the explicit gem target is surfaced once; nothing retried on acme."""
    _provider_4xx(root, monkeypatch)
    outcome: dict[str, Any] = {}

    def hold(started: RootHarness) -> None:
        """Spawn the explicit gem target against the rejecting endpoint."""
        monkeypatch.setattr(agent_controller, "react_loop", _loop("success"))
        outcome["spawn"], outcome["record"] = _spawn_gem(started, monkeypatch)

    root.start(saved_default=MAIN_ID, hold=hold)

    posts = [r for r in root.requests if r.method == "POST" and r.url.host in (ACME_HOST, GEM_HOST)]
    assert len(posts) == 1 and posts[0].url.host == GEM_HOST  # surfaced once, no acme fallback
    spawn = outcome["spawn"]
    assert spawn["success"] is True and spawn.get("agent_id"), spawn  # admitted, then it failed
    done = outcome["record"]
    assert done is not None and done.agent_id == spawn["agent_id"]
    assert done.status == "failed", done  # the provider rejection surfaces; it is not swallowed
    assert "400" in done.result
    assert SECRETS["KEY_GEM"] not in done.result and GEM_HOST not in done.result
    assert all(c._closed for c in root.llm_clients)
    assert root.open_clients_at_exit == []
    _assert_keys_only_for_owner(root)
