"""Dynamic nsjail configuration builder for sandboxed shell execution.

Generates per-call nsjail configuration files and command lists, adapting to the
host Linux environment for system mount layout, cgroup v2 availability, and
resource limit delegation.  The jail's mount table is derived from the frozen
:class:`path_policy.PathPolicy`; the static config (mounts, namespaces, limits)
is generated once per session and reused for every shell call.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from path_policy import PathPolicy

logger = logging.getLogger(__name__)

CGROUP2_SUPER_MAGIC: int = 0x63677270

# Minimal IPv4 address validator — rejects empty/garbage values that would
# produce a non-functional /etc/resolv.conf inside the jail.
_IPV4_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$")


def _is_valid_ipv4(value: str) -> bool:
    """Return True if *value* is a syntactically valid IPv4 address."""
    return bool(_IPV4_RE.match(value))


class NsjailConfigBuilder:
    """Build an nsjail config and command list for a single shell invocation.

    The builder receives a frozen :class:`path_policy.PathPolicy` at construction
    time.  The mount table is derived exclusively from the policy tiers; the
    builder performs no path filtering of its own.  The static parts of the
    nsjail config (namespaces, system mounts, base envars, tier-derived mounts,
    limits) are generated lazily on the first :meth:`build` call and cached for
    the lifetime of the builder.  Only the per-call exec block (command,
    ``time_limit``, ``-E`` env flags) varies between calls.
    """

    def __init__(
        self,
        session_tmpdir: str,
        tmp_dir: str,
        path_policy: "PathPolicy",
        *,
        memory_mb: int = 256,
        pids_max: int = 64,
        cpu_percent: int = 50,
        allow_net: bool = False,
        dns_nameserver: str = "8.8.8.8",
    ) -> None:
        """Initialize the builder.

        Args:
            session_tmpdir: Per-session temporary directory; mounted as ``/tmp``
                inside the sandbox.
            tmp_dir: The agent's default trusted temp/handoff directory
                (``f"/tmp/{agent_name}"``, already resolved and guaranteed to
                exist by agent startup). Bind-mounted read-write at its real
                host path, immediately after the ``/tmp`` scratch mount.
            path_policy: Frozen session-static path policy.  Tier 1 and Tier 2
                entries are mounted read-write (or read-only for skills) inside
                the jail at their real host paths.
            memory_mb: Memory limit in megabytes.
            pids_max: Maximum number of PIDs allowed inside the sandbox.
            cpu_percent: Cgroup CPU limit as a percentage of one CPU.
            allow_net: When False (default) networking is isolated inside the
                sandbox; when True the host network namespace is shared.
            dns_nameserver: Nameserver IP written to ``/etc/resolv.conf`` inside
                the jail when ``allow_net`` is true.  The jail has an isolated
                mount namespace (``clone_newns``), so the host's
                ``/etc/resolv.conf`` is not visible and DNS resolution would
                fail without this entry.  Defaults to ``8.8.8.8``.
        """
        self.session_tmpdir = os.path.realpath(os.path.abspath(session_tmpdir))
        self.tmp_dir = os.path.realpath(os.path.abspath(tmp_dir)) if tmp_dir else ""
        self.path_policy = path_policy
        self.memory_mb = memory_mb
        self.pids_max = pids_max
        self.cpu_percent = cpu_percent
        self.allow_net = allow_net
        if not dns_nameserver or not _is_valid_ipv4(dns_nameserver):
            logger.warning(
                "nsjail: invalid dns_nameserver %r — falling back to 8.8.8.8",
                dns_nameserver,
            )
            self.dns_nameserver = "8.8.8.8"
        else:
            self.dns_nameserver = dns_nameserver
        self._cgroup_info = self._detect_cgroup_capability()
        # Cache for the session-static config text (without the per-call exec
        # block).  Populated lazily by the first build() call.
        self._static_config_path: Optional[str] = None

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

    def _load_static_config(self) -> str:
        """Generate and cache the session-static nsjail config.

        The static config contains everything except the per-call exec block:
        namespaces, environment, DNS/TLS, mounts, and resource limits.  It is
        written to a stable tempfile inside ``session_tmpdir`` so it can be
        reused for every shell call in the session.

        Returns:
            Absolute path to the cached static config file.
        """
        if self._static_config_path is not None and os.path.exists(
            self._static_config_path
        ):
            return self._static_config_path

        system_mounts = self._detect_system_mounts()
        cgroup = self._cgroup_info
        ns_lines = self._build_namespace_lines()
        env_lines = self._build_env_lines()
        dns_tls_lines, cafile, capath = self._build_dns_tls_lines(self.allow_net)
        mount_lines = self._build_mount_lines(
            system_mounts=system_mounts,
            cafile=cafile,
            capath=capath,
        )
        limits_lines = self._build_limits_lines(
            cgroup["available"], cgroup.get("cgroupv2_mount")
        )

        lines: list[str] = [
            'name: "agent-shell"',
            "mode: ONCE",
            'hostname: "nsjail"',
            "",
        ]
        lines.extend(ns_lines)
        lines.extend(env_lines)
        lines.extend(dns_tls_lines)
        lines.extend(mount_lines)
        lines.extend(limits_lines)

        os.makedirs(self.session_tmpdir, mode=0o700, exist_ok=True)
        cfg_fd, cfg_path = tempfile.mkstemp(
            suffix="-static.cfg", dir=self.session_tmpdir, text=True
        )
        with os.fdopen(cfg_fd, "w") as cfg_fh:
            cfg_fh.write("\n".join(lines))
            cfg_fh.write("\n")

        self._static_config_path = cfg_path
        return cfg_path

    def _build_tier_mounts(self) -> list[str]:
        """Return bind-mount lines derived from the PathPolicy tiers.

        Tier 1 entries are emitted in policy order with their designated modes.
        The agent's default temp directory (``tmp_dir``) is skipped if it is
        already covered by the dedicated ``/tmp/<agent>`` system mount, so it is
        never double-mounted.  Tier 2 entries are emitted read-write with
        ``mandatory: true``.  Non-existent Tier 1/2 directories are skipped with
        a debug log (PathPolicy already warned at construction time).  The
        skills directory is skipped silently when it does not exist, per spec.
        """
        lines: list[str] = []
        tmp_dir_real = self.tmp_dir

        # Tier 1: workspace, downloads, tmp, skills, results (in policy order).
        for path, mode in self.path_policy.tier1_entries():
            if not path or not os.path.isdir(path):
                logger.debug("nsjail: tier1 dir does not exist, skipping mount: %s", path)
                continue
            # Avoid double-mounting tmp_dir — the dedicated /tmp/<agent> system
            # mount covers it and is emitted with mandatory: true right after
            # the per-session /tmp scratch mount.
            if mode == "rw" and tmp_dir_real and path == tmp_dir_real:
                continue
            rw = "true" if mode == "rw" else "false"
            lines.append(
                f'mount: {{ src: {json.dumps(path)} dst: {json.dumps(path)} '
                f'is_bind: true rw: {rw} mandatory: true }}'
            )

        # Tier 2: operator-allowed directories (rw-only).
        for path in self.path_policy.tier2_entries():
            if not path or not os.path.isdir(path):
                logger.debug("nsjail: tier2 dir does not exist, skipping mount: %s", path)
                continue
            lines.append(
                f'mount: {{ src: {json.dumps(path)} dst: {json.dumps(path)} '
                f'is_bind: true rw: true mandatory: true }}'
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

    def _build_namespace_lines(self) -> list[str]:
        """Return namespace clone directives and the header comment block."""
        clone_newnet = "true" if not self.allow_net else "false"
        return [
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
        ]

    def _build_env_lines(self) -> list[str]:
        """Return the base environment isolation and injected env vars."""
        return [
            "# Environment",
            "keep_env: false",
            'envar: "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"',
            'envar: "HOME=/tmp"',
            'envar: "LANG=C.UTF-8"',
            'envar: "TERM=xterm-256color"',
            'envar: "TMPDIR=/tmp"',
            'envar: "TMP=/tmp"',
            'envar: "TEMP=/tmp"',
            "",
        ]

    def _build_dns_tls_lines(self, allow_net: bool) -> tuple[list[str], Optional[str], Optional[str]]:
        """Return DNS/TLS-related config lines and detected CA cert paths.

        When *allow_net* is true, injects a minimal ``/etc/resolv.conf`` via
        ``src_content`` and, if a CA store is detected, ``SSL_CERT_*`` env
        vars.  Returns ``(lines, cafile, capath)`` so the CA store can later be
        bind-mounted read-only.
        """
        lines: list[str] = []
        cafile: Optional[str] = None
        capath: Optional[str] = None
        if not allow_net:
            return lines, cafile, capath

        # DNS resolution: the jail has an isolated mount namespace
        # (clone_newns), so the host's /etc/resolv.conf is not visible.
        # Inject a minimal resolv.conf via src_content so tools like curl,
        # git, and Python ssl can resolve hostnames.  See nsjail's
        # configs/telegram.cfg for the canonical pattern.
        resolv_content = f"nameserver {self.dns_nameserver}\n"
        lines.append("# DNS resolution (allow_net=true)")
        lines.append(
            f'mount: {{ src_content: {json.dumps(resolv_content)}'
            f' dst: {json.dumps("/etc/resolv.conf")} }}'
        )
        lines.append("")
        cafile, capath = self._detect_ca_certs()
        if cafile is not None or capath is not None:
            lines.append("# TLS cert env vars (allow_net=true)")
            if cafile and '"' not in cafile and '\\' not in cafile:
                lines.append(f'envar: "SSL_CERT_FILE={cafile}"')
            if capath and '"' not in capath and '\\' not in capath:
                lines.append(f'envar: "SSL_CERT_DIR={capath}"')
            lines.append("")
        return lines, cafile, capath

    def _build_mount_lines(
        self,
        system_mounts: list[str],
        cafile: Optional[str],
        capath: Optional[str],
    ) -> list[str]:
        """Return the full mount section: system, tier-derived, session, and optional mounts."""
        lines: list[str] = [
            "# System mounts",
        ]
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

        tier_mounts = self._build_tier_mounts()
        if tier_mounts:
            lines.append("# Tier-derived mounts")
            lines.extend(tier_mounts)
            lines.append("")

        lines.extend([
            "# Session mounts",
            f'mount: {{ src: {json.dumps(self.session_tmpdir)} dst: "/tmp" '
            f'is_bind: true rw: true mandatory: true }}',
            "# Agent's default trusted temp/handoff directory — system mount,",
            "# not subject to the trusted-dir blocklist, no operator approval needed.",
            f'mount: {{ src: {json.dumps(self.tmp_dir)} dst: {json.dumps(self.tmp_dir)} '
            f'is_bind: true rw: true mandatory: true }}',
            "",
        ])

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

        return lines

    def _build_limits_lines(self, cgroup_available: bool, cgroupv2_mount: Optional[str]) -> list[str]:
        """Return resource limit lines, using cgroup delegation when available."""
        memory_bytes = self.memory_mb * 1024 * 1024
        cpu_ms_per_sec = self.cpu_percent * 10

        if cgroup_available and cgroupv2_mount:
            return [
                "# Resource limits (Tier 1 - cgroup delegation)",
                f"cgroup_mem_max: {memory_bytes}",
                f"cgroup_pids_max: {self.pids_max}",
                f"cgroup_cpu_ms_per_sec: {cpu_ms_per_sec}",
                "use_cgroupv2: true",
                f'cgroupv2_mount: {json.dumps(cgroupv2_mount)}',
            ]

        logger.warning(
            "nsjail: cgroup v2 delegation unavailable — using rlimits fallback "
            "(rlimit_nproc is user-wide, not per-jail)"
        )
        return [
            "# Resource limits (Tier 2 - rlimits fallback)",
            f"rlimit_as: {memory_bytes}",
            "rlimit_fsize: 104857600",
            "rlimit_nofile: 256",
            f"rlimit_nproc: {self.pids_max}",
        ]

    def _build_command_block(self, command: str, timeout: int) -> list[str]:
        """Return the trailing time limit, cwd, and exec_bin block."""
        return [
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
        ]

    def build(
        self,
        command: str,
        timeout: int,
        shell_env: Optional[dict] = None,
    ) -> tuple[str, list[str]]:
        """Generate the nsjail config file and command list for a shell call.

        The session-static parts of the config are generated once and cached; a
        per-call tempfile is created that includes the static config plus the
        command-specific exec block.  The caller is responsible for deleting
        the returned per-call config file.

        Args:
            command: Shell command to execute inside the sandbox.
            timeout: Maximum wall-clock execution time in seconds.
            shell_env: Optional environment variables passed as ``-E KEY=VALUE``
                flags to nsjail.

        Returns:
            A tuple of ``(config_path, nsjail_cmd)``.  The config file is a
            temporary ``.cfg`` file created with ``delete=False`` so the caller
            is responsible for cleaning it up.
        """
        static_path = self._load_static_config()

        cgroup = dict(self._cgroup_info)
        # Re-verify the cached cgroup path still exists; degrade to rlimits if stale.
        if cgroup["available"] and cgroup["cgroupv2_mount"]:
            if not os.path.isdir(cgroup["cgroupv2_mount"]):
                logger.warning(
                    "nsjail: cached cgroup v2 path no longer exists (%s) — falling back to rlimits",
                    cgroup["cgroupv2_mount"],
                )
                cgroup = {"available": False, "cgroupv2_mount": None}

        # The per-call config is the static config plus the command block.
        command_block = self._build_command_block(command, timeout)
        per_call_fh = tempfile.NamedTemporaryFile(
            mode="w", suffix=".cfg", delete=False
        )
        cfg_path = per_call_fh.name
        with per_call_fh:
            with open(static_path, encoding="utf-8") as static_fh:
                shutil.copyfileobj(static_fh, per_call_fh)
            per_call_fh.write("\n".join(command_block))
            per_call_fh.write("\n")

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
