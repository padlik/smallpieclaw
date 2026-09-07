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

import json
import os
import tempfile
import uuid
from typing import Generator
from unittest.mock import patch

import pytest

from nsjail_config import NsjailConfigBuilder
from path_policy import PathPolicy
from tests.nsjail.conftest import NsjailVM


def _make_builder(
    session_tmpdir: str,
    tmp_dir: str,
    *,
    skills_dir: str = "",
    results_dir: str = "",
    allowed_dirs: list[str] | None = None,
    prohibited_dirs: list[str] | None = None,
    existing_paths: set[str] | None = None,
    agent_internal_dir: str = "",
) -> NsjailConfigBuilder:
    """Create a builder with realistic parameters matching main.py wiring.

    Args:
        session_tmpdir: A path that exists *inside the VM* (e.g. /tmp/nsjail-e2e-xxx).
        tmp_dir: The agent's default trusted temp/handoff directory; must also
            exist *inside the VM* since the mount is ``mandatory: true``.
        skills_dir: Optional Tier 1 skills directory (read-only inside the jail).
        results_dir: Optional Tier 1 results directory (read-write inside the jail).
        allowed_dirs: Optional Tier 2 operator-allowed directories.
        prohibited_dirs: Optional config-appended prohibited directories.
        existing_paths: Paths that exist inside the VM but not on the macOS host.
            ``os.path.exists`` and ``os.path.isdir`` are patched so the builder
            treats them as present when generating the mount table.
        agent_internal_dir: Directory to use for XDG data/state/config home.
            Defaults to a temp dir under /tmp on the host so it does not overlap
            the default ``~/.<agent>`` prohibited directory.

    The builder calls ``os.path.realpath`` on the session tmpdir in its
    ``__init__``, which on a macOS host resolves ``/tmp`` → ``/private/tmp``.
    Since the path must be valid inside the VM (not on the host), we patch
    ``realpath`` to return the path unchanged.  The same patch is applied
    during ``build()`` in ``_build_config_and_cmd`` for system mount paths.
    """
    existing_paths = set(existing_paths or set())
    if skills_dir:
        existing_paths.add(skills_dir)
    if results_dir:
        existing_paths.add(results_dir)
    existing_paths.add(session_tmpdir)
    existing_paths.add(f"{session_tmpdir}/workspace")
    existing_paths.add(f"{session_tmpdir}/downloads")
    if tmp_dir:
        existing_paths.add(tmp_dir)

    if not agent_internal_dir:
        # Use a host-side temp directory that does not overlap the default
        # ~/.<agent> prohibited directory, avoiding FU-2 Tier-1 conflict errors.
        agent_internal_dir = tempfile.mkdtemp(prefix="nsjail-e2e-internal-")
    existing_paths.add(agent_internal_dir)

    _orig_exists = os.path.exists
    _orig_isdir = os.path.isdir

    def _vm_exists(path: str) -> bool:
        if path in existing_paths:
            return True
        return _orig_exists(path)

    def _vm_isdir(path: str) -> bool:
        if path in existing_paths:
            return True
        return _orig_isdir(path)

    with patch("os.path.realpath", side_effect=lambda p: p), \
         patch("os.path.exists", side_effect=_vm_exists), \
         patch("os.path.isdir", side_effect=_vm_isdir):
        policy = PathPolicy.create(
            agent_name="nsjail-e2e",
            workspace_dir=f"{session_tmpdir}/workspace",
            downloads_dir=f"{session_tmpdir}/downloads",
            tmp_dir=tmp_dir,
            skills_dir=skills_dir,
            results_dir=results_dir,
            data_home=os.path.join(agent_internal_dir, "data"),
            state_home=os.path.join(agent_internal_dir, "state"),
            config_home=os.path.join(agent_internal_dir, "config"),
            vault_path=os.path.join(agent_internal_dir, "vault.toml"),
            config_path=os.path.join(agent_internal_dir, "config", "config.toml"),
            allowed_dirs=allowed_dirs,
            prohibited_dirs=prohibited_dirs,
        )
        return NsjailConfigBuilder(
            session_tmpdir=session_tmpdir,
            tmp_dir=tmp_dir,
            path_policy=policy,
            memory_mb=256,
            pids_max=64,
            cpu_percent=50,
            allow_net=False,
        )


