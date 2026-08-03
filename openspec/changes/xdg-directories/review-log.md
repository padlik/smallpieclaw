## proposal Round 1 — 2026-08-02

**Batch:** proposal (first batch)
**Frozen artifacts:** none
**Baseline:** `explore-brief.md`

### 🔴 Fixed
- `workspace_dir` undefined — clarified as a remaining `[paths]` config parameter (default `~/Documents`)
- `skills-dir-sandbox-mount` missing from Modified Capabilities — added

### 🟡 Fixed
- Scheduler state filenames not enumerated — all four named explicitly
- `agent-scoped-directories` not in Impact/Modified Capabilities — added
- PathsConfig count 9 → corrected to 8
- `agent.jsonl` missing — added alongside `agent.log`
- `migrate.py --source` flag — added
- Non-goals section — added

---

## proposal Round 2 — 2026-08-02

**Batch:** proposal (second round)
**Frozen artifacts:** none
**Baseline:** `explore-brief.md`

### 🔴 Outstanding
None.

### 🟡 Fixed
- `config_schema.py` Impact missing `AgentConfig.agent_home` removal — added
- `migrate.py` CLI missing `--agent-name` — added to "What Changes" and capability description
- `agent-scoped-directories` Modified Capability missing `log_file` scenario coverage — added
- `nsjail-shell-sandboxing` was spurious in Modified Capabilities — removed (covered by `skills-dir-sandbox-mount`)

### ⚖️ Verdict
**Proposal frozen. Ready to proceed to design.**

---

## design Round 1 — 2026-08-02

**Batch:** design (first round)
**Frozen artifacts:** `proposal.md`
**Baseline:** `explore-brief.md`

### 🔴 Fixed
- `migration_sentinel` as `@property` unimplementable — replaced with `migration_sentinel_exists()` / `write_migration_sentinel()` helpers using `state_home.glob()`
- Downloads path in data flow wrong — corrected to `Path(workspace_dir).expanduser() / "downloads"`
- "All copies are non-destructive" contradicted `tool_index.json` deletion — prose qualified

### 🟡 Fixed
- `--agent-name` required/default contradiction — clarified as `required=True` with no default
- `--dry-run` missing from CLI — added
- `log_file` / `log_jsonl` absent from `XDGPaths` — added as explicit leaf fields
- `agent-scoped-directories` `log_file` scenario not flagged — spec impact note added
- `SPC_VAULT_FILE` / `SPC_LOG_DIR` fate unspecified — explicitly retired

---

## design Round 2 — 2026-08-02

**Batch:** design (second round)
**Frozen artifacts:** `proposal.md`
**Baseline:** `explore-brief.md`

### 🔴 Fixed
- `runtime_dir` code snippet missing `/ agent_name` — fixed to `Path(runtime_env) / agent_name`

### 🟡 Fixed
- `_create_xdg_dirs` "runtime_dir parent" → "runtime_dir itself"
- Spec impact note incomplete — all invalidated scenarios for `agent-scoped-directories` and `skills-dir-sandbox-mount` enumerated
- `data_dir` alias of `data_home` ambiguous — `data_dir` removed; all callers use `data_home`
- `_warn_relative_paths` heuristic too broad — tightened to values starting with `.` only
- `--dry-run` sentinel behavior unspecified — explicit note added
- Systemd unit section absent — brief section added
- `datetime.utcnow()` deprecated — replaced with `datetime.now(timezone.utc)`

### ⚖️ Verdict
**Design frozen. Ready to proceed to specs.**

---

## design Round 4 — 2026-08-03

**Batch:** design (Round 4 — Round 3 fixes verified)
**Frozen artifacts:** `proposal.md`
**Baseline:** `explore-brief.md`

> Note: Round 3 entry is absent from this log (review log showed "Design frozen" at Round 2, then Round 3 fixes were applied). Round 3 fixes are confirmed applied in the current `design.md`; this entry records the Round 4 review.

### ✅ Round 3 Fixes Confirmed

- `graph_memory_db` rename applied at all three locations (XDGPaths dataclass, config_schema removal table, Full XDG Path Table)
- Migration table `graph_memory*` destination corrected to `paths.data_home / <filename>` with per-file copy semantics
- 6th invalidated scenario ("Explicit agent home does NOT affect log location") added to `agent-scoped-directories` spec impact notes

### 🔴 Outstanding

None.

### 🟡 Found

