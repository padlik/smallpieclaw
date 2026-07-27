"""Tests for NsjailConfigBuilder — system mount detection and config generation.

These tests mock filesystem calls to verify config generation logic without
requiring a real Linux host or nsjail binary.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

from nsjail_config import NsjailConfigBuilder


class TestDetectSystemMounts:
    """Tests for _detect_system_mounts() — symlink vs real dir detection."""

    def _make_builder(self) -> NsjailConfigBuilder:
        return NsjailConfigBuilder(
            project_dir="/tmp/test-project",
            session_tmpdir="/tmp/test-session",
            trusted_dirs_path="/tmp/test-data/trusted_dirs.json",
        )

    def test_usr_always_mounted_first_and_mandatory(self) -> None:
        """/usr is always mounted read-only with mandatory: true."""
        builder = self._make_builder()
        with patch("os.path.exists", return_value=False):
            mounts = builder._detect_system_mounts()
        # /usr should be the first line
        assert 'src: "/usr"' in mounts[0]
        assert "mandatory: true" in mounts[0]
        assert "rw: false" in mounts[0]

    def test_symlinked_dirs_use_mandatory_false(self) -> None:
        """Symlinked system dirs (e.g. /bin → usr/bin) use mandatory: false."""
        builder = self._make_builder()

        def mock_exists(path: str) -> bool:
            return path in {"/usr", "/bin", "/lib"}

        with patch("os.path.exists", side_effect=mock_exists), \
             patch("os.path.islink", side_effect=lambda p: p in {"/bin", "/lib"}), \
             patch("os.path.realpath", side_effect=lambda p: f"/usr{p}"):
            mounts = builder._detect_system_mounts()
        # /usr first, then /bin and /lib with mandatory: false
        assert len(mounts) == 3
        assert "mandatory: true" in mounts[0]  # /usr
        assert "mandatory: false" in mounts[1]  # /bin
        assert "mandatory: false" in mounts[2]  # /lib

    def test_real_dirs_use_mandatory_true(self) -> None:
        """Real (non-symlink) system dirs use mandatory: true."""
        builder = self._make_builder()

        def mock_exists(path: str) -> bool:
            return path in {"/usr", "/bin", "/sbin"}

        with patch("os.path.exists", side_effect=mock_exists), \
             patch("os.path.islink", return_value=False):
            mounts = builder._detect_system_mounts()
        assert len(mounts) == 3
        for line in mounts:
            assert "mandatory: true" in line

    def test_absent_dirs_are_skipped(self) -> None:
        """Non-existent system dirs are skipped entirely."""
        builder = self._make_builder()

        def mock_exists(path: str) -> bool:
            return path == "/usr"

        with patch("os.path.exists", side_effect=mock_exists):
            mounts = builder._detect_system_mounts()
        # Only /usr
        assert len(mounts) == 1
        assert 'src: "/usr"' in mounts[0]


class TestBuild:
    """Tests for build() — full config generation."""

    def test_config_contains_time_limit(self) -> None:
        """Config contains the correct time_limit from the timeout parameter."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("make test", timeout=60)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "time_limit: 60" in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_cwd(self) -> None:
        """Config contains the correct cwd from the project_dir."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert f'cwd: {json.dumps(builder.project_dir)}' in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_project_mount(self) -> None:
        """Config contains a RW bind mount for the project directory."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert f'src: {json.dumps(builder.project_dir)}' in content
            assert f'dst: {json.dumps(builder.project_dir)}' in content
            assert "rw: true" in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_tmp_mount(self) -> None:
        """Config contains a RW bind mount for the session tmpdir as /tmp."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert f'src: {json.dumps(builder.session_tmpdir)}' in content
            assert 'dst: "/tmp"' in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_base_envars(self) -> None:
        """Config contains base envar entries for PATH, HOME, LANG, TERM."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "envar: \"PATH=" in content
            assert "envar: \"HOME=" in content
            assert "envar: \"LANG=" in content
            assert "envar: \"TERM=" in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_keep_env_false(self) -> None:
        """Config sets keep_env: false for environment isolation."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "keep_env: false" in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_namespaces(self) -> None:
        """Config contains all required namespace clone directives."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "clone_newuser: true" in content
            assert "clone_newns: true" in content
            assert "clone_newpid: true" in content
            assert "clone_newipc: true" in content
            assert "clone_newuts: true" in content
            assert "clone_newcgroup: true" in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_command(self) -> None:
        """Config contains the command as the exec target."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("make test", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert 'path: "/bin/sh"' in content
            assert "make test" in content
        finally:
            os.unlink(cfg_path)

    def test_config_allow_net_false_creates_net_namespace(self) -> None:
        """allow_net=False sets clone_newnet: true (network isolated)."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
            allow_net=False,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "clone_newnet: true" in content
        finally:
            os.unlink(cfg_path)

    def test_config_allow_net_true_shares_net_namespace(self) -> None:
        """allow_net=True sets clone_newnet: false (host network)."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
            allow_net=True,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "clone_newnet: false" in content
        finally:
            os.unlink(cfg_path)

    def test_config_skills_dir_mounted_when_exists(self) -> None:
        """Existing skills_dir outside project_dir appears as a RO bind mount."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = os.path.join(tmpdir, "skills")
            os.makedirs(skills_dir)
            builder = NsjailConfigBuilder(
                project_dir="/home/user/project",
                session_tmpdir="/tmp/session",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                skills_dir=skills_dir,
            )
            cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert f'src: {json.dumps(builder.skills_dir)}' in content
                assert f'dst: {json.dumps(builder.skills_dir)}' in content
                assert "rw: false" in content
            finally:
                os.unlink(cfg_path)

    def test_config_skills_dir_skipped_when_missing(self) -> None:
        """A non-existent skills_dir path does not appear in the config."""
        missing_dir = "/tmp/nonexistent-skills-dir-for-test"
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
            skills_dir=missing_dir,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert builder.skills_dir not in content
        finally:
            os.unlink(cfg_path)

    def test_config_skills_dir_skipped_when_nested_under_project_dir(self) -> None:
        """A skills_dir nested under project_dir is not mounted separately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = os.path.join(tmpdir, "skills")
            os.makedirs(skills_dir)
            builder = NsjailConfigBuilder(
                project_dir=tmpdir,
                session_tmpdir="/tmp/session",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                skills_dir=skills_dir,
            )
            cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert builder.skills_dir not in content
            finally:
                os.unlink(cfg_path)

    def test_config_skills_dir_rejected_when_blocked_system_path(self) -> None:
        """A skills_dir under a blocked system prefix is not mounted."""
        with patch("os.path.isdir", return_value=True):
            builder = NsjailConfigBuilder(
                project_dir="/home/user/project",
                session_tmpdir="/tmp/session",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                skills_dir="/etc",
            )
            cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert 'src: "/etc"' not in content
            assert 'dst: "/etc"' not in content
        finally:
            os.unlink(cfg_path)

    def test_config_skills_dir_skipped_when_parent_of_project_dir(self) -> None:
        """A skills_dir that is a parent of project_dir is not mounted separately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = os.path.join(tmpdir, "project")
            os.makedirs(project_dir)
            builder = NsjailConfigBuilder(
                project_dir=project_dir,
                session_tmpdir="/tmp/session",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                skills_dir=tmpdir,
            )
            cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert f'src: {json.dumps(builder.skills_dir)}' not in content
                assert f'dst: {json.dumps(builder.skills_dir)}' not in content
            finally:
                os.unlink(cfg_path)

    def test_config_skills_dir_skipped_when_equal_to_project_dir(self) -> None:
        """A skills_dir equal to project_dir is not mounted separately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            builder = NsjailConfigBuilder(
                project_dir=tmpdir,
                session_tmpdir="/tmp/session",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                skills_dir=tmpdir,
            )
            cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "# Skills directory mount (read-only)" not in content
            finally:
                os.unlink(cfg_path)

    def test_config_skills_dir_under_home_nested_in_project_not_blocked(self) -> None:
        """Regression: skills_dir under /home but nested under project_dir must
        be skipped via commonpath, NOT rejected by the /home blocklist prefix.

        On Linux the default skills_dir resolves to /home/user/agent/skills which
        is under /home — the blocklist must not fire before the commonpath check.
        """
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.realpath", side_effect=lambda p: p), \
             patch("os.path.abspath", side_effect=lambda p: p):
            builder = NsjailConfigBuilder(
                project_dir="/home/user/myagent",
                session_tmpdir="/tmp/session",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                skills_dir="/home/user/myagent/skills",
            )
            cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            # The mount must be skipped (nested under project_dir), not rejected
            assert "# Skills directory mount (read-only)" not in content
            assert "/home/user/myagent/skills" not in content
        finally:
            os.unlink(cfg_path)

    def test_command_list_contains_nsjail_and_config(self) -> None:
        """Returned command list starts with nsjail --config."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, nsjail_cmd = builder.build("ls", timeout=30)
        try:
            assert nsjail_cmd[0] == "nsjail"
            assert nsjail_cmd[1] == "--config"
            assert nsjail_cmd[2] == cfg_path
        finally:
            os.unlink(cfg_path)

    def test_command_list_includes_env_flags(self) -> None:
        """Returned command list includes -E KEY=VALUE flags from shell_env."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, nsjail_cmd = builder.build(
            "ls", timeout=30, shell_env={"FOO": "bar", "BAZ": "qux"},
        )
        try:
            # nsjail --config <path> -E FOO=bar -E BAZ=qux
            assert "-E" in nsjail_cmd
            assert "FOO=bar" in nsjail_cmd
            assert "BAZ=qux" in nsjail_cmd
        finally:
            os.unlink(cfg_path)

    def test_trusted_dirs_loaded_from_json(self) -> None:
        """Trusted dirs from trusted_dirs.json appear as mount entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create trusted_dirs.json — use /opt/data (not under /home or other
            # blocklisted prefixes) so the test is platform-independent.
            trusted = [{"path": "/srv/archive", "mode": "r"}, {"path": "/opt/data", "mode": "rw"}]
            trusted_path = os.path.join(tmpdir, "trusted_dirs.json")
            with open(trusted_path, "w") as f:
                json.dump(trusted, f)

            builder = NsjailConfigBuilder(
                project_dir="/home/user/project",
                session_tmpdir="/tmp/session",
                trusted_dirs_path=os.path.join(tmpdir, "trusted_dirs.json"),
            )
            # Mock os.path.exists to return True for trusted dir paths
            with patch("os.path.exists", return_value=True):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "/srv/archive" in content
                assert "/opt/data" in content
            finally:
                os.unlink(cfg_path)

    def test_blocked_paths_rejected_from_trusted_mounts(self) -> None:
        """Sensitive paths in trusted_dirs.json are rejected by the blocklist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Paths that should be blocked on all platforms
            home = os.path.expanduser("~")
            blocked_paths = [
                "/etc/something",
                "/var/log",
                "/run/socket",
                os.path.join(home, ".ssh"),
                os.path.join(home, ".local", "share"),
            ]
            trusted = [{"path": p, "mode": "rw"} for p in blocked_paths]
            trusted_path = os.path.join(tmpdir, "trusted_dirs.json")
            with open(trusted_path, "w") as f:
                json.dump(trusted, f)

            builder = NsjailConfigBuilder(
                project_dir="/home/user/project",
                session_tmpdir="/tmp/session",
                trusted_dirs_path=os.path.join(tmpdir, "trusted_dirs.json"),
            )
            # Mock os.path.exists and os.path.realpath to simulate Linux behavior
            # (realpath returns input unchanged, not macOS /System/Volumes/Data/...)
            with patch("os.path.exists", return_value=True), \
                 patch("os.path.realpath", side_effect=lambda p: p), \
                 patch("os.path.islink", return_value=False):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                # None of the blocked paths should appear as mount entries
                for p in blocked_paths:
                    assert p not in content, f"Blocked path {p!r} should not appear in config"
            finally:
                os.unlink(cfg_path)

    def test_missing_trusted_dirs_file_is_graceful(self) -> None:
        """Missing trusted_dirs.json does not cause an error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            builder = NsjailConfigBuilder(
                project_dir="/home/user/project",
                session_tmpdir="/tmp/session",
                trusted_dirs_path=os.path.join(tmpdir, "trusted_dirs.json"),
            )
            # Should not raise
            cfg_path, _ = builder.build("ls", timeout=30)
            os.unlink(cfg_path)

    def test_rlimits_fallback_when_no_cgroup(self) -> None:
        """When cgroup delegation is unavailable, rlimits are used."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
            memory_mb=512,
        )
        # Force cgroup unavailable
        builder._cgroup_info = {"available": False, "cgroupv2_mount": None}
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "rlimit_as:" in content
            assert "rlimit_nproc:" in content
            assert "rlimit_fsize:" in content
            assert "rlimit_nofile:" in content
            assert "cgroup_mem_max" not in content
        finally:
            os.unlink(cfg_path)

    def test_cgroup_limits_when_delegation_available(self) -> None:
        """When cgroup delegation is available, cgroup limits are used."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
            memory_mb=256,
            pids_max=64,
            cpu_percent=50,
        )
        # Force cgroup available
        builder._cgroup_info = {
            "available": True,
            "cgroupv2_mount": "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service",
        }
        with patch("nsjail_config.os.path.isdir", return_value=True):
            cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "cgroup_mem_max: 268435456" in content  # 256 MB in bytes
            assert "cgroup_pids_max: 64" in content
            assert "cgroup_cpu_ms_per_sec: 500" in content  # 50% * 10
            assert "use_cgroupv2: true" in content
            assert "rlimit_as" not in content
        finally:
            os.unlink(cfg_path)

    def test_systemd_run_wrapper_when_cgroup_available(self) -> None:
        """When cgroup delegation is available, command is wrapped in systemd-run."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        builder._cgroup_info = {
            "available": True,
            "cgroupv2_mount": "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service",
        }
        with patch("nsjail_config.os.path.isdir", return_value=True):
            cfg_path, nsjail_cmd = builder.build("ls", timeout=30)
        try:
            assert nsjail_cmd[0] == "systemd-run"
            assert "--user" in nsjail_cmd
            assert "--scope" in nsjail_cmd
            assert "--property=Delegate=yes" in nsjail_cmd
        finally:
            os.unlink(cfg_path)

    def test_no_systemd_run_wrapper_when_cgroup_unavailable(self) -> None:
        """When cgroup delegation is unavailable, command is raw nsjail."""
        builder = NsjailConfigBuilder(
            project_dir="/home/user/project",
            session_tmpdir="/tmp/session",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        builder._cgroup_info = {"available": False, "cgroupv2_mount": None}
        cfg_path, nsjail_cmd = builder.build("ls", timeout=30)
        try:
            assert nsjail_cmd[0] == "nsjail"
        finally:
            os.unlink(cfg_path)