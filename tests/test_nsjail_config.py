"""Tests for NsjailConfigBuilder — system mount detection and config generation.

These tests mock filesystem calls to verify config generation logic without
requiring a real Linux host or nsjail binary.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
from unittest.mock import MagicMock, mock_open, patch

from nsjail_config import NsjailConfigBuilder
from path_policy import PathPolicy


def _make_policy(
    tmp_path: str,
    *,
    allowed_dirs: list[str] | None = None,
    prohibited_dirs: list[str] | None = None,
    logger: logging.Logger | None = None,
    agent_name: str = "test-agent",
) -> PathPolicy:
    """Create a PathPolicy using temp paths and monkeypatched HOME/XDG env."""
    workspace = os.path.join(tmp_path, "workspace")
    downloads = os.path.join(tmp_path, "downloads")
    data_home = os.path.join(tmp_path, "xdg", "data", agent_name)
    state_home = os.path.join(tmp_path, "xdg", "state", agent_name)
    config_home = os.path.join(tmp_path, "xdg", "config", agent_name)
    vault = os.path.join(tmp_path, "vault.toml")
    config = os.path.join(config_home, "config.toml")
    skills = os.path.join(tmp_path, "skills")
    results = os.path.join(tmp_path, "results")

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
        logger=logger,
    )


class TestDetectSystemMounts:
    """Tests for _detect_system_mounts() — symlink vs real dir detection."""

    def _make_builder(self, tmp_path: str) -> NsjailConfigBuilder:
        policy = _make_policy(str(tmp_path))
        return NsjailConfigBuilder(
            session_tmpdir="/tmp/test-session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )

    def test_usr_always_mounted_first_and_mandatory(self, tmp_path: str) -> None:
        """/usr is always mounted read-only with mandatory: true."""
        builder = self._make_builder(tmp_path)
        with patch("os.path.exists", return_value=False):
            mounts = builder._detect_system_mounts()
        # /usr should be the first line
        assert 'src: "/usr"' in mounts[0]
        assert "mandatory: true" in mounts[0]
        assert "rw: false" in mounts[0]

    def test_symlinked_dirs_use_mandatory_false(self, tmp_path: str) -> None:
        """Symlinked system dirs (e.g. /bin → usr/bin) use mandatory: false."""
        builder = self._make_builder(tmp_path)

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

    def test_real_dirs_use_mandatory_true(self, tmp_path: str) -> None:
        """Real (non-symlink) system dirs use mandatory: true."""
        builder = self._make_builder(tmp_path)

        def mock_exists(path: str) -> bool:
            return path in {"/usr", "/bin", "/sbin"}

        with patch("os.path.exists", side_effect=mock_exists), \
             patch("os.path.islink", return_value=False):
            mounts = builder._detect_system_mounts()
        assert len(mounts) == 3
        for line in mounts:
            assert "mandatory: true" in line

    def test_absent_dirs_are_skipped(self, tmp_path: str) -> None:
        """Non-existent system dirs are skipped entirely."""
        builder = self._make_builder(tmp_path)

        def mock_exists(path: str) -> bool:
            return path == "/usr"

        with patch("os.path.exists", side_effect=mock_exists):
            mounts = builder._detect_system_mounts()
        # Only /usr
        assert len(mounts) == 1
        assert 'src: "/usr"' in mounts[0]


class TestBuild:
    """Tests for build() — full config generation."""

    def test_config_contains_time_limit(self, tmp_path: str) -> None:
        """Config contains the correct time_limit from the timeout parameter."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("make test", timeout=60)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "time_limit: 60" in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_cwd(self, tmp_path: str) -> None:
        """Config contains the cwd set to /tmp."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert 'cwd: "/tmp"' in content
        finally:
            os.unlink(cfg_path)

    def test_config_has_no_project_mount(self, tmp_path: str) -> None:
        """Config does not contain a bind mount for the project directory."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert '/home/user/project' not in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_tmp_mount(self, tmp_path: str) -> None:
        """Config contains a RW bind mount for the session tmpdir as /tmp."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert f'src: {json.dumps(builder.session_tmpdir)}' in content
            assert 'dst: "/tmp"' in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_tmp_dir_mount_after_scratch_mount(self, tmp_path: str) -> None:
        """tmp_dir is bind-mounted RW at its real path, immediately after the /tmp scratch mount."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir=f"/tmp/{policy.agent_name}",
            path_policy=policy,
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

    def test_config_contains_dev_null_and_dev_zero_mounts(self, tmp_path: str) -> None:
        """Config contains bind mounts for /dev/null and /dev/zero (quoted paths)."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_config_contains_base_envars(self, tmp_path: str) -> None:
        """Config contains base envar entries for PATH, HOME, LANG, TERM."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_config_contains_tmpdir_tmp_temp_envars_set_to_scratch_tmp(self, tmp_path: str) -> None:
        """TMPDIR/TMP/TEMP are injected as base envars pointing at /tmp (scratch), not tmp_dir."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_config_contains_keep_env_false(self, tmp_path: str) -> None:
        """Config sets keep_env: false for environment isolation."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "keep_env: false" in content
        finally:
            os.unlink(cfg_path)

    def test_config_contains_namespaces(self, tmp_path: str) -> None:
        """Config contains all required namespace clone directives."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_config_contains_command(self, tmp_path: str) -> None:
        """Config contains the command as the exec target."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("make test", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert 'path: "/bin/sh"' in content
            assert "make test" in content
        finally:
            os.unlink(cfg_path)

    def test_config_allow_net_false_creates_net_namespace(self, tmp_path: str) -> None:
        """allow_net=False sets clone_newnet: true (network isolated)."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
            allow_net=False,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "clone_newnet: true" in content
        finally:
            os.unlink(cfg_path)

    def test_config_allow_net_true_shares_net_namespace(self, tmp_path: str) -> None:
        """allow_net=True sets clone_newnet: false (host network)."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
            allow_net=True,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "clone_newnet: false" in content
        finally:
            os.unlink(cfg_path)

    def test_tier1_mounts_present(self, tmp_path: str) -> None:
        """Tier 1 directories from PathPolicy appear as bind mounts."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir=policy.tmp_dir,
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert f'src: {json.dumps(policy.workspace_dir)}' in content
            assert f'src: {json.dumps(policy.downloads_dir)}' in content
            assert f'src: {json.dumps(policy.results_dir)}' in content
            assert f'src: {json.dumps(policy.skills_dir)}' in content
        finally:
            os.unlink(cfg_path)

    def test_every_tier1_entry_present_or_subsumed_by_session_mount(
        self, tmp_path: str
    ) -> None:
        """Each Tier-1 entry is either emitted as a bind mount or, when it equals
        tmp_dir, is covered by the dedicated tmp_dir system mount."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir=policy.tmp_dir,
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            for path, mode in policy.tier1_entries():
                if path == builder.tmp_dir:
                    assert (
                        f"mount: {{ src: {json.dumps(builder.tmp_dir)} "
                        f"dst: {json.dumps(builder.tmp_dir)} "
                        f"is_bind: true rw: true mandatory: true }}"
                    ) in content
                    continue
                if not os.path.isdir(path):
                    continue
                rw = "true" if mode == "rw" else "false"
                assert (
                    f"mount: {{ src: {json.dumps(path)} "
                    f"dst: {json.dumps(path)} "
                    f"is_bind: true rw: {rw} mandatory: true }}"
                ) in content, f"missing Tier-1 mount for {path!r} mode={mode}"
        finally:
            os.unlink(cfg_path)


    def test_results_mount_is_rw(self, tmp_path: str) -> None:
        """results_dir is mounted read-write."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            results_escaped = json.dumps(policy.results_dir).replace("\\", "\\\\")
            assert f"src: {results_escaped} dst: {results_escaped} is_bind: true rw: true" in content
        finally:
            os.unlink(cfg_path)

    def test_skills_mount_is_ro(self, tmp_path: str) -> None:
        """skills_dir is mounted read-only."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            skills_escaped = json.dumps(policy.skills_dir).replace("\\", "\\\\")
            assert f"src: {skills_escaped} dst: {skills_escaped} is_bind: true rw: false" in content
        finally:
            os.unlink(cfg_path)

    def test_tmp_dir_skipped_when_equal_to_skills_dir(self, tmp_path: str) -> None:
        """A read-only tier-1 entry exactly matching tmp_dir is not double-mounted."""
        shared_dir = os.path.join(str(tmp_path), "shared")
        os.makedirs(shared_dir, exist_ok=True)
        # Use the realpath of the shared dir as tmp_dir so macOS /tmp → /private/tmp
        # handling does not introduce a mismatch.
        shared_real = os.path.realpath(shared_dir)
        policy = PathPolicy.create(
            agent_name="test-agent",
            workspace_dir=os.path.join(str(tmp_path), "workspace"),
            downloads_dir=os.path.join(str(tmp_path), "downloads"),
            tmp_dir=shared_real,
            skills_dir=shared_real,
            results_dir=os.path.join(str(tmp_path), "results"),
            data_home=os.path.join(str(tmp_path), "xdg", "data", "test-agent"),
            state_home=os.path.join(str(tmp_path), "xdg", "state", "test-agent"),
            config_home=os.path.join(str(tmp_path), "xdg", "config", "test-agent"),
            vault_path=os.path.join(str(tmp_path), "vault.toml"),
            config_path=os.path.join(str(tmp_path), "xdg", "config", "test-agent", "config.toml"),
            allowed_dirs=None,
            prohibited_dirs=None,
        )
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir=shared_real,
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            escaped = json.dumps(shared_real)
            matches = content.count(f"src: {escaped} dst: {escaped}")
            # Exactly one bind mount for the shared path (the dedicated /tmp/<agent>
            # system mount covers it; the tier-1 read-only skills entry is skipped).
            assert matches == 1
        finally:
            os.unlink(cfg_path)

    def test_config_skills_dir_skipped_when_missing(self, tmp_path: str) -> None:
        """A non-existent skills_dir path does not appear in the config."""
        policy = _make_policy(str(tmp_path))
        missing_skills = os.path.join(tmp_path, "no-such-skills")
        # Replace the policy's skills entry with a non-existent path.
        policy = PathPolicy.create(
            agent_name=policy.agent_name,
            workspace_dir=policy.workspace_dir,
            downloads_dir=policy.downloads_dir,
            tmp_dir=policy.tmp_dir,
            skills_dir=missing_skills,
            results_dir=policy.results_dir,
            data_home=policy.data_home,
            state_home=policy.state_home,
            config_home=policy.config_home,
            vault_path=policy.vault_path,
            config_path=policy.config_path,
            logger=policy.logger,
        )
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert missing_skills not in content
        finally:
            os.unlink(cfg_path)

    def test_tier2_mounts_present(self, tmp_path: str) -> None:
        """Operator allowed_dirs appear as RW bind mounts."""
        projects = os.path.join(str(tmp_path), "projects")
        os.makedirs(projects, exist_ok=True)
        policy = _make_policy(str(tmp_path), allowed_dirs=[projects])
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert projects in content
            assert f"dst: {json.dumps(projects)} is_bind: true rw: true" in content
        finally:
            os.unlink(cfg_path)

    def test_tier2_conflict_skip_not_mounted(self, tmp_path: str, caplog) -> None:
        """An allowed dir that contains a prohibited path is absent from mounts."""
        caplog.set_level(logging.WARNING)
        work = os.path.join(str(tmp_path), "work")
        vaults = os.path.join(work, "vaults")
        os.makedirs(vaults, exist_ok=True)
        policy = _make_policy(str(tmp_path), allowed_dirs=[work], prohibited_dirs=[vaults])
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            work_real = os.path.realpath(work)
            assert f"src: {json.dumps(work_real)} dst: {json.dumps(work_real)}" not in content
            # Other Tier 1 entries are still present.
            assert f'src: {json.dumps(policy.workspace_dir)}' in content
            assert f'src: {json.dumps(policy.downloads_dir)}' in content
            assert f'src: {json.dumps(policy.results_dir)}' in content
        finally:
            os.unlink(cfg_path)

    def test_command_list_contains_nsjail_and_config(self, tmp_path: str) -> None:
        """Returned command list starts with nsjail --config."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, nsjail_cmd = builder.build("ls", timeout=30)
        try:
            assert nsjail_cmd[0] == "nsjail"
            assert nsjail_cmd[1] == "--config"
            assert nsjail_cmd[2] == cfg_path
        finally:
            os.unlink(cfg_path)

    def test_command_list_includes_env_flags(self, tmp_path: str) -> None:
        """Returned command list includes -E KEY=VALUE flags from shell_env."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_session_logs_mount_absent(self, tmp_path: str) -> None:
        """No session-logs mount is emitted regardless of caller kwarg."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("ls", timeout=30)
        try:
            with open(cfg_path) as f:
                content = f.read()
            assert "# Session logs" not in content
            assert 'dst: "/tmp/session/session_logs"' not in content
        finally:
            os.unlink(cfg_path)

    def test_rlimits_fallback_when_no_cgroup(self, tmp_path: str) -> None:
        """When cgroup delegation is unavailable, rlimits are used."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_cgroup_limits_when_delegation_available(self, tmp_path: str) -> None:
        """When cgroup delegation is available, cgroup limits are used."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_systemd_run_wrapper_when_cgroup_available(self, tmp_path: str) -> None:
        """When cgroup delegation is available, command is wrapped in systemd-run."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_no_systemd_run_wrapper_when_cgroup_unavailable(self, tmp_path: str) -> None:
        """When cgroup delegation is unavailable, command is raw nsjail."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        builder._cgroup_info = {"available": False, "cgroupv2_mount": None}
        cfg_path, nsjail_cmd = builder.build("ls", timeout=30)
        try:
            assert nsjail_cmd[0] == "nsjail"
        finally:
            os.unlink(cfg_path)


class TestStaticConfigCaching:
    """Tests for once-per-session config generation."""

    def test_static_config_cached_between_builds(self, tmp_path: str) -> None:
        """Two build() calls reuse the same static config file."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_a, _ = builder.build("echo a", timeout=10)
        cfg_b, _ = builder.build("echo b", timeout=10)
        try:
            assert builder._static_config_path is not None
            assert cfg_a != cfg_b
            with open(cfg_a) as fa, open(cfg_b) as fb:
                a_static, _, _ = fa.read().partition('exec_bin {')
                b_static, _, _ = fb.read().partition('exec_bin {')
            assert a_static == b_static
        finally:
            for p in (cfg_a, cfg_b):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_static_config_regenerated_when_deleted(self, tmp_path: str) -> None:
        """If the cached static config is deleted, build() regenerates it."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_a, _ = builder.build("echo a", timeout=10)
        try:
            static_a = builder._static_config_path
            assert static_a is not None
            os.unlink(static_a)
            cfg_b, _ = builder.build("echo b", timeout=10)
            assert builder._static_config_path is not None
            assert builder._static_config_path != static_a
        finally:
            for p in (cfg_a, cfg_b):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_per_call_tempfile_cleaned_up(self, tmp_path: str) -> None:
        """The per-call config tempfile is deleted after build returns."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        cfg_path, _ = builder.build("echo", timeout=10)
        assert os.path.exists(cfg_path)
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


