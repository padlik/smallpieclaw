## Why

Skills are authored with relative paths (`./scripts/run.sh`, `assets/.env`) per the Agent Skills specification — this is the cross-harness convention, not a per-skill choice. The harness is expected to resolve these paths, but currently delegates to an LLM prompt instruction that the agent forgets during multi-step skill execution, causing file-not-found failures.

## What Changes

- **`_run_file_read` intercept**: When reading any `SKILL.md` file, the harness substitutes relative path references with absolute paths before returning content to the agent. The agent never sees a relative path — no LLM compliance required.
- **`BuiltinExecutor` skill registry wiring**: `skill_registry` is stored on the executor so `FileTools` can look up the skill directory during path substitution.
- **`prompt_builder.py` cleanup**: The `cd <skill_dir> &&` instruction in `format_skills` is removed since it is made redundant by harness-level substitution. The `Skill dir:` reference line is kept.

## Capabilities

### New Capabilities

- `skill-path-resolution`: When the harness returns `SKILL.md` content via `file_read`, relative paths are substituted with absolute paths rooted at the skill directory. Tier 1: `./foo` → `<skill_dir>/foo` (global). Tier 2: `scripts/`, `assets/`, `references/`, `tests/` at word boundary within code fences/spans → `<skill_dir>/<subdir>/`.

### Modified Capabilities

(none — `file-access-zones` and `builtin-tool-execution` dispatch framework are unchanged)

## Impact

- `builtin_tools/files.py` — `_run_file_read` gains SKILL.md detection and calls `_expand_skill_paths()`; new `_expand_skill_paths(content, skill_dir) → str` helper added
- `builtin_executor.py` — stores `skill_registry` reference (set by `main.py` after construction, same pattern as `trusted_zone_checker`)
- `main.py` — wires `skill_registry` onto `builtin` executor after both are constructed
- `prompt_builder.py` — removes the `cd <skill_dir> &&` instruction; keeps `Skill dir:` line
- No API changes, no breaking changes, no dependency additions, no trust zone changes
