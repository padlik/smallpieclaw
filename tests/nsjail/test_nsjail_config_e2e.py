"""End-to-end tests: NsjailConfigBuilder output → real nsjail binary.

Unlike the other nsjail VM tests, which use hand-written config templates, these
tests call ``NsjailConfigBuilder.build()`` — the exact code path the agent uses at
runtime — and feed the generated config to a real nsjail binary inside the Lima
VM.  This closes the gap where a config-generation bug (e.g. unquoted paths) is
invisible to both the unit tests (which only do substring assertions) and the VM
tests (which never use the builder).

The session tmpdir is created *inside the VM* so the bind-mount source path
exists when nsjail tries to mount it.  The builder runs on the host but only
generates text (the config file); the paths it references must be valid inside
the VM.  Because the builder calls ``os.path.realpath`` on the session tmpdir
(which on macOS resolves ``/tmp`` → ``/private/tmp``), we patch ``realpath`` to
preserve VM paths verbatim.

Tests run inside a Lima VM and are skipped when Lima is not installed.
"""

from __future__ import annotations

import os
import uuid
from typing import Generator
from unittest.mock import patch

import pytest

from nsjail_config import NsjailConfigBuilder
from tests.nsjail.conftest import NsjailVM


def _make_builder(session_tmpdir: str, tmp_dir: str) -> NsjailConfigBuilder:
    """Create a builder with realistic parameters matching main.py wiring.

    Args:
        session_tmpdir: A path that exists *inside the VM* (e.g. /tmp/nsjail-e2e-xxx).
        tmp_dir: The agent's default trusted temp/handoff directory; must also
            exist *inside the VM* since the mount is ``mandatory: true``.

    The builder calls ``os.path.realpath`` on the session tmpdir in its
    ``__init__``, which on a macOS host resolves ``/tmp`` → ``/private/tmp``.
    Since the path must be valid inside the VM (not on the host), we patch
    ``realpath`` to return the path unchanged.  The same patch is applied
    during ``build()`` in ``_run_builder_config_in_vm`` for system mount paths.
    """
    with patch("os.path.realpath", side_effect=lambda p: p):
        return NsjailConfigBuilder(
            session_tmpdir=session_tmpdir,
            tmp_dir=tmp_dir,
            trusted_dirs_path="",
            memory_mb=256,
            pids_max=64,
            cpu_percent=50,
            allow_net=False,
            skills_dir="",
            agent_dir="",
        )


def _run_builder_config_in_vm(
    vm: NsjailVM,
    builder: NsjailConfigBuilder,
    command: str,
    timeout: int = 10,
) -> tuple[int, str, str]:
    """Build a config, transfer it to the VM, and run nsjail.

    Patches ``os.path.realpath`` and ``os.path.exists`` during ``build()`` so
    the generated config references paths as they appear inside the VM (Ubuntu),
    not on the macOS host.  Specifically:
    - ``realpath`` is identity so ``/tmp/...`` isn't rewritten to ``/private/tmp/...``.
    - ``exists`` reports ``/lib`` as present (it exists in the VM as a symlink
      to ``/usr/lib`` but is absent on macOS).  ``/lib64`` and ``/lib32`` are
      absent in the VM, so they are not faked.

    Returns:
        ``(returncode, stdout, stderr)`` from the nsjail invocation.
    """
    _vm_extra_paths = {"/lib"}
    _orig_exists = os.path.exists

    def _vm_exists(path: str) -> bool:
        if path in _vm_extra_paths:
            return True
        return _orig_exists(path)

    with patch("os.path.realpath", side_effect=lambda p: p), \
         patch("os.path.exists", side_effect=_vm_exists):
        cfg_path, _ = builder.build(command, timeout)
    try:
        with open(cfg_path, encoding="utf-8") as f:
            config_content = f.read()
    finally:
        os.unlink(cfg_path)

    remote_cfg = f"/tmp/e2e_{abs(hash(command))}.cfg"
    vm.run(f"cat > {remote_cfg} <<'EOF'\n{config_content}\nEOF")
    result = vm.run_nsjail(remote_cfg, timeout=timeout + 10)
    vm.run(f"rm -f {remote_cfg}")
    return result.returncode, result.stdout, result.stderr


