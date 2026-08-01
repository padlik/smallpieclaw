## Context

Skills are authored with relative paths per the Agent Skills specification (`scripts/`, `./assets/.env`, etc.). The harness is expected to resolve these paths — this is a spec-level contract, not a per-skill choice. Currently the harness does not fulfil this contract: it relies on a prompt instruction (`prompt_builder.py:193`) to add `cd <skill_dir> &&` prefixes, which fails silently when the LLM forgets it across multi-step skill execution.

The path substitution must work identically whether or not nsjail is active. Under nsjail, `cd` inside the command string works fine (skills_dir is bind-mounted at its real host path inside the jail), so the fix does not require any nsjail config changes.

In-force ADRs relevant to this change:
- **ADR-0010** (Accepted): Zone-based file access control. No change to trust zones in this change.
- **ADR-0008** (Accepted): Facade/handler package for built-in tools. `FileTools` in `builtin_tools/files.py` is the correct location for SKILL.md intercept logic.

## Goals / Non-Goals

**Goals:**
- Relative paths in `SKILL.md` resolve to absolute paths before the agent sees them
- Works for all skills without any per-skill changes
- Transparent to the LLM — no new tools, no new prompt instructions
- Fallback when `skill_registry` is not yet set (dirname of path)

**Non-Goals:**
- Trust zone changes for `skills_dir` (deferred to a separate change)
- Fixing file_read confirmation dialogs for SKILL.md (separate concern)
- Resolving paths in scripts called indirectly by skill scripts
- Shell tool `cwd` parameter
- Substitution in non-SKILL.md files (e.g., skill reference docs)

## Decisions

### Decision 1: Intercept at `_run_file_read`, not at shell dispatch

**Chosen**: Transform content in `_run_file_read` when the path ends with `SKILL.md` **and is found in the skill registry** (see Decision 3 for the lookup pattern).

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| Prompt instruction only | Already in place and failing in production |
| Add `cwd` to shell tool | LLM still has to choose to pass it |
| `skill_shell` dedicated tool | LLM must learn new tool; same compliance problem |
| Ambient skill context state | Lifecycle unclear; breaks across sub-agents |

**Rationale**: The `file_read` return boundary is the earliest point where the harness has both SKILL.md content and the skill directory in scope. Substituting there means the LLM never receives a relative path — there is nothing to forget.

### Decision 2: Two-tier substitution strategy

**Tier 1 — global (all SKILL.md content):**
```
./foo  →  <skill_dir>/foo
```
`./` is unambiguous: it only means "current directory" in shell/path contexts. Safe to apply globally across all content.

**Tier 2 — within code fences and inline code spans only:**
```
scripts/  →  <skill_dir>/scripts/
assets/   →  <skill_dir>/assets/
references/ →  <skill_dir>/references/
tests/    →  <skill_dir>/tests/
```
Bare subdirectory names (`scripts/foo.py`) are common in prose and URLs. Restricting to code contexts reduces false positives. These four names are the standard agentskills.io skill subdirectories.

Each name must match at a path boundary — anchored so it is not preceded by a word character, `/`, or `-` (e.g., `(?<![/\w-])scripts/`). This prevents compound names like `static-assets/` or `app-scripts/` from partially matching and producing corrupted paths.

### Decision 3: Registry lookup via inline scan; wire `skill_registry` onto `BuiltinExecutor`

**Lookup pattern** — no new method on `SkillRegistry` (public API: `refresh()`, `all()`, `get(name)`, `count()`):

```python
registry = self._owner.skill_registry
if registry is None:
    # Pre-wiring (startup) or test harness: use dirname as fallback skill dir.
    skill_dir = os.path.dirname(path)
else:
    skill = next(
        (s for s in registry.all() if s.skill_md_path == path),
        None,
    )
    if skill is None:
        return raw_result  # not a registered skill — skip substitution entirely
    skill_dir = skill.path  # skill DIRECTORY (not skill_md_path which is the SKILL.md file)
```

Two distinct cases:
- **`registry is None`** (pre-wiring / test harness): fall back to `os.path.dirname(path)` and proceed with substitution. Correct result in all realistic cases.
- **`registry` is set, skill not found**: skip substitution entirely. Guards against substituting content in arbitrary user files named `SKILL.md` that happen to be read.

