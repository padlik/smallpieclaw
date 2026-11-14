# 0045-select-models-only-through-list-buttons

## Status

accepted, supersedes ADR-0040 (partial; see Supersedes)

## Date

2026-11-14

## Supersedes

ADR-0040 (partial: replace only its planned typed Telegram selection entry — `/models select <connection>/<upstream>` and the statement that textual selection works without cache or Main). Everything else in ADR-0040 stays in force, including the advisory disposable cache, the separate deliberate preference, atomic write-before-publish, corrupt-preference preservation and its deliberate replacement without restart, the saved-selector-is-active-whether-cached-or-not rule, deterministic unsaved bootstrap, and opaque expiring button tokens. ADR-0041 is unchanged: the exact `connection-id/upstream-id` selector grammar, exact configured connection, uncached upstream admission and scheduled selectors still apply to runtime and scheduled requests.

## Context

ADR-0040 planned two deliberate selection routes: candidate buttons and a typed `/models select <connection>/<upstream>` command that needed neither cache nor Main. The cache/preference split, the registry selection service and the `/models` listing with buttons are now implemented; the typed command and its parsing, error and recovery handling were never built (the implemented empty-cache display only names it). The operator decided that a second typed entry is not wanted: `/models` should list cached candidates, refresh the cache, and select only through its list buttons.

## Considered Options

- Keep both routes (ADR-0040): lets an operator select an uncached upstream with an empty cache, but adds a second entry with its own parsing, errors and recovery tests beside the buttons.
- Buttons only, refresh when the cache is empty (chosen): one entry and one persistence path; an operator with an empty cache and a corrupt preference must first get a successful refresh or wait for providers to recover.
- Add another hidden CLI or UI route to compensate: rejected by the operator as a new surface the decision is meant to avoid.

## Decision Outcome

Remove only the typed Telegram selection entry. Selection is made by candidate buttons rendered from the cached list, and `/models` arguments never select. A button selection keeps ADR-0040's behavior: its opaque token resolves to the selector captured at render, only the non-empty first-slash shape and configured connection are checked (never cache membership or upstream existence), the preference is replaced atomically before the active selection is published or success reported, a failed write keeps the old bytes and active selection, and a corrupt preference is replaced the same way without restart. Button selection needs no active Main selection, and its tokens stay opaque, at most 64 bytes, scoped to the user and chat and expiring; an unexpired token is not invalidated merely because refresh later removed its candidate from the cache (the unchanged callback contract of ADR-0040 and task 9.3). The empty-cache display offers Refresh only and names no typed command, and no cache-eligibility validation is added anywhere. Refresh changes only the cache and never writes or repairs the preference. The registry's selection service remains available to its other callers; only the Telegram typed entry is removed. A valid saved selector stays active and displayed whether cached or not.

## Consequences

- Good: one selection route and one persistence path to test and explain; no second parser or error surface.
- Bad: a new selection now depends on provider discovery having returned at least one candidate; with a corrupt preference, an empty cache and failing providers, selection cannot be repaired from chat until a refresh returns candidates. No hidden command or UI compensates; the baseline alternative is to repair or remove the file manually and restart.
- Follow-up: repurposed task 9.2 removes the typed-command guidance from the empty-cache display and README and adds button recovery and no-typed-route regressions; task 11.1 documents button selection. Completed task 9.1's recorded behavior is historical.
