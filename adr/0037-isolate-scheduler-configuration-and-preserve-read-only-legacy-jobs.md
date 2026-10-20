# 0037-isolate-scheduler-configuration-and-preserve-read-only-legacy-jobs

## Status

accepted

## Date

2026-10-20

## Supersedes

None. ADR-0033's execution ownership, context isolation, callbacks, and record contracts are unchanged; this record governs only scheduler configuration parsing, registration, storage, and legacy-file compatibility.

## Context

The scheduler currently couples startup availability to job/file validity, and its persistence loses raw operator data. `_read_config_jobs` returns the same empty map for missing, unreadable, unparseable, and valid-empty files; unguarded numeric casts can raise out of load and construction; load rewrites migrated legacy schedules back into the file; `_serialize_job` rebuilds jobs from runtime metadata and drops unknown fields; `_save_scheduler_toml_locked` swallows `OSError`; and `_load_dynamic_jobs` destructively imports-and-deletes legacy `scheduled_jobs.json`. A corrupt file or bad job therefore gives the operator no actionable path in cold states, and scheduler writes can silently rewrite saved configuration, including selectors and unknown fields.

## Considered Options

- Fail startup or job load on a bad scheduler file or bad jobs: rejected (user-rejected) — makes bot availability depend on job configuration and violates per-job containment.
- Rebuild the persisted document from runtime metadata, dropping unknown fields: rejected (user-rejected) — destroys raw saved values and invites silent migration of operator data.
- Destructive migrate-or-delete of legacy `scheduled_jobs.json` on load: rejected (user-rejected) — legacy jobs must stay runnable and the file must not be deleted, overwritten, or auto-adopted.

## Decision Outcome

- **Authoritative raw document + derived runtime view.** The parsed raw scheduler document is the authoritative persisted job configuration; `_jobs_meta` becomes a derived runtime view overlaid with run state (`last_run`, `last_error`) and is never used to rebuild the file. Legacy cron derivation is in-memory only: load never writes, and no new automatic rewrite, delete, or import migration exists.
- **Startup never depends on job validity.** A scheduler-local load result distinguishes `valid` (including zero jobs), `missing`, `unreadable`, and `unparseable`, carrying the parsed document, path, a hash of the exact bytes parsed, and one actionable operator notice (file path, what failed, what to do — no file content); commands, Telegram, and model work stay ready in every cold state. Cold unreadable/unparseable state keeps no executable map and its bytes are preserved (mutations are refused until the file parses); cold missing state allows the first mutation to create the file; a failed reload keeps the last-good map, document, hash, and registrations untouched. Parseable-but-bad jobs keep raw values and are contained: bad scheduling fields leave the job visibly unscheduled with a `config_error`, other bad fields fail at fire time as recorded launches (a non-string `task` is not a reminder), field checks are type-strict with no coercion, `model` is never classified at load, and healthy sibling jobs load, register, and run.
- **Type-strict raw values, local encoder.** Raw TOML values are re-emitted type-strictly and NaN-aware through a scheduler-local recursive encoder covering exactly what `tomli` can return (no new dependency, not a general serializer). Only parsed-value equality is promised; comments and layout are not preserved, and nothing claims otherwise.
- **Explicit mutation protocol.** Mutations patch only requested fields in a deep copy of the raw document; schedule triggers and registrations are prepared pre-commit; under the held locks the source document/generation is revalidated and stale candidates are discarded and re-prepared, the disk is required to equal the stored hash (or the missing-file sentinel), the write is atomic with explicit `OSError` failures, and document + hash + runtime view + prepared registrations publish as one unit. Lock order is `_history_lock` outermost only where already required, then `_running_lock` then `_save_lock`, held across write and publication.
- **Reload and failure semantics.** Reload publishes only its own parsed bytes: the locked recheck compares the file hash with the hash of the bytes it parsed (and the legacy fingerprint from its own parsed bytes), never the old stored hash. A successful reload replaces registrations completely and clears the `RELOAD_REQUIRED` latch; after `os.replace` commits bytes but publication cannot complete, the latch is set with no active mixed set, the original attempt is an `executed` failure, and future mutations are refused before `_on_attempt` until a successful reload. A harmless reload race preserves the existing latch as-is and sets none.
- **Legacy read-only compatibility (user constraint).** Legacy `scheduled_jobs.json` jobs remain runnable and the file is never deleted, overwritten, renamed, or adopted; TOML wins duplicate tags; JSON-only jobs carry a visible `legacy, read-only` badge; modify, remove, pause, and resume of a JSON-only job are refused before `_on_attempt` with the legacy path and manual cleanup/reload steps, and `run_now` of a legacy job uses normal admission. Removing a TOML job whose tag shadows a live legacy entry is also refused, checked against the current legacy file on disk (not the cached map) with the fingerprint rechecked under the locks. No adoption, tombstones, or import mutation exist.
- **State JSON is a safe projection.** `scheduler_state.json` keeps its format and projects runtime-safe fields only; raw selectors and other unvalidated values are never copied in and non-JSON values are never masked by string conversion — state JSON is a projection, not a duplication of the raw document.

## Consequences

- Good: Bot availability and last-good state no longer depend on scheduler file validity; raw operator values (selectors, unknown fields) survive scheduler writes; every cold file state has an actionable recovery path.
- Bad: Both locks are held across a small-file write; every mutation pays a roundtrip/verify complexity; legacy jobs require manual operator maintenance with no auto-conversion.
- Neutral: ADR-0033 ownership/context/callback contracts and R6 `_on_attempt` disposition semantics are unchanged.
