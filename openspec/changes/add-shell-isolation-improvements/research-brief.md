# Research Brief: Shell Isolation Improvements

## Explored

Two improvement ideas for the nsjail shell isolation backend:
1. Add a boolean `allow_net` config switch (instead of the string `shell_nsjail_network`)
2. Mount `skills/` and `shell_logs/` inside the jail by default

## Key Findings

### Current Architecture
- `shell_nsjail_network` already exists in `AgentConfig` as `"none"` | `"host"` (string enum)
- `NsjailConfigBuilder` reads this and sets `clone_newnet: true/false` in the generated nsjail .cfg
- Confirmation gate `_should_confirm(category="network")` adapts based on `shell_nsjail_network == "none"`
- Default mounts: `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, project_dir (RW), session_tmpdir as `/tmp` (RW)
- Trusted mounts come from `trusted_dirs.json` (user-configured, loaded at build time)

### `skills/` Directory
- **Not mounted** inside the jail today
- System prompt explicitly tells the LLM to resolve skill scripts against `skills/<name>/`
- Commands like `cd <skill_dir> && ./scripts/run.sh` fail inside nsjail with "No such file or directory"
- `TrustedZoneChecker` already treats `skills_dir` as a trusted zone for file access control, but nsjail doesn't know about it
- **Decision**: Mount `skills_dir` read-only in the nsjail config

### `shell_logs/` Directory
- Used **only** as an internal overflow bucket when shell output exceeds `max_output`
- Full text is saved to `data/shell_logs/shell-<timestamp>-<random>.log`
- No tool reads from this directory. No inter-script exchange happens here.
- **Decision**: Do NOT mount — it's internal-only and offers no value inside the sandbox

### `downloads/` Directory
- Already in `TrustedZoneChecker._default_trusted_dirs` (via `paths_config.downloads_dir`)
- But NOT mounted by nsjail unless added to `trusted_dirs.json`
- **Out of scope for this change** — considered but not included

## Decisions Made

1. **Replace `shell_nsjail_network` with `allow_net: bool`** — boolean is more intuitive. Breaking change to config schema.
2. **Mount `skills_dir` read-only** in `NsjailConfigBuilder` when the directory exists and is outside the project directory (project_dir is already mounted RW).
3. **Do NOT mount `shell_logs/`** — internal overflow, no inter-script use.

## Open Questions Answered

- Should we keep both config fields? → No, replace with boolean for clarity.
- Should `skills_dir` be mounted even when nested under project_dir? → No, it's already accessible via the project mount.
- Is `shell_logs` used for script exchange? → No. Verified by grep across entire codebase.
