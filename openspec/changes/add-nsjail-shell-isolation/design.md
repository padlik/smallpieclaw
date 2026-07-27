## Context

The shell built-in tool (`builtin_tools/shell.py`) currently runs commands via `subprocess.Popen(command, shell=True)` or `PtyProcessUnicode.spawn`. Both backends execute as the agent process's own UID/GID with full filesystem access, full network access, and full environment inheritance. The only protections are a regex denylist (`_is_dangerous_shell` in `patterns.py`) and operator confirmation via Telegram inline buttons.

Google's nsjail is a Linux sandboxing tool that uses kernel namespaces (mount, PID, net, user, IPC, UTS, cgroup), seccomp-bpf syscall filtering (via the Kafel policy language), and cgroup v2 resource limits to isolate processes. It was validated on both a leased Ubuntu 22.04 playground (192.168.0.100) and a local Lima VM (Ubuntu 26.04 ARM64, Apple Silicon) with the following confirmed behaviors:

- Real-time stdout streaming (verified: output arrives incrementally, not buffered)
- Exit code fidelity (verified: 0→0, 1→1, 42→42, 126→126, 255→255)
- Timeout enforcement (verified: SIGKILL at time_limit, exit code 137)
- stdout/stderr separation (verified: separate pipes)
- Large output passthrough (verified: 10K lines, 199KB, no truncation)
- RW workspace bind mount (verified: files written in jail appear on host)
- Mount at original host path (verified: deep nested paths work, no path translation needed)
- Per-session /tmp persistence (verified: files persist across separate nsjail invocations)
- Unmounted paths invisible (verified: /etc/shadow, /root/ not found)
- cgroup v2 delegation via systemd-run (verified: hard memory/CPU/PID limits work)
- Dynamic config generation (verified: 0.21ms per call with 20 trusted dirs)
- `-E` flag env injection (verified: overrides config envar, adds new vars)

The existing architecture (ADR-0008: façade + handler-module package) means the shell tool is a handler in `builtin_tools/shell.py` that reads settings from the `BuiltinExecutor` façade. ADR-0010 (zone-based file access) and ADR-0011 (per-prompt approval scope) are in force and constrain how confirmation and trusted directories interact. **This change amends ADR-0011's invariant that "shell is never auto-approved for the main agent"** — the configurable `shell_nsjail_confirm_mode` allows the operator to relax shell confirmation when nsjail sandboxing is active. This supersession is recorded in the ADR step.

## Goals / Non-Goals

**Goals:**
- Add nsjail as a third shell backend (`shell_backend: "nsjail"`) that provides kernel-level isolation for shell commands
- Mount the project directory at its original host path inside the jail (zero path translation)
- Mount trusted directories (from existing `data/trusted_dirs.json`) at their original host paths
- Provide per-session /tmp persistence across nsjail invocations
- Generate nsjail configs dynamically per call (no static config file to maintain)
- Fall back gracefully to rlimits when cgroup v2 delegation is unavailable
- Auto-detect host filesystem layout for portable system mounts
- Make confirmation flow configurable (`always` / `adaptive` / `never`) while preserving backward compatibility
- Isolate environment variables (`keep_env: false`) so secrets don't leak into shell commands
- Provide session-scoped env management via new built-in tools
- Enable macOS development via Lima VM-based integration tests

**Non-Goals:**
- Replacing the subprocess or pty backends — they remain as fallbacks
- Sandboxing non-shell built-in tools (file_read, file_write, etc.) — those run in the agent process
- Sandboxing external `.sh`/`.py` tools (tool_executor.py) — those are being eliminated
- Supporting PTY inside nsjail — the nsjail backend uses subprocess.Popen; ANSI colors and isatty() are not available (real-time streaming IS available)
- Dynamic runtime trusted-dir requests — static config from `trusted_dirs.json` only
- **Network access inside the jail** — `clone_newnet: true` by default (no network). The `shell_nsjail_network` config field toggles the net-namespace isolation on/off. Future pasta/loopback *connectivity* (userland NAT, veth pairs) is out of scope — the field only controls whether the net namespace is created.
- macOS production support — nsjail is Linux-only; macOS uses subprocess fallback

## Decisions

### Decision 1: nsjail as a third shell backend, not a replacement

**Choice**: Add `shell_backend: "nsjail"` alongside `"subprocess"` and `"pty"`.

**Rationale**: The existing backends work and are cross-platform. nsjail is Linux-only. Making it a third backend lets operators opt in and lets the system fall back gracefully when nsjail is unavailable (macOS, missing binary, cgroup issues).

**Alternatives rejected**:
- Replace subprocess entirely — breaks macOS, removes fallback
- Wrap all backends in nsjail — pty + nsjail interaction is untested and complex