- **`_create_xdg_dirs` mkdir strategy for `runtime_dir` unspecified** — prose states parent must not be created but does not say `parents=False`; in containers/CI where `$XDG_RUNTIME_DIR` is set but `/run/user/` does not exist, `parents=True` silently causes a `PermissionError`; `tmp_xdg` fixture masks this in tests.
- **`agent-scoped-directories` spec impact notes incomplete — 5 scenarios and 2 Rule texts unaddressed** — spec has 11 scenarios; design addressed only 6. Unaddressed: vault migration scenarios ("Vault migrated from old XDG_DATA_HOME location", "Both old and new vault paths exist"); log location scenarios ("Default log location for default agent", "Custom agent name derives log location", "Logs are no longer written into the source checkout"); Rule texts in Requirements 1 and 2 referencing retired `agent_home` and removed `log_file` override.
- **`logs_dir` missing from Full XDG Path Table** — `logs_dir` is a named `XDGPaths` field and created in `_create_xdg_dirs` but had no table row.

### 💡 Optional Found

- `_warn_relative_paths` description "starts with `.` but does not start with `/` or `~`" is logically redundant.

### 🔧 Round 4 Fixes Applied

- `_create_xdg_dirs`: added `parents=False, exist_ok=True` for `runtime_dir`; `parents=True, exist_ok=True` for all other dirs
- `agent-scoped-directories` spec impact notes: added 2 more invalidated scenarios (vault migration); added 3 update scenarios (log location); added Rule text update notes for Requirements 1 and 2; updated Purpose line; added vault `secrets.toml` row to `migrate.py` migration steps
- Full XDG Path Table: added `logs/` row with `logs_dir` field before `logs/agent.log`
- Optional: `_warn_relative_paths` simplified to "starts with `.`"

---

## design Round 5 — 2026-08-03

**Batch:** design (Round 5 — Round 4 fixes verified)
**Frozen artifacts:** `proposal.md`
**Baseline:** `explore-brief.md`

### ✅ Round 4 Fixes Confirmed

All 4 Round 4 fixes verified present and correct:
- `_create_xdg_dirs`: `parents=False, exist_ok=True` for `runtime_dir`; `parents=True` for all other dirs
- `agent-scoped-directories` spec impact notes: all 11 scenarios accounted for (8 invalidated + 3 updated); Rule text updates for Requirements 1 and 2; updated Purpose line
- Full XDG Path Table: `logs/` row with `logs_dir` present before `logs/agent.log`
- Optional: `_warn_relative_paths` simplified

### 🔴 Outstanding

None.

### 🟡 Found

- **`secrets.toml` migration step contradicts explore-brief baseline** — explore-brief explicitly marks `secrets.toml` as "already here; no migration needed" (vault was already at `$XDG_STATE_HOME/<name>/secrets.toml` in the old layout). Round 4 added the row unnecessarily; also lacked a source-not-exists guard.

### 🔧 Round 5 Fix Applied

- Removed `<source>/secrets.toml → paths.secrets_file` row from migration table
- Added clarifying note: "secrets.toml was already at `$XDG_STATE_HOME/<name>/secrets.toml` in the old layout — no migration step needed"

### ⚖️ Verdict

**Design frozen. Ready to proceed to specs.**

---

## specs Round 1 — 2026-08-03

**Batch:** specs (Round 1)
**Frozen artifacts:** `proposal.md`, `design.md`
**Baseline:** `explore-brief.md`

### 🔴 Found & Fixed

- **`secrets.toml` conflict in `design.md`** — spec impact notes at line 284 still said "replace with a `migrate.py`-based vault migration scenario" referencing the removed `<source>/secrets.toml` migration row; also a duplicate vault note at lines 198/200. Fixed: merged duplicate notes; updated bullet to "remove this scenario without replacement"

### 🟡 Found & Fixed

