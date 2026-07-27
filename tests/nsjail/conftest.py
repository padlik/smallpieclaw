"""Conftest for nsjail Lima VM integration tests.

Session-scoped fixture creates/starts a Lima VM, provisions nsjail, and
provides a helper for running commands inside the VM.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

VM_NAME = "nsjail-test"


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
def nsjail_vm():
    """Session-scoped Lima VM with nsjail provisioned."""
    if not _lima_available():
        pytest.skip("Lima not installed -- skipping nsjail integration tests")

    result = subprocess.run(
        ["limactl", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    vms = [line for line in result.stdout.strip().split("\n") if line]
    vm_exists = any(f'"{VM_NAME}"' in vm for vm in vms)

    if not vm_exists:
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

    result = subprocess.run(
        ["limactl", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if "Stopped" in result.stdout:
        subprocess.run(["limactl", "start", VM_NAME], check=True)

    vm = _NsjailVMHelper()

    result = vm.run("which nsjail")
    if result.returncode != 0:
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

    vm.run(
        'echo "kernel.apparmor_restrict_unprivileged_userns=0" | '
        "sudo tee /etc/sysctl.d/99-nsjail.conf",
        timeout=10,
    )
    vm.run("sudo sysctl -p /etc/sysctl.d/99-nsjail.conf", timeout=10)

    yield vm


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers used by the nsjail test suite."""
    config.addinivalue_line(
        "markers", "nsjail: marks tests that run inside the Lima VM"
    )