class TestCaCertDetection:
    """Tests for CA certificate detection and env var injection."""

    def test_allow_net_true_injects_mount_and_envars(self, tmp_path: str) -> None:
        """allow_net=True with detected CA certs adds mount + SSL_CERT_* envars."""
        policy = _make_policy(str(tmp_path))
        with tempfile.TemporaryDirectory() as capath, \
             tempfile.NamedTemporaryFile(suffix=".crt") as cafile_fh:
            cafile = cafile_fh.name
            builder = NsjailConfigBuilder(
                session_tmpdir=str(tmp_path / "session"),
                tmp_dir="/tmp/test-tmpdir",
                path_policy=policy,
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

    def test_allow_net_false_skips_ca_certs(self, tmp_path: str) -> None:
        """allow_net=False does not inject SSL_CERT_* envars or CA cert mount."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_allow_net_true_no_ca_certs_graceful(self, tmp_path: str) -> None:
        """allow_net=True with no detected CA certs still generates valid config."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
            allow_net=True,
        )
        with patch.object(builder, "_detect_ca_certs", return_value=(None, None)):
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

    def test_detect_ca_certs_debian(self, tmp_path: str) -> None:
        """Debian/Ubuntu layout returns ca-certificates.crt + certs dir."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_detect_ca_certs_alpine(self, tmp_path: str) -> None:
        """Alpine layout returns cert.pem file with no capath."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )

        def mock_isfile(path: str) -> bool:
            return path == "/etc/ssl/cert.pem"

        with patch("os.path.isdir", return_value=False), \
             patch("os.path.isfile", side_effect=mock_isfile):
            cafile, capath = builder._detect_ca_certs()
        assert cafile == "/etc/ssl/cert.pem"
        assert capath is None

    def test_detect_ca_certs_fedora(self, tmp_path: str) -> None:
        """Fedora/RHEL layout returns ca-bundle.crt + certs dir."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_detect_ca_certs_none_when_missing(self, tmp_path: str) -> None:
        """When no known CA layout exists, returns (None, None)."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir="/tmp/session",
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
        )
        with patch("os.path.isdir", return_value=False), \
             patch("os.path.isfile", return_value=False):
            cafile, capath = builder._detect_ca_certs()
        assert cafile is None
        assert capath is None


