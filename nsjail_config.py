"""Dynamic nsjail configuration builder for sandboxed shell execution.

Generates per-call nsjail configuration files and command lists, adapting to the
host Linux environment for system mount layout, cgroup v2 availability, and
resource limit delegation.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from typing import Any, Optional

logger = logging.getLogger(__name__)

CGROUP2_SUPER_MAGIC: int = 0x63677270

# Paths that must never appear as trusted-dir mount entries.
_BLOCKED_SYSTEM_PREFIXES: tuple[str, ...] = (
    "/etc", "/proc", "/sys", "/dev", "/boot",
    "/bin", "/sbin", "/lib", "/lib64", "/usr", "/root",
    "/var", "/run",
)


class NsjailConfigBuilder:
    """Build an nsjail config and command list for a single shell invocation."""

    def __init__(
        self,
        session_tmpdir: str,
        trusted_dirs_path: str = "",
        memory_mb: int = 256,
        pids_max: int = 64,
        cpu_percent: int = 50,
        allow_net: bool = False,
        skills_dir: str = "",
        agent_dir: str = "",
    ) -> None:
        """Initialize the builder.

        Args:
            session_tmpdir: Per-session temporary directory; mounted as ``/tmp``
                inside the sandbox.
            trusted_dirs_path: Absolute path to ``trusted_dirs.json``.
            memory_mb: Memory limit in megabytes.
            pids_max: Maximum number of PIDs allowed inside the sandbox.
            cpu_percent: Cgroup CPU limit as a percentage of one CPU.
            allow_net: When False (default) networking is isolated inside the
                sandbox; when True the host network namespace is shared.
            skills_dir: Absolute path to the skills directory; mounted read-only
                when it exists and is not on a restricted path.
            agent_dir: Absolute path to the agent's own installation directory;
                blocked as a trusted mount to prevent the agent from mounting its
                source code RW inside the sandbox.
        """
        self.session_tmpdir = os.path.realpath(os.path.abspath(session_tmpdir))
        self.trusted_dirs_path = os.path.abspath(trusted_dirs_path) if trusted_dirs_path else ""
        self._agent_dir = os.path.realpath(os.path.abspath(agent_dir)) if agent_dir else ""
        # Dynamic blocked prefixes: user-home paths that must never be trusted mounts.
        home = os.path.expanduser("~")
        blocked = [
            os.path.join(home, ".ssh"),
            os.path.join(home, ".local"),
            os.path.join(home, ".config"),
            os.path.join(home, ".gnupg"),
            os.path.join(home, ".aws"),
            os.path.join(home, ".kube"),
            os.path.join(home, ".docker"),
            os.path.join(home, ".cache"),
        ]
        if self._agent_dir:
            blocked.append(self._agent_dir)
        self._blocked_user_prefixes: tuple[str, ...] = tuple(blocked)
        self.memory_mb = memory_mb
        self.pids_max = pids_max
        self.cpu_percent = cpu_percent
        self.allow_net = allow_net
        self.skills_dir = os.path.realpath(os.path.abspath(skills_dir)) if skills_dir else ""
        self._cgroup_info = self._detect_cgroup_capability()

    def _detect_system_mounts(self) -> list[str]:
        """Detect system mount layout and return nsjail mount config lines.

        ``/usr`` is always mounted read-only and marked mandatory.  For each of
        ``/bin``, ``/sbin``, ``/lib``, ``/lib64``, and ``/lib32``: if the path is a
        symlink, it is mounted with ``mandatory: false``; if it is a real
        directory, it is mounted with ``mandatory: true``; if absent, it is
        skipped.  Symlink targets are resolved via ``os.path.realpath``.

        Returns:
            List of nsjail ``mount: {{ ... }}`` configuration lines.
        """
        lines: list[str] = []
        lines.append(
            f'mount: {{ src: {json.dumps("/usr")} dst: {json.dumps("/usr")}'
            f' is_bind: true rw: false mandatory: true }}'
        )

        candidates = ["/bin", "/sbin", "/lib", "/lib64", "/lib32"]
        for path in candidates:
            if not os.path.exists(path):
                continue
            real = os.path.realpath(path)
            mandatory = "false" if os.path.islink(path) else "true"
            lines.append(
                f'mount: {{ src: {json.dumps(real)} dst: {json.dumps(path)} is_bind: true '
                f'rw: false mandatory: {mandatory} }}'
            )
        return lines

    def _detect_cgroup_capability(self) -> dict[str, Any]:
        """Detect whether cgroup v2 delegation is available.

        Requires both ``systemd-run`` on PATH and a cgroup2 root mounted at
        ``/sys/fs/cgroup``.  When available, the user's cgroup path is derived from
        the current UID.

        Detection of the cgroup2 filesystem type uses ``os.statfs`` when available
        (Linux-only, checks the magic number), falling back to ``os.statvfs`` +
        ``/proc/filesystems`` on Python builds where ``os.statfs`` is missing.

        Returns:
            Mapping with keys ``available`` (bool) and ``cgroupv2_mount`` (str or
            None).
        """
        available = False
        cgroupv2_mount: Optional[str] = None

        has_systemd_run = shutil.which("systemd-run") is not None
        if not has_systemd_run:
            return {"available": False, "cgroupv2_mount": None}

        if not self._is_cgroup2_mounted():
            return {"available": False, "cgroupv2_mount": None}

        uid = os.getuid()
        user_cgroup = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"
        if os.path.isdir(user_cgroup):
            available = True
            cgroupv2_mount = user_cgroup

        return {"available": available, "cgroupv2_mount": cgroupv2_mount}

    @staticmethod
    def _is_cgroup2_mounted() -> bool:
        """Check whether ``/sys/fs/cgroup`` is a cgroup v2 mount.

        Uses ``os.statfs`` (Linux-only, checks the magic number against
        ``CGROUP2_SUPER_MAGIC``) when available.  Falls back to parsing
        ``/proc/filesystems`` for a ``cgroup2`` entry when ``os.statfs`` is
        missing from the Python build (some distributions ship Python without
        the Linux-specific statfs binding).
        """
        _statfs = getattr(os, "statfs", None)
        if _statfs is not None:
            try:
                return _statfs("/sys/fs/cgroup").f_type == CGROUP2_SUPER_MAGIC
            except OSError:
                return False

        # Fallback: parse /proc/filesystems for a cgroup2 entry.
        try:
            with open("/proc/filesystems", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 2 and parts[-1] == "cgroup2":
                        return True
            return False
        except OSError:
            return False

    def _load_trusted_mounts(self) -> list[str]:
        """Load trusted directory mounts from the configured trusted_dirs.json path.

        The file is read fresh on every call and is expected to contain a list of
        dictionaries with ``path`` and ``mode`` keys.  Missing files are handled
        gracefully by returning an empty list.  Each resolved path is checked
        against a denylist of critical system prefixes before being accepted.

        Returns:
            List of nsjail ``mount: {{ ... }}`` configuration lines.
        """
        trusted_path = self.trusted_dirs_path
        if not os.path.exists(trusted_path):
            return []

        lines: list[str] = []
        try:
            with open(trusted_path, encoding="utf-8") as fh:
                entries = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Unable to load trusted_dirs.json: %s", exc)
            return []

        if not isinstance(entries, list):
            logger.warning("trusted_dirs.json did not contain a list")
            return []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            mode = entry.get("mode", "r")
            if not path or not isinstance(path, str):
                continue
            real = os.path.realpath(os.path.abspath(path))
            # Root-containment check: reject the filesystem root and known system paths.
            if real == "/" or any(
                real == p or real.startswith(p + "/")
                for p in (_BLOCKED_SYSTEM_PREFIXES + self._blocked_user_prefixes)
            ):
                logger.warning("Trusted directory rejected (restricted system path): %s", real)
                continue
            if not os.path.exists(real):
                logger.warning("Trusted directory does not exist: %s", real)
                continue
            # Skip trusted-dir entries that are under an already-mounted path
            # (e.g., session_tmpdir is mounted as /tmp).
            if real == self.session_tmpdir or real.startswith(self.session_tmpdir + os.sep):
                logger.debug("Trusted directory under session_tmpdir, skipping (already mounted as /tmp): %s", real)
                continue
            rw = "true" if mode == "rw" else "false"
            lines.append(
                f'mount: {{ src: {json.dumps(real)} dst: {json.dumps(real)} is_bind: true '
                f'rw: {rw} mandatory: true }}'
            )
        return lines

    def _detect_ca_certs(self) -> tuple[Optional[str], Optional[str]]:
        """Detect the system CA certificate store path (distro-aware).

        Returns (cafile, capath) or (None, None) if none found.
        Detection order: Debian -> Alpine -> Fedora/RHEL.
        """
        # Debian/Ubuntu: /etc/ssl/certs dir + /etc/ssl/certs/ca-certificates.crt file
        if os.path.isdir("/etc/ssl/certs") and os.path.isfile("/etc/ssl/certs/ca-certificates.crt"):
            return "/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/certs"
        # Alpine: /etc/ssl/cert.pem file, no dir
        if os.path.isfile("/etc/ssl/cert.pem"):
            return "/etc/ssl/cert.pem", None
        # Fedora/RHEL: /etc/pki/tls/certs dir + /etc/pki/tls/certs/ca-bundle.crt file
        if os.path.isdir("/etc/pki/tls/certs") and os.path.isfile("/etc/pki/tls/certs/ca-bundle.crt"):
            return "/etc/pki/tls/certs/ca-bundle.crt", "/etc/pki/tls/certs"
        return None, None

    def build(
        self,
        command: str,
        timeout: int,
        shell_env: Optional[dict] = None,
        session_logs_dir: str = "",
    ) -> tuple[str, list[str]]:
        """Generate the nsjail config file and command list for a shell call.

        Args:
            command: Shell command to execute inside the sandbox.
            timeout: Maximum wall-clock execution time in seconds.
            shell_env: Optional environment variables passed as ``-E KEY=VALUE``
                flags to nsjail.
            session_logs_dir: Optional absolute path to session logs directory;
                mounted read-only inside the jail when it exists.

        Returns:
            A tuple of ``(config_path, nsjail_cmd)``.  The config file is a
            temporary ``.cfg`` file created with ``delete=False`` so the caller
            is responsible for cleaning it up.
        """
        system_mounts = self._detect_system_mounts()
        trusted_mounts = self._load_trusted_mounts()
        cgroup = self._cgroup_info

        # Re-verify the cached cgroup path still exists; degrade to rlimits if stale.
        if cgroup["available"] and cgroup["cgroupv2_mount"]:
            if not os.path.isdir(cgroup["cgroupv2_mount"]):
                logger.warning(
                    "nsjail: cached cgroup v2 path no longer exists (%s) — falling back to rlimits",
                    cgroup["cgroupv2_mount"],
                )
                cgroup = {"available": False, "cgroupv2_mount": None}

        clone_newnet = "true" if not self.allow_net else "false"
        memory_bytes = self.memory_mb * 1024 * 1024
        cpu_ms_per_sec = self.cpu_percent * 10

        lines: list[str] = [
            'name: "agent-shell"',
            "mode: ONCE",
            'hostname: "nsjail"',
            "",
            "# Namespaces",
            f"clone_newnet: {clone_newnet}",
            "clone_newuser: true",
            "clone_newns: true",
            "clone_newpid: true",
            "clone_newipc: true",
            "clone_newuts: true",
            "clone_newcgroup: true",
            "",
            "# Seccomp: deferred — no policy applied; isolation relies on namespaces + cgroup.",
            "",
            "# Environment",
            "keep_env: false",
            'envar: "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"',
            'envar: "HOME=/tmp"',
            'envar: "LANG=C.UTF-8"',
            'envar: "TERM=xterm-256color"',
            "",
        ]

        cafile: Optional[str] = None
        capath: Optional[str] = None
        if self.allow_net:
            cafile, capath = self._detect_ca_certs()
            if cafile is not None or capath is not None:
                lines.append("# TLS cert env vars (allow_net=true)")
                if cafile and '"' not in cafile and '\\' not in cafile:
                    lines.append(f'envar: "SSL_CERT_FILE={cafile}"')
                if capath and '"' not in capath and '\\' not in capath:
                    lines.append(f'envar: "SSL_CERT_DIR={capath}"')
                lines.append("")

        lines.extend([
            "# System mounts",
        ])
        lines.extend(system_mounts)

        # Minimal /dev nodes for shell redirections (2>/dev/null, etc.)
        # Paths must be quoted (json.dumps) — nsjail's config parser rejects bare paths.
        lines.append("# Minimal /dev nodes for shell redirections")
        lines.append(
            f'mount: {{ src: {json.dumps("/dev/null")} dst: {json.dumps("/dev/null")}'
            f' is_bind: true rw: false mandatory: false }}'
        )
        lines.append(
            f'mount: {{ src: {json.dumps("/dev/zero")} dst: {json.dumps("/dev/zero")}'
            f' is_bind: true rw: false mandatory: false }}'
        )
        lines.append("")

        if trusted_mounts:
            lines.append("")
            lines.append("# Trusted mounts")
            lines.extend(trusted_mounts)

        lines.extend([
            "",
            "# Session mounts",
            f'mount: {{ src: {json.dumps(self.session_tmpdir)} dst: "/tmp" '
            f'is_bind: true rw: true mandatory: true }}',
            "",
        ])

        if session_logs_dir and os.path.isdir(session_logs_dir):
            lines.append("# Session logs (read-only mount — agent writes outside jail, shell reads inside)")
            lines.append(
                f'mount: {{ src: {json.dumps(session_logs_dir)} dst: {json.dumps(session_logs_dir)}'
                f' is_bind: true rw: false mandatory: false }}'
            )
            lines.append("")

        if self.allow_net and (cafile is not None or capath is not None):
            lines.append("# CA certificate store (read-only, allow_net=true)")
            if capath and os.path.isdir(capath):
                lines.append(
                    f'mount: {{ src: {json.dumps(capath)} dst: {json.dumps(capath)}'
                    f' is_bind: true rw: false mandatory: false }}'
                )
            elif cafile and os.path.isfile(cafile):
                lines.append(
                    f'mount: {{ src: {json.dumps(cafile)} dst: {json.dumps(cafile)}'
                    f' is_bind: true rw: false mandatory: false }}'
                )
            lines.append("")

        skills_mounts: list[str] = []
        if self.skills_dir and os.path.isdir(self.skills_dir):
            if self.skills_dir == "/" or any(
                self.skills_dir == p or self.skills_dir.startswith(p + "/")
                for p in (_BLOCKED_SYSTEM_PREFIXES + self._blocked_user_prefixes)
            ):
                logger.warning(
                    "nsjail: skills_dir rejected (restricted path), skipping mount: %s",
                    self.skills_dir,
                )
            else:
                lines.append("# Skills directory mount (read-only)")
                skills_mounts.append(
                    f'mount: {{ src: {json.dumps(self.skills_dir)} dst: {json.dumps(self.skills_dir)} '
                    f'is_bind: true rw: false mandatory: true }}'
                )
        if skills_mounts:
            lines.extend(skills_mounts)
            lines.append("")

        if cgroup["available"] and cgroup["cgroupv2_mount"]:
            lines.extend([
                "# Resource limits (Tier 1 - cgroup delegation)",
                f"cgroup_mem_max: {memory_bytes}",
                f"cgroup_pids_max: {self.pids_max}",
                f"cgroup_cpu_ms_per_sec: {cpu_ms_per_sec}",
                "use_cgroupv2: true",
                f'cgroupv2_mount: {json.dumps(cgroup["cgroupv2_mount"])}',
            ])
        else:
            logger.warning(
                "nsjail: cgroup v2 delegation unavailable — using rlimits fallback "
                "(rlimit_nproc is user-wide, not per-jail)"
            )
            lines.extend([
                "# Resource limits (Tier 2 - rlimits fallback)",
                f"rlimit_as: {memory_bytes}",
                "rlimit_fsize: 104857600",
                "rlimit_nofile: 256",
                f"rlimit_nproc: {self.pids_max}",
            ])

        lines.extend([
            "",
            f"time_limit: {timeout}",
            "",
            'cwd: "/tmp"',
            "",
            'exec_bin {',
            '  path: "/bin/sh"',
            '  arg: "-c"',
            f'  arg: {json.dumps(command)}',
            '}',
        ])

        cfg_fh = tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False)
        cfg_path = cfg_fh.name
        with cfg_fh:
            cfg_fh.write("\n".join(lines))
            cfg_fh.write("\n")

        nsjail_cmd: list[str] = ["nsjail", "--config", cfg_path]
        for key, value in (shell_env or {}).items():
            nsjail_cmd.append("-E")
            nsjail_cmd.append(f"{key}={value}")

        if cgroup["available"]:
            nsjail_cmd = [
                "systemd-run",
                "--user",
                "--scope",
                "--property=Delegate=yes",
                *nsjail_cmd,
            ]

        return cfg_path, nsjail_cmd
