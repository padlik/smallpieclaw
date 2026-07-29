"""Tests for session_logs age-based retention cleanup on startup."""

import os
import time

from main import _cleanup_old_session_logs


class TestSessionLogsRetention:
    def test_old_folders_deleted_new_preserved(self, tmp_path):
        state_dir = str(tmp_path)
        session_logs_root = tmp_path / "session_logs"
        conversations_dir = tmp_path / "conversations"
        session_logs_root.mkdir()
        conversations_dir.mkdir()

        # Old conversation (older than 7 days)
        old_conv = session_logs_root / "oldconv123"
        old_conv.mkdir()
        old_file = old_conv / "shell-old.log"
        old_file.write_text("old output")
        old_time = time.time() - (10 * 86400)
        os.utime(old_file, (old_time, old_time))
        (conversations_dir / "oldconv123.json").write_text("{}")
        os.utime(conversations_dir / "oldconv123.json", (old_time, old_time))

        # New conversation (recent)
        new_conv = session_logs_root / "newconv456"
        new_conv.mkdir()
        (new_conv / "shell-new.log").write_text("new output")
        (conversations_dir / "newconv456.json").write_text("{}")

        # Active conversation (old but should be preserved)
        active_conv = session_logs_root / "active789"
        active_conv.mkdir()
        active_file = active_conv / "shell-active.log"
        active_file.write_text("active output")
        os.utime(active_file, (old_time, old_time))
        (conversations_dir / "active789.json").write_text("{}")

        _cleanup_old_session_logs(state_dir, "active789", retention_days=7)

        assert not old_conv.exists()  # deleted
        assert not (conversations_dir / "oldconv123.json").exists()
        assert new_conv.exists()  # preserved
        assert (conversations_dir / "newconv456.json").exists()
        assert active_conv.exists()  # active preserved even though old
        assert (conversations_dir / "active789.json").exists()

    def test_no_session_logs_dir_is_noop(self, tmp_path):
        # No session_logs dir -- should not error
        _cleanup_old_session_logs(str(tmp_path), "active", 7)

    def test_empty_folder_uses_dir_mtime(self, tmp_path):
        state_dir = str(tmp_path)
        session_logs_root = tmp_path / "session_logs"
        conversations_dir = tmp_path / "conversations"
        session_logs_root.mkdir()
        conversations_dir.mkdir()

        empty_conv = session_logs_root / "emptyconv"
        empty_conv.mkdir()
        old_time = time.time() - (10 * 86400)
        os.utime(empty_conv, (old_time, old_time))
        (conversations_dir / "emptyconv.json").write_text("{}")
        os.utime(conversations_dir / "emptyconv.json", (old_time, old_time))

        _cleanup_old_session_logs(state_dir, "active", retention_days=7)

        assert not empty_conv.exists()
        assert not (conversations_dir / "emptyconv.json").exists()

    def test_configurable_retention(self, tmp_path):
        state_dir = str(tmp_path)
        session_logs_root = tmp_path / "session_logs"
        conversations_dir = tmp_path / "conversations"
        session_logs_root.mkdir()
        conversations_dir.mkdir()

        ten_day_conv = session_logs_root / "tenconv"
        ten_day_conv.mkdir()
        ten_file = ten_day_conv / "shell-ten.log"
        ten_file.write_text("ten day output")
        ten_time = time.time() - (10 * 86400)
        os.utime(ten_file, (ten_time, ten_time))
        (conversations_dir / "tenconv.json").write_text("{}")

        _cleanup_old_session_logs(state_dir, "active", retention_days=30)

        assert ten_day_conv.exists()
        assert (conversations_dir / "tenconv.json").exists()

    def test_orphan_conversation_json_deleted(self, tmp_path):
        state_dir = str(tmp_path)
        session_logs_root = tmp_path / "session_logs"
        conversations_dir = tmp_path / "conversations"
        session_logs_root.mkdir()
        conversations_dir.mkdir()

        orphan_json = conversations_dir / "orphanconv.json"
        orphan_json.write_text("{}")
        old_time = time.time() - (10 * 86400)
        os.utime(orphan_json, (old_time, old_time))

        _cleanup_old_session_logs(state_dir, "active", retention_days=7)

        assert not orphan_json.exists()

    def test_active_conv_preserved_regardless_of_age(self, tmp_path):
        state_dir = str(tmp_path)
        session_logs_root = tmp_path / "session_logs"
        conversations_dir = tmp_path / "conversations"
        session_logs_root.mkdir()
        conversations_dir.mkdir()

        active_conv = session_logs_root / "activeold"
        active_conv.mkdir()
        active_file = active_conv / "shell-active.log"
        active_file.write_text("active output")
        old_time = time.time() - (365 * 86400)
        os.utime(active_file, (old_time, old_time))
        (conversations_dir / "activeold.json").write_text("{}")

        _cleanup_old_session_logs(state_dir, "activeold", retention_days=7)

        assert active_conv.exists()
        assert (conversations_dir / "activeold.json").exists()
