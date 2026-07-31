"""Tests for NsjailConfigBuilder — system mount detection and config generation.

These tests mock filesystem calls to verify config generation logic without
requiring a real Linux host or nsjail binary.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import mock_open, patch

from nsjail_config import NsjailConfigBuilder


class TestDetectSystemMounts:
    """Tests for _detect_system_mounts() — symlink vs real dir detection."""

    def _make_builder(self) -> NsjailConfigBuilder:
        return NsjailConfigBuilder(
            session_tmpdir="/tmp/test-session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/test-data/trusted_dirs.json",
            agent_dir="/tmp/test-agent",
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
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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
        """Config contains the cwd set to /tmp."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert 'cwd: "/tmp"' in content
        finally:
            os.unlink(cfg_path)

    def test_config_has_no_project_mount(self) -> None:
        """Config does not contain a bind mount for the project directory."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert '/home/user/project' not in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_tmp_mount(self) -> None:
        """Config contains a RW bind mount for the session tmpdir as /tmp."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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

    def test_config_contains_tmp_dir_mount_after_scratch_mount(self) -> None:
        """tmp_dir is bind-mounted RW at its real path, immediately after the /tmp scratch mount."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            scratch_idx = content.index('dst: "/tmp"')
            tmp_dir_idx = content.index(json.dumps(builder.tmp_dir))
            assert scratch_idx < tmp_dir_idx
            assert (
                f'mount: {{ src: {json.dumps(builder.tmp_dir)} '
                f'dst: {json.dumps(builder.tmp_dir)} '
                f'is_bind: true rw: true mandatory: true }}'
            ) in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_dev_null_and_dev_zero_mounts(self) -> None:
        """Config contains bind mounts for /dev/null and /dev/zero (quoted paths)."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("echo test", timeout=10)
        try:
            with open(cfg_path) as f:
                content = f.read()
            # Paths must be quoted — nsjail's config parser rejects bare paths.
            assert 'src: "/dev/null"' in content
            assert 'dst: "/dev/null"' in content
            assert 'src: "/dev/zero"' in content
            assert 'dst: "/dev/zero"' in content
            assert "is_bind: true" in content
            assert "rw: false" in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_base_envars(self) -> None:
        """Config contains base envar entries for PATH, HOME, LANG, TERM."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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

    def test_config_contains_tmpdir_tmp_temp_envars_set_to_scratch_tmp(self) -> None:
        """TMPDIR/TMP/TEMP are injected as base envars pointing at /tmp (scratch), not tmp_dir."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert 'envar: "TMPDIR=/tmp"' in content
            assert 'envar: "TMP=/tmp"' in content
            assert 'envar: "TEMP=/tmp"' in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_keep_env_false(self) -> None:
        """Config sets keep_env: false for environment isolation."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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
        """Existing skills_dir appears as a RO bind mount."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = os.path.join(tmpdir, "skills")
            os.makedirs(skills_dir)
            builder = NsjailConfigBuilder(
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
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

    def test_config_skills_dir_mounted_under_home(self) -> None:
        """skills_dir under /home is accepted and mounted read-only."""
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.realpath", side_effect=lambda p: p), \
             patch("os.path.abspath", side_effect=lambda p: p):
            builder = NsjailConfigBuilder(
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                skills_dir="/home/user/.agents/skills",
            )
            cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "# Skills directory mount (read-only)" in content
            assert '/home/user/.agents/skills' in content
            assert "rw: false" in content
        finally:
            os.unlink(cfg_path)

    def test_config_skills_dir_accepted_on_blocked_user_prefix(self) -> None:
        """skills_dir on a blocked user prefix (e.g. ~/.local/share/agent/skills) is accepted because the mount is read-only and the user-prefix blocklist only applies to RW trusted-dir mounts."""
        home = os.path.expanduser("~")
        skills_dir = os.path.join(home, ".local", "share", "agent", "skills")
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.realpath", side_effect=lambda p: p), \
             patch("os.path.abspath", side_effect=lambda p: p):
            builder = NsjailConfigBuilder(
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                skills_dir=skills_dir,
            )
            cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "# Skills directory mount (read-only)" in content
            assert skills_dir in content
            assert "rw: false" in content
        finally:
            os.unlink(cfg_path)

    def test_config_skills_dir_skipped_when_missing(self) -> None:
        """A non-existent skills_dir path does not appear in the config."""
        missing_dir = "/tmp/nonexistent-skills-dir-for-test"
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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

    def test_config_skills_dir_rejected_when_blocked_system_path(self) -> None:
        """A skills_dir under a blocked system prefix is not mounted."""
        with patch("os.path.isdir", return_value=True):
            builder = NsjailConfigBuilder(
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
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

    def test_command_list_contains_nsjail_and_config(self) -> None:
        """Returned command list starts with nsjail --config."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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

    def test_trusted_dir_under_session_tmpdir_skipped(self) -> None:
        """A trusted dir under session_tmpdir is skipped because it is already /tmp."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_tmpdir = os.path.join(tmpdir, "test-session-tmp")
            os.makedirs(session_tmpdir)
            subdir = os.path.join(session_tmpdir, "subdir")
            trusted = [{"path": subdir, "mode": "rw"}]
            trusted_path = os.path.join(tmpdir, "trusted_dirs.json")
            with open(trusted_path, "w") as f:
                json.dump(trusted, f)

            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path=trusted_path,
            )
            with patch("os.path.exists", return_value=True), \
                 patch("os.path.realpath", side_effect=lambda p: p), \
                 patch("os.path.islink", return_value=False):
                cfg_path, _ = builder.build("echo test", timeout=10)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert subdir not in content
                assert f'src: {json.dumps(builder.session_tmpdir)}' in content
                assert 'dst: "/tmp"' in content
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
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
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

    def test_trusted_dir_under_home_accepted(self) -> None:
        """A trusted dir under /home is accepted and appears as a mount entry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            trusted = [{"path": "/home/user/projects/myproject", "mode": "rw"}]
            trusted_path = os.path.join(tmpdir, "trusted_dirs.json")
            with open(trusted_path, "w") as f:
                json.dump(trusted, f)

            builder = NsjailConfigBuilder(
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path=os.path.join(tmpdir, "trusted_dirs.json"),
            )
            with patch("os.path.exists", return_value=True), \
                 patch("os.path.realpath", side_effect=lambda p: p), \
                 patch("os.path.islink", return_value=False):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "/home/user/projects/myproject" in content
            finally:
                os.unlink(cfg_path)

    def test_trusted_dir_gnupg_rejected(self) -> None:
        """~/.gnupg is rejected by _blocked_user_prefixes when used as a trusted dir."""
        home = os.path.expanduser("~")
        gnupg_dir = os.path.join(home, ".gnupg")
        with tempfile.TemporaryDirectory() as tmpdir:
            trusted = [{"path": gnupg_dir, "mode": "rw"}]
            trusted_path = os.path.join(tmpdir, "trusted_dirs.json")
            with open(trusted_path, "w") as f:
                json.dump(trusted, f)

            builder = NsjailConfigBuilder(
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path=os.path.join(tmpdir, "trusted_dirs.json"),
                agent_dir="/tmp/test-agent",
            )
            with patch("os.path.exists", return_value=True), \
                 patch("os.path.realpath", side_effect=lambda p: p), \
                 patch("os.path.islink", return_value=False):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert gnupg_dir not in content
            finally:
                os.unlink(cfg_path)

    def test_trusted_dir_agent_dir_rejected(self) -> None:
        """The agent's own directory is rejected when used as a trusted dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = os.path.join(tmpdir, "agent-root")
            os.makedirs(agent_dir)
            trusted = [{"path": agent_dir, "mode": "rw"}]
            trusted_path = os.path.join(tmpdir, "trusted_dirs.json")
            with open(trusted_path, "w") as f:
                json.dump(trusted, f)

            builder = NsjailConfigBuilder(
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path=os.path.join(tmpdir, "trusted_dirs.json"),
                agent_dir=agent_dir,
            )
            with patch("os.path.exists", return_value=True), \
                 patch("os.path.realpath", side_effect=lambda p: p), \
                 patch("os.path.islink", return_value=False):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert agent_dir not in content
            finally:
                os.unlink(cfg_path)

    def test_trusted_dir_aws_rejected(self) -> None:
        """~/.aws is rejected by _blocked_user_prefixes when used as a trusted dir."""
        home = os.path.expanduser("~")
        aws_dir = os.path.join(home, ".aws")
        with tempfile.TemporaryDirectory() as tmpdir:
            trusted = [{"path": aws_dir, "mode": "rw"}]
            trusted_path = os.path.join(tmpdir, "trusted_dirs.json")
            with open(trusted_path, "w") as f:
                json.dump(trusted, f)

            builder = NsjailConfigBuilder(
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path=os.path.join(tmpdir, "trusted_dirs.json"),
                agent_dir="/tmp/test-agent",
            )
            with patch("os.path.exists", return_value=True), \
                 patch("os.path.realpath", side_effect=lambda p: p), \
                 patch("os.path.islink", return_value=False):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert aws_dir not in content
            finally:
                os.unlink(cfg_path)

    def test_trusted_dir_kube_rejected(self) -> None:
        """~/.kube is rejected by _blocked_user_prefixes when used as a trusted dir."""
        home = os.path.expanduser("~")
        kube_dir = os.path.join(home, ".kube")
        with tempfile.TemporaryDirectory() as tmpdir:
            trusted = [{"path": kube_dir, "mode": "rw"}]
            trusted_path = os.path.join(tmpdir, "trusted_dirs.json")
            with open(trusted_path, "w") as f:
                json.dump(trusted, f)

            builder = NsjailConfigBuilder(
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path=os.path.join(tmpdir, "trusted_dirs.json"),
                agent_dir="/tmp/test-agent",
            )
            with patch("os.path.exists", return_value=True), \
                 patch("os.path.realpath", side_effect=lambda p: p), \
                 patch("os.path.islink", return_value=False):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert kube_dir not in content
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
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
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
                session_tmpdir="/tmp/session",
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path=os.path.join(tmpdir, "trusted_dirs.json"),
            )
            # Should not raise
            cfg_path, _ = builder.build("ls", timeout=30)
            os.unlink(cfg_path)

    def test_rlimits_fallback_when_no_cgroup(self) -> None:
        """When cgroup delegation is unavailable, rlimits are used."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
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
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        builder._cgroup_info = {"available": False, "cgroupv2_mount": None}
        cfg_path, nsjail_cmd = builder.build("ls", timeout=30)
        try:
            assert nsjail_cmd[0] == "nsjail"
        finally:
            os.unlink(cfg_path)


class TestCgroup2Detection:
    """Tests for _is_cgroup2_mounted() — statfs and /proc/filesystems fallback."""

    def test_statfs_detects_cgroup2(self) -> None:
        """When os.statfs is available and f_type matches, returns True."""
        from nsjail_config import CGROUP2_SUPER_MAGIC

        class FakeStatResult:
            f_type = CGROUP2_SUPER_MAGIC

        with patch("nsjail_config.os.statfs", return_value=FakeStatResult(), create=True):
            assert NsjailConfigBuilder._is_cgroup2_mounted() is True

    def test_statfs_rejects_non_cgroup2(self) -> None:
        """When os.statfs returns a different magic number, returns False."""
        class FakeStatResult:
            f_type = 0x73717368  # squashfs magic, not cgroup2

        with patch("nsjail_config.os.statfs", return_value=FakeStatResult(), create=True):
            assert NsjailConfigBuilder._is_cgroup2_mounted() is False

    def test_statfs_oserror_returns_false(self) -> None:
        """When os.statfs raises OSError, returns False."""
        with patch("nsjail_config.os.statfs", side_effect=OSError("nope"), create=True):
            assert NsjailConfigBuilder._is_cgroup2_mounted() is False

    def test_proc_filesystems_fallback_detects_cgroup2(self) -> None:
        """When os.statfs is missing, /proc/filesystems fallback detects cgroup2."""
        proc_content = (
            "nodev\tdevtmpfs\n"
            "nodev\tproc\n"
            "nodev\tcgroup2\n"
            "nodev\ttmpfs\n"
        )
        # Remove os.statfs to simulate a Python build without it
        with patch("nsjail_config.os.statfs", None, create=True), \
             patch("builtins.open", mock_open(read_data=proc_content)):
            assert NsjailConfigBuilder._is_cgroup2_mounted() is True

    def test_proc_filesystems_fallback_rejects_no_cgroup2(self) -> None:
        """When /proc/filesystems has no cgroup2 entry, returns False."""
        proc_content = "nodev\tdevtmpfs\nnodev\tproc\nnodev\ttmpfs\n"
        with patch("nsjail_config.os.statfs", None, create=True), \
             patch("builtins.open", mock_open(read_data=proc_content)):
            assert NsjailConfigBuilder._is_cgroup2_mounted() is False

    def test_proc_filesystems_fallback_oserror_returns_false(self) -> None:
        """When /proc/filesystems can't be read, returns False."""
        with patch("nsjail_config.os.statfs", None, create=True), \
             patch("builtins.open", side_effect=OSError("nope")):
            assert NsjailConfigBuilder._is_cgroup2_mounted() is False