### Decision 2: Mount at original host path

**Choice**: Bind-mount the project directory at its original host path (e.g., `/home/user/projects/myproject` → same path inside jail).

**Rationale**: Eliminates path translation. File tools (file_read, file_write) run in the agent process and see host paths. Shell tools run inside nsjail. If both see the same paths, the LLM doesn't need to maintain a path mapping. Verified on playground: deep nested paths (4+ levels) work, nsjail auto-creates parent directory scaffolds.

**Alternatives rejected**:
- Mount at `/workspace` — creates two path namespaces, LLM confusion
- Mount at original path with symlink to `/workspace` — adds complexity for no benefit

### Decision 3: Trusted dirs from existing `data/trusted_dirs.json`

**Choice**: Use the existing `trusted_dirs.json` (managed by `/dir` commands, ADR-0010) as the single source of truth for nsjail mount entries. Each trusted dir with `mode: "rw"` becomes a RW bind mount; `mode: "r"` becomes a RO bind mount.

**Rationale**: Single source of truth. No new config field. The operator already manages trusted dirs via `/dir add`, `/dir list`, `/dir del`. Reusing this list means nsjail mounts automatically reflect operator preferences.

**Alternatives rejected**:
- New `[[agent.shell_nsjail_mount]]` TOML config array — duplicates the trusted dirs concept
- Dynamic runtime requests (agent asks for access, operator approves) — confirmation fatigue, implementation complexity

### Decision 4: Fully dynamic config generation (no base config file)

**Choice**: Generate the nsjail config as a tempfile per shell call. Static parts (namespaces, seccomp, system mounts, base envars) + dynamic parts (time_limit, cwd, project mount, trusted mounts, /tmp mount, command).

**Rationale**: Single source of truth (one Python function). All parts in one file (easy to debug — log the generated config). Trivially fast (0.21ms per call). No file management between calls.

**Alternatives rejected**:
- Base config file + CLI overrides — two sources of truth, CLI flags for mounts are verbose
- Persistent config file updated on trusted-dir changes — stale config risk, more state to manage

### Decision 5: Cgroup delegation via systemd-run with rlimits fallback

**Choice**: Tier 1: `systemd-run --user --scope --property=Delegate=yes` + `use_cgroupv2` + explicit `cgroupv2_mount` pointing to the user's delegated cgroup subtree (`/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service`). Tier 2: rlimits only (`rlimit_as`, `rlimit_cpu`, `rlimit_fsize`, `rlimit_nofile`).

**Rationale**: Playground tests proved that unprivileged cgroup v2 access requires systemd delegation. Direct writes to `/sys/fs/cgroup/` fail with Permission denied. The systemd-run wrapper creates a delegated scope where nsjail can create per-jail cgroups. When systemd or cgroup v2 is unavailable, rlimits provide weaker but universal limits.

**Detection at startup**: Check `shutil.which("systemd-run")` + `statfs(/sys/fs/cgroup)` for `CGROUP2_SUPER_MAGIC` + user cgroup path existence.

**Per-call overhead**: `systemd-run --user --scope` creates a transient systemd scope unit per shell call. The scope is automatically reaped by systemd when the nsjail process exits — no manual cleanup needed. The overhead is negligible (~1-2ms for scope creation) compared to nsjail's fork+exec (~5ms) and the LLM API call latency that dominates the ReAct loop.

**Alternatives rejected**:
- `detect_cgroupv2` alone (no systemd-run) — fails with Permission denied on unprivileged users
- Root-only cgroup access — requires running the agent as root, unacceptable
- No cgroups (rlimits only) — no hard RSS limit, no PID limit, no CPU quota

### Decision 6: System mount auto-detection at startup

**Choice**: At agent startup, detect the host filesystem layout. Mount `/usr` first (covers `/bin`, `/lib`, `/sbin` via symlinks on modern distros). For each of `/bin`, `/sbin`, `/lib`, `/lib64`, `/lib32`: if symlink → `mandatory: false` (nsjail skips if redundant); if real dir → `mandatory: true`; if absent → skip.

**Rationale**: Ubuntu/Debian use symlinks (`/bin → usr/bin`); Alpine/Fedora use real dirs. Bind-mounting `/` first then `/lib` separately fails with `EINVAL` on symlink systems. Auto-detection handles all layouts portably.

**Alternatives rejected**:
- Always mount `/` as read-only root — gives jail visibility into entire host filesystem
- Hardcode for Ubuntu — not portable
- Mount `/` and skip individual dirs — too much filesystem exposure

### Decision 7: Configurable confirmation flow with pattern categories

