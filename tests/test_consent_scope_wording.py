"""FU-3: confirmation descriptions must disclose grant granularity.

Each file-tool confirmation description shown to the operator must include a
scope line stating that approval grants the parent directory recursively.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock

import pytest

from builtin_tools.files import FileTools
from path_policy import PathPolicy


def _make_policy(tmp_dir: str, *, allowed_dirs: list[str] | None = None) -> PathPolicy:
    """Build a real PathPolicy using temp directories."""
    workspace = os.path.join(tmp_dir, "workspace")
    downloads = os.path.join(tmp_dir, "downloads")
    data_home = os.path.join(tmp_dir, "xdg", "data", "test-agent")
    state_home = os.path.join(tmp_dir, "xdg", "state", "test-agent")
    config_home = os.path.join(tmp_dir, "xdg", "config", "test-agent")
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
        agent_name="test-agent",
        workspace_dir=workspace,
        downloads_dir=downloads,
        tmp_dir="/tmp/test-agent",
        skills_dir=skills,
        results_dir=results,
        data_home=data_home,
        state_home=state_home,
        config_home=config_home,
        vault_path=vault,
        config_path=config,
        allowed_dirs=allowed_dirs,
        prohibited_dirs=None,
        logger=logging.getLogger("test"),
    )


def _make_file_tools(policy: PathPolicy) -> tuple[FileTools, MagicMock]:
    """Create a FileTools instance backed by a mocked owner with a real policy."""
    owner = MagicMock()
    owner.path_policy = policy
    owner._state_home = policy.state_home
    owner._workspace_dir = policy.workspace_dir
    owner._requires_confirmation.return_value = {
        "requires_confirmation": True,
        "token": "tok",
    }
    owner._scope_owner_from_caller_tag = lambda caller_depth, caller_tag: None
    owner._coordinator = None
    return FileTools(owner), owner


def _scope_line(real_path: str) -> str:
    """Return the expected scope line for a staged zone path."""
    return (
        f"📦 Scope: Confirm grants <code>{os.path.dirname(real_path)}</code> "
        "(all files under it) for this prompt; Till /reset grants it for the whole session"
    )


class TestConsentScopeWording:
    """Every file-tool description on an unrecognised path names the parent dir."""

    def test_file_read_scope_line(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = "/some/outside/data.txt"
        real_path = os.path.realpath(path)

        result = ft._exec_file_read({"path": path})

        assert result.get("requires_confirmation") is True
        desc = owner._requires_confirmation.call_args.args[2]
        assert f"Read file: <code>{path}</code>" in desc
        assert _scope_line(real_path) in desc

    def test_file_write_scope_line(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = "/some/outside/output.txt"
        real_path = os.path.realpath(path)

        result = ft._exec_file_write({"path": path, "content": "x"})

        assert result.get("requires_confirmation") is True
        desc = owner._requires_confirmation.call_args.args[2]
        assert f"Overwrite file: <code>{path}</code>" in desc
        assert _scope_line(real_path) in desc

    def test_file_patch_scope_line(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = "/some/outside/target.txt"
        real_path = os.path.realpath(path)

        result = ft._exec_file_patch(
            {"path": path, "old_str": "hello", "new_str": "hi"}
        )

        assert result.get("requires_confirmation") is True
        desc = owner._requires_confirmation.call_args.args[2]
        assert f"Patch file: <code>{path}</code>" in desc
        assert _scope_line(real_path) in desc
        # Confirm-before-read: description must be built from args only.
        assert "hello" in desc
        assert "hi" in desc

    def test_file_send_scope_line(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path = "/some/outside/file.txt"
        real_path = os.path.realpath(path)

        result = ft._exec_file_send({"path": path})

        assert result.get("requires_confirmation") is True
        desc = owner._requires_confirmation.call_args.args[2]
        assert f"Send file: <code>{path}</code>" in desc
        assert _scope_line(real_path) in desc

    def test_file_diff_scope_line_prefers_path_b(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path_a = "/some/outside/a.txt"
        path_b = "/other/outside/b.txt"
        real_path_b = os.path.realpath(path_b)

        result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})

        assert result.get("requires_confirmation") is True
        desc = owner._requires_confirmation.call_args.args[2]
        assert f"Diff files: <code>{path_a}</code> ↔ <code>{path_b}</code>" in desc
        assert _scope_line(real_path_b) in desc

    def test_file_diff_scope_line_falls_back_to_path_a(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        ft, owner = _make_file_tools(policy)
        path_a = "/some/outside/a.txt"
        path_b = os.path.join(policy.workspace_dir, "b.txt")
        with open(path_b, "w") as f:
            f.write("hello\n")
        real_path_a = os.path.realpath(path_a)

        result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})

        assert result.get("requires_confirmation") is True
        desc = owner._requires_confirmation.call_args.args[2]
        assert _scope_line(real_path_a) in desc


class TestConsentScopeRoundTrip:
    """Real BuiltinExecutor descriptions include the scope line end-to-end."""

    def test_confirm_file_write_description_scope(self, make_builtin_executor, tmp_path):
        builtin = make_builtin_executor(path_policy=_make_policy(str(tmp_path)))
        outside = os.path.join(str(tmp_path), "outside")
        os.makedirs(outside, exist_ok=True)
        path = os.path.join(outside, "out.txt")

        result = builtin.execute(
            "file_write", {"path": path, "content": "x"},
            caller_depth=0, caller_tag="[main]",
        )
        assert result.get("requires_confirmation") is True
        assert "📦 Scope: Confirm grants" in result.get("description", "")
        assert f"<code>{outside}</code>" in result.get("description", "")


@pytest.mark.skip(reason="vision_query ReactContext mock is heavy; covered by other lanes")
def test_vision_query_scope_line():
    """Placeholder: vision_query scope wording is verified by vision tests."""
