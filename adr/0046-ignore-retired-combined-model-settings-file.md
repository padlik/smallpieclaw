# 0046-ignore-retired-combined-model-settings-file

## Status

accepted, supersedes ADR-0040 (partial; see Supersedes)

## Date

2026-11-19

## Supersedes

ADR-0040 (partial: only the sentence requiring an existence check and one startup notice for the old combined `models-config.toml`, and its associated path handling, i.e. the `models_config_file` XDG exposure). Everything else in ADR-0040 stays in force as amended by ADR-0045: the advisory disposable cache, the separate deliberate preference, startup/bootstrap selection rules, corrupt-preference preservation, and the XDG leaves for `model-selection.toml` and the cache.

## Context

ADR-0040 had the runtime check for the retired `models-config.toml` and emit a notice suggesting `/models`. The runtime no longer derives any selection from that file. The leftover combined `ModelSettings` API, together with this existence check, path and notice, is removed by `remove-obsolete-model-paths`. ADR-0044's general requirement for actionable upgrade guidance is met by documentation for this file; its scheduler notice is unaffected.

## Considered Options

- Keep the existence notice: harmless, but retains a path, config exposure and tests for a retired concept.
- Migrate or delete the old file: loses operator evidence and was already rejected by ADR-0044.
- Stop referencing the file entirely, leaving it untouched (chosen).

## Decision Outcome

Remove the existence check, notice and `models_config_file` path. An existing `models-config.toml` is never read, converted, renamed or deleted, and no notice mentions it. Obsolete model keys in `config.toml` are still rejected by ADR-0044's rules.

## Consequences

- Good: fewer paths and tests; one model-settings authority (`model-selection.toml` plus cache).
- Bad: operators upgrading with only the old file get no hint; documentation, not runtime, covers `/models`.
- Follow-up: remove the code, XDG exposure and tests in the authorized apply of `remove-obsolete-model-paths`.
