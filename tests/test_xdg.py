"""Tests for xdg.py — XDG Base Directory path resolution."""

from __future__ import annotations

import dataclasses
import os

import pytest

from path_policy import PathPolicy, PathVerdict
from xdg import migration_sentinel_exists, write_migration_sentinel, xdg_paths


class TestXdgPathsDefaults:
    def test_default_resolved_paths(self, tmp_xdg, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        home_config = tmp_xdg / "config"
        home_data = tmp_xdg / "data"
        home_state = tmp_xdg / "state"
        home_cache = tmp_xdg / "cache"

        paths = xdg_paths("test-agent")

        assert paths.config_home == home_config / "test-agent"
        assert paths.data_home == home_data / "test-agent"
        assert paths.state_home == home_state / "test-agent"
        assert paths.cache_home == home_cache / "test-agent"
        assert paths.runtime_dir == home_state / "test-agent"

        assert paths.config_file == paths.config_home / "config.toml"
        assert paths.models_config_file == paths.config_home / "models-config.toml"
        assert paths.model_selection_file == paths.config_home / "model-selection.toml"
        assert paths.models_cache_file == paths.cache_home / "models-cache.json"
        assert paths.scheduler_config == paths.config_home / "scheduler.toml"
        assert paths.memory_file == paths.data_home / "memory.json"
        assert paths.graph_memory_db == paths.data_home / "graph_memory"
        assert paths.tool_index_file == paths.cache_home / "tool_index.json"
        assert paths.pid_file == paths.runtime_dir / "agent.pid"
        assert paths.secrets_file == paths.state_home / "secrets.toml"
        assert paths.mcp_tokens_dir == paths.state_home / "mcp_tokens"
        assert paths.logs_dir == paths.state_home / "logs"
        assert paths.log_file == paths.logs_dir / "agent.log"
        assert paths.log_store == paths.logs_dir / "agent_logs.sqlite"
        assert paths.skills_dir == paths.state_home / "skills"
        assert paths.scheduler_state == paths.state_home / "scheduler_state.json"
        assert paths.scheduler_commands == paths.state_home / "scheduler_commands.json"
        assert paths.scheduler_jobs == paths.state_home / "scheduler_jobs.json"
        assert paths.job_execution_log == paths.state_home / "job_execution_log.jsonl"

    def test_each_env_var_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c1"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "c2"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "c3"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c4"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "c5"))

        paths = xdg_paths("agent")

        assert paths.config_home == tmp_path / "c1" / "agent"
        assert paths.models_config_file == tmp_path / "c1" / "agent" / "models-config.toml"
        assert paths.model_selection_file == tmp_path / "c1" / "agent" / "model-selection.toml"
        assert paths.models_cache_file == tmp_path / "c4" / "agent" / "models-cache.json"
        assert paths.data_home == tmp_path / "c2" / "agent"
        assert paths.state_home == tmp_path / "c3" / "agent"
        assert paths.cache_home == tmp_path / "c4" / "agent"
        assert paths.runtime_dir == tmp_path / "c5" / "agent"

    def test_runtime_dir_unset_falls_back_to_state_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

        paths = xdg_paths("agent")

        assert paths.runtime_dir == paths.state_home
        assert paths.pid_file == paths.state_home / "agent.pid"

    def test_runtime_dir_set_uses_runtime_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

        paths = xdg_paths("agent")

        assert paths.runtime_dir == tmp_path / "run" / "agent"
        assert paths.pid_file == tmp_path / "run" / "agent" / "agent.pid"

    def test_purity_no_dirs_created(self, tmp_xdg):
        paths = xdg_paths("agent")
        # tmp_xdg pre-creates the XDG_RUNTIME_DIR root itself (mirroring
        # systemd-logind) — xdg_paths() must not create anything below it.
        for d in (paths.config_home, paths.data_home, paths.state_home,
                  paths.cache_home, paths.runtime_dir):
            assert not d.exists()
        for leaf in (paths.model_selection_file, paths.models_cache_file,
                     paths.models_config_file):
            assert not leaf.exists()
        assert sorted(p.name for p in tmp_xdg.iterdir()) == ["runtime"]


def test_model_selection_and_cache_leaves_resolve_in_config_and_cache_homes(
    tmp_path, monkeypatch,
):
    """Selection preference in protected config home; cache in cache home only."""
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom-cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "custom-cache"))
    before = sorted(tmp_path.rglob("*"))

    paths = xdg_paths("piclaw")

    assert sorted(tmp_path.rglob("*")) == before  # pure: nothing created
    assert paths.model_selection_file == tmp_path / "custom-cfg" / "piclaw" / "model-selection.toml"
    assert paths.models_cache_file == tmp_path / "custom-cache" / "piclaw" / "models-cache.json"
    # Retired combined settings path stays resolvable for its existence check only.
    assert paths.models_config_file == tmp_path / "custom-cfg" / "piclaw" / "models-config.toml"
    assert paths.models_config_file != paths.model_selection_file
    assert paths.data_home == tmp_path / "home" / ".local" / "share" / "piclaw"

    # Moving only the cache root leaves the durable preference in config home.
    monkeypatch.delenv("XDG_CACHE_HOME")
    moved = xdg_paths("piclaw")
    assert moved.model_selection_file == paths.model_selection_file
    assert moved.models_cache_file == tmp_path / "home" / ".cache" / "piclaw" / "models-cache.json"

    with pytest.raises(dataclasses.FrozenInstanceError):
        paths.model_selection_file = tmp_path / "elsewhere.toml"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        paths.models_cache_file = tmp_path / "elsewhere.json"  # type: ignore[misc]

    # Real access-control boundary built from the resolved homes (ADR-0019/0028).
    for d in (paths.config_home, paths.data_home, paths.state_home,
              tmp_path / "ws", tmp_path / "dl"):
        d.mkdir(parents=True, exist_ok=True)
    policy = PathPolicy.create(
        agent_name="piclaw",
        workspace_dir=str(tmp_path / "ws"),
        downloads_dir=str(tmp_path / "dl"),
        tmp_dir=str(tmp_path / "tmp"),
        skills_dir=str(tmp_path / "skills"),
        results_dir=str(tmp_path / "results"),
        data_home=str(paths.data_home),
        state_home=str(paths.state_home),
        config_home=str(paths.config_home),
    )
    selection = os.path.realpath(paths.model_selection_file)
    for operation in ("read", "write"):
        verdict, _reason = policy.classify(selection, operation)
        assert verdict is PathVerdict.PROHIBITED
    # No new rule for the cache leaf: it is not opened up by the policy.
    cache_verdict, _ = policy.classify(os.path.realpath(paths.models_cache_file), "write")
    assert cache_verdict is not PathVerdict.ALLOWED


class TestMigrationSentinel:
    def test_sentinel_absent_returns_false(self, tmp_xdg):
        paths = xdg_paths("agent")
        assert migration_sentinel_exists(paths) is False

    def test_sentinel_round_trip(self, tmp_xdg):
        paths = xdg_paths("agent")
        paths.state_home.mkdir(parents=True)
        write_migration_sentinel(paths)
        assert migration_sentinel_exists(paths) is True

    def test_sentinel_file_is_timestamped(self, tmp_xdg):
        paths = xdg_paths("agent")
        paths.state_home.mkdir(parents=True)
        write_migration_sentinel(paths)
        sentinels = list(paths.state_home.glob("migrated_from_*.sentinel"))
        assert len(sentinels) == 1
        assert sentinels[0].name.startswith("migrated_from_")
        assert sentinels[0].name.endswith(".sentinel")
