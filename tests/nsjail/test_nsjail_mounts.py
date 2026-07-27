"""Integration tests for nsjail mount behavior.

Tests run inside a Lima VM and are skipped when Lima is not installed.
"""

from __future__ import annotations

import uuid

from tests.nsjail.conftest import NsjailVM


def _write_config(vm: NsjailVM, path: str, config: str) -> None:
    """Write an nsjail config to a file inside the VM."""
    vm.run(f"cat > {path} <<'EOF'\n{config}\nEOF")


def _base_config(name: str, command: str, extra_mounts: str = "") -> str:
    """Return a minimal nsjail config string."""
    return f"""
name: "{name}"
mode: ONCE
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
keep_env: false
envar: "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
mount: {{ src: "/bin" dst: "/bin" is_bind: true rw: false mandatory: true }}
mount: {{ src: "/usr" dst: "/usr" is_bind: true rw: false mandatory: true }}
mount: {{ src: "/lib" dst: "/lib" is_bind: true rw: false mandatory: false }}
mount: {{ src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }}
{extra_mounts}
time_limit: 10
exec_bin {{
  path: "/bin/sh"
  arg: "-c"
  arg: {repr(command)}
}}
"""


def test_project_dir_mount_original_path(nsjail_vm: NsjailVM) -> None:
    """Project directory mounted at the same path as on the host."""
    host_dir = "/tmp/nsjail_project_test"
    nsjail_vm.run(f"mkdir -p {host_dir} && echo marker > {host_dir}/flag.txt")
    config = _base_config(
        "test-proj-mount",
        "cat /tmp/nsjail_project_test/flag.txt",
        f'mount: {{ src: "{host_dir}" dst: "{host_dir}" '
        'is_bind: true rw: true mandatory: true }',
    )
    cfg_path = "/tmp/test_proj_mount.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "marker"


def test_workspace_rw_bind_mount(nsjail_vm: NsjailVM) -> None:
    """Files written inside the jail should appear on the host workspace."""
    host_dir = "/tmp/nsjail_workspace_test"
    nsjail_vm.run(f"rm -rf {host_dir} && mkdir -p {host_dir}")
    config = _base_config(
        "test-workspace-rw",
        f"echo inside-jail > {host_dir}/written.txt",
        f'mount: {{ src: "{host_dir}" dst: "{host_dir}" '
        'is_bind: true rw: true mandatory: true }',
    )
    cfg_path = "/tmp/test_workspace_rw.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
    assert result.returncode == 0, result.stderr
    read_back = nsjail_vm.run(f"cat {host_dir}/written.txt")
    assert read_back.stdout.strip() == "inside-jail"


def test_trusted_dir_read_only(nsjail_vm: NsjailVM) -> None:
    """Writes to a read-only trusted mount should fail."""
    host_dir = "/tmp/nsjail_trusted_test"
    nsjail_vm.run(f"rm -rf {host_dir} && mkdir -p {host_dir} && echo ro > {host_dir}/file.txt")
    config = _base_config(
        "test-trusted-ro",
        f"echo jail-write > {host_dir}/file.txt || echo WRITE_BLOCKED",
        f'mount: {{ src: "{host_dir}" dst: "{host_dir}" '
        'is_bind: true rw: false mandatory: true }',
    )
    cfg_path = "/tmp/test_trusted_ro.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
    assert "WRITE_BLOCKED" in result.stdout
    host_check = nsjail_vm.run(f"cat {host_dir}/file.txt")
    assert host_check.stdout.strip() == "ro"


def test_unmounted_paths_invisible(nsjail_vm: NsjailVM) -> None:
    """Paths not mounted into the jail should not be visible."""
    config = _base_config(
        "test-unmounted",
        "test -e /etc/shadow && echo FOUND || echo MISSING",
    )
    cfg_path = "/tmp/test_unmounted.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
    assert result.returncode == 0
    assert result.stdout.strip() == "MISSING"


def test_deep_nested_paths(nsjail_vm: NsjailVM) -> None:
    """Bind mounts at four or more directory levels should work."""
    host_dir = "/tmp/level1/level2/level3/level4"
    nsjail_vm.run(f"rm -rf /tmp/level1 && mkdir -p {host_dir} && echo deep > {host_dir}/data.txt")
    config = _base_config(
        "test-deep-paths",
        f"cat {host_dir}/data.txt",
        f'mount: {{ src: "{host_dir}" dst: "{host_dir}" '
        'is_bind: true rw: true mandatory: true }',
    )
    cfg_path = "/tmp/test_deep_paths.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
    assert result.returncode == 0
    assert result.stdout.strip() == "deep"


def test_session_tmp_persistence(nsjail_vm: NsjailVM) -> None:
    """Per-session /tmp persists across separate nsjail invocations."""
    session_tmp = f"/tmp/nsjail_session_{uuid.uuid4().hex}"
    nsjail_vm.run(f"rm -rf {session_tmp} && mkdir -p {session_tmp}")
    first_config = _base_config(
        "test-session-first",
        "echo persisted > /tmp/leave_behind.txt",
        f'mount: {{ src: "{session_tmp}" dst: "/tmp" '
        'is_bind: true rw: true mandatory: true }',
    )
    cfg_path_first = "/tmp/test_session_first.cfg"
    _write_config(nsjail_vm, cfg_path_first, first_config)
    first = nsjail_vm.run_nsjail(cfg_path_first, timeout=15)
    assert first.returncode == 0, first.stderr

    second_config = _base_config(
        "test-session-second",
        "cat /tmp/leave_behind.txt",
        f'mount: {{ src: "{session_tmp}" dst: "/tmp" '
        'is_bind: true rw: true mandatory: true }',
    )
    cfg_path_second = "/tmp/test_session_second.cfg"
    _write_config(nsjail_vm, cfg_path_second, second_config)
    second = nsjail_vm.run_nsjail(cfg_path_second, timeout=15)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "persisted"