- **`xdg-path-resolution`**: "Default XDG paths" scenario missing 5 assertions (`secrets_file`, `scheduler_state`, `scheduler_commands`, `scheduler_jobs`, `job_execution_log`) — added all 5 `AND` clauses
- **`agent-xdg-launch`**: `_AGENT_DIR` called "environment variable" (it's a module-level constant) — corrected; also missing `downloads_dir` requirement — added requirement + 2 scenarios covering default and custom `workspace_dir`
- **`agent-scoped-directories`**: Default log scenario GIVEN referenced removed `log_file` field — removed stale GIVEN clause
- **`skills-dir-sandbox-mount` (optional)**: "configured `skills_dir`" → "XDG-derived `skills_dir`" in scenario 3

### ✅ Already Ready

- `skills-dir-sandbox-mount/spec.md` — passed with 💡 suggestions only; optional fix applied

---

## specs Round 2 — 2026-08-03

**Batch:** specs (Round 2 — Round 1 fixes verified)
**Frozen artifacts:** `proposal.md`, `design.md`
**Baseline:** `explore-brief.md`

### ✅ Round 1 Fixes Confirmed

All 5 Round 1 fixes verified correct: `design.md` secrets.toml cleanup (single note, no stale "replace with" instruction), 5 missing default-path assertions in `xdg-path-resolution`, `_AGENT_DIR` label + `downloads_dir` requirement in `agent-xdg-launch`, stale `log_file` GIVEN removed from `agent-scoped-directories`, optional "XDG-derived" terminology in `skills-dir-sandbox-mount`.

### 🔴 Outstanding

None.

### 🟡 Found & Fixed

- **`agent-scoped-directories/spec.md`**: "Vault path derives from agent_name only" scenario had stale `AND no file_vault field appears in [paths]` GIVEN clause (symmetric counterpart to the fixed `log_file` clause) — removed

### 💡 Optional Fixed

- **`xdg-data-migration/spec.md` line 39**: "regenerates…from" → "regenerates…to" (tool_index is written to, not read from, that path)

### ⚖️ Verdict

**Specs frozen. Ready to proceed to adr.md.**

## adr Round 1 — 2026-08-03

**Batch:** adr (newly created: `adr.md`, `adr/0019-xdg-base-directory-layout-for-agent-storage.md`)
**Frozen artifacts:** proposal.md, design.md, all 5 specs

### 🟡 Fixed

- **`adr.md` ADR-0015 note**: Did not address ADR-0015 Consequence 4's anticipated `nsjail_state_dir` config override — added one-sentence clarification that the override was never implemented and `XDG_STATE_HOME` is the correct mechanism
- **`ADR-0019` Commitment 3**: `downloads_dir` disposition was absent — added sentence clarifying it remains as a runtime value derived from `workspace_dir` but is not an `XDGPaths` field
- **`ADR-0019` Consequences**: Missing negative consequence for `$XDG_RUNTIME_DIR` parent non-existence (`parents=False` means hard startup failure on non-systemd-logind hosts) — added

### 🔴 Outstanding

*(none)*

### ⚖️ Verdict

**ADR frozen. Ready to proceed to tasks.md.**

## tasks Round 1 — 2026-08-03

**Batch:** tasks (first draft of `tasks.md`)
**Frozen artifacts:** proposal.md, design.md, all 5 specs, adr.md, adr/0019

### 🔴 Fixed

- **nsjail exemption logic absent**: task 8.3 only updated callers; missing the `nsjail_config.py` user-home prefix blocklist exemption for read-only skills mount + no test task → split into tasks 9.3 (callers), 9.4 (exemption logic), 9.5 (exemption tests)
- **`tmp_xdg` fixture used before it was created**: `test_migrate.py` (section 5) referenced `tmp_xdg` which was only created in section 9 → moved to task 1.5 (immediately after `xdg.py`)
- **No test tasks for `agent-xdg-launch` scenarios**: all `main.py` startup behaviour had zero test coverage → added section 6 (`tests/test_main.py`) covering all 9 spec scenarios including `_check_migration` auto-trigger

### 🟡 Fixed

- Task 7.3 duplicate `GraphMemoryConfig.db_path` reference → task 8.3 notes "already removed in task 2.2"
- Task 3.3 `warnings.warn` → changed to `logger.warning(...)` (structlog)
- No `_check_migration` auto-trigger test → covered in task 6.2
- No `--source` non-default directory test → added to task 5.2
- Task 2.5 vulture was premature → narrowed to deleted symbols only; XDGPaths fields deferred to task 11.2

### 🔴 Outstanding

*(none)*

## tasks Round 2 — 2026-08-03

**Batch:** tasks (revised `tasks.md` addressing all Round 1 findings)

### 🟡 Fixed

- Task 3.4 implicit forward dependency on task 4.1 (`migrate.py` not yet created when `_check_migration` is written) → added one-line note: "complete task 4.1 before implementing the `migrate.main()` call"

### 🔴 Outstanding

*(none)*

### ⚖️ Verdict

**Tasks frozen. All artifacts complete. Ready to proceed to `/opsx-apply`.**
