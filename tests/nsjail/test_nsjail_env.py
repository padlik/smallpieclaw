"""Integration tests for nsjail environment isolation.

Tests run inside a Lima VM and are skipped when Lima is not installed.
"""

from __future__ import annotations

import uuid

from tests.nsjail.conftest import NsjailVM


def _write_config(vm: NsjailVM, path: str, config: str) -> None:
    """Write an nsjail config to a file inside the VM."""
    vm.run(f"cat > {path} <<'EOF'\n{config}\nEOF")


def _base_config(
    name: str, command: str, keep_env: str = "false", extra_env: str = ""
) -> str:
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
keep_env: {keep_env}
{extra_env}
mount: {{ src: "/bin" dst: "/bin" is_bind: true rw: false mandatory: true }}
mount: {{ src: "/usr" dst: "/usr" is_bind: true rw: false mandatory: true }}
mount: {{ src: "/lib" dst: "/lib" is_bind: true rw: false mandatory: false }}
mount: {{ src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }}
time_limit: 10
exec_bin {{
  path: "/bin/sh"
  arg: "-c"
  arg: {repr(command)}
}}
"""


def test_keep_env_false_hides_secrets(nsjail_vm: NsjailVM) -> None:
    """With keep_env false, host secrets should not leak into the jail."""
    secret_name = f"NSJAIL_SECRET_{uuid.uuid4().hex.upper()}"
    nsjail_vm.run(f'export {secret_name}="super-secret"')
    config = _base_config(
        "test-keep-env-false",
        f"printenv {secret_name} || echo NOT_FOUND",
    )
    cfg_path = "/tmp/test_keep_env_false.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
    assert result.returncode == 0
    assert "super-secret" not in result.stdout
    assert "NOT_FOUND" in result.stdout


def test_base_envars_present(nsjail_vm: NsjailVM) -> None:
    """PATH, HOME, LANG, and TERM should be present in the jail."""
    config = _base_config(
        "test-base-env",
        "echo PATH=$PATH; echo HOME=$HOME; echo LANG=$LANG; echo TERM=$TERM",
        extra_env='envar: "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'
        'envar: "HOME=/root"\n'
        'envar: "LANG=C.UTF-8"\n'
        'envar: "TERM=xterm-256color"',
    )
    cfg_path = "/tmp/test_base_env.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run_nsjail(cfg_path, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in result.stdout
    assert "HOME=/root" in result.stdout
    assert "LANG=C.UTF-8" in result.stdout
    assert "TERM=xterm-256color" in result.stdout


def test_e_flag_injection(nsjail_vm: NsjailVM) -> None:
    """Custom variables passed via -E should be visible in the jail."""
    config = _base_config(
        "test-e-flag",
        "echo CUSTOM=$CUSTOM_VAR",
    )
    cfg_path = "/tmp/test_e_flag.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    result = nsjail_vm.run(
        f'nsjail --config {cfg_path} -E CUSTOM_VAR="hello-jail"', timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert "CUSTOM=hello-jail" in result.stdout


def test_session_env_persistence(nsjail_vm: NsjailVM) -> None:
    """A variable injected once should be visible across subsequent calls."""
    config = _base_config(
        "test-session-env",
        "echo SESSION=$SESSION_ID",
    )
    cfg_path = "/tmp/test_session_env.cfg"
    _write_config(nsjail_vm, cfg_path, config)
    session_id = uuid.uuid4().hex
    first = nsjail_vm.run(
        f'nsjail --config {cfg_path} -E SESSION_ID="{session_id}"', timeout=15
    )
    assert first.returncode == 0
    assert f"SESSION={session_id}" in first.stdout
    second = nsjail_vm.run(
        f'nsjail --config {cfg_path} -E SESSION_ID="{session_id}"', timeout=15
    )
    assert second.returncode == 0
    assert f"SESSION={session_id}" in second.stdout
