"""Tests for xdg.py — XDG Base Directory path resolution."""

from __future__ import annotations

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
        assert paths.scheduler_config == paths.config_home / "scheduler.toml"
        assert paths.memory_file == paths.data_home / "memory.json"
        assert paths.graph_memory_db == paths.data_home / "graph_memory"
        assert paths.tool_index_file == paths.cache_home / "tool_index.json"
        assert paths.pid_file == paths.runtime_dir / "agent.pid"
        assert paths.secrets_file == paths.state_home / "secrets.toml"
        assert paths.logs_dir == paths.state_home / "logs"
        assert paths.log_file == paths.logs_dir / "agent.log"
        assert paths.log_jsonl == paths.logs_dir / "agent.jsonl"
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