**Choice**: Add `shell_nsjail_confirm_mode` config field (`"always"` | `"adaptive"` | `"never"`, default `"always"`). Patterns get a category tag. `"always"` confirms all dangerous patterns (backward-compatible). `"adaptive"` skips `resource` patterns (kernel-bounded by cgroup pids_max). `"never"` auto-approves when nsjail is active. Falls back to `"always"` when nsjail is not active. The gate applies only at depth 0 (main agent); sub-agents (depth ≥ 1) fail closed.

**Pattern categories**:
| Category | Patterns | Skip in adaptive? | Rationale |
|---|---|---|---|
| `host_escape` | `rm -rf /`, `dd of=`, `mkfs`, `> /dev/`, `> /etc/`, `> /boot/` | No | Safety net for config bugs |
| `network` | `curl\|sh`, `wget\|sh`, `/dev/tcp/`, `nc -e` | No | Config-dependent (network may be enabled) |
| `resource` | fork bomb | Yes | Kernel-bounded by cgroup pids_max |
| `project` | `rm -rf`, `chmod 777` | No | Can damage project inside jail |
| `policy` | `sudo su` | No | Policy decision, not safety |

**Rationale**: The confirmation flow should not blindly trust the jail config. Only skip confirmation for patterns where the kernel enforces a hard limit regardless of config (fork bomb → cgroup pids_max). Network patterns depend on `clone_newnet: true` being correct — if config is wrong, they're dangerous.

**Alternatives rejected**:
- Full auto-approve in jail (Option 4) — trusts jail config correctness, risky
- Keep confirmation exactly as-is (Option 1) — confirmation fatigue, no benefit from sandboxing
- Path-based confirmation (Option 5) — can't predict which paths a command will touch

### Decision 8: Environment variable isolation with three-layer injection

**Choice**: `keep_env: false` (nsjail default). Three layers:
1. Config `envar` (base, always present): `PATH`, `HOME`, `LANG`, `TERM`. Acts as fallback.
2. `-E` flags (session env, per-call): Agent-side `_shell_env` dict, injected via nsjail `-E KEY=VALUE` CLI flags. Overrides config envars. Managed by new `shell_env_set/unset/list/get` built-in tools.
3. Shell syntax (per-command, ephemeral): `FOO=bar command`. Already works, unchanged.

**Rationale**: Security improvement — shell no longer sees `os.environ` (API keys, tokens). The `-E` flag is the entire mechanism for session env. Verified: `-E` overrides config `envar` and adds new vars on top.

**Alternatives rejected**:
- `keep_env: true` — leaks all secrets into shell, no security improvement
- `src_content` file injection for env — overkill for env vars, `src_content` is for files
- Environment passthrough from agent process — defeats the purpose of isolation

### Decision 9: Per-session /tmp with shared-namespace semantics

**Choice**: A per-session temp directory (created at agent startup, cleaned up at agent shutdown) is bind-mounted as `/tmp` inside every nsjail invocation. Files persist across separate nsjail calls within the same session. Concurrent shell calls share the same `/tmp` namespace — a file written by one call is visible to a concurrent or subsequent call.

**Rationale**: Without this, each nsjail invocation gets a fresh tmpfs `/tmp` that is destroyed when the jail exits. This breaks common agent patterns (write intermediate results to `/tmp`, read them in the next step). The per-session bind mount makes `/tmp` behave like a real machine's `/tmp` — persistent for the session, shared across calls.

**Concurrency**: The agent's ReAct loop is single-threaded for shell calls (one command at a time), so concurrent `/tmp` access is not a concern in practice. Sub-agents run in separate threads but use their own shell calls — if two sub-agents write to the same `/tmp` path simultaneously, last-write-wins. This is acceptable for a temp directory and matches real `/tmp` semantics.

**Alternatives rejected**:
- Fresh tmpfs per call — breaks multi-step patterns that rely on `/tmp` persistence
- Bind-mount host `/tmp` — agent can see other processes' temp files (information leak)
- Per-sub-agent `/tmp` — adds complexity, sub-agents can't share intermediate results

### Decision 10: Lima VM for integration testing on macOS

**Choice**: Lima with VZ backend (Apple Virtualization.framework), Ubuntu 26.04 ARM64. Session-scoped pytest fixture creates/starts VM, provisions nsjail (one-time, persisted), runs tests via `limactl shell`. Temp dirs created inside VM's own ext4 (not virtiofs). VM persists between runs. No macOS directories mounted — disposable temp dir for test artifacts only.

