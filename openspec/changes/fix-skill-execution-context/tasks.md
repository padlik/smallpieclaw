# Implementation Tasks

## Tasks

- [x] Task 1: Add `skill_registry` attribute to `BuiltinExecutor`
  - Files: `builtin_executor.py`
  - Add `self.skill_registry = None` in `__init__`, alongside the existing `self.trusted_zone_checker = None` declaration (same post-construction wiring pattern)
  - Verify: attribute is accessible as `self.skill_registry` with value `None` at construction time

- [x] Task 2: Implement `_expand_skill_paths(content, skill_dir)` in `builtin_tools/files.py`
  - Files: `builtin_tools/files.py`
  - Tier 1: Replace all `./` occurrences with `{skill_dir}/` globally using `content.replace("./", f"{skill_dir}/")`
  - Tier 2: Extract all fenced code blocks (` ``` `-delimited) and inline code spans (`` ` ``-delimited); within each, replace `scripts/`, `assets/`, `references/`, `tests/` using `re.sub(r'(?<![/\w-])scripts/', f'{skill_dir}/scripts/', ...)` (and same pattern for each subdir name); reassemble content
  - Verify: unit test covers `./` expansion, code-fence subdir expansion, compound-name non-match (`static-assets/`), and prose-outside-code non-match

- [x] Task 3: Wire `_expand_skill_paths` call into `_run_file_read`
  - Files: `builtin_tools/files.py`
  - After `content = f.read(max_bytes)`, detect `path.endswith("SKILL.md")` and apply two-case branching:
    - Case (a): `registry = self._owner.skill_registry; if registry is None: skill_dir = os.path.dirname(path)` → call `_expand_skill_paths`
    - Case (b): `skill = next((s for s in registry.all() if s.skill_md_path == path), None); if skill is None: return raw_result` (skip substitution); else `skill_dir = skill.path` → call `_expand_skill_paths`
  - Verify: substitution is reachable via both `_exec_table` (normal path) and `_trusted_exec_table` (bypass path) because substitution is in `_run_file_read`, which both dispatch paths call

- [x] Task 4: Wire `skill_registry` onto executor in `main.py`
  - Files: `main.py`
  - After `skills = SkillRegistry(skills_dir=skills_dir)`, add `builtin.skill_registry = skills` (same pattern as `builtin.trusted_zone_checker = _trusted_zone_checker`)
  - Also wire for sub-agent executor if applicable (check if `agent.trusted_zone_checker` wiring has a parallel `skill_registry` assignment that also needs updating)
  - Verify: `builtin.skill_registry` is not `None` after startup; `SkillRegistry` instance is accessible from `FileTools` via `self._owner.skill_registry`

- [x] Task 5: Add tests for `_run_file_read` SKILL.md interception
  - Files: `tests/test_builtin_files.py` (create if needed) or nearest existing file test module
  - Test cases:
    - Tier 1: SKILL.md with `./scripts/run.sh` → returned content has `<skill_dir>/scripts/run.sh`
    - Tier 2: SKILL.md with `` `scripts/fetch.py` `` inline → returned content has `<skill_dir>/scripts/fetch.py`; prose `scripts/` unchanged
    - Word-boundary: `static-assets/css/style.css` in code fence → unchanged
    - Registry-None fallback (Case a): `skill_registry = None` → dirname used, substitution runs
    - Non-skill guard (Case b): registered registry but path not in registry → raw content returned unchanged
  - Verify: `pytest tests/test_builtin_files.py -v` passes (or equivalent targeted run)

- [x] Task 6: Remove `cd <skill_dir> &&` instruction from `prompt_builder.py`
  - Files: `prompt_builder.py`
  - Remove the line at prompt_builder.py:193 that instructs `cd <skill_dir> && <command>`
  - Keep the `Skill dir: {s.path}/` line in `format_skills` output
  - Verify: `format_skills` output no longer contains `cd` instruction; `Skill dir:` line still present in output

- [x] Task 7: Final verification
  - Run `make check` (lint + tests)
  - Verify: `ruff check .` passes, `vulture . vulture_whitelist.py --min-confidence 80` passes, `pytest tests/ -v --tb=short` passes
  - If vulture flags `skill_registry` on `BuiltinExecutor` as unused, add it to `vulture_whitelist.py`