def _build_config_and_cmd(
    builder: NsjailConfigBuilder,
    command: str = "true",
    timeout: int = 10,
    existing_paths: set[str] | None = None,
    shell_env: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    """Generate config content and the nsjail command list.

    Patches ``os.path.realpath``, ``os.path.exists`` and ``os.path.isdir`` so
    VM paths are treated as present on the host during config generation.

    Args:
        command: Shell command to execute inside the sandbox.
        timeout: Maximum wall-clock execution time in seconds.
        existing_paths: VM-only paths to treat as present during generation.
        shell_env: Optional environment variables passed as ``-E KEY=VALUE``
            flags to nsjail.

    Returns:
        ``(config_content, nsjail_cmd)``.  The temporary config file is deleted
        before returning.
    """
    existing: set[str] = {"/lib"}
    if existing_paths:
        existing.update(existing_paths)
    existing.add(builder.session_tmpdir)
    existing.add(f"{builder.session_tmpdir}/workspace")
    existing.add(f"{builder.session_tmpdir}/downloads")
    existing.add(builder.tmp_dir)

    _orig_exists = os.path.exists
    _orig_isdir = os.path.isdir

    def _vm_exists(path: str) -> bool:
        if path in existing:
            return True
        return _orig_exists(path)

    def _vm_isdir(path: str) -> bool:
        if path in existing:
            return True
        return _orig_isdir(path)

    with patch("os.path.realpath", side_effect=lambda p: p), \
         patch("os.path.exists", side_effect=_vm_exists), \
         patch("os.path.isdir", side_effect=_vm_isdir):
        cfg_path, cmd = builder.build(command, timeout, shell_env=shell_env)
    try:
        with open(cfg_path, encoding="utf-8") as f:
            config_content = f.read()
    finally:
        os.unlink(cfg_path)
    return config_content, cmd


def _run_builder_config_in_vm(
    vm: NsjailVM,
    builder: NsjailConfigBuilder,
    command: str,
    timeout: int = 10,
    existing_paths: set[str] | None = None,
) -> tuple[int, str, str]:
    """Build a config, transfer it to the VM, and run nsjail.

    Uses ``_build_config_and_cmd`` so VM-only paths are treated as present
    during generation.  The config content is shipped to the VM and executed
    with the real nsjail binary.

    Returns:
        ``(returncode, stdout, stderr)`` from the nsjail invocation.
    """
    config_content, _ = _build_config_and_cmd(
        builder, command, timeout, existing_paths=existing_paths
    )

    # Static config is written to session_tmpdir on the host during generation,
    # but session_tmpdir is inside the VM.  The per-call config only references
    # VM paths, so copy it to the VM before running nsjail.
    remote_cfg = f"/tmp/e2e_{abs(hash(command))}.cfg"
    vm.run(f"cat > {remote_cfg} <<'EOF'\n{config_content}\nEOF")
    result = vm.run_nsjail(remote_cfg, timeout=timeout + 10)
    vm.run(f"rm -f {remote_cfg}")
    return result.returncode, result.stdout, result.stderr


def _vm_session_tmpdir(vm: NsjailVM) -> str:
    """Create a session tmpdir inside the VM and return its path."""
    path = f"/tmp/nsjail-e2e-{uuid.uuid4().hex[:8]}"
    result = vm.run(f"mkdir -p {path}/workspace {path}/downloads && ls -d {path}")
    assert result.returncode == 0, f"failed to create session tmpdir: {result.stderr}"
    actual = result.stdout.strip()
    assert actual == path, f"session tmpdir path mismatch: {actual!r} != {path!r}"
    return path


@pytest.fixture
def vm_session_tmpdir(nsjail_vm: NsjailVM) -> Generator[str, None, None]:
    """Per-test session tmpdir inside the VM, cleaned up after the test.

    The session directory and the workspace/downloads subdirectories are
    created in the VM so the builder's Tier 1 mounts are not missing mandatory
    sources at nsjail launch time.
    """
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


@pytest.fixture
def vm_skills_dir(nsjail_vm: NsjailVM) -> Generator[str, None, None]:
    """Per-test skills directory inside the VM — created before nsjail launches."""
    path = f"/tmp/nsjail-e2e-skills-{uuid.uuid4().hex[:8]}"
    nsjail_vm.run(f"mkdir -p {path}")
    yield path
    nsjail_vm.run(f"rm -rf {path}")


@pytest.fixture
def vm_results_dir(nsjail_vm: NsjailVM) -> Generator[str, None, None]:
    """Per-test results directory inside the VM — created before nsjail launches."""
    path = f"/tmp/nsjail-e2e-results-{uuid.uuid4().hex[:8]}"
    nsjail_vm.run(f"mkdir -p {path}")
    yield path
    nsjail_vm.run(f"rm -rf {path}")


@pytest.fixture
def vm_allowed_dir(nsjail_vm: NsjailVM) -> Generator[str, None, None]:
    """Per-test Tier-2 allowed directory nested under /tmp inside the VM."""
    path = f"/tmp/nsjail-e2e-allowed-{uuid.uuid4().hex[:8]}"
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
    config_content, cmd = _build_config_and_cmd(
        builder, "echo $TMPDIR", timeout=10, shell_env={"TMPDIR": "/custom/tmp"}
    )

    assert "-E" in cmd and "TMPDIR=/custom/tmp" in cmd

    remote_cfg = "/tmp/e2e_override_tmpdir.cfg"
    nsjail_vm.run(f"cat > {remote_cfg} <<'EOF'\n{config_content}\nEOF")
    remote_cmd = " ".join(["nsjail", "--config", remote_cfg, "-E", "TMPDIR=/custom/tmp"])
    result = nsjail_vm.run(remote_cmd, timeout=20)
    nsjail_vm.run(f"rm -f {remote_cfg}")
    assert result.returncode == 0, f"nsjail failed: {result.stderr}"
    assert result.stdout.strip() == "/custom/tmp"


@pytest.mark.nsjail
def test_builder_mounts_skills_readonly_and_results_rw(
    nsjail_vm: NsjailVM,
    vm_session_tmpdir: str,
    vm_tmp_dir: str,
    vm_skills_dir: str,
    vm_results_dir: str,
) -> None:
    """Tier 1 skills mount is read-only and results mount is read-write.

    Config-side assertions verify the generated mount lines.  Functional
    assertions run inside the VM: reading from skills works, writing to skills
    fails, and writing to results succeeds and is visible on the host.
    """
    existing_paths = {vm_skills_dir, vm_results_dir, f"{vm_session_tmpdir}/workspace"}
    builder = _make_builder(
        vm_session_tmpdir,
        vm_tmp_dir,
        skills_dir=vm_skills_dir,
        results_dir=vm_results_dir,
        existing_paths=existing_paths,
    )
    config_content, _ = _build_config_and_cmd(builder, existing_paths=existing_paths)

    skills_escaped = json.dumps(vm_skills_dir).replace("\\", "\\\\")
    results_escaped = json.dumps(vm_results_dir).replace("\\", "\\\\")
    assert (
        f"mount: {{ src: {skills_escaped} dst: {skills_escaped} "
        f"is_bind: true rw: false mandatory: true }}"
    ) in config_content
    assert (
        f"mount: {{ src: {results_escaped} dst: {results_escaped} "
        f"is_bind: true rw: true mandatory: true }}"
    ) in config_content

    nsjail_vm.run(f"echo skill_data > {vm_skills_dir}/file.txt")

    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm,
        builder,
        f"cat {vm_skills_dir}/file.txt",
        existing_paths=existing_paths,
    )
    assert rc == 0, f"nsjail failed (rc={rc}):\nstdout={stdout}\nstderr={stderr}"
    assert stdout.strip() == "skill_data"

    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm,
        builder,
        f"echo x > {vm_skills_dir}/file.txt",
        existing_paths=existing_paths,
    )
    assert rc != 0, f"expected writing to skills to fail, got rc={rc}"
    assert "Permission denied" in stderr or "Read-only file system" in stderr, stderr

    unchanged = nsjail_vm.run(f"cat {vm_skills_dir}/file.txt")
    assert unchanged.stdout.strip() == "skill_data"

    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm,
        builder,
        f"echo data > {vm_results_dir}/out.txt",
        existing_paths=existing_paths,
    )
    assert rc == 0, f"nsjail failed (rc={rc}):\nstdout={stdout}\nstderr={stderr}"
    result = nsjail_vm.run(f"cat {vm_results_dir}/out.txt")
    assert result.stdout.strip() == "data"


