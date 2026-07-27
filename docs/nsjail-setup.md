# Nsjail Shell Backend Setup

nsjail is Google's Linux sandboxing tool, built on kernel namespaces and cgroup v2 (seccomp-bpf filtering is not currently applied by this agent). The agent can use it as a shell backend (`shell_backend = "nsjail"`) to run shell commands inside a kernel-level sandbox.

nsjail is **Linux-only**. On macOS the agent falls back to the subprocess backend. Minimum supported version: **v2.9** (cgroup v2 support). Recommended version: **v3.6** (latest stable tag).

## Prerequisites (all methods)

- Linux host (Ubuntu 22.04+ or similar with systemd, cgroup v2)
- Kernel >= 4.6 (required for `CLONE_NEWCGROUP`); >= 5.3 recommended
- For Ubuntu 24.04+: AppArmor restricts unprivileged user namespaces. Apply this one-time fix:
  ```bash
  echo "kernel.apparmor_restrict_unprivileged_userns=0" | sudo tee /etc/sysctl.d/99-nsjail.conf
  sudo sysctl -p /etc/sysctl.d/99-nsjail.conf
  ```

## Method 1: Build from source on a Linux host

1. Install build dependencies:
   ```bash
   sudo apt-get update
   sudo apt-get install -y autoconf bison flex libprotobuf-dev libnl-route-3-dev protobuf-compiler pkg-config build-essential git
   ```

2. Clone and build (pinned to v3.6):
   ```bash
   cd /tmp
   git clone --depth=1 --branch 3.6 https://github.com/google/nsjail.git
   cd nsjail
   make -j$(nproc)
   sudo cp nsjail /usr/local/bin/nsjail
   ```

3. Verify:
   ```bash
   nsjail --help | head -1
   ```

4. Enable in the agent config:
   ```toml
   [agent]
   shell_backend = "nsjail"
   shell_nsjail_confirm_mode = "always"  # or "adaptive" or "never"
   shell_nsjail_memory_mb = 256
   shell_nsjail_pids_max = 64
   shell_nsjail_cpu_percent = 50
   shell_nsjail_network = "none"
   ```

5. Verify cgroup v2 delegation is working:
   ```bash
   # Check cgroup v2 is mounted
   stat -f -c '%T' /sys/fs/cgroup
   # Should output: cgroup2
   
   # Check systemd-run is available
   which systemd-run
   ```

If cgroup v2 delegation is unavailable, nsjail falls back to rlimits. rlimits provide weaker isolation: no CPU quota, and ``rlimit_nproc`` limits are user-wide rather than per-jail. A warning is logged in this case.

## Method 2: Docker container

nsjail does not have an official Docker image. Build a simple image from Ubuntu:

```dockerfile
FROM ubuntu:26.04
RUN apt-get update && apt-get install -y \
    autoconf bison flex libprotobuf-dev libnl-route-3-dev \
    protobuf-compiler pkg-config build-essential git \
    && rm -rf /var/lib/apt/lists/*
RUN cd /tmp && git clone --depth=1 --branch 3.6 https://github.com/google/nsjail.git \
    && cd nsjail && make -j$(nproc) && cp nsjail /usr/local/bin/ && rm -rf /tmp/nsjail
RUN echo "kernel.apparmor_restrict_unprivileged_userns=0" > /etc/sysctl.d/99-nsjail.conf
ENTRYPOINT ["nsjail"]
```

Build and sanity-check:
```bash
docker build -t nsjail .
docker run --rm nsjail --help | head -1
```

**Important:** Running nsjail inside Docker requires `--privileged` or specific capabilities such as `CAP_SYS_ADMIN` and `CAP_NET_ADMIN`, because nsjail creates kernel namespaces. The agent process itself should run on the host and invoke the nsjail binary directly. The Docker image is mainly useful for building or porting the binary, not for running the agent inside it.

To extract the binary from the image for host use:
```bash
docker create --name nsjail-tmp nsjail
docker cp nsjail-tmp:/usr/local/bin/nsjail /usr/local/bin/nsjail
docker rm nsjail-tmp
```

## Method 3: Lima VM (macOS development/testing)

Use this path to run the nsjail integration test suite on macOS. The test infrastructure auto-provisions the VM.

1. Install Lima:
   ```bash
   brew install lima
   ```