class TestDnsResolvConf:
    """Tests for /etc/resolv.conf injection when allow_net is true."""

    def test_allow_net_true_injects_resolv_conf(self, tmp_path: str) -> None:
        """allow_net=True injects a src_content mount for /etc/resolv.conf."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_allow_net_false_skips_resolv_conf(self, tmp_path: str) -> None:
        """allow_net=False does not inject a resolv.conf mount."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_custom_dns_nameserver_used(self, tmp_path: str) -> None:
        """A custom dns_nameserver is written into the resolv.conf content."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_resolv_conf_src_content_has_real_newline(self, tmp_path: str) -> None:
        """src_content value contains a real newline, not a literal backslash-n."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
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

    def test_dns_and_ca_certs_both_present_when_allow_net(self, tmp_path: str) -> None:
        """allow_net=True with detected CA certs produces both DNS and CA mounts."""
        policy = _make_policy(str(tmp_path))
        with tempfile.TemporaryDirectory() as capath, \
             tempfile.NamedTemporaryFile(suffix=".crt") as cafile_fh:
            cafile = cafile_fh.name
            builder = NsjailConfigBuilder(
                session_tmpdir=str(tmp_path / "session"),
                tmp_dir="/tmp/test-tmpdir",
                path_policy=policy,
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

    def test_invalid_dns_nameserver_falls_back_to_default(self, tmp_path: str) -> None:
        """An invalid dns_nameserver falls back to 8.8.8.8 with a warning."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
            allow_net=True,
            dns_nameserver="not-an-ip",
        )
        assert builder.dns_nameserver == "8.8.8.8"

    def test_empty_dns_nameserver_falls_back_to_default(self, tmp_path: str) -> None:
        """An empty dns_nameserver falls back to 8.8.8.8."""
        policy = _make_policy(str(tmp_path))
        builder = NsjailConfigBuilder(
            session_tmpdir=str(tmp_path / "session"),
            tmp_dir="/tmp/test-tmpdir",
            path_policy=policy,
            allow_net=True,
            dns_nameserver="",
        )
        assert builder.dns_nameserver == "8.8.8.8"


class TestShellArtifactRelocation:
    """Tests for _finalize_shell_log artifact relocation to results_dir."""

    def _make_shell(self, results_dir: str, max_output: int = 50, vault_secrets=None):
        owner = MagicMock()
        owner._paths.results_dir = results_dir
        owner.max_output = max_output
        owner._vault_secrets = vault_secrets or []
        from builtin_tools.shell import ShellTools
        return ShellTools(owner)

    def _read_results_artifact(self, tmp_path: pathlib.Path, kept: str) -> str:
        """Find and read the relocated artifact file under results_dir."""
        results_dir = tmp_path / "results"
        candidates = list(results_dir.rglob("shell-*"))
        assert len(candidates) == 1, candidates
        return candidates[0].read_text()

    def test_artifact_relocated_to_results_trace_dir(self, tmp_path: str) -> None:
        """Oversized output is moved to results/<trace-id>/shell-<name>."""
        results_dir = str(tmp_path / "results")
        shell = self._make_shell(results_dir)
        session_log = tmp_path / "session_logs" / "conv" / "shell-test.log"
        session_log.parent.mkdir(parents=True)
        session_log.write_text("x" * 100)
        kept = shell._finalize_shell_log(
            None, str(session_log), 100, caller_tag="main r-deadbeef"
        )
        assert kept is not None
        assert kept.startswith(results_dir)
        assert "r-deadbeef" in kept
        assert "shell-" in os.path.basename(kept)
        assert not session_log.exists()
        assert os.path.exists(kept)

    def test_artifact_path_reported_in_result(self, tmp_path: str) -> None:
        """The relocated artifact path flows through full_log_path in the result."""
        results_dir = str(tmp_path / "results")
        shell = self._make_shell(results_dir)
        session_log = tmp_path / "session_logs" / "conv" / "shell-test.log"
        session_log.parent.mkdir(parents=True)
        session_log.write_text("x" * 100)
        kept = shell._finalize_shell_log(
            None, str(session_log), 100, caller_tag="main r-cafebabe"
        )
        assert kept is not None
        assert kept.startswith(results_dir)
        assert "r-cafebabe" in kept
        assert "file_read" not in kept

    def test_redaction_applied_to_relocated_artifact(self, tmp_path: str) -> None:
        """Vault secrets are redacted from the retained artifact."""
        results_dir = str(tmp_path / "results")
        shell = self._make_shell(results_dir, vault_secrets=["supersecret"])
        session_log = tmp_path / "session_logs" / "conv" / "shell-test.log"
        session_log.parent.mkdir(parents=True)
        session_log.write_text("prefix supersecret suffix\n")
        kept = shell._finalize_shell_log(None, str(session_log), 100)
        assert kept is not None
        text = self._read_results_artifact(tmp_path, kept)
        assert "supersecret" not in text
        assert "[REDACTED]" in text

    def test_results_dir_empty_fallback(self, tmp_path: str) -> None:
        """When results_dir is empty, the artifact stays in session_logs."""
        shell = self._make_shell(results_dir="", max_output=50)
        session_log = tmp_path / "session_logs" / "conv" / "shell-test.log"
        session_log.parent.mkdir(parents=True)
        session_log.write_text("x" * 100)
        kept = shell._finalize_shell_log(None, str(session_log), 100)
        assert kept == str(session_log)
        assert session_log.exists()

    def test_artifacts_persist_for_manual_cleanup(self, tmp_path: str) -> None:
        """Repeated finalize calls do not delete anything under results_dir."""
        results_dir = str(tmp_path / "results")
        shell = self._make_shell(results_dir)
        for i in range(3):
            session_log = tmp_path / "session_logs" / "conv" / f"shell-{i}.log"
            session_log.parent.mkdir(parents=True, exist_ok=True)
            session_log.write_text("x" * 100)
            shell._finalize_shell_log(None, str(session_log), 100, caller_tag="main")
        assert len(list((tmp_path / "results").rglob("shell-*"))) == 3

    def test_artifact_file_permissions_after_relocation(self, tmp_path: str) -> None:
        """Relocated artifact retains owner-only (0600) permissions."""
        if os.name == "nt":
            return
        import stat
        results_dir = str(tmp_path / "results")
        shell = self._make_shell(results_dir)
        session_log = tmp_path / "session_logs" / "conv" / "shell-test.log"
        session_log.parent.mkdir(parents=True)
        # Open with _open_shell_log to ensure the file is created with 0600.
        fh, log_path = shell._open_shell_log()
        assert log_path is not None
        try:
            fh.write("x" * 100)
        finally:
            fh.close()
        kept = shell._finalize_shell_log(None, log_path, 100)
        assert kept is not None
        mode = stat.S_IMODE(os.stat(kept).st_mode)
        assert mode == 0o600
