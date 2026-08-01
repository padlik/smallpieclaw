# ADR Review Manifest

- Status: completed
- Review date: 2026-07-31

## Review Summary

ADR review completed for this change. The implementation follows existing patterns and introduces no major durable architectural decisions beyond what is already captured by in-force ADRs.

## In-Force ADRs Reviewed

- **ADR-0008** (Accepted) — Use facade/handler package for built-in tools. The `_expand_skill_paths` helper and `_run_file_read` intercept follow the established pattern: tool logic lives in `builtin_tools/files.py` (the `FileTools` handler), accessed via `self._owner` façade. No deviation from this ADR.
- **ADR-0010** (Accepted) — Zone-based file access control. No trust zone changes are introduced. `skills_dir` remains UNRECOGNISED in `TrustedZoneChecker`; the file-read confirmation dialog for SKILL.md reads is unchanged by this change. Deferred to a future change.

## What This Change Does Not Change

- **prompt_builder.py format_skills cleanup (Decision 4)**: Removal of the `cd <skill_dir> &&` instruction is a prompt-hygiene change, reviewed against in-force ADRs and found to introduce no durable architectural commitment. The `Skill dir:` reference line is retained.

## New Durable ADRs Created

None — no major durable architectural decisions were introduced. The path substitution at `_run_file_read` is a targeted content transformation following ADR-0008's handler pattern. The registry wiring pattern follows the precedent established by `trusted_zone_checker` (assigned post-construction in `main.py`).