def _vm_session_tmpdir(vm: NsjailVM) -> str:
    """Create a session tmpdir inside the VM and return its path."""
    path = f"/tmp/nsjail-e2e-{uuid.uuid4().hex[:8]}"
    vm.run(f"mkdir -p {path}")
    return path


@pytest.fixture
def vm_session_tmpdir(nsjail_vm: NsjailVM) -> Generator[str, None, None]:
    """Per-test session tmpdir inside the VM, cleaned up after the test."""
    path = _vm_session_tmpdir(nsjail_vm)
    yield path
    nsjail_vm.run(f"rm -rf {path}")


@pytest.fixture
def vm_tmp_dir(nsjail_vm: NsjailVM) -> Generator[str, None, None]:
    """Per-test agent tmp_dir inside the VM — the mount is mandatory: true, so
    it must exist before nsjail launches, same as main.py's os.makedirs at startup."""
    path = f"/tmp/nsjail-e2e-tmpdir-{uuid.uuid4().hex[:8]}"
    nsjail_vm.run(f"mkdir -p {path}")
    yield path
    nsjail_vm.run(f"rm -rf {path}")


@pytest.mark.nsjail
def test_builder_config_echo(nsjail_vm: NsjailVM, vm_session_tmpdir: str, vm_tmp_dir: str) -> None:
    """A config generated by NsjailConfigBuilder runs ``echo`` successfully.

    This is the minimal smoke test: if the builder produces an invalid config
    (e.g. unquoted paths, malformed protobuf text), nsjail exits 255 before the
    sandboxed command even starts.
    """
    builder = _make_builder(vm_session_tmpdir, vm_tmp_dir)
    rc, stdout, stderr = _run_builder_config_in_vm(nsjail_vm, builder, "echo hello")
    assert rc == 0, f"nsjail failed (rc={rc}):\nstdout={stdout}\nstderr={stderr}"
    assert stdout.strip() == "hello"


@pytest.mark.nsjail
def test_builder_config_dev_null_redirection(
    nsjail_vm: NsjailVM, vm_session_tmpdir: str, vm_tmp_dir: str
) -> None:
    """A builder-generated config supports ``2>/dev/null`` redirection.

    This specifically exercises the /dev/null bind mount that was the source of
    the unquoted-path bug.  If /dev/null is not mounted (or the config line is
    malformed), the redirection fails and the command exits non-zero.
    """
    builder = _make_builder(vm_session_tmpdir, vm_tmp_dir)
    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm, builder, "echo ok 2>/dev/null"
    )
    assert rc == 0, f"nsjail failed (rc={rc}):\nstdout={stdout}\nstderr={stderr}"
    assert stdout.strip() == "ok"


@pytest.mark.nsjail
def test_builder_config_exit_code(
    nsjail_vm: NsjailVM, vm_session_tmpdir: str, vm_tmp_dir: str
) -> None:
    """Exit codes from the sandboxed command pass through a builder-generated config."""
    builder = _make_builder(vm_session_tmpdir, vm_tmp_dir)
    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm, builder, "exit 42"
    )
    assert rc == 42, f"expected exit 42, got {rc}\nstderr={stderr}"


@pytest.mark.nsjail
def test_builder_config_stderr_capture(
    nsjail_vm: NsjailVM, vm_session_tmpdir: str, vm_tmp_dir: str
) -> None:
    """stderr from the sandboxed command is captured with a builder-generated config."""
    builder = _make_builder(vm_session_tmpdir, vm_tmp_dir)
    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm, builder, "echo errmsg >&2"
    )
    assert rc == 0, f"nsjail failed (rc={rc}):\nstderr={stderr}"
    assert stdout.strip() == ""
    assert "errmsg" in stderr


@pytest.mark.nsjail
def test_file_written_inside_jail_visible_on_host(
    nsjail_vm: NsjailVM, vm_session_tmpdir: str, vm_tmp_dir: str
) -> None:
    """A file written inside the jail under tmp_dir is visible on the host afterward."""
    builder = _make_builder(vm_session_tmpdir, vm_tmp_dir)
    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm, builder, f"echo data > {vm_tmp_dir}/result.txt"
    )
    assert rc == 0, f"nsjail failed (rc={rc}):\nstdout={stdout}\nstderr={stderr}"
    result = nsjail_vm.run(f"cat {vm_tmp_dir}/result.txt")
    assert result.stdout.strip() == "data"


