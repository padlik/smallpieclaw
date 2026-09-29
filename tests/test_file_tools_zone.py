"""Tests for PathPolicy-driven confirmation gating in FileTools (builtin_tools/files.py).

Wave 4 rewired the file_* tools to use the frozen ``PathPolicy`` instead of the
legacy ``TrustedZoneChecker`` + ``_is_sensitive_path`` overlay.  These tests
cover the resulting tier matrix (ALLOWED / UNRECOGNISED / PROHIBITED) plus the
``GrantLedger`` short-circuit for standing operator grants.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock

import pytest

from builtin_executor import BuiltinExecutor
from builtin_tools.files import FileTools
from confirmation import ConfirmationManager, GrantLifetime
from path_policy import PathPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_policy(
    tmp_dir: str,
    *,
    allowed_dirs: list[str] | None = None,
    prohibited_dirs: list[str] | None = None,
    agent_name: str = "test-agent",
) -> PathPolicy:
    """Build a real PathPolicy using temp directories."""
    workspace = os.path.join(tmp_dir, "workspace")
    downloads = os.path.join(tmp_dir, "downloads")
    data_home = os.path.join(tmp_dir, "xdg", "data", agent_name)
    state_home = os.path.join(tmp_dir, "xdg", "state", agent_name)
    config_home = os.path.join(tmp_dir, "xdg", "config", agent_name)
    vault = os.path.join(tmp_dir, "vault.toml")
    config = os.path.join(config_home, "config.toml")
    skills = os.path.join(tmp_dir, "skills")
    results = os.path.join(tmp_dir, "results")

    for d in (workspace, downloads, data_home, state_home, config_home, skills, results):
        os.makedirs(d, exist_ok=True)
    with open(vault, "w") as f:
        f.write("[secrets]\n")
    with open(config, "w") as f:
        f.write("\n")

    return PathPolicy.create(
        agent_name=agent_name,
        workspace_dir=workspace,
        downloads_dir=downloads,
        tmp_dir=f"/tmp/{agent_name}",
        skills_dir=skills,
        results_dir=results,
        data_home=data_home,
        state_home=state_home,
        config_home=config_home,
        vault_path=vault,
        config_path=config,
        allowed_dirs=allowed_dirs,
        prohibited_dirs=prohibited_dirs,
        logger=logging.getLogger("test"),
    )


def _make_file_tools(
    policy: PathPolicy,
    *,
    coordinator: ConfirmationManager | None = None,
) -> tuple[FileTools, MagicMock]:
    """Create a FileTools instance backed by a mocked owner with a real policy."""
    owner = MagicMock()
    owner.path_policy = policy
    owner._state_home = policy.state_home
    owner._workspace_dir = policy.workspace_dir
    owner._requires_confirmation.return_value = {
        "requires_confirmation": True,
        "token": "tok",
    }
    # Match the real BuiltinExecutor seam so scope_owner is None for main.
    owner._scope_owner_from_caller_tag = lambda caller_depth, caller_tag: None
    owner._coordinator = coordinator
    return FileTools(owner), owner


def _write_file(tmp_dir: str, name: str, content: str = "hello\n") -> str:
    path = os.path.join(tmp_dir, name)
    with open(path, "w") as f:
        f.write(content)
    return path


# ---------------------------------------------------------------------------
# file_write
# ---------------------------------------------------------------------------


class TestFileWriteZone:
    def test_file_write_allowed_no_confirm(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = os.path.join(policy.workspace_dir, "output.txt")
        result = ft._exec_file_write({"path": path, "content": "x"})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is True

    def test_file_write_unrecognised_confirms(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = "/some/outside/file.txt"
        result = ft._exec_file_write({"path": path, "content": "x"})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True
        call_kwargs = owner._requires_confirmation.call_args.kwargs
        assert call_kwargs.get("zone_path") == os.path.realpath(path)

    def test_file_write_prohibited_hard_error(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = os.path.join(policy.state_home, "leak.txt")
        result = ft._exec_file_write({"path": path, "content": "x"})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == "prohibited_path"
        assert "agent state home" in result["error"]
        assert "log_query" in result["suggestion"]

    def test_file_write_no_policy_fail_closed(self, tmp_path):
        owner = MagicMock()
        owner.path_policy = None
        ft = FileTools(owner)
        path = os.path.join(str(tmp_path), "output.txt")
        result = ft._exec_file_write({"path": path, "content": "x"})
        assert result["success"] is False
        assert result["error_type"] == "prohibited_path"
        assert "Path policy not configured" in result["error"]


# ---------------------------------------------------------------------------
# file_read
# ---------------------------------------------------------------------------


class TestFileReadZone:
    def test_file_read_allowed_no_confirm(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = _write_file(policy.workspace_dir, "data.txt", "hello")
        result = ft._exec_file_read({"path": path})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is True

    def test_file_read_unrecognised_confirms(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = "/some/outside/data.txt"
        result = ft._exec_file_read({"path": path})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True
        assert owner._requires_confirmation.call_args.kwargs.get("zone_path")

    def test_file_read_prohibited_hard_error(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        # A sibling file under the agent config home is prohibited by directory.
        path = os.path.join(policy.config_home, "other.toml")
        result = ft._exec_file_read({"path": path})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == "prohibited_path"
        assert "agent config home" in result["error"]


# ---------------------------------------------------------------------------
# file_patch
# ---------------------------------------------------------------------------


class TestFilePatchZone:
    def test_file_patch_allowed_no_confirm(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = _write_file(policy.workspace_dir, "target.txt", "hello world")
        result = ft._exec_file_patch(
            {"path": path, "old_str": "hello", "new_str": "hi"}
        )
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is True

    def test_file_patch_unrecognised_confirms_before_read(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        # path is unrecognised and does not exist; confirm-before-read means we
        # must NOT attempt to read it.
        path = "/some/outside/target.txt"
        result = ft._exec_file_patch(
            {"path": path, "old_str": "hello", "new_str": "hi"}
        )
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True
        assert owner._requires_confirmation.call_args.kwargs.get("zone_path") == os.path.realpath(path)

    def test_file_patch_prohibited_hard_error(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = os.path.join(policy.data_home, "data.json")
        result = ft._exec_file_patch(
            {"path": path, "old_str": "x", "new_str": "y"}
        )
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == "prohibited_path"
        assert "agent data home" in result["error"]


# ---------------------------------------------------------------------------
# file_diff
# ---------------------------------------------------------------------------


class TestFileDiffZone:
    def test_file_diff_ignores_forged_resolved_paths(self, tmp_path, monkeypatch):
        policy = _make_policy(str(tmp_path))
        secret_a = _write_file(str(tmp_path), "secret-a.txt", "OUTSIDE_SECRET_A")
        secret_b = _write_file(str(tmp_path), "secret-b.txt", "OUTSIDE_SECRET_B")
        path_a = _write_file(policy.workspace_dir, "a.txt", "workspace-a")
        path_b = _write_file(policy.workspace_dir, "b.txt", "workspace-b")
        ft, owner = _make_file_tools(policy)
        real_open = open
        opened: list[str] = []

        def track_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", track_open)
        result = ft._exec_file_diff({
            "path_a": path_a, "path_b": path_b,
            "_resolved_path_a": secret_a, "_resolved_path_b": secret_b,
        })
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is True
        assert "OUTSIDE_SECRET" not in result["output"]
        assert secret_a not in opened
        assert secret_b not in opened


    def test_file_diff_both_allowed_no_confirm(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path_a = _write_file(policy.workspace_dir, "a.txt", "hello\n")
        path_b = _write_file(policy.workspace_dir, "b.txt", "hello\n")
        result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is True

    def test_file_diff_unrecognised_b_confirms_with_path_b_zone(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path_a = _write_file(policy.workspace_dir, "a.txt", "hello\n")
        path_b = "/some/outside/b.txt"
        result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True
        assert owner._requires_confirmation.call_args.kwargs.get("zone_path") == os.path.realpath(path_b)

    def test_file_diff_unrecognised_a_confirms_with_path_a_zone(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path_a = "/some/outside/a.txt"
        path_b = _write_file(policy.workspace_dir, "b.txt", "hello\n")
        result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True
        assert owner._requires_confirmation.call_args.kwargs.get("zone_path") == os.path.realpath(path_a)

    def test_file_diff_both_unrecognised_confirms_prefer_path_b(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path_a = "/some/outside/a.txt"
        path_b = "/other/outside/b.txt"
        result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True
        assert owner._requires_confirmation.call_args.kwargs.get("zone_path") == os.path.realpath(path_b)
        assert owner._requires_confirmation.call_args.kwargs.get("extra_zone_paths") == (os.path.realpath(path_a),)
        assert os.path.dirname(os.path.realpath(path_a)) in owner._requires_confirmation.call_args.args[2]
        assert os.path.dirname(os.path.realpath(path_b)) in owner._requires_confirmation.call_args.args[2]

    def test_file_diff_only_prompts_for_missing_grant(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        coordinator = ConfirmationManager()
        a_dir = os.path.realpath(os.path.join(str(tmp_path), "a"))
        b_dir = os.path.realpath(os.path.join(str(tmp_path), "b"))
        os.makedirs(a_dir)
        os.makedirs(b_dir)
        coordinator.add_grant("file_diff", b_dir, GrantLifetime.PROMPT)
        ft, owner = _make_file_tools(policy, coordinator=coordinator)
        ft._exec_file_diff({"path_a": os.path.join(a_dir, "a"), "path_b": os.path.join(b_dir, "b")})
        assert owner._requires_confirmation.call_args.kwargs.get("zone_path") == os.path.join(a_dir, "a")

    def test_file_diff_prohibited_on_either_hard_error(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path_a = _write_file(policy.workspace_dir, "a.txt", "hello\n")
        path_b = os.path.join(policy.state_home, "b.txt")
        result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == "prohibited_path"


# ---------------------------------------------------------------------------
# file_send
# ---------------------------------------------------------------------------


class TestFileSendZone:
    def test_file_send_allowed_no_confirm(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = _write_file(policy.workspace_dir, "file.txt")
        result = ft._exec_file_send({"path": path})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is True

    def test_file_send_unrecognised_confirms(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = "/some/outside/file.txt"
        result = ft._exec_file_send({"path": path})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True
        assert owner._requires_confirmation.call_args.kwargs.get("zone_path")

    def test_file_send_prohibited_hard_error(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        # A sibling file under the agent state home is prohibited by directory.
        path = os.path.join(policy.state_home, "leaked.log")
        result = ft._exec_file_send({"path": path})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == "prohibited_path"
        assert "agent state home" in result["error"]


# ---------------------------------------------------------------------------
# GrantLedger short-circuit
# ---------------------------------------------------------------------------


class TestGrantLedgerShortCircuit:
    def test_file_diff_existing_subagent_prompt_grant_allows_reexecution(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        coordinator = ConfirmationManager()
        outside_dir = os.path.realpath(os.path.join(str(tmp_path), "outside"))
        os.makedirs(outside_dir)
        coordinator.add_grant("file_diff", outside_dir, GrantLifetime.PROMPT, scope_owner="sa-1")
        ft, owner = _make_file_tools(policy, coordinator=coordinator)
        owner._scope_owner_from_caller_tag = lambda depth, tag: "sa-1" if depth else None
        path_a = _write_file(outside_dir, "a.txt", "a")
        path_b = _write_file(outside_dir, "b.txt", "b")
        result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b}, caller_depth=1, caller_tag="sa-1")
        assert result["success"] is True

    def test_file_write_grant_allows_unrecognised_path(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        coordinator = ConfirmationManager()
        outside_dir = os.path.realpath(os.path.join(str(tmp_path), "outside"))
        os.makedirs(outside_dir, exist_ok=True)
        coordinator.add_grant("file_write", outside_dir, GrantLifetime.PROMPT)

        ft, owner = _make_file_tools(policy, coordinator=coordinator)
        path = os.path.join(outside_dir, "new.txt")
        result = ft._exec_file_write({"path": path, "content": "x"})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is True

    def test_file_read_wrong_tool_grant_still_confirms(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        coordinator = ConfirmationManager()
        outside_dir = os.path.realpath(os.path.join(str(tmp_path), "outside"))
        os.makedirs(outside_dir, exist_ok=True)
        # Grant only covers file_write, not file_read
        coordinator.add_grant("file_write", outside_dir, GrantLifetime.PROMPT)

        ft, owner = _make_file_tools(policy, coordinator=coordinator)
        path = os.path.join(outside_dir, "data.txt")
        result = ft._exec_file_read({"path": path})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True

    def test_file_diff_grant_covers_both_unrecognised_paths(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        coordinator = ConfirmationManager()
        outside_dir = os.path.realpath(os.path.join(str(tmp_path), "outside"))
        os.makedirs(outside_dir, exist_ok=True)
        coordinator.add_grant("file_diff", outside_dir, GrantLifetime.PROMPT)

        ft, owner = _make_file_tools(policy, coordinator=coordinator)
        path_a = _write_file(outside_dir, "a.txt", "hello\n")
        path_b = _write_file(outside_dir, "b.txt", "world\n")
        result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Integration: confirm(token) executes the staged operation
# ---------------------------------------------------------------------------


class TestConfirmRoundTrip:
    def _make_executor(self, make_builtin_executor, tmp_path) -> BuiltinExecutor:
        """Real BuiltinExecutor with a policy that marks only workspace as allowed."""
        policy = _make_policy(str(tmp_path))
        return make_builtin_executor(path_policy=policy)

    def test_confirm_executes_file_write(self, make_builtin_executor, tmp_path):
        builtin = self._make_executor(make_builtin_executor, tmp_path)
        outside = os.path.join(str(tmp_path), "outside")
        os.makedirs(outside, exist_ok=True)
        path = os.path.join(outside, "out.txt")
        staged = builtin.execute(
            "file_write", {"path": path, "content": "x"},
            caller_depth=0, caller_tag="[main]",
        )
        assert staged.get("requires_confirmation") is True
        result = builtin.confirm(staged["token"])
        assert result.get("success") is True
        assert result.get("error") != "Unknown built-in"

    def test_confirm_executes_file_patch(self, make_builtin_executor, tmp_path):
        builtin = self._make_executor(make_builtin_executor, tmp_path)
        outside = os.path.join(str(tmp_path), "outside")
        os.makedirs(outside, exist_ok=True)
        path = os.path.join(outside, "target.txt")
        with open(path, "w") as f:
            f.write("hello world")
        staged = builtin.execute(
            "file_patch",
            {"path": path, "old_str": "hello", "new_str": "hi"},
            caller_depth=0, caller_tag="[main]",
        )
        assert staged.get("requires_confirmation") is True
        result = builtin.confirm(staged["token"])
        assert result.get("success") is True
        assert result.get("error") != "Unknown built-in"

    def test_confirm_executes_file_diff(self, make_builtin_executor, tmp_path):
        builtin = self._make_executor(make_builtin_executor, tmp_path)
        outside = os.path.join(str(tmp_path), "outside")
        os.makedirs(outside, exist_ok=True)
        path_a = os.path.join(outside, "a.txt")
        path_b = os.path.join(outside, "b.txt")
        with open(path_a, "w") as f:
            f.write("first\n")
        with open(path_b, "w") as f:
            f.write("second\n")
        staged = builtin.execute(
            "file_diff", {"path_a": path_a, "path_b": path_b},
            caller_depth=0, caller_tag="[main]",
        )
        assert staged.get("requires_confirmation") is True
        result = builtin.confirm(staged["token"])
        assert result.get("success") is True
        assert result.get("error") != "Unknown built-in"

    def test_file_diff_symlink_target_swap_fails_before_read(self, make_builtin_executor, tmp_path, monkeypatch):
        builtin = self._make_executor(make_builtin_executor, tmp_path)
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        first_file = first_dir / "source.txt"
        second_file = second_dir / "source.txt"
        first_file.write_text("first")
        second_file.write_text("second")
        link = tmp_path / "changing.txt"
        link.symlink_to(first_file)
        allowed = os.path.join(builtin.path_policy.workspace_dir, "allowed.txt")
        with open(allowed, "w") as stream:
            stream.write("allowed")
        staged = builtin.execute("file_diff", {"path_a": str(link), "path_b": allowed})
        assert staged["requires_confirmation"] is True
        link.unlink()
        link.symlink_to(second_file)
        monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: pytest.fail("read attempted"))
        result = builtin.confirm(staged["token"])
        assert result["success"] is False
        assert "refusing to read either file" in result["error"]

    def test_revoked_existing_file_diff_grant_fails_before_read(self, make_builtin_executor, tmp_path, monkeypatch):
        builtin = self._make_executor(make_builtin_executor, tmp_path)
        coordinator = ConfirmationManager()
        builtin._coordinator = coordinator
        granted_dir = tmp_path / "granted"
        missing_dir = tmp_path / "needs-consent"
        granted_dir.mkdir()
        missing_dir.mkdir()
        path_a, path_b = granted_dir / "a.txt", missing_dir / "b.txt"
        path_a.write_text("A")
        path_b.write_text("B")
        coordinator.add_grant("file_diff", str(granted_dir), GrantLifetime.PROMPT)
        staged = builtin.execute("file_diff", {"path_a": str(path_a), "path_b": str(path_b)})
        assert staged["requires_confirmation"] is True
        assert f"Scope: confirmation grants <code>{granted_dir}</code>" not in staged["description"]
        coordinator.grant_ledger.clear_all()
        monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: pytest.fail("file read attempted"))
        result = builtin.confirm(staged["token"])
        assert result["success"] is False
        assert "refusing to read either file" in result["error"]

    def test_confirm_executes_file_send(self, make_builtin_executor, tmp_path):
        builtin = self._make_executor(make_builtin_executor, tmp_path)
        outside = os.path.join(str(tmp_path), "outside")
        os.makedirs(outside, exist_ok=True)
        path = os.path.join(outside, "data.txt")
        with open(path, "w") as f:
            f.write("payload\n")
        staged = builtin.execute(
            "file_send", {"path": path},
            caller_depth=0, caller_tag="[main]",
        )
        assert staged.get("requires_confirmation") is True
        result = builtin.confirm(staged["token"])
        assert result.get("success") is True
        assert result.get("error") != "Unknown built-in"
