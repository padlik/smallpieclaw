"""Tests for migrate.py — one-shot migration to the XDG layout."""

from __future__ import annotations

import json

import migrate
from xdg import migration_sentinel_exists, xdg_paths


def _make_old_layout(source, with_data: bool = True) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "config.toml").write_text('[agent]\nagent_name = "old"\n')
    (source / "scheduler.toml").write_text("[[jobs]]\n")
    if with_data:
        data = source / "data"
        data.mkdir()
        (data / "memory.json").write_text(json.dumps({"facts": []}))
        (data / "graph_memory").write_text("db")
        (data / "graph_memory.wal").write_text("wal")
        (data / "graph_memory.wal.checkpoint").write_text("ckpt")
        (data / "scheduler_state.json").write_text("{}")
        (data / "scheduler_commands.json").write_text("{}")
        (data / "scheduled_jobs.json").write_text("[]")
        (data / "job_execution_log.jsonl").write_text("")
        (data / "tool_index.json").write_text("{}")
        (data / "results_memory.json").write_text("{}")
        (data / "longterm_memory.json").write_text("{}")
        (data / "graph_memory_backfill_state.json").write_text("{}")
        (data / "trusted_dirs.json").write_text("[]")
    skills = source / "skills"
    skills.mkdir()
    (skills / "hello" / "SKILL.md").parent.mkdir(parents=True)
    (skills / "hello" / "SKILL.md").write_text("---\nname: hello\n---\n")


class TestDetection:
    def test_old_layout_present_migrates(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        summary = migrate.main("agent", source)
        assert summary
        paths = xdg_paths("agent")
        assert paths.config_file.exists()

    def test_no_config_toml_skips(self, tmp_xdg, tmp_path):
        source = tmp_path / "empty"
        source.mkdir()
        summary = migrate.main("agent", source)
        assert summary == []

    def test_sentinel_exists_skips(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        paths = xdg_paths("agent")
        paths.state_home.mkdir(parents=True)
        from xdg import write_migration_sentinel
        write_migration_sentinel(paths)

        summary = migrate.main("agent", source)
        assert summary == []
        assert not paths.config_file.exists()


class TestMigrationSteps:
    def test_config_toml_copied(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert paths.config_file.read_text() == (source / "config.toml").read_text()

    def test_scheduler_toml_copied(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert paths.scheduler_config.read_text() == (source / "scheduler.toml").read_text()

    def test_memory_json_copied(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert paths.memory_file.exists()

    def test_graph_memory_glob_copies_all_three_variants(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert (paths.data_home / "graph_memory").exists()
        assert (paths.data_home / "graph_memory.wal").exists()
        assert (paths.data_home / "graph_memory.wal.checkpoint").exists()

    def test_scheduler_state_files_copied(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert paths.scheduler_state.exists()
        assert paths.scheduler_commands.exists()
        assert paths.scheduler_jobs.exists()
        assert paths.job_execution_log.exists()

    def test_results_memory_copied(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert (paths.data_home / "results_memory.json").exists()

    def test_longterm_memory_copied(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert (paths.data_home / "longterm_memory.json").exists()

    def test_graph_memory_backfill_state_copied(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert (paths.data_home / "graph_memory_backfill_state.json").exists()

    def test_trusted_dirs_copied(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert (paths.state_home / "trusted_dirs.json").exists()

    def test_skills_dir_copied_recursively(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert (paths.skills_dir / "hello" / "SKILL.md").exists()

    def test_skip_if_destination_exists(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        paths = xdg_paths("agent")
        paths.config_home.mkdir(parents=True)
        paths.config_file.write_text("EXISTING")
        migrate.main("agent", source)
        assert paths.config_file.read_text() == "EXISTING"

    def test_custom_source_directory(self, tmp_xdg, tmp_path):
        source = tmp_path / "custom" / "location"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert paths.config_file.exists()
        assert paths.config_file.read_text() == (source / "config.toml").read_text()


class TestToolIndexDeletion:
    def test_tool_index_deleted_after_success(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        tool_index = source / "data" / "tool_index.json"
        assert tool_index.exists()
        migrate.main("agent", source)
        assert not tool_index.exists()

    def test_tool_index_not_copied(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source)
        paths = xdg_paths("agent")
        assert not (paths.cache_home / "tool_index.json").exists()

    def test_tool_index_absent_is_noop(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        (source / "data" / "tool_index.json").unlink()
        summary = migrate.main("agent", source)
        assert summary  # other steps still ran


class TestSentinel:
    def test_sentinel_written_after_success(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        paths = xdg_paths("agent")
        assert not migration_sentinel_exists(paths)
        migrate.main("agent", source)
        assert migration_sentinel_exists(paths)

    def test_dry_run_does_not_write_sentinel(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        migrate.main("agent", source, dry_run=True)
        paths = xdg_paths("agent")
        assert not migration_sentinel_exists(paths)

    def test_dry_run_does_not_copy_files(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        summary = migrate.main("agent", source, dry_run=True)
        assert summary
        paths = xdg_paths("agent")
        assert not paths.config_file.exists()

    def test_dry_run_prints_would_actions(self, tmp_xdg, tmp_path):
        source = tmp_path / "old_agent"
        _make_old_layout(source)
        summary = migrate.main("agent", source, dry_run=True)
        assert any("would copy" in line for line in summary)