**`skill_registry` wiring** — same pattern as `trusted_zone_checker`:

```
main.py construction order:
  builtin = BuiltinExecutor(...)       # self.skill_registry = None (at construction)
  skills = SkillRegistry(...)
  builtin.skill_registry = skills      # ← set after construction, same as trusted_zone_checker
```

`FileTools._run_file_read` accesses it as `self._owner.skill_registry`.

**C4 Component View — path substitution flow:**

```
┌─────────────────────────────────────────────────────────────┐
│  BuiltinExecutor                                            │
│                                                             │
│  .skill_registry ──────────────────────────┐               │
│                                            │               │
│  ┌──────────────┐  file_read   ┌──────────▼────────────┐  │
│  │  ReAct Loop  │─────────────▶│      FileTools         │  │
│  └──────────────┘              │   _run_file_read(path) │  │
│                                │                         │  │
│                                │  path.endswith(         │  │
│                                │    "SKILL.md")?         │  │
│                                │        │ YES             │  │
│                                │  skill = next(s for s   │  │
│                                │    in registry.all()    │  │
│                                │    if s.skill_md_path   │  │
│                                │    == path, None)       │  │
│                                │        │                │  │
│                                │  skill_dir =            │  │
│                                │    skill.path           │  │
│                                │   (skill DIRECTORY)     │  │
│                                │    or dirname(path)     │  │
│                                │        │                │  │
│                                │  _expand_skill_paths(   │  │
│                                │    content, skill_dir)  │  │
│                                │   Tier1: ./→ abs        │  │
│                                │   Tier2: subdirs in     │  │
│                                │          code blocks    │  │
│                                │        │                │  │
│                                │  return augmented output│  │
│                                └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

Note: the diagram above simplifies the registry branching. The authoritative two-case logic is in the pseudocode in Decision 3: when `registry is None` use `dirname(path)` fallback; when `registry` is set but skill not found, return raw content without substitution.

### Decision 4: Remove `cd <skill_dir> &&` from prompt

With substitution in place, the LLM receives absolute paths. The `cd <skill_dir> &&` instruction in `prompt_builder.py:193` is redundant and adds noise. Remove it. Keep `Skill dir: {s.path}/` in the `format_skills` output — it remains useful reference context.

## Risks / Trade-offs

- **Tier 2 false positive**: A SKILL.md that shows `scripts/` in a code block as a documentation example (not for execution) would get the path substituted. Risk is low — SKILL.md files are agent-instruction documents. If it occurs, the agent receives an absolute path pointing at the skill's own scripts dir — which is the correct resolution anyway.
- **Skill registry None at intercept time**: If `_run_file_read` is called before `main.py` sets `builtin.skill_registry`, the fallback `os.path.dirname(path)` produces the correct skill directory. No error path.
- **Non-skill SKILL.md**: If a user project contains a file named `SKILL.md` outside the skills directory, `next(...)` returns `None` (not found in the registry) and substitution is skipped entirely — the raw content is returned unchanged. This is Case (b) in Decision 3; the `os.path.dirname` fallback is only used when the registry itself is `None` (Case a).
- **Sub-skills**: Each `file_read` on a SKILL.md path is intercepted independently. If a skill reads another skill's SKILL.md, that read also gets substituted correctly.

## Migration Plan

No migration required:
- `skill_registry` attribute on executor initialises to `None` (backward compatible)
- `_expand_skill_paths` is called only when path ends with `SKILL.md` (no effect on other reads)
- Prompt change removes one instruction line — existing prompts and configs unaffected

Rollback: revert `builtin_tools/files.py`, `builtin_executor.py`, `main.py`, `prompt_builder.py`.

## Open Questions

- **Tier 2 subdirectory list completeness**: The four names cover all current agentskills.io standard dirs. If the spec adds new standard dirs, this list needs updating — not a concern today.
- **`_default_trusted_dirs` and skills**: Trust zone classification of `skills_dir` is deferred. The confirmation dialog on SKILL.md reads remains until a future change addresses trusted-dir reordering.
