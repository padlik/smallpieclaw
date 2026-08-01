## ADDED Requirements

### Requirement: SKILL.md content is returned with Tier 1 relative paths substituted

When `file_read` is invoked on a SKILL.md file belonging to a registered skill, all `./`-prefixed relative path references in the content are replaced with absolute paths rooted at the skill directory before the content is returned to the agent.

#### Scenario: Tier 1 ./foo references are expanded to absolute paths
- **GIVEN** a skill is registered with directory `/abs/skills/team-load`
- **AND** its SKILL.md contains `./scripts/run.sh` and `./assets/.env`
- **WHEN** `file_read` is called with the skill's SKILL.md path
- **THEN** the returned content contains `/abs/skills/team-load/scripts/run.sh`
- **AND** the returned content contains `/abs/skills/team-load/assets/.env`
- **AND** no `./` prefix remains in the returned content

### Requirement: SKILL.md content is returned with Tier 2 subdirectory references substituted inside code contexts

When `file_read` returns SKILL.md content, occurrences of the four standard agentskills.io subdirectory names (`scripts/`, `assets/`, `references/`, `tests/`) are replaced with absolute paths rooted at the skill directory, but only within fenced code blocks (` ``` ` delimiters) and inline code spans (`` ` `` delimiters). Prose references outside code contexts are left unchanged.

#### Scenario: Tier 2 subdirectory reference inside a code fence is expanded
- **GIVEN** a skill is registered with directory `/abs/skills/team-load`
- **AND** its SKILL.md contains a fenced code block with `python scripts/fetch.py`
- **WHEN** `file_read` is called with the skill's SKILL.md path
- **THEN** the returned content contains `python /abs/skills/team-load/scripts/fetch.py` inside the code block
- **AND** prose text outside code blocks that mentions `scripts/` is left unchanged

#### Scenario: Tier 2 substitution does not match compound directory names
- **GIVEN** a skill is registered with directory `/abs/skills/team-load`
- **AND** its SKILL.md contains a fenced code block with `static-assets/css/style.css`
- **WHEN** `file_read` is called with the skill's SKILL.md path
- **THEN** the returned content is unchanged for `static-assets/css/style.css`
- **AND** no occurrence of the skill directory path is injected before `assets/css/style.css`

### Requirement: SKILL.md read before skill registry is available falls back to path dirname

When `file_read` is invoked on a SKILL.md file and `skill_registry` has not yet been wired onto the executor (is `None`), the skill directory is derived from the file's parent directory and path substitution still runs.

#### Scenario: Pre-wiring fallback uses dirname when skill registry is not yet available
- **GIVEN** the executor's `skill_registry` attribute is `None`
- **AND** `file_read` is called with a path ending in `SKILL.md` at `/abs/skills/team-load/SKILL.md`
- **WHEN** the content is processed
- **THEN** `/abs/skills/team-load` is used as the skill directory (derived from `os.path.dirname`)
- **AND** Tier 1 and Tier 2 substitutions run using that directory as the root

### Requirement: Non-skill SKILL.md files are returned without path substitution

When `file_read` is invoked on a file named `SKILL.md` that is not registered in the skill registry, the raw file content is returned unchanged. No path substitution is applied.

#### Scenario: Non-skill SKILL.md is returned without substitution
- **GIVEN** the skill registry is wired and contains only skills under `/abs/skills/`
- **AND** `file_read` is called with `/user/project/docs/SKILL.md` (not in the registry)
- **WHEN** the content is processed
- **THEN** the returned content is identical to the raw file content
- **AND** no path substitution has been applied