@pytest.mark.nsjail
def test_builder_session_logs_absent_from_config(
    nsjail_vm: NsjailVM, vm_session_tmpdir: str, vm_tmp_dir: str
) -> None:
    """No session-logs mount or state-home path appears in the generated config."""
    builder = _make_builder(vm_session_tmpdir, vm_tmp_dir)
    config_content, _ = _build_config_and_cmd(builder)
    assert "session_logs" not in config_content
    state_home = builder.path_policy.state_home
    if state_home:
        assert state_home not in config_content


@pytest.mark.nsjail
def test_builder_prohibited_overlap_dir_not_mounted(
    nsjail_vm: NsjailVM, vm_session_tmpdir: str, vm_tmp_dir: str
) -> None:
    """An allowed dir containing a prohibited path is skipped from the mount table.

    The same Tier 1 entries that do not conflict are still emitted.  File-plane
    classification is verified host-side (no VM needed).
    """
    work = f"{vm_session_tmpdir}/work"
    keys = f"{work}/keys"
    existing_paths = {
        work,
        keys,
        f"{vm_session_tmpdir}/workspace",
        f"{vm_session_tmpdir}/downloads",
    }
    builder = _make_builder(
        vm_session_tmpdir,
        vm_tmp_dir,
        allowed_dirs=[work],
        prohibited_dirs=[keys],
        existing_paths=existing_paths,
    )
    config_content, _ = _build_config_and_cmd(builder, existing_paths=existing_paths)

    assert f"src: {json.dumps(work)}" not in config_content
    assert keys not in config_content
    # Tier 1 entries that exist are still mounted; work is not.  Default
    # skills/results entries resolve to host paths that don't exist here and
    # are legitimately skipped, so only assert the entries we forced to exist.
    for tier1_path in (
        f"{vm_session_tmpdir}/workspace",
        f"{vm_session_tmpdir}/downloads",
    ):
        assert f"src: {json.dumps(tier1_path)}" in config_content

    # Classify takes an already-resolved real path; the policy entries are
    # VM-verbatim, so pass the paths verbatim (host realpath would rewrite
    # /tmp -> /private/tmp on macOS and break containment matching).
    verdict, _ = builder.path_policy.classify(f"{work}/report.txt", "read")
    assert verdict.value == "allowed"
    verdict, _ = builder.path_policy.classify(f"{keys}/x", "read")
    assert verdict.value == "prohibited"


