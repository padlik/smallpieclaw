# Explore Brief: add-nsjail-shell-isolation

## Alternatives Rejected

1. **bubblewrap (bwrap)** — simpler CLI, widely deployed (Flatpak), but no built-in seccomp policy language (Kafel). nsjail's Kafel seccomp is a key advantage for syscall filtering.
2. **Firejail** — desktop-focused, heavier, X11/Wayland integration we don't need.
3. **gVisor/runsc** — user-space kernel, 5-15% syscall overhead, designed for multi-tenant services. Overkill for short-lived single commands.
4. **Firecracker microVM** — full kernel isolation, ~125ms boot, requires KVM. Too heavy for per-command sandboxing.
5. **Docker/Podman** — OCI container ecosystem, ~100-500ms boot, image management overhead. User explicitly rejected Docker.
6. **Mount at `/workspace` (not original path)** — rejected because it creates two path namespaces (file tools see host paths, shell sees `/workspace`), causing LLM confusion. Mount at original host path eliminates path translation entirely.
7. **Static config file + CLI overrides** — rejected in favor of fully dynamic config generation. Two sources of truth (file + flags) add complexity. Dynamic generation is 0.21ms per call, trivially simple.
8. **Alpine as VM distro** — rejected because Alpine uses OpenRC, not systemd. cgroups v2 user delegation requires systemd. Ubuntu 24.04+ matches the playground exactly.
9. **OrbStack** — shared-kernel (not a true VM), blocks bpf() syscalls in isolated mode (would break nsjail's seccomp-bpf), closed source, $8/mo commercial.
10. **Full auto-approve confirmation (Option 4)** — rejected because it trusts the jail config to be correct. If config has a bug (network accidentally enabled), dangerous commands slip through. Configurable confirmation mode is safer.

## Final Approach: Key Decisions

### nsjail backend (`shell_backend: "nsjail"`)

Third shell backend alongside `"subprocess"` and `"pty"`. The `_run_shell_nsjail()` method reuses the exact same select() loop, output truncation, artifact logging, and error classification as `_run_shell_subprocess`. Only the `subprocess.Popen` command changes.

### Mount at original host path

Project directory is bind-mounted at its original host path inside the jail (e.g., `/home/user/projects/myproject` → same path inside jail). This eliminates path translation — file tools and shell tools see identical paths. nsjail's `buildMountTree` auto-creates parent directory scaffolds.

### Trusted directories

User-configurable additional mounts, each at their original host path. Config via TOML:
```toml
[[agent.shell_nsjail_mount]]
host_path = "/home/user/.cache/pip"
mode = "rw"  # or "ro"
```
Each entry becomes one `mount { src: "..." dst: "..." is_bind: true rw: ... }` line in the generated config.

### Per-session /tmp

A per-session temp directory (created at agent startup) is bind-mounted as `/tmp` inside every jail. Files persist across nsjail invocations within a session. Cleaned up at agent shutdown.

### Dynamic config generation (fully dynamic, no base file)

Config generated per shell call as a tempfile. Static parts (namespaces, seccomp, system mounts) + dynamic parts (time_limit, cwd, project mount, trusted mounts, /tmp mount, command). ~50 lines of Python, 0.21ms per call.

### Cgroup delegation: tiered fallback

- **Tier 1**: `systemd-run --user --scope --property=Delegate=yes` + `use_cgroupv2` + explicit `cgroupv2_mount` pointing to user's delegated cgroup subtree. Hard memory/CPU/PID limits. Proven on playground and Lima VM.
- **Tier 2 (fallback)**: rlimits only (`rlimit_as`, `rlimit_cpu`, `rlimit_fsize`, `rlimit_nofile`). No hard RSS limit, no PID limit, no CPU quota. Works everywhere, no cgroup access needed.
- Detection at startup: check `systemd-run` availability + cgroup v2 + user cgroup path.

### System mount auto-detection

At agent startup, detect host filesystem layout:
- Mount `/usr` first (covers `/bin`, `/lib`, `/sbin` via symlinks on modern distros)
- For each of `/bin`, `/sbin`, `/lib`, `/lib64`, `/lib32`: if symlink → `mandatory: false` (nsjail skips if redundant); if real dir → `mandatory: true`; if absent → skip.

### Confirmation flow: configurable mode

```toml
shell_nsjail_confirm_mode = "always"  # default, backward-compatible
# "adaptive" — skip confirmation for jail-bounded patterns (fork bomb)
# "never" — auto-approve in jail
```

Patterns get a category tag: `host_escape`, `network`, `resource`, `project`, `policy`.
- `always` (default): confirm all dangerous patterns (current behavior, unchanged)
- `adaptive`: skip `resource` category (kernel-bounded by cgroup pids_max)
- `never`: skip all patterns when nsjail is active
- When nsjail is NOT active (fallback to subprocess): always uses `always` mode

### Environment variables: three-layer injection

1. **Config `envar`** (base, always present): `PATH`, `HOME`, `LANG`, `TERM`. Set in config template. Acts as fallback.
2. **`-E` flags** (session env, per-call): Agent-side `_shell_env` dict, injected via nsjail `-E KEY=VALUE` CLI flags. Overrides config envars. New built-in tools: `shell_env_set`, `shell_env_unset`, `shell_env_list`, `shell_env_get`.
3. **Shell syntax** (per-command, ephemeral): `FOO=bar command`. Already works, unchanged.

`keep_env: false` (nsjail default) — shell does NOT inherit agent's `os.environ`. Security improvement: API keys, tokens are not visible inside the jail. Only explicitly injected vars are visible.

Verified: `-E` flags override config `envar` entries and add new vars on top.

### Test infrastructure: Lima VM

- **Tool**: Lima with VZ backend (Apple Virtualization.framework), Ubuntu 26.04 ARM64
- **Path**: `~/` auto-mounted at same path via virtiofs (read-only — fine for tests, which use VM-local temp dirs)
- **Provisioning** (one-time, persisted in VM): apt-get install build deps + build nsjail from source + AppArmor fix (`kernel.apparmor_restrict_unprivileged_userns=0` persisted via sysctl.d)
- **Test flow**: pytest session fixture creates/starts VM, provisions nsjail, runs tests via `limactl shell`. Temp dirs created inside VM's own ext4 (not virtiofs). VM persists between runs.
- **No macOS dirs mounted** — test artifacts copied in/out via the mount or `limactl copy`. Disposable temp dir, deleted after tests.

## Cross-Module Data Flows

```
main.py
  → AgentConfig (new fields: shell_backend, shell_nsjail_*, shell_nsjail_mounts)
  → BuiltinExecutor.__init__(shell_backend, shell_nsjail_confirm_mode, ...)
    → NsjailConfigBuilder(project_dir, trusted_dirs, session_tmp, use_cgroups, user_cgroup)
      → _detect_system_mounts() [once at startup]
      → build(command, timeout) → (cfg_path, nsjail_cmd) [per call]

ShellTools._exec_shell(args)
  → _is_dangerous_shell(command) → (dangerous, reason, category) [3-tuple now]
  → _should_confirm(category) → bool [new, checks confirm_mode + nsjail_active]
  → _run_shell(args)
    → if backend == "nsjail": _run_shell_nsjail(args)
      → NsjailConfigBuilder.build(command, timeout) → (cfg_path, cmd)
      → subprocess.Popen(cmd, ...) [same select loop as subprocess backend]
      → -E flags from _shell_env dict
    → elif backend == "pty": _run_shell_pty(args) [unchanged]
    → else: _run_shell_subprocess(args) [unchanged]

New built-in tools (shell_env_set/unset/list/get):
  → Modify/read BuiltinExecutor._shell_env dict
  → No IPC, no files, no subprocess
```

## Open Questions

1. **Signal exit code anomaly**: `kill -9 $$` inside jail returns exit 0 (not 137). Shell race condition between kill() and exit(). Not nsjail-specific. Normal exits and timeout kills are perfect. Accepted as known behavior — not a blocker.

2. **PTY inside nsjail**: Not tested. The nsjail backend uses subprocess.Popen (not PtyProcessUnicode). The `shell_streaming` feature (live output to progress panel) works because nsjail streams child stdout to its own stdout in real-time (verified). PTY-specific features (ANSI colors, isatty()) are not available in the nsjail backend. Acceptable tradeoff — the agent gets real-time streaming without colors.

3. **Confirmation flow interaction with sub-agents**: Sub-agents (caller_depth >= 1) already fail closed for shell commands. The `_should_confirm` gate only applies to depth 0 (main agent). Sub-agent behavior is unchanged.