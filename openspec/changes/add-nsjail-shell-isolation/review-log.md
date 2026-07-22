## proposal Round 1 — 2026-07-22

### 🔴 Fixed
- Trusted-mount source of truth contradiction: resolved to use existing `data/trusted_dirs.json` as the single source of truth (no new `shell_nsjail_mounts` config field). Removed `shell_nsjail_mounts` from `config_schema.py` impact. Updated trusted-dir-mounts bullet to explicitly reference `/dir` commands and existing `mode: "rw"`/`mode: "r"` setting.

### 🟡 Addressed
- Env layer 1 (base config `envar`): added explicit mention of base envars (`PATH`, `HOME`, `LANG`, `TERM`) in the environment-variable-isolation bullet.
- Session temp dir lifecycle: added "clean it up at agent shutdown" to `main.py` impact.
- Sub-agent confirmation constraint: added depth-0-only scoping note to the configurable-confirmation-flow bullet.
- `-E` flag override semantics: added note that `-E` flags override config `envar` entries.

### 🔴 Outstanding
- (none)

## proposal Round 2 — 2026-07-22

### 🔴 Fixed
- Trusted-mount source of truth — now fully consistent across all three sections. `shell_nsjail_mounts` removed from config_schema.py impact. `data/trusted_dirs.json` is the single source of truth.

### 🟡 Addressed
- Base envars (PATH, HOME, LANG, TERM) explicitly mentioned in env-isolation bullet.
- Session temp dir lifecycle: "clean it up at agent shutdown" added to main.py impact.
- Sub-agent depth-0 scoping: configurable gate applies only at depth 0; sub-agents fail closed.
- `-E` override semantics documented.

### 🔴 Outstanding
- (none)

### Verdict: Batch passes. Proposal frozen.

## design Round 1 — 2026-07-22

### 🔴 Fixed
- ADR-0011 supersession: design.md Context now explicitly records that this change amends ADR-0011's "shell never auto-approved for the main agent" invariant. Sub-agent fail-closed half preserved.
- `"never"`-mode data-loss risk: added dedicated risk entry for host_escape/project patterns running unconfirmed under `"never"`, with RW bind-mount destruction reaching real host files.

### 🟡 Addressed
- `shell_nsjail_network` field reconciled with network Non-Goal: field toggles net-namespace isolation on/off, distinct from future pasta/loopback connectivity.
- systemd-run per-call overhead: added note about transient scope auto-reaping, ~1-2ms, negligible.
- Per-session /tmp: promoted to Decision 9 with shared-namespace semantics and concurrency note.

### 🔴 Outstanding
- (none)

## design Round 2 — 2026-07-22

### 🔴 Fixed
- ADR-0011 supersession verified as explicitly acknowledged. Amendment precisely scoped — only main-agent half touched, sub-agent fail-closed preserved.
- `"never"`-mode data-loss risk verified as captured with opt-in + default-"always" + system-prompt-warning mitigation.

### 🟡 Addressed
- All three Round 1 minor issues verified as fixed (network field, systemd-run overhead, per-session /tmp decision block).
- Renumbering consistent: 10 decisions, no stale references.

### 🔴 Outstanding
- (none)

### Verdict: Batch passes. Design frozen.

## specs Round 1 — 2026-07-22

### 🔴 Outstanding
- Wrong operation heading: "The built-in tool set includes shell env management tools" was under MODIFIED but is a new requirement. Must be under ADDED.
- never-mode scenario contradicted frozen design: said `rm -rf /` "fails inside the jail" but design says RW-mounted host dirs can be destroyed.
- Stale count: base requirement hardcodes "17 built-in tools"; delta adds 4 (→21) but does not MODIFY that requirement.
- Ambiguous network values: "none"/"off" were confusing.
- never-mode project-category data loss not scenario-covered.

## specs Round 2 — 2026-07-22

### 🔴 Fixed
- Wrong operation heading (R1 #1): split into MODIFIED (dispatch count + confirmation gate) + ADDED (shell_env tools). Both MODIFIED requirements preserve full content.
- never-mode contradiction (R1 #2): host_escape scenario rewritten to state RW-mounted host dirs can be destroyed. Added project-category scenario covering `rm -rf <project>/src/` deleting real host files.

### 🟡 Addressed
- Stale count (R1 #3): dispatch requirement now MODIFIED with "21 built-in tools".
- Ambiguous network values (R1 #4): changed to "none" (isolated) / "host" (shares host network).
- net namespace added to nsjail requirement 1 prose.
- shell-env example uses original host path.

### 🔴 Outstanding
- shell-env scenario self-contradiction: WHEN sets PYTHONPATH="/home/user/projects/myproject/lib" but THEN asserts output "/workspace/lib". Fixed: THEN now matches input.

### Verdict: Batch passes. Specs frozen.

## tasks Round 1 — 2026-07-22

### 🔴 Outstanding
- Resource-limit config generation (cgroup + rlimit entries) had no implementing task. Test 7.4 unbacked.
- Network toggle wiring not tasked.
- keep_env: false not explicit.
- Per-session /tmp persistence test missing.

## tasks Round 2 — 2026-07-22

### 🔴 Fixed
- Resource-limit config generation (R1 blocker): task 2.9 added. Tier 1 (cgroup_mem_max, cgroup_pids_max, cgroup_cpu_ms_per_sec) + Tier 2 (rlimit_as, rlimit_cpu, rlimit_fsize, rlimit_nofile). Backs test 7.4.

### 🟡 Addressed
- Network toggle wiring: task 2.10 added — "none" → clone_newnet: true, "host" → clone_newnet: false.
- keep_env: false: now explicit in task 2.4 static parts.
- Per-session /tmp test: task 7.3 now includes persistence across invocations.
- System-prompt warning for "never" mode: deferred as design mitigation, not a frozen-spec requirement.

### 🔴 Outstanding
- (none)

### Verdict: Batch passes. Tasks frozen.