class TestSessionLogsMount:
    """Tests for session_logs_dir kwarg in build()."""

    def test_session_logs_dir_mounts_when_directory_exists(self) -> None:
        """A real session_logs_dir produces a read-only bind mount with src==dst."""
        with tempfile.TemporaryDirectory() as session_tmpdir, \
             tempfile.TemporaryDirectory() as session_logs_dir:
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
            )
            cfg_path, _ = builder.build(
                "ls", timeout=30, session_logs_dir=session_logs_dir,
            )
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "# Session logs" in content
                assert (
                    f'mount: {{ src: {json.dumps(session_logs_dir)} '
                    f'dst: {json.dumps(session_logs_dir)}'
                ) in content
                assert "is_bind: true" in content
                assert "rw: false" in content
                assert "mandatory: false" in content
            finally:
                os.unlink(cfg_path)

    def test_session_logs_dir_empty_produces_no_mount(self) -> None:
        """Default empty session_logs_dir does not produce a session logs mount."""
        with tempfile.TemporaryDirectory() as session_tmpdir:
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
            )
            cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "# Session logs" not in content
            finally:
                os.unlink(cfg_path)


class TestCaCertDetection:
    """Tests for CA certificate detection and env var injection."""

    def test_allow_net_true_injects_mount_and_envars(self) -> None:
        """allow_net=True with detected CA certs adds mount + SSL_CERT_* envars."""
        with tempfile.TemporaryDirectory() as session_tmpdir, \
             tempfile.TemporaryDirectory() as capath, \
             tempfile.NamedTemporaryFile(suffix=".crt") as cafile_fh:
            cafile = cafile_fh.name
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                allow_net=True,
            )
            with patch.object(
                builder, "_detect_ca_certs", return_value=(cafile, capath),
            ):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "# TLS cert env vars (allow_net=true)" in content
                assert f'envar: "SSL_CERT_FILE={cafile}"' in content
                assert f'envar: "SSL_CERT_DIR={capath}"' in content
                assert "# CA certificate store (read-only, allow_net=true)" in content
                assert f'src: {json.dumps(capath)}' in content
                assert f'dst: {json.dumps(capath)}' in content
                assert "rw: false" in content
            finally:
                os.unlink(cfg_path)

    def test_allow_net_false_skips_ca_certs(self) -> None:
        """allow_net=False does not inject SSL_CERT_* envars or CA cert mount."""
        with tempfile.TemporaryDirectory() as session_tmpdir:
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                allow_net=False,
            )
            cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "SSL_CERT_FILE" not in content
                assert "SSL_CERT_DIR" not in content
                assert "# CA certificate store" not in content
            finally:
                os.unlink(cfg_path)

    def test_allow_net_true_no_ca_certs_graceful(self) -> None:
        """allow_net=True with no detected CA certs still generates valid config."""
        with tempfile.TemporaryDirectory() as session_tmpdir:
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                allow_net=True,
            )
            with patch.object(
                builder, "_detect_ca_certs", return_value=(None, None),
            ):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "SSL_CERT_FILE" not in content
                assert "SSL_CERT_DIR" not in content
                assert "# CA certificate store" not in content
                assert "time_limit: 30" in content
            finally:
                os.unlink(cfg_path)

    def test_detect_ca_certs_debian(self) -> None:
        """Debian/Ubuntu layout returns ca-certificates.crt + certs dir."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )

        def mock_isdir(path: str) -> bool:
            return path in {"/etc/ssl/certs"}

        def mock_isfile(path: str) -> bool:
            return path == "/etc/ssl/certs/ca-certificates.crt"

        with patch("os.path.isdir", side_effect=mock_isdir), \
             patch("os.path.isfile", side_effect=mock_isfile):
            cafile, capath = builder._detect_ca_certs()
        assert cafile == "/etc/ssl/certs/ca-certificates.crt"
        assert capath == "/etc/ssl/certs"

    def test_detect_ca_certs_alpine(self) -> None:
        """Alpine layout returns cert.pem file with no capath."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )

        def mock_isfile(path: str) -> bool:
            return path == "/etc/ssl/cert.pem"

        with patch("os.path.isdir", return_value=False), \
             patch("os.path.isfile", side_effect=mock_isfile):
            cafile, capath = builder._detect_ca_certs()
        assert cafile == "/etc/ssl/cert.pem"
        assert capath is None

    def test_detect_ca_certs_fedora(self) -> None:
        """Fedora/RHEL layout returns ca-bundle.crt + certs dir."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )

        def mock_isdir(path: str) -> bool:
            return path in {"/etc/pki/tls/certs"}

        def mock_isfile(path: str) -> bool:
            return path == "/etc/pki/tls/certs/ca-bundle.crt"

        with patch("os.path.isdir", side_effect=mock_isdir), \
             patch("os.path.isfile", side_effect=mock_isfile):
            cafile, capath = builder._detect_ca_certs()
        assert cafile == "/etc/pki/tls/certs/ca-bundle.crt"
        assert capath == "/etc/pki/tls/certs"

    def test_detect_ca_certs_none_when_missing(self) -> None:
        """When no known CA layout exists, returns (None, None)."""
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            trusted_dirs_path="/tmp/data/trusted_dirs.json",
        )
        with patch("os.path.isdir", return_value=False), \
             patch("os.path.isfile", return_value=False):
            cafile, capath = builder._detect_ca_certs()
        assert cafile is None
        assert capath is None


class TestDnsResolvConf:
    """Tests for /etc/resolv.conf injection when allow_net is true."""

    def test_allow_net_true_injects_resolv_conf(self) -> None:
        """allow_net=True injects a src_content mount for /etc/resolv.conf."""
        with tempfile.TemporaryDirectory() as session_tmpdir:
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                allow_net=True,
            )
            with patch.object(builder, "_detect_ca_certs", return_value=(None, None)):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "# DNS resolution (allow_net=true)" in content
                assert 'dst: "/etc/resolv.conf"' in content
                assert "src_content:" in content
                assert "nameserver 8.8.8.8" in content
            finally:
                os.unlink(cfg_path)

    def test_allow_net_false_skips_resolv_conf(self) -> None:
        """allow_net=False does not inject a resolv.conf mount."""
        with tempfile.TemporaryDirectory() as session_tmpdir:
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                allow_net=False,
            )
            cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "/etc/resolv.conf" not in content
                assert "src_content:" not in content
            finally:
                os.unlink(cfg_path)

    def test_custom_dns_nameserver_used(self) -> None:
        """A custom dns_nameserver is written into the resolv.conf content."""
        with tempfile.TemporaryDirectory() as session_tmpdir:
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                allow_net=True,
                dns_nameserver="1.1.1.1",
            )
            with patch.object(builder, "_detect_ca_certs", return_value=(None, None)):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                assert "nameserver 1.1.1.1" in content
                assert "nameserver 8.8.8.8" not in content
            finally:
                os.unlink(cfg_path)

    def test_resolv_conf_src_content_has_real_newline(self) -> None:
        """src_content value contains a real newline, not a literal backslash-n."""
        with tempfile.TemporaryDirectory() as session_tmpdir:
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                allow_net=True,
            )
            with patch.object(builder, "_detect_ca_certs", return_value=(None, None)):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                # json.dumps("nameserver 8.8.8.8\n") => "nameserver 8.8.8.8\n"
                # The \n is the two-character JSON escape (backslash + n),
                # which nsjail's protobuf text parser interprets as a real
                # newline. A double-escaped \\n (literal backslash + n) would
                # be a bug.
                assert '"nameserver 8.8.8.8\\n"' in content, (
                    "src_content must contain the JSON newline escape (\\n), not a literal backslash-n (\\\\n)"
                )
                assert '"nameserver 8.8.8.8\\\\n"' not in content
            finally:
                os.unlink(cfg_path)

    def test_dns_and_ca_certs_both_present_when_allow_net(self) -> None:
        """allow_net=True with detected CA certs produces both DNS and CA mounts."""
        with tempfile.TemporaryDirectory() as session_tmpdir, \
             tempfile.TemporaryDirectory() as capath, \
             tempfile.NamedTemporaryFile(suffix=".crt") as cafile_fh:
            cafile = cafile_fh.name
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                allow_net=True,
            )
            with patch.object(
                builder, "_detect_ca_certs", return_value=(cafile, capath),
            ):
                cfg_path, _ = builder.build("ls", timeout=30)
            try:
                with open(cfg_path) as f:
                    content = f.read()
                # DNS mount
                assert "# DNS resolution (allow_net=true)" in content
                assert 'dst: "/etc/resolv.conf"' in content
                # CA cert env vars + mount
                assert f'envar: "SSL_CERT_FILE={cafile}"' in content
                assert f'envar: "SSL_CERT_DIR={capath}"' in content
                assert "# CA certificate store (read-only, allow_net=true)" in content
            finally:
                os.unlink(cfg_path)

    def test_invalid_dns_nameserver_falls_back_to_default(self) -> None:
        """An invalid dns_nameserver falls back to 8.8.8.8 with a warning."""
        with tempfile.TemporaryDirectory() as session_tmpdir:
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                allow_net=True,
                dns_nameserver="not-an-ip",
            )
            assert builder.dns_nameserver == "8.8.8.8"

    def test_empty_dns_nameserver_falls_back_to_default(self) -> None:
        """An empty dns_nameserver falls back to 8.8.8.8."""
        with tempfile.TemporaryDirectory() as session_tmpdir:
            builder = NsjailConfigBuilder(
                session_tmpdir=session_tmpdir,
                tmp_dir="/tmp/test-tmpdir",
                trusted_dirs_path="/tmp/data/trusted_dirs.json",
                allow_net=True,
                dns_nameserver="",
            )
            assert builder.dns_nameserver == "8.8.8.8"