@pytest.mark.nsjail
def test_builder_results_artifact_round_trip(
    nsjail_vm: NsjailVM,
    vm_session_tmpdir: str,
    vm_tmp_dir: str,
    vm_results_dir: str,
) -> None:
    """A file written inside the jail under results_dir is readable on the host."""
    existing_paths = {vm_results_dir, f"{vm_session_tmpdir}/workspace"}
    builder = _make_builder(
        vm_session_tmpdir,
        vm_tmp_dir,
        results_dir=vm_results_dir,
        existing_paths=existing_paths,
    )
    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm,
        builder,
        f"echo artifact > {vm_results_dir}/artifact.txt",
        existing_paths=existing_paths,
    )
    assert rc == 0, f"nsjail failed (rc={rc}):\nstdout={stdout}\nstderr={stderr}"
    result = nsjail_vm.run(f"cat {vm_results_dir}/artifact.txt")
    assert result.stdout.strip() == "artifact"


@pytest.mark.nsjail
def test_session_mounts_before_tier_mounts_order_required(
    nsjail_vm: NsjailVM,
    vm_session_tmpdir: str,
    vm_tmp_dir: str,
    vm_allowed_dir: str,
) -> None:
    """A Tier-2 allowed dir nested under /tmp is only writable if the scratch
    /tmp session mount precedes the tier-derived bind.

    Reordering session mounts after tier mounts shadows the allowed_dir bind
    with the scratch remount and makes nsjail fail with remountPt statvfs ENOENT
    (rc=255).  The write-and-read round trip therefore enforces the mount order.
    """
    existing_paths = {
        vm_allowed_dir,
        f"{vm_session_tmpdir}/workspace",
        f"{vm_session_tmpdir}/downloads",
    }
    builder = _make_builder(
        vm_session_tmpdir,
        vm_tmp_dir,
        allowed_dirs=[vm_allowed_dir],
        existing_paths=existing_paths,
    )
    rc, stdout, stderr = _run_builder_config_in_vm(
        nsjail_vm,
        builder,
        f"echo ordercheck > {vm_allowed_dir}/proof.txt",
        existing_paths=existing_paths,
    )
    assert rc == 0, (
        f"nsjail failed (rc={rc}) — session mounts may no longer precede tier mounts:\n"
        f"stdout={stdout}\nstderr={stderr}"
    )
    result = nsjail_vm.run(f"cat {vm_allowed_dir}/proof.txt")
    assert result.stdout.strip() == "ordercheck"
