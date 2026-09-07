"""Tests for vision_query operator confirmation flow (FU-1).

The ReAct loop executes ``vision_query`` itself (it needs LLM access), but
UNRECOGNISED image paths must still go through operator confirmation.  These
tests verify the full approve/deny/allowed/prohibited matrix without a live
Telegram bot or real image files.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import patch

from confirmation import GrantLedger, GrantLifetime
from path_policy import PathPolicy
from react_loop import ReactContext, _exec_vision_query, _run_vision_query


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeConfirmationManager:
    """Stand-in ConfirmationManager that records the confirmation request."""

    approved: bool = False
    grant_dir: str = ""
    last_call: Optional[tuple[str, str, str]] = field(default=None, init=False)
    grant_ledger: GrantLedger = field(default_factory=GrantLedger, init=False)

    def request_confirmation(
        self,
        token: str,
        tool_name: str,
        description: str,
        progress_cb: object,
    ) -> bool:
        """Record the call and optionally add a standing grant for the parent dir."""
        self.last_call = (token, tool_name, description)
        if self.approved and self.grant_dir:
            self.grant_ledger.add(
                tool_name, self.grant_dir, GrantLifetime.PROMPT, scope_owner=None,
            )
        return self.approved

    def check_grant(
        self,
        tool: str,
        dir: str,
        scope_owner: Optional[str] = None,
    ) -> bool:
        """Delegate to the real grant ledger."""
        return self.grant_ledger.check(tool, dir, scope_owner=scope_owner)


@dataclass
class _FakePendingConfirmations:
    """Stand-in for BuiltinExecutor._pending_confirmations with take/discard."""

    _data: dict[str, tuple[str, dict]] = field(default_factory=dict)
    taken: list[str] = field(default_factory=list)

    def take(self, token: str) -> Optional[tuple[str, dict]]:
        """Atomically pop the staged entry and record the take."""
        self.taken.append(token)
        return self._data.pop(token, None)

    def discard(self, token: str) -> bool:
        """Remove the staged entry without recording a take."""
        return self._data.pop(token, None) is not None


class _FakeBuiltinExecutor:
    """Minimal BuiltinExecutor stand-in with the seams _exec_vision_query needs."""

    def __init__(self, path_policy: PathPolicy):
        self.path_policy = path_policy
        self._pending_confirmations = _FakePendingConfirmations()
        self.cancelled: list[str] = []

    @property
    def _pending(self) -> dict[str, tuple[str, dict]]:
        """Public compat property used by Telegram callbacks and by the loop."""
        return self._pending_confirmations._data

    def cancel(self, token: str) -> None:
        """Discard the staged confirmation."""
        self.cancelled.append(token)
        self._pending_confirmations.discard(token)

    @staticmethod
    def _scope_owner_from_caller_tag(
        caller_depth: int, caller_tag: str,
    ) -> Optional[str]:
        """Main-agent tests run at depth 0, so no scope owner."""
        return None

    def _requires_confirmation(
        self,
        tool_name: str,
        args: dict,
        description: str,
        caller_depth: int = 0,
        caller_tag: str = "",
        zone_path: str = "",
    ) -> dict:
        """Stage a fake confirmation entry and return the confirmation dict."""
        token = f"tok-{len(self._pending_confirmations._data)}"
        self._pending_confirmations._data[token] = (tool_name, dict(args))
        # Use zone_path so the signature matches BuiltinExecutor and vulture is quiet.
        self._last_zone_path = zone_path
        return {
            "requires_confirmation": True,
            "token": token,
            "description": description,
        }


class _FakeLLM:
    """Fake LLMClient that returns a fixed answer and records vision calls."""

    def __init__(self, answer: str = "A cat.") -> None:
        self.answer = answer
        self.vision_calls: list[list[dict]] = []

    def chat(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        progress_cb: object = None,
        _json_mode: bool = False,
    ) -> str:
        self.vision_calls.append(messages)
        return self.answer


def _build_ctx(
    policy: PathPolicy,
    confirmation: _FakeConfirmationManager,
    approved_answer: str = "A cat.",
) -> ReactContext:
    """Return a minimal ReactContext wired for the vision confirmation tests."""
    return ReactContext(
        llm=_FakeLLM(answer=approved_answer),
        tool_index=object(),
        memory=object(),
        builtin_executor=_FakeBuiltinExecutor(policy),
        mcp_manager=None,
        skill_registry=None,
        cancel_event=threading.Event(),
        confirmation=confirmation,
    )


def _make_policy(tmp_path: str) -> PathPolicy:
    """Create a policy with temp Tier 1 dirs and no extra allowed/prohibited dirs."""
    workspace = os.path.join(tmp_path, "workspace")
    downloads = os.path.join(tmp_path, "downloads")
    results = os.path.join(tmp_path, "results")
    skills = os.path.join(tmp_path, "skills")
    data_home = os.path.join(tmp_path, "xdg", "data", "test-agent")
    state_home = os.path.join(tmp_path, "xdg", "state", "test-agent")
    config_home = os.path.join(tmp_path, "xdg", "config", "test-agent")
    for d in (workspace, downloads, results, skills, data_home, state_home, config_home):
        os.makedirs(d, exist_ok=True)
    return PathPolicy.create(
        agent_name="test-agent",
        workspace_dir=workspace,
        downloads_dir=downloads,
        tmp_dir="/tmp/test-agent",
        skills_dir=skills,
        results_dir=results,
        data_home=data_home,
        state_home=state_home,
        config_home=config_home,
    )


# ---------------------------------------------------------------------------
# Confirmation matrix
# ---------------------------------------------------------------------------


class TestVisionQueryConfirmation:
    """Operator confirmation for vision_query on UNRECOGNISED paths."""

    def test_unrecognised_approve_reinvokes_and_succeeds(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        image_path = "/unrecognised/pic.png"
        grant_dir = os.path.dirname(image_path)
        confirmation = _FakeConfirmationManager(approved=True, grant_dir=grant_dir)
        ctx = _build_ctx(policy, confirmation)

        with patch("react_loop._encode_images", return_value=["<b64>"]):
            outcome = _run_vision_query(
                ctx, {"path": image_path, "question": "What?"}, lambda _m: None,
            )

        assert outcome["success"] is True
        assert outcome["output"] == "A cat."
        # The confirmation was requested with the staged token and tool name.
        assert confirmation.last_call is not None
        token, tool_name, description = confirmation.last_call
        assert tool_name == "vision_query"
        assert "Unrecognised zone" in description
        # Successful run silently removes the staged token via take(), not cancel().
        assert token in ctx.builtin_executor._pending_confirmations.taken
        assert token not in ctx.builtin_executor.cancelled
        assert ctx.builtin_executor._pending == {}
        # The LLM saw the original path/question.
        assert len(ctx.llm.vision_calls) == 1
        assert ctx.llm.vision_calls[0][0]["content"] == "What?"
        assert ctx.llm.vision_calls[0][0]["images"] == [image_path]

    def test_unrecognised_deny_returns_cancelled_outcome_and_clears_staging(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        image_path = "/unrecognised/pic.png"
        confirmation = _FakeConfirmationManager(approved=False)
        ctx = _build_ctx(policy, confirmation)

        with patch("react_loop._encode_images", return_value=["<b64>"]):
            outcome = _run_vision_query(
                ctx, {"path": image_path, "question": "What?"}, lambda _m: None,
            )

        assert outcome["success"] is False
        assert outcome.get("_operator_cancelled") is True
        assert "cancelled" in outcome["error"].lower()
        assert ctx.llm.vision_calls == []
        assert confirmation.last_call is not None
        token = confirmation.last_call[0]
        assert token in ctx.builtin_executor.cancelled
        assert ctx.builtin_executor._pending == {}

    def test_allowed_path_runs_without_confirmation(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        image_path = os.path.join(policy.workspace_dir, "pic.png")
        confirmation = _FakeConfirmationManager(approved=False)
        ctx = _build_ctx(policy, confirmation)

        with patch("react_loop._encode_images", return_value=["<b64>"]):
            outcome = _run_vision_query(
                ctx, {"path": image_path, "question": "What?"}, lambda _m: None,
            )

        assert outcome["success"] is True
        assert outcome["output"] == "A cat."
        assert confirmation.last_call is None
        assert ctx.builtin_executor.cancelled == []
        assert ctx.builtin_executor._pending_confirmations.taken == []

    def test_prohibited_path_fails_closed_without_staging(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        image_path = os.path.join(policy.state_home, "pic.png")
        confirmation = _FakeConfirmationManager(approved=False)
        ctx = _build_ctx(policy, confirmation)

        outcome = _run_vision_query(
            ctx, {"path": image_path, "question": "What?"}, lambda _m: None,
        )

        assert outcome["success"] is False
        assert "prohibited" in outcome["error"].lower()
        assert confirmation.last_call is None
        assert ctx.builtin_executor._pending == {}
        assert ctx.builtin_executor.cancelled == []


class TestVisionQuerySubagentBridge:
    """Depth>=1 vision approval returns a re-execute sentinel from the bridge."""

    def test_vision_reexecute_sentinel_runs_after_grant(self, tmp_path):
        """A depth>=1 approval yields a sentinel; the loop re-runs via the grant."""
        policy = _make_policy(str(tmp_path))
        image_path = "/unrecognised/pic.png"
        confirmation = _FakeConfirmationManager()
        # Pre-seed the grant that Telegram's cb_subagent_confirm would have added.
        confirmation.grant_ledger.add(
            "vision_query", os.path.dirname(image_path), GrantLifetime.PROMPT,
        )
        ctx = _build_ctx(policy, confirmation)

        # Simulate the sub-agent bridge returning the sentinel.
        sentinel = {
            "vision_reexecute": True,
            "args": {"path": image_path, "question": "What?"},
            "success": False,
            "output": "",
            "error": "",
        }
        with patch("react_loop._encode_images", return_value=["<b64>"]), \
                patch.object(
                    ctx.builtin_executor, "_requires_confirmation",
                    return_value=sentinel,
                ):
            outcome = _run_vision_query(
                ctx, {"path": image_path, "question": "What?"}, lambda _m: None,
            )

        assert outcome["success"] is True
        assert outcome["output"] == "A cat."

    def test_vision_reexecute_sentinel_re_stages_fails_closed(self, tmp_path):
        """If the sentinel re-run itself re-stages, the leak guard refuses."""
        policy = _make_policy(str(tmp_path))
        image_path = "/unrecognised/pic.png"
        confirmation = _FakeConfirmationManager()
        ctx = _build_ctx(policy, confirmation)

        sentinel = {
            "vision_reexecute": True,
            "args": {"path": image_path, "question": "What?"},
            "success": False,
            "output": "",
            "error": "",
        }
        with patch("react_loop._encode_images", return_value=["<b64>"]), \
                patch.object(
                    ctx.builtin_executor, "_requires_confirmation",
                    return_value=sentinel,
                ):
            outcome = _run_vision_query(
                ctx, {"path": image_path, "question": "What?"}, lambda _m: None,
            )

        assert outcome["success"] is False
        assert "usable grant" in outcome["error"].lower()
        assert ctx.llm.vision_calls == []


class TestVisionQueryGrantCheck:
    """Standing grants promote UNRECOGNISED vision_query paths to allowed."""

    def test_grant_covers_unrecognised_path_directly(self, tmp_path):
        """_exec_vision_query consults the grant ledger before staging."""
        policy = _make_policy(str(tmp_path))
        image_path = "/unrecognised/pic.png"
        confirmation = _FakeConfirmationManager()
        confirmation.grant_ledger.add(
            "vision_query", os.path.dirname(image_path), GrantLifetime.PROMPT,
        )
        ctx = _build_ctx(policy, confirmation)

        with patch("react_loop._encode_images", return_value=["<b64>"]):
            outcome = _exec_vision_query(ctx, {"path": image_path, "question": "What?"})

        assert outcome["success"] is True
        assert outcome["output"] == "A cat."
        # No staging happened because the grant covered the path.
        assert ctx.builtin_executor._pending == {}
