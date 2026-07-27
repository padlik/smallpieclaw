"""Integration tests for nsjail cgroup delegation.

Tests run inside a Lima VM and are skipped when Lima is not installed.
"""

from __future__ import annotations

from tests.nsjail.conftest import NsjailVM


def _write_config(vm: NsjailVM, path: str, config: str) -> None:
    """Write an nsjail config to a file inside the VM."""
    vm.run(f"cat > {path} <<'EOF'\n{config}\nEOF")


def _base_config(name: str, command: str, extra: str = "") -> str:
    """Return a minimal nsjail config string with cgroup support."""
    return f"""
name: "{name}"
mode: ONCE
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
clone_newcgroup: true
keep_env: false
envar: "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
mount: {{ src: "/bin" dst: "/bin" is_bind: true rw: false mandatory: true }}
mount: {{ src: "/usr" dst: "/usr" is_bind: true rw: false mandatory: true }}
mount: {{ src: "/lib" dst: "/lib" is_bind: true rw: false mandatory: false }}
mount: {{ src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }}
mount: {{ src: "/sys" dst: "/sys" is_bind: true rw: false mandatory: false }}
mount: {{ src: "/sys/fs/cgroup" dst: "/sys/fs/cgroup" is_bind: true rw: true mandatory: false }}
{extra}
time_limit: 30
exec_bin {{
  path: "/bin/sh"
  arg: "-c"
  arg: {repr(command)}
}}
"""


def test_systemd_run_wrapper_works(nsjail_vm: NsjailVM) -> None:
    """nsjail wrapped in systemd-run --scope --property=Delegate=yes runs."""
    config = _base_config(
        "test-systemd-run",
        "echo delegated",
    )
    cfg_path = "/tmp/test_systemd_run.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail_cgroup(cfg_path, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "delegated" in result.stdout


def test_memory_limit_enforcement(nsjail_vm: NsjailVM) -> None:
    """A process exceeding cgroup_mem_max is killed."""
    config = _base_config(
        "test-memory-limit",
        "python3 -c \"a = bytearray(200 * 1024 * 1024)\"",
        "cgroup_mem_max: 104857600\n"  # 100 MiB
        "cgroup_pids_max: 32\n"
        "use_cgroupv2: true",
    )
    cfg_path = "/tmp/test_memory_limit.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail_cgroup(cfg_path, timeout=30)
    assert result.returncode != 0, f"expected failure, got {result.returncode}"
    assert any(
        token in result.stderr.lower()
        for token in ("memory", "killed", "signal", "oom")
    ), result.stderr


def test_pid_limit_enforcement(nsjail_vm: NsjailVM) -> None:
    """A fork bomb is contained by cgroup_pids_max."""
    config = _base_config(
        "test-pid-limit",
        "python3 -c \"import os; [os.fork() for _ in range(200)]\"",
        "cgroup_mem_max: 209715200\n"  # 200 MiB
        "cgroup_pids_max: 8\n"
        "use_cgroupv2: true",
    )
    cfg_path = "/tmp/test_pid_limit.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail_cgroup(cfg_path, timeout=30)
    assert result.returncode != 0
