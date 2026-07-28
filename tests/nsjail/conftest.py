"""Conftest for nsjail Lima VM integration tests.

Session-scoped fixture creates/starts a Lima VM, provisions nsjail, and
provides a helper for running commands inside the VM.

A session-scoped ``nsjail_status`` fixture records the reason nsjail tests were
skipped (Lima missing, VM start failed, nsjail provisioning failed, etc.) so it
can be surfaced via ``pytest_report_header`` and the terminal summary.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

import pytest

VM_NAME = "nsjail-test"


@dataclass
class NsjailTestStatus:
    """Tracks whether nsjail VM tests can run and why they might not.

    ``reason`` is empty when tests ran successfully; otherwise it holds a
    human-readable explanation that is surfaced in the pytest report header
    and terminal summary so skipped tests are never silently ignored.
    """

    available: bool = False
    reason: str = ""


# Module-level singleton shared between the fixture and pytest hooks.
_status = NsjailTestStatus()


class NsjailVM:
    """Helper wrapper for command execution inside the Lima VM."""

    def run(self, cmd: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        """Run a command inside the Lima VM."""
        return subprocess.run(
            ["limactl", "shell", VM_NAME, "bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def run_nsjail(
        self, config_path: str, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        """Run nsjail inside the VM with a config file."""
        return self.run(f"nsjail --config {config_path}", timeout=timeout)

    def run_nsjail_cgroup(
        self, config_path: str, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        """Run nsjail inside a systemd-run delegated cgroup scope."""
        return self.run(
            "sudo systemd-run --scope --property=Delegate=yes "
            f"nsjail --config {config_path}",
            timeout=timeout,
        )


class _NsjailVMHelper(NsjailVM):
    """Internal concrete implementation returned by the fixture."""

    pass


def _lima_available() -> bool:
    """Return True if Lima CLI is on PATH."""
    return shutil.which("limactl") is not None


@pytest.fixture(scope="session")
def nsjail_status() -> NsjailTestStatus:
    """Session-scoped status tracker for nsjail test availability."""
    return _status


@pytest.fixture(scope="session")
def nsjail_vm(nsjail_status: NsjailTestStatus):
    """Session-scoped Lima VM with nsjail provisioned.

    On any failure, records the reason in ``nsjail_status`` and calls
    ``pytest.skip`` so the reason is visible per-test.
    """
    if not _lima_available():
        nsjail_status.reason = "Lima not installed (limactl not on PATH)"
        pytest.skip(nsjail_status.reason)

    result = subprocess.run(
        ["limactl", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    vms = [line for line in result.stdout.strip().split("\n") if line]
    vm_exists = any(f'"{VM_NAME}"' in vm for vm in vms)

    if not vm_exists:
        try:
            subprocess.run(
                [
                    "limactl",
                    "create",
                    "--name",
                    VM_NAME,
                    "--arch",
                    "aarch64",
                    "--vm-type",
                    "vz",
                    "template:ubuntu-26.04",
                ],
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            nsjail_status.reason = f"Failed to create Lima VM: {exc}"
            pytest.skip(nsjail_status.reason)

    result = subprocess.run(
        ["limactl", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if "Stopped" in result.stdout:
        try:
            subprocess.run(["limactl", "start", VM_NAME], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            nsjail_status.reason = f"Failed to start Lima VM: {exc}"
            pytest.skip(nsjail_status.reason)

    vm = _NsjailVMHelper()

    result = vm.run("which nsjail")
    if result.returncode != 0:
        try:
            vm.run("sudo apt-get update -qq", timeout=120)
            vm.run(
                "sudo apt-get install -y -qq autoconf bison flex libprotobuf-dev "
                "libnl-route-3-dev protobuf-compiler pkg-config build-essential git",
                timeout=120,
            )
            vm.run(
                "cd /tmp && git clone --depth=1 --branch 3.6 https://github.com/google/nsjail.git",
                timeout=60,
            )
            vm.run("cd /tmp/nsjail && make -j$(nproc)", timeout=120)
            vm.run("sudo cp /tmp/nsjail/nsjail /usr/local/bin/nsjail", timeout=10)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            nsjail_status.reason = f"Failed to provision nsjail in VM: {exc}"
            pytest.skip(nsjail_status.reason)

    vm.run(
        'echo "kernel.apparmor_restrict_unprivileged_userns=0" | '
        "sudo tee /etc/sysctl.d/99-nsjail.conf",
        timeout=10,
    )
    vm.run("sudo sysctl -p /etc/sysctl.d/99-nsjail.conf", timeout=10)

    nsjail_status.available = True
    yield vm


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers used by the nsjail test suite."""
    config.addinivalue_line(
        "markers", "nsjail: marks tests that run inside the Lima VM"
    )


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Surface nsjail test availability in the pytest report header.

    When nsjail tests are skipped, the reason appears here so it is never
    silently buried in a wall of ``S`` characters.
    """
    if _status.available:
        return ["nsjail VM tests: enabled (Lima VM running, nsjail provisioned)"]
    if _status.reason:
        return [f"nsjail VM tests: SKIPPED — {_status.reason}"]
    # Before the fixture runs, check the gate: is Lima installed?
    if not _lima_available():
        return [
            "nsjail VM tests: will be SKIPPED — Lima not installed (limactl not on PATH)",
            "  Install Lima (brew install lima) to enable nsjail integration tests.",
        ]
    return ["nsjail VM tests: Lima detected — VM fixture will provision nsjail"]


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Print a prominent notice when nsjail tests were skipped.

    This appears at the end of the test run so the user sees it even if the
    report header scrolled away.  Only fires when tests were actually skipped
    due to environment issues, not when all tests passed.
    """
    if _status.available:
        return
    if not _status.reason:
        return
    terminalreporter.write_sep("=", "nsjail VM test status", bold=True)
    terminalreporter.write_line(
        f"  nsjail VM tests SKIPPED — {_status.reason}"
    )
    terminalreporter.write_line(
        "  These tests validate the sandbox against a real nsjail binary."
    )
    terminalreporter.write_line(
        "  Install Lima (brew install lima) to enable them."
    )