@pytest.mark.nsjail
def test_file_present_on_host_visible_inside_jail(
    nsjail_vm: NsjailVM, vm_session_tmpdir: str, vm_tmp_dir: str
) -> None:
    """A file placed on the host beforehand under tmp_dir is readable inside the jail."""
    nsjail_vm.run(f"echo input_data > {vm_tmp_dir}/input.txt")
    builder = _make_builder(vm_session_tmpdir, vm_tmp_dir)
    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm, builder, f"cat {vm_tmp_dir}/input.txt"
    )
    assert rc == 0, f"nsjail failed (rc={rc}):\nstdout={stdout}\nstderr={stderr}"
    assert stdout.strip() == "input_data"


@pytest.mark.nsjail
def test_missing_tmp_dir_fails_shell_call_not_degraded_jail(
    nsjail_vm: NsjailVM, vm_session_tmpdir: str
) -> None:
    """If tmp_dir has been removed from the host, the shell call fails loudly
    (mandatory: true) instead of launching a jail missing this mount."""
    missing_tmp_dir = f"/tmp/nsjail-e2e-missing-{uuid.uuid4().hex[:8]}"
    builder = _make_builder(vm_session_tmpdir, missing_tmp_dir)
    rc, stdout, stderr = _run_builder_config_in_vm(nsjail_vm, builder, "echo unreachable")
    assert rc != 0, f"expected nsjail to fail with a missing mandatory mount, got rc={rc}"
    assert stdout.strip() != "unreachable"


@pytest.mark.nsjail
def test_tmpdir_tmp_temp_envars_point_at_scratch_tmp(
    nsjail_vm: NsjailVM, vm_session_tmpdir: str, vm_tmp_dir: str
) -> None:
    """TMPDIR/TMP/TEMP inside the jail are the ephemeral /tmp scratch mount, not tmp_dir."""
    builder = _make_builder(vm_session_tmpdir, vm_tmp_dir)
    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm, builder, "echo $TMPDIR $TMP $TEMP"
    )
    assert rc == 0, f"nsjail failed (rc={rc}):\nstdout={stdout}\nstderr={stderr}"
    assert stdout.strip() == "/tmp /tmp /tmp"


@pytest.mark.nsjail
def test_session_env_var_overrides_tmpdir(
    nsjail_vm: NsjailVM, vm_session_tmpdir: str, vm_tmp_dir: str
) -> None:
    """A shell_env_set-style -E override wins over the config envar for TMPDIR."""
    builder = _make_builder(vm_session_tmpdir, vm_tmp_dir)
    _vm_extra_paths = {"/lib"}
    _orig_exists = os.path.exists

    def _vm_exists(path: str) -> bool:
        if path in _vm_extra_paths:
            return True
        return _orig_exists(path)

    with patch("os.path.realpath", side_effect=lambda p: p), \
         patch("os.path.exists", side_effect=_vm_exists):
        cfg_path, cmd = builder.build(
            "echo $TMPDIR", timeout=10, shell_env={"TMPDIR": "/custom/tmp"}
        )
    try:
        with open(cfg_path, encoding="utf-8") as f:
            config_content = f.read()
    finally:
        os.unlink(cfg_path)

    assert "-E" in cmd and "TMPDIR=/custom/tmp" in cmd

    remote_cfg = "/tmp/e2e_override_tmpdir.cfg"
    nsjail_vm.run(f"cat > {remote_cfg} <<'EOF'\n{config_content}\nEOF")
    remote_cmd = " ".join(["nsjail", "--config", remote_cfg, "-E", "TMPDIR=/custom/tmp"])
    result = nsjail_vm.run(remote_cmd, timeout=20)
    nsjail_vm.run(f"rm -f {remote_cfg}")
    assert result.returncode == 0, f"nsjail failed: {result.stderr}"
    assert result.stdout.strip() == "/custom/tmp"