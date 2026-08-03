"""Tests for main.py — CLI arg parsing, XDG dir creation, and startup flow helpers."""

from __future__ import annotations

import logging

import pytest

import main as main_mod
from xdg import xdg_paths


class TestAgentNameArg:
    def test_absent_agent_name_exits_nonzero(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py"])
        with pytest.raises(SystemExit) as exc:
            main_mod._parse_args()
        assert exc.value.code != 0

    def test_present_agent_name_parsed(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py", "--agent-name", "myagent"])
        args = main_mod._parse_args()
        assert args.agent_name == "myagent"


class TestCreateXdgDirs:
    def test_first_launch_creates_all_dirs(self, tmp_xdg):
        paths = xdg_paths("agent")
        main_mod._create_xdg_dirs(paths)
        for d in (paths.config_home, paths.data_home, paths.state_home,
                  paths.cache_home, paths.logs_dir, paths.skills_dir, paths.runtime_dir):
            assert d.is_dir()

    def test_second_launch_is_idempotent(self, tmp_xdg):
        paths = xdg_paths("agent")
        main_mod._create_xdg_dirs(paths)
        main_mod._create_xdg_dirs(paths)  # must not raise
        assert paths.state_home.is_dir()

    def test_runtime_dir_parent_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-such-parent" / "run"))
        paths = xdg_paths("agent")
        with pytest.raises(FileNotFoundError):
            main_mod._create_xdg_dirs(paths)

    def test_state_home_permissions_owner_only(self, tmp_xdg):
        import stat

        paths = xdg_paths("agent")
        main_mod._create_xdg_dirs(paths)
        mode = stat.S_IMODE(paths.state_home.stat().st_mode)
        assert mode == 0o700


class TestConfigMissingExit:
    def test_missing_config_exits_with_path_in_message(self, tmp_xdg, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py", "--agent-name", "agent"])
        paths = xdg_paths("agent")
        with pytest.raises(SystemExit) as exc:
            main_mod.main()
        assert str(paths.config_file) in str(exc.value.code)


class TestWarnRelativePaths:
    def test_relative_path_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="main"):
            main_mod._warn_relative_paths({"paths": {"workspace_dir": "./foo"}})
        assert any("looks like a relative path" in r.message for r in caplog.records)

    def test_tilde_path_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="main"):
            main_mod._warn_relative_paths({"paths": {"workspace_dir": "~/Documents"}})
        assert not any("looks like a relative path" in r.message for r in caplog.records)

    def test_absolute_path_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="main"):
            main_mod._warn_relative_paths({"paths": {"workspace_dir": "/opt/workspace"}})
        assert not any("looks like a relative path" in r.message for r in caplog.records)

    def test_nested_dict_scanned(self, caplog):
        with caplog.at_level(logging.WARNING, logger="main"):
            main_mod._warn_relative_paths({"agent": {"nested": {"value": "./relative"}}})
        assert any("agent.nested.value" in r.message for r in caplog.records)


class TestDownloadsDirDerivation:
    def test_default_workspace_gives_documents_downloads(self, tmp_xdg, monkeypatch):
        import os

        monkeypatch.setattr("sys.argv", ["main.py", "--agent-name", "agent"])
        # Recreate the derivation logic exactly as main() computes it, given a
        # config with no explicit workspace_dir.
        cfg = {"paths": {}}
        workspace_dir = os.path.abspath(os.path.expanduser(
            cfg.get("paths", {}).get("workspace_dir", "~/Documents")
        ))
        downloads_dir = os.path.join(workspace_dir, "downloads")
        assert workspace_dir == os.path.expanduser("~/Documents")
        assert downloads_dir == os.path.join(os.path.expanduser("~/Documents"), "downloads")

    def test_custom_workspace_gives_custom_downloads(self, tmp_path):
        import os

        cfg = {"paths": {"workspace_dir": str(tmp_path / "myworkspace")}}
        workspace_dir = os.path.abspath(os.path.expanduser(
            cfg.get("paths", {}).get("workspace_dir", "~/Documents")
        ))
        downloads_dir = os.path.join(workspace_dir, "downloads")
        assert workspace_dir == str(tmp_path / "myworkspace")
        assert downloads_dir == str(tmp_path / "myworkspace" / "downloads")


class TestCheckMigration:
    def test_auto_triggers_when_config_toml_alongside_main_and_no_sentinel(self, tmp_xdg, monkeypatch):
        calls = []
        monkeypatch.setattr(main_mod.migrate, "migration_sentinel_exists", lambda p: False)
        monkeypatch.setattr(main_mod.Path, "exists", lambda self: True)
        monkeypatch.setattr(main_mod.migrate, "main", lambda name, source: calls.append((name, source)) or ["did something"])

        paths = xdg_paths("agent")
        main_mod._check_migration(paths, "agent")
        assert calls

    def test_skips_when_sentinel_exists(self, tmp_xdg, monkeypatch):
        monkeypatch.setattr(main_mod.migrate, "migration_sentinel_exists", lambda p: True)
        called = []
        monkeypatch.setattr(main_mod.migrate, "main", lambda *a, **k: called.append(1))

        paths = xdg_paths("agent")
        main_mod._check_migration(paths, "agent")
        assert not called

    def test_skips_when_no_config_toml_present(self, tmp_xdg, monkeypatch):
        monkeypatch.setattr(main_mod.migrate, "migration_sentinel_exists", lambda p: False)
        called = []
        monkeypatch.setattr(main_mod.migrate, "main", lambda *a, **k: called.append(1))

        paths = xdg_paths("agent")
        main_mod._check_migration(paths, "agent")
        assert not called