2. Run the nsjail integration tests:
   ```bash
   pytest tests/nsjail/ -v
   ```

3. Manual VM management:
   ```bash
   # List VMs
   limactl list
   
   # Stop/start the test VM
   limactl stop nsjail-test
   limactl start nsjail-test
   
   # Shell into the VM
   limactl shell nsjail-test bash
   
   # Delete and recreate (forces a fresh nsjail build)
   limactl delete nsjail-test
   ```

The VM uses the VZ backend (Apple Virtualization.framework), Ubuntu 26.04 ARM64, 2 CPUs, 2 GiB RAM, and 10 GiB disk. It persists between test runs.

## Configuration reference

| Field | Default | Description |
|---|---|---|
| `shell_backend` | `"subprocess"` | Set to `"nsjail"` to enable sandboxing |
| `shell_nsjail_confirm_mode` | `"always"` | Confirmation mode: `"always"`, `"adaptive"`, `"never"` |
| `shell_nsjail_memory_mb` | `256` | Memory limit in MB (cgroup) or `RLIMIT_AS` (rlimits fallback) |
| `shell_nsjail_pids_max` | `64` | Max PIDs inside the jail (cgroup only) |
| `shell_nsjail_cpu_percent` | `50` | CPU quota as a percentage of one core (cgroup only) |
| `shell_nsjail_network` | `"none"` | `"none"` = isolated (no network), `"host"` = share host network |

## Confirmation modes

- `"always"` (default): Confirm all dangerous shell patterns. Backward-compatible with the subprocess backend.
- `"adaptive"`: Skip confirmation for `network`-category patterns (`curl|sh`, `wget|sh`, `/dev/tcp`, `nc -e`) when network isolation is active (`shell_nsjail_network = "none"`). All other categories (`host_escape`, `project`, `resource`, `policy`) still require confirmation.
- `"never"`: Skip all confirmation while nsjail is active. **Warning:** RW-mounted host directories such as the project directory and RW trusted dirs are **not** protected by the jail. A command like `rm -rf` on a project path will delete real host files. Use with caution.

When nsjail is inactive (binary missing, non-Linux host), all modes fall back to `"always"`.

## Trusted directories and XDG state location

The trusted-directory store (`trusted_dirs.json`) doubles as the nsjail mount list: every user-added trusted directory is bind-mounted at its original host path inside the jail (RW entries read-write, `r` entries read-only). Because this file influences sandbox configuration, it **must reside outside the sandbox's write scope** — otherwise a jailed command could overwrite it and inject arbitrary host paths as trusted mounts on the next `build()` call (see ADR-0015).

The store is kept under the XDG state directory, never inside the project dir:

```
~/.local/state/<agent_name>/trusted_dirs.json
```

Resolved at startup in `main.py` from `XDG_STATE_HOME` (defaulting to `~/.local/state`) joined with `agent_name`. The path is passed to both `TrustedZoneChecker` (file-access-zone enforcement) and `NsjailConfigBuilder` (mount generation); neither reads the environment directly.

- A one-time migration copies `data/trusted_dirs.json` → the XDG location on first start if the new path does not yet exist.
- The agent installation directory (`_AGENT_DIR`) and XDG state/data dirs are on the trusted-mount blocklist, so no `trusted_dirs.json` entry can re-expose them.
- Managed at runtime via the `/dir` command: `/dir list`, `/dir del <n>`, `/dir reload`.

Operators who override `XDG_STATE_HOME` (e.g. container deployments) get the new location automatically; there is no separate `nsjail_state_dir` config key.

## Troubleshooting

- **nsjail binary not found**: The agent logs a warning and falls back to the subprocess backend. Install nsjail or set `shell_backend = "subprocess"`.
- **Permission denied on cgroup writes**: cgroup v2 delegation is not set up. The agent falls back to rlimits. For full limits, ensure systemd user delegation is available.
- **AppArmor blocks user namespaces (Ubuntu 24.04+)**: Apply the sysctl fix shown in Prerequisites.
- **`clone_newcgroup` fails on old kernels**: Kernel >= 4.6 is required. Upgrade the kernel or use the subprocess backend.

## Version compatibility note

The agent uses text-format nsjail config files (field names, not protobuf wire numbers), so config compatibility across nsjail versions is not a concern. Minimum version is **v2.9** for cgroup v2 support. **v3.6** is recommended and is the version pinned in the test infrastructure.
