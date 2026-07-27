"""Integration tests for basic nsjail sandbox behavior.

Tests run inside a Lima VM and are skipped when Lima is not installed.
"""

from __future__ import annotations

from tests.nsjail.conftest import NsjailVM


def _write_config(vm: NsjailVM, path: str, config: str) -> None:
    """Write an nsjail config to a file inside the VM."""
    vm.run(f"cat > {path} <<'EOF'\n{config}\nEOF")


def test_streaming_output(nsjail_vm: NsjailVM) -> None:
    """Incremental output should arrive in stdout without buffering loss."""
    config = """
name: "test-streaming"
mode: ONCE
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
keep_env: false
envar: "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
mount: { src: "/bin" dst: "/bin" is_bind: true rw: false mandatory: true }
mount: { src: "/usr" dst: "/usr" is_bind: true rw: false mandatory: true }
mount: { src: "/lib" dst: "/lib" is_bind: true rw: false mandatory: false }
mount: { src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }
time_limit: 10
log_level: FATAL
exec_bin {
  path: "/bin/sh"
  arg: "-c"
  arg: "for i in 1 2 3; do echo $i; sleep 0.1; done"
}
"""
    cfg_path = "/tmp/test_streaming.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1\n2\n3"


def test_exit_code_fidelity(nsjail_vm: NsjailVM) -> None:
    """Exit codes from the child should pass through unchanged."""
    cfg_path = "/tmp/test_exit_fidelity.cfg"
    for code in (0, 1, 42):
        config = f"""
name: "test-exit-{code}"
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
time_limit: 10
log_level: FATAL
exec_bin {{
  path: "/bin/sh"
  arg: "-c"
  arg: "exit {code}"
}}
"""
        _write_config(nsjail_vm, cfg_path, config)
        result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
        assert result.returncode == code, f"expected {code}, got {result.returncode}"


def test_timeout_enforcement(nsjail_vm: NsjailVM) -> None:
    """A long-running command should be killed and return 137."""
    config = """
name: "test-timeout"
mode: ONCE
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
keep_env: false
envar: "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
mount: { src: "/bin" dst: "/bin" is_bind: true rw: false mandatory: true }
mount: { src: "/usr" dst: "/usr" is_bind: true rw: false mandatory: true }
mount: { src: "/lib" dst: "/lib" is_bind: true rw: false mandatory: false }
mount: { src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }
time_limit: 1
log_level: FATAL
exec_bin {
  path: "/bin/sh"
  arg: "-c"
  arg: "sleep 60"
}
"""
    cfg_path = "/tmp/test_timeout.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
    assert result.returncode == 137, result.returncode


def test_stdout_stderr_separation(nsjail_vm: NsjailVM) -> None:
    """stdout and stderr should remain in separate pipes."""
    config = """
name: "test-stdouterr"
mode: ONCE
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
keep_env: false
envar: "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
mount: { src: "/bin" dst: "/bin" is_bind: true rw: false mandatory: true }
mount: { src: "/usr" dst: "/usr" is_bind: true rw: false mandatory: true }
mount: { src: "/lib" dst: "/lib" is_bind: true rw: false mandatory: false }
mount: { src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }
time_limit: 10
log_level: FATAL
exec_bin {
  path: "/bin/sh"
  arg: "-c"
  arg: "echo out; echo err >&2"
}
"""
    cfg_path = "/tmp/test_stdouterr.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
    assert result.returncode == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_large_output(nsjail_vm: NsjailVM) -> None:
    """A 10K-line output should not be truncated."""
    config = """
name: "test-large-output"
mode: ONCE
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
keep_env: false
envar: "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
mount: { src: "/bin" dst: "/bin" is_bind: true rw: false mandatory: true }
mount: { src: "/usr" dst: "/usr" is_bind: true rw: false mandatory: true }
mount: { src: "/lib" dst: "/lib" is_bind: true rw: false mandatory: false }
mount: { src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }
time_limit: 30
log_level: FATAL
exec_bin {
  path: "/bin/sh"
  arg: "-c"
  arg: "seq 1 10000"
}
"""
    cfg_path = "/tmp/test_large_output.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=45)
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 10000
    assert lines[0] == "1"
    assert lines[-1] == "10000"