**Rationale**: nsjail needs a real Linux kernel. Lima provides a lightweight VM with VZ backend (near-native performance), virtiofs file sharing, and CLI-driven operation. Ubuntu 26.04 matches the playground (systemd, cgroups v2, user delegation). The AppArmor user namespace restriction on Ubuntu 26.04 requires a one-time sysctl fix (`kernel.apparmor_restrict_unprivileged_userns=0`), persisted via `/etc/sysctl.d/99-nsjail.conf`.

**Alternatives rejected**:
- Docker — user explicitly rejected
- OrbStack — shared-kernel (not a true VM), blocks bpf() syscalls (breaks seccomp), closed source
- Alpine — no systemd, cgroup delegation doesn't work for unprivileged users
- Multipass — QEMU-only (no VZ), Ubuntu-only, security vulnerabilities
- Tart — CI-focused, no Alpine, 20GB minimum disk

## Risks / Trade-offs

- **[nsjail binary not installed]** → Mitigation: runtime detection (`shutil.which("nsjail")`), graceful fallback to subprocess, clear error message in logs
- **[AppArmor blocks user namespaces on Ubuntu 24.04+]** → Mitigation: one-time sysctl fix, documented in provisioning script, detected at startup with actionable error message
- **[cgroup v2 delegation unavailable]** → Mitigation: tiered fallback to rlimits, log warning that resource limits are weaker
- **[Signal exit code anomaly]** → `kill -9 $$` inside jail returns 0 (not 137). Shell race condition, not nsjail bug. → Mitigation: accepted as known behavior; error classification relies on exit code + stderr text, not signal detection
- **[No PTY/colors in nsjail backend]** → The nsjail backend uses subprocess.Popen, not PtyProcessUnicode. `isatty()` returns false, no ANSI colors. → Mitigation: real-time streaming still works (verified); `shell_streaming` feature works via nsjail's stdout passthrough; operators who need colors can use the pty backend (without sandboxing)
- **[Config bug leaks dangerous commands]** → If nsjail config is wrong (e.g., network enabled), `"adaptive"` confirmation mode could skip network patterns. → Mitigation: `"adaptive"` only skips `resource` patterns (kernel-bounded); network/host_escape patterns always confirm; `"always"` (default) confirms everything
- **[`"never"` mode removes host data-loss safety net]** → Under `confirm_mode = "never"`, `host_escape`/`project` patterns (`rm -rf /`, `rm -rf <project>`, `chmod 777`) run with no confirmation. Because the project and trusted RW dirs are bind-mounted at their original host paths (Decisions 2–3), destruction propagates to real host files — the jail does not protect RW-mounted host directories. → Mitigation: `"never"` is opt-in and operator must explicitly set it; default is `"always"`; operators who set `"never"` accept that the jail contains network/resource risks but NOT that it protects RW-mounted host dirs from deletion. The system prompt should warn the LLM about this when `"never"` is active.
- **[Trusted dirs change after startup]** → If operator adds/removes trusted dirs via `/dir` during a session, the nsjail config builder needs the updated list. → Mitigation: read trusted dirs at call time (not cached at startup), or reload on `/dir` command
- **[macOS development without nsjail]** → nsjail is Linux-only. macOS developers can't test the nsjail backend locally (except via Lima VM). → Mitigation: Lima VM integration tests; subprocess backend works on macOS for non-sandboxed testing

## Migration Plan

1. **No breaking changes**: `shell_backend` defaults to `"subprocess"` (unchanged). `shell_nsjail_confirm_mode` defaults to `"always"` (unchanged confirmation behavior). All new features are opt-in.
2. **Operator enables nsjail**: Set `shell_backend = "nsjail"` in config. Install nsjail binary. Apply AppArmor fix if needed.
3. **Operator configures trusted dirs**: Use existing `/dir add` command. Trusted dirs automatically become nsjail mounts.
4. **Operator tunes confirmation mode**: Optionally set `shell_nsjail_confirm_mode = "adaptive"` to reduce confirmation fatigue.
5. **Rollback**: Set `shell_backend = "subprocess"` — all nsjail features are bypassed, no code changes needed.

## Open Questions

1. **Trusted dirs reload timing**: Should the `NsjailConfigBuilder` read `data/trusted_dirs.json` at every shell call (always fresh, slight I/O) or cache and reload on `/dir` command (faster, needs invalidation hook)? Leaning toward read-at-call-time for simplicity — the file is small and `json.load()` is fast.
2. **PTY inside nsjail**: Not tested. If operators need both sandboxing AND PTY features (colors, isatty), a future investigation could test nsjail's `--pass_fd` with a PTY pair. For now, it's a known trade-off.
3. **`shell_env_set` with vault secrets**: Should `shell_env_set` accept `sec:KEY` values resolved from the vault (like config string expansion)? This would let the agent inject API tokens into shell commands safely. Leaning toward yes — but it's a separate concern that could be added later.