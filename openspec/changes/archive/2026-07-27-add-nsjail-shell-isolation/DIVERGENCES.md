# Archive Divergence Record

## Change: add-nsjail-shell-isolation
## Archived: 2026-07-27

### Known spec/code divergences (documented at archive time)

These divergences were introduced by security fixes applied during panel review
rounds 1-4, after the spec artifacts were frozen. The code is more conservative
(safer) than the spec describes in all cases.

1. **Adaptive confirm mode**: Spec says "adaptive skips resource category"
   (spec.md:53-54, design.md:116, ADR-0012:23). Code skips "network" category
   when `shell_nsjail_network == "none"` and always confirms "resource"
   (shell.py:70-75). Rationale: `rlimit_nproc` is user-wide, not per-jail, so
   fork bombs are not safely kernel-bounded in the Tier 2 fallback.

2. **Seccomp-bpf filtering**: Spec claims "seccomp-bpf syscall filtering" as an
   active feature (nsjail-shell-sandboxing/spec.md:5, design.md:5). Code defers
   it with a comment: "Seccomp: deferred — no policy applied; isolation relies
   on namespaces + cgroup" (nsjail_config.py:236). No Kafel policy is generated.

3. **trusted_dirs.json path**: Spec references `data/trusted_dirs.json`
   (trusted-dir-management/spec.md, nsjail-shell-sandboxing/spec.md:59). Code
   moved it to `$XDG_STATE_HOME/<agent_name>/trusted_dirs.json` per ADR-0015
   (main.py:225-235). Rationale: the old path was inside the nsjail-mounted
   project dir, creating a sandbox escape vector.

4. **Tier 2 rlimits**: Design says `rlimit_cpu` (design.md:90, tasks.md:20).
   Code uses `rlimit_nproc` instead (nsjail_config.py:279). Rationale:
   `rlimit_cpu` is CPU seconds (not wall-clock); `time_limit` already covers
   wall-clock. `rlimit_nproc` adds PID limiting in the Tier 2 fallback.

5. **HOME envar**: Design says `HOME=/root` (design.md:137). Code sets
   `HOME=/tmp` (nsjail_config.py:247). Rationale: `/root` is blocklisted and
   never mounted; `/tmp` is the session tmpdir, always mounted RW.