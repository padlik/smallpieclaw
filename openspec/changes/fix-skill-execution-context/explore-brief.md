# Explore Brief: fix-skill-execution-context

## Problem Statement

When an agent executes a skill, two related bugs surface:

1. **Relative path failure**: Skills are authored with relative paths (`./scripts/run.sh`, `assets/.env`) per the Agent Skills spec convention. The agent executes these from the wrong working directory (project root or `/tmp` in nsjail), causing file-not-found errors.

2. **Spurious file_read confirmation**: Reading `SKILL.md` triggers a user confirmation dialog even though skills are trusted content, because `skills_dir` is never registered in `TrustedZoneChecker`.

## Approaches Rejected

| Approach | Rejected Because |
|---|---|
| Modify individual skills to use absolute paths | Violates skill portability; breaks cross-harness compatibility |
| Add `cwd` parameter to shell tool | LLM still has to choose to pass it; doesn't fix the trust confirmation bug |
| Prompt-only fix (improve `format_skills` instruction) | Already partially in place and failing; LLM forgets distant system prompt instructions during multi-step skill execution |
| `skill_shell` dedicated tool | Requires LLM to learn a new tool; doesn't fix trust bug; doesn't help nsjail case since `cd` already works there |
| Skill context injection (auto-cd ambient state) | Complex lifecycle management; tricky to know when skill execution ends |

## Final Approach

### Fix 1: Variable substitution at `file_read` intercept (primary)

When `_run_file_read` is called on a path ending with `SKILL.md`:
- Detect it is a skill file (path ends with `/SKILL.md`, under known skills_dir)
- Look up skill in registry via `self._owner.skill_registry`
- Apply path substitution to the returned content:
  - **Tier 1 (safe, always)**: Replace `./` prefix → `{skill_dir}/`
  - **Tier 2 (in code blocks/spans only)**: Replace known skill subdirs at word boundary (`scripts/`, `assets/`, `references/`, `tests/`) → `{skill_dir}/{subdir}/`
- Agent receives already-resolved absolute paths; never sees relative ones

This is the most reliable fix because the agent never sees a relative path in the first place — there is nothing to forget.

### Fix 2: Register skills_dir as trusted zone

`TrustedZoneChecker.__init__` accepts `paths_config`, `data_dir`, `agent_name`, etc. but not `skills_dir`. The `_default_trusted_dirs` list contains `workspace_dir`, `downloads_dir`, `/tmp/{agent_name}` — but not `skills_dir`.

Fix: pass `skills_dir` to `TrustedZoneChecker.__init__` and include it in `trusted_candidates`. Wire `skills_dir_abs` from `main.py` at checker construction time.

### Fix 3: format_skills reference info (supporting, keep)

`format_skills()` already shows `Skill dir: {s.path}/`. Keep this — it provides useful reference context for the agent. Remove the now-redundant `cd <skill_dir> &&` instruction from `prompt_builder.py:193` since path substitution makes it unnecessary.

## Cross-Module Data Flows

```
main.py
  skills_dir_abs
    → TrustedZoneChecker(skills_dir=skills_dir_abs)   [NEW]
        _default_trusted_dirs includes skills_dir
    → BuiltinExecutor(skills_dir=skills_dir_abs)
        → NsjailConfigBuilder(skills_dir=...)           [existing]
        → self.skill_registry = None                    [NEW: wire skill_registry]

builtin_tools/files.py
  _run_file_read(path)
    if path ends with SKILL.md:
      skill_dir = lookup from self._owner.skill_registry  [NEW]
                  or fallback: os.path.dirname(path)
      content = _expand_skill_paths(content, skill_dir)   [NEW]

_expand_skill_paths(content, skill_dir) → str
  Tier 1: re.sub(r'\.\/', skill_dir + '/', content)
  Tier 2: within code fences/spans only:
    re.sub(r'\b(scripts|assets|references|tests)/', skill_dir + r'/\1/', ...)
```

## Open Questions (resolved during explore)

- **Why can't agent just `cd`?** It CAN. nsjail mounts skills_dir at real path, `cd` works inside jail. The problem is LLM compliance — it doesn't reliably do it across multi-step execution.
- **Is nsjail a barrier?** No. `cwd: "/tmp"` is just the start dir. `cd /abs/skill && command` works.
- **Does a standard exist?** No universal standard. Agent Skills spec says "use relative paths" but doesn't define harness behavior. Claude Code added `${CLAUDE_SKILL_DIR}` variable (v2.1.64) requiring skill authors to adopt it. Our substitution approach is transparent — no skill changes needed.
- **Should skills_dir be rw or ro trusted?** ro is semantically correct (skills are installed, not modified by agent). But `_default_trusted_dirs` is currently undifferentiated by mode. Adding as-is is fine; write operations to skills still go through other checks.
