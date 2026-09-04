"""Tests for main._bootstrap_dot_home and path-policy wiring through BuiltinExecutor."""

from __future__ import annotations

import os
from unittest.mock import patch

import main as main_mod
from config_schema import ExecutorPaths
from path_policy import PathPolicy


# ---------------------------------------------------------------------------
# _bootstrap_dot_home tests
# ---------------------------------------------------------------------------


def _bootstrap(agent_name: str, xdg_state_home: str) -> tuple[str, str]:
    """Call main._bootstrap_dot_home and return realpath'd results."""
    skills, results = main_mod._bootstrap_dot_home(agent_name, xdg_state_home)
    return os.path.realpath(skills), os.path.realpath(results)


class TestBootstrapDotHome:
    def test_creates_both_dirs(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        skills, results = _bootstrap("test-agent", str(tmp_path / "state"))

        assert skills == os.path.realpath(str(home / ".test-agent" / "skills"))
        assert results == os.path.realpath(str(home / ".test-agent" / "results"))
        assert os.path.isdir(skills)
        assert os.path.isdir(results)

    def test_idempotent_second_call(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        skills1, results1 = _bootstrap("test-agent", str(tmp_path / "state"))
        marker = os.path.join(skills1, "existing", "SKILL.md")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as f:
            f.write("keep me")

        skills2, results2 = _bootstrap("test-agent", str(tmp_path / "state"))
        assert skills1 == skills2
        assert results1 == results2
        assert os.path.exists(marker)

    def test_one_time_copy_from_legacy(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        state_home = tmp_path / "state"
        legacy = state_home / "skills"
        for name in ("skill_a", "skill_b"):
            path = legacy / name
            os.makedirs(path, exist_ok=True)
            with open(path / "SKILL.md", "w") as f:
                f.write(f"# {name}")

        skills, _ = _bootstrap("test-agent", str(state_home))
        assert os.path.exists(os.path.join(skills, "skill_a", "SKILL.md"))
        assert os.path.exists(os.path.join(skills, "skill_b", "SKILL.md"))
        # Legacy dir still exists and is unchanged.
        assert os.path.exists(legacy / "skill_a" / "SKILL.md")
        assert os.path.exists(legacy / "skill_b" / "SKILL.md")

    def test_populated_target_skips_copy(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        state_home = tmp_path / "state"
        legacy = state_home / "skills"
        os.makedirs(legacy / "legacy_skill", exist_ok=True)
        with open(legacy / "legacy_skill" / "SKILL.md", "w") as f:
            f.write("legacy")

        dot_skills = home / ".test-agent" / "skills"
        os.makedirs(dot_skills / "existing_skill", exist_ok=True)
        with open(dot_skills / "existing_skill" / "SKILL.md", "w") as f:
            f.write("existing")

        skills, _ = _bootstrap("test-agent", str(state_home))
        assert os.path.exists(os.path.join(skills, "existing_skill", "SKILL.md"))
        assert not os.path.exists(os.path.join(skills, "legacy_skill", "SKILL.md"))

    def test_missing_legacy_dir_still_creates_empty_dirs(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        skills, results = _bootstrap("test-agent", str(tmp_path / "state"))
        assert os.path.isdir(skills)
        assert os.path.isdir(results)
        assert os.listdir(skills) == []


# ---------------------------------------------------------------------------
# Wiring smoke tests
# ---------------------------------------------------------------------------


def _make_path_policy(tmp_path: str, agent_name: str = "test-agent") -> PathPolicy:
    workspace = os.path.join(tmp_path, "workspace")
    downloads = os.path.join(tmp_path, "downloads")
    data_home = os.path.join(tmp_path, "xdg", "data", agent_name)
    state_home = os.path.join(tmp_path, "xdg", "state", agent_name)
    config_home = os.path.join(tmp_path, "xdg", "config", agent_name)
    skills = os.path.join(tmp_path, "skills")
    results = os.path.join(tmp_path, "results")
    vault = os.path.join(tmp_path, "vault.toml")
    config = os.path.join(config_home, "config.toml")
    for d in (workspace, downloads, data_home, state_home, config_home, skills, results):
        os.makedirs(d, exist_ok=True)
    for f in (vault, config):
        with open(f, "w") as fh:
            fh.write("\n")
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
    )


class TestBuiltinExecutorWiring:
    def test_builtin_executor_stores_path_policy(self, make_builtin_executor, tmp_path):
        policy = _make_path_policy(str(tmp_path))
        ex = make_builtin_executor(path_policy=policy)
        assert ex.path_policy is policy

    def test_builtin_executor_omitted_path_policy_is_none(self, make_builtin_executor):
        ex = make_builtin_executor()
        assert ex.path_policy is None

    def test_nsjail_without_path_policy_is_inactive(self, make_builtin_executor, tmp_path):
        ex = make_builtin_executor(
            shell_backend="nsjail",
            paths=ExecutorPaths(
                data_dir=str(tmp_path / "data"),
                state_home=str(tmp_path / "state"),
                workspace_dir=str(tmp_path / "workspace"),
                tmp_dir=str(tmp_path / "tmp"),
                nsjail_session_tmpdir=str(tmp_path / "nsjail_session"),
            ),
        )
        assert ex._shell_nsjail_active is False

    def test_nsjail_with_path_policy_is_active(self, make_builtin_executor, tmp_path):
        policy = _make_path_policy(str(tmp_path))
        with patch("shutil.which", return_value="/usr/bin/nsjail"):
            ex = make_builtin_executor(
                path_policy=policy,
                shell_backend="nsjail",
                paths=ExecutorPaths(
                    data_dir=str(tmp_path / "data"),
                    state_home=str(tmp_path / "state"),
                    workspace_dir=str(tmp_path / "workspace"),
                    tmp_dir=str(tmp_path / "tmp"),
                    nsjail_session_tmpdir=str(tmp_path / "nsjail_session"),
                ),
            )
        assert ex._shell_nsjail_active is True
        assert ex._nsjail_builder is not None

    def test_nsjail_inactive_when_binary_missing(self, make_builtin_executor, tmp_path):
        policy = _make_path_policy(str(tmp_path))
        with patch("shutil.which", return_value=None):
            ex = make_builtin_executor(
                path_policy=policy,
                shell_backend="nsjail",
                paths=ExecutorPaths(
                    data_dir=str(tmp_path / "data"),
                    state_home=str(tmp_path / "state"),
                    workspace_dir=str(tmp_path / "workspace"),
                    tmp_dir=str(tmp_path / "tmp"),
                    nsjail_session_tmpdir=str(tmp_path / "nsjail_session"),
                ),
            )
        assert ex._shell_nsjail_active is False

    def test_executor_paths_results_dir_is_wired(self, tmp_path):
        paths = ExecutorPaths(
            data_dir=str(tmp_path / "data"),
            state_home=str(tmp_path / "state"),
            workspace_dir=str(tmp_path / "workspace"),
            tmp_dir=str(tmp_path / "tmp"),
            results_dir=str(tmp_path / "results"),
        )
        assert paths.results_dir == str(tmp_path / "results")
