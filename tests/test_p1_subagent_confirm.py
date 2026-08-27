"""P1: Sub-agent headless Telegram confirmation bridge tests.

Tests for BuiltinExecutor._headless_confirm_bridge and signal_headless_confirm,
covering: approval, denial, timeout, bridge-not-wired fail-closed, and
double-press safety.
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock


from builtin_executor import BuiltinExecutor


def _make_executor(make_builtin_executor) -> BuiltinExecutor:
    return make_builtin_executor(default_timeout=30)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_result_after(executor: BuiltinExecutor, token_holder: list, approved: bool, delay: float = 0.05):
    """Background thread: waits until token is populated then signals the bridge."""
    def _worker():
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if token_holder:
                break
            time.sleep(0.005)
        if token_holder:
            executor.signal_headless_confirm(token_holder[0], approved)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Bridge not wired (fail-closed)
# ---------------------------------------------------------------------------

class TestHeadlessBridgeNotWired:
    def test_fails_closed_when_no_prompt_fn(self, make_builtin_executor):
        exe = _make_executor(make_builtin_executor)
        assert exe._subagent_confirm_prompt_fn is None
        result = exe._requires_confirmation("file_write", {"path": "/tmp/x", "content": "hi"},
                                            "Write /tmp/x", caller_depth=1)
        assert result["success"] is False
        assert "not available" in result["error"]

    def test_fails_closed_sensitive_file_read(self, make_builtin_executor):
        exe = _make_executor(make_builtin_executor)
        result = exe._requires_confirmation("file_read", {"path": "/etc/passwd"},
                                            "Read /etc/passwd", caller_depth=1)
        assert result["success"] is False

    def test_fails_closed_file_patch(self, make_builtin_executor):
        exe = _make_executor(make_builtin_executor)
        result = exe._requires_confirmation("file_patch", {"path": "/etc/hosts", "old_str": "a", "new_str": "b"},
                                            "Patch /etc/hosts", caller_depth=1)
        assert result["success"] is False

    def test_shell_still_blocked_at_depth1(self, make_builtin_executor):
        """Dangerous shell at depth>=1 is always blocked, regardless of bridge."""
        exe = _make_executor(make_builtin_executor)
        exe._subagent_confirm_prompt_fn = MagicMock()  # bridge wired, but shell blocked first
        result = exe._requires_confirmation("shell", {"command": "rm -rf /"},
                                            "danger", caller_depth=1)
        assert result["success"] is False
        assert "blocked" in result["error"].lower()

    def test_depth0_still_returns_requires_confirmation(self, make_builtin_executor):
        """At depth=0 the normal requires_confirmation dict is returned."""
        exe = _make_executor(make_builtin_executor)
        result = exe._requires_confirmation("file_write", {"path": "/tmp/x", "content": ""},
                                            "desc", caller_depth=0)
        assert result.get("requires_confirmation") is True
        assert "token" in result


# ---------------------------------------------------------------------------
# Bridge wired — approval path
# ---------------------------------------------------------------------------

class TestHeadlessBridgeApproved:
    def test_approved_executes_and_returns_success(self, make_builtin_executor, tmp_path):
        exe = _make_executor(make_builtin_executor)
        test_file = str(tmp_path / "out.txt")
        content = "hello from sub-agent"

        tokens_seen: list[str] = []

        def prompt_fn(token, tool_name, description, caller_tag):
            tokens_seen.append(token)

        exe._subagent_confirm_prompt_fn = prompt_fn

        # Approve in a background thread after the token is captured
        def _approve():
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if tokens_seen:
                    break
                time.sleep(0.005)
            if tokens_seen:
                exe.signal_headless_confirm(tokens_seen[0], True)

        t = threading.Thread(target=_approve, daemon=True)
        t.start()

        result = exe._requires_confirmation(
            "file_write",
            {"path": test_file, "content": content, "mode": "w"},
            f"Write {test_file}",
            caller_depth=1,
        )
        t.join(timeout=4.0)

        assert result["success"] is True, result.get("error")
        assert os.path.exists(test_file)
        assert open(test_file).read() == content

    def test_approved_sensitive_file_read(self, make_builtin_executor, tmp_path):
        exe = _make_executor(make_builtin_executor)
        secret = tmp_path / ".env"
        secret.write_text("SECRET=xyz")

        tokens_seen: list[str] = []

        def prompt_fn(token, tool_name, desc, tag):
            tokens_seen.append(token)

        exe._subagent_confirm_prompt_fn = prompt_fn

        def _approve():
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if tokens_seen:
                    break
                time.sleep(0.005)
            if tokens_seen:
                exe.signal_headless_confirm(tokens_seen[0], True)

        t = threading.Thread(target=_approve, daemon=True)
        t.start()

        result = exe._requires_confirmation(
            "file_read", {"path": str(secret)},
            f"Read {secret}", caller_depth=1,
        )
        t.join(timeout=4.0)

        assert result["success"] is True
        assert "SECRET=xyz" in result["output"]


# ---------------------------------------------------------------------------
# Bridge wired — denial path
# ---------------------------------------------------------------------------

class TestHeadlessBridgeDenied:
    def test_denied_returns_failure_and_does_not_write(self, make_builtin_executor, tmp_path):
        exe = _make_executor(make_builtin_executor)
        test_file = str(tmp_path / "should_not_exist.txt")

        tokens_seen: list[str] = []

        def prompt_fn(token, tool_name, desc, tag):
            tokens_seen.append(token)

        exe._subagent_confirm_prompt_fn = prompt_fn

        def _deny():
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if tokens_seen:
                    break
                time.sleep(0.005)
            if tokens_seen:
                exe.signal_headless_confirm(tokens_seen[0], False)

        t = threading.Thread(target=_deny, daemon=True)
        t.start()

        result = exe._requires_confirmation(
            "file_write",
            {"path": test_file, "content": "should not be written", "mode": "w"},
            f"Write {test_file}",
            caller_depth=1,
        )
        t.join(timeout=4.0)

        assert result["success"] is False
        assert "denied" in result["error"].lower()
        assert not os.path.exists(test_file)


# ---------------------------------------------------------------------------
# Timeout path
# ---------------------------------------------------------------------------

class TestHeadlessBridgeTimeout:
    def test_timeout_returns_failure(self, make_builtin_executor):
        exe = _make_executor(make_builtin_executor)
        exe._subagent_confirm_timeout = 1  # very short for testing

        def prompt_fn(token, tool_name, desc, tag):
            pass  # never signals

        exe._subagent_confirm_prompt_fn = prompt_fn

        result = exe._requires_confirmation(
            "file_write", {"path": "/tmp/never", "content": "x"},
            "Write /tmp/never", caller_depth=1,
        )

        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    def test_timeout_cleans_up_pending(self, make_builtin_executor):
        exe = _make_executor(make_builtin_executor)
        exe._subagent_confirm_timeout = 1

        def prompt_fn(token, tool_name, desc, tag):
            pass

        exe._subagent_confirm_prompt_fn = prompt_fn

        exe._requires_confirmation(
            "file_write", {"path": "/tmp/never", "content": "x"},
            "desc", caller_depth=1,
        )

        # After timeout, no orphaned pending entries should remain
        assert len(exe._pending) == 0
        assert len(exe._headless_confirm_events) == 0


# ---------------------------------------------------------------------------
# Double-press safety
# ---------------------------------------------------------------------------

class TestHeadlessBridgeDoublePress:
    def test_second_signal_returns_false(self, make_builtin_executor):
        exe = _make_executor(make_builtin_executor)
        tokens_seen: list[str] = []

        def prompt_fn(token, tool_name, desc, tag):
            tokens_seen.append(token)

        exe._subagent_confirm_prompt_fn = prompt_fn

        # Simulate: token emitted → approve asynchronously
        def _approve():
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if tokens_seen:
                    break
                time.sleep(0.005)
            if tokens_seen:
                exe.signal_headless_confirm(tokens_seen[0], True)

        t = threading.Thread(target=_approve, daemon=True)
        t.start()

        exe._requires_confirmation(
            "file_write", {"path": "/tmp/never2", "content": "x"},
            "desc", caller_depth=1,
        )
        t.join(timeout=4.0)

        # Now try pressing the button again with the same token
        second = exe.signal_headless_confirm(tokens_seen[0], True)
        assert second is False  # already consumed


# ---------------------------------------------------------------------------
# Prompt-fn raises (fail-closed)
# ---------------------------------------------------------------------------

class TestHeadlessBridgePromptError:
    def test_prompt_fn_raises_fails_closed(self, make_builtin_executor):
        exe = _make_executor(make_builtin_executor)

        def bad_prompt_fn(token, tool_name, desc, tag):
            raise ConnectionError("Telegram down")

        exe._subagent_confirm_prompt_fn = bad_prompt_fn

        result = exe._requires_confirmation(
            "file_write", {"path": "/tmp/x", "content": "x"},
            "desc", caller_depth=1,
        )

        assert result["success"] is False
        assert "Telegram" in result["error"] or "prompt" in result["error"].lower()

    def test_prompt_fn_raises_cleans_up(self, make_builtin_executor):
        exe = _make_executor(make_builtin_executor)

        def bad_prompt_fn(token, tool_name, desc, tag):
            raise OSError("network unreachable")

        exe._subagent_confirm_prompt_fn = bad_prompt_fn

        exe._requires_confirmation(
            "file_write", {"path": "/tmp/x", "content": "x"},
            "desc", caller_depth=1,
        )

        assert len(exe._pending) == 0
        assert len(exe._headless_confirm_events) == 0

