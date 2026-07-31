# Default-trusted zones (not just operator-approved dirs) may be mounted into nsjail

## Status

Accepted, supersedes ADR-0016 (partial — Decision 3's wording only)

## Date

2026-07-31

## Supersedes

ADR-0016 — specifically Decision 3: "The sandbox's writable scope is confined to `/tmp` (session scratch) and explicitly-approved trusted RW directories only." The rest of ADR-0016 (no `project_dir` mount, `cwd = /tmp`, `/home` removed from the system blocklist, vault relocation) is unaffected and remains in force.

## Context

`builtin_tools/access_control.py`'s `TrustedZoneChecker` classifies paths into three zones (`access_control.py:3-6`): **TRUSTED** ("user workspace + downloads + tmp + user-added dirs — auto-allow"), **REQUEST_GRANT**, and **UNRECOGNISED**. `workspace_dir`, `downloads_dir`, and `tmp_dir` are grouped in the *same* auto-allow tier as directories an operator has approved through the zone-confirmation flow (`TrustedZoneChecker.add_trusted()`, invoked from an inline Telegram button when a file operation touches an UNRECOGNISED path — there is no `/dir add` command; `/dir` only supports `list`/`del`/`reload`) — none of the three require any operator action; they are trusted by default, unconditionally, from the moment the agent starts.

ADR-0016's Decision 3 was written without this tier in mind: it names only "`/tmp` (session scratch)" and "explicitly-approved trusted RW directories" as the sandbox's writable scope, which reads as covering solely operator-added `trusted_dirs.json` entries. It does not mention the default-trusted tier at all — an omission, not a deliberate exclusion, since nothing in ADR-0016's Context or Consequences argues against exposing a default-trusted zone inside the jail; the tier simply wasn't part of the sandbox's writable-scope discussion at the time.

`openspec/changes/fix-nsjail-tmp-handoff` needs to bind-mount `tmp_dir` read-write into the jail at its real path — `tmp_dir` is one of the three directories already in the default-trusted tier, not a new kind of directory and not an operator-approved one. Read literally, this is a new mount outside Decision 3's two named categories; read against what `access_control.py` already does, it's making an already-default-trusted zone visible where it wasn't before.

## Decision

The sandbox's writable scope, as governed by ADR-0016 Decision 3, explicitly includes **any of the agent's default-trusted zones** (`workspace_dir`, `downloads_dir`, `tmp_dir` — the same tier `access_control.py`'s `TrustedZoneChecker` already auto-allows with no operator action) **when a specific mount for that zone is defined in the `nsjail-shell-sandboxing` spec** — not only `/tmp` scratch and operator-approved `trusted_dirs.json` entries.

This is not a blanket rule that all three directories are now mounted — only `tmp_dir` is, per `fix-nsjail-tmp-handoff`. `workspace_dir` and `downloads_dir` remain unmounted until (if ever) a future change adds them explicitly, at which point that change points here rather than re-deriving the justification.

## Consequences

- **Positive**: Closes the gap between what `access_control.py` already trusts outside the jail and what the jail can see — a default-trusted zone was previously invisible inside the sandbox for no principled reason, only because ADR-0016 didn't anticipate this tier.
- **Positive**: Future mounts of `workspace_dir` or `downloads_dir` (if ever needed) have a named precedent instead of each requiring its own ADR.
- **Negative**: A reader of ADR-0016 alone, without checking the supersession graph, would incorrectly conclude this class of mount isn't permitted. ADR-0016 is left unedited (per the immutable-ADR rule) rather than annotated in place.
- **Neutral**: Operator-added `trusted_dirs.json` entries and the zone-confirmation approval flow they come through are completely unaffected — this decision only concerns the pre-existing, always-on default-trusted tier, not user-managed directories.
