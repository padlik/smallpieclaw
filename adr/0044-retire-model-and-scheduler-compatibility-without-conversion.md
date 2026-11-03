# 0044-retire-model-and-scheduler-compatibility-without-conversion

## Status

accepted, supersedes ADR-0034, ADR-0037, ADR-0038 (partial; see Supersedes)

## Date

2026-11-01

## Supersedes

ADR-0034 (partial: replace its legacy-tuning migration/discard consequence with actionable rejection and no automated model conversion; retain actionable rejection of obsolete fallback settings and credential isolation). The specific preview/apply/recover tool belongs to the pending predecessor implementation/specification rather than ADR-0034's text. ADR-0037 (partial: remove read-only RUNNABLE state-home JSON jobs, TOML-vs-legacy shadow collision/refusal rules, legacy JSON reload/freshness fingerprints, and in-memory `daily`/`interval` cron derivation; retain authoritative raw TOML, type-strict job containment, atomic patch/locks/TOML hashes and generations, TOML freshness rechecks, reload latch, refusal timing, safe state projection, history and last-good behavior). ADR-0038 (partial: replace warn-and-ignore legacy per-job `fallback_models` with contained, actionable job error; its selector/Main policy is separately superseded by ADR-0041). ADR-0019's unrelated XDG storage-layout migration remains in force.

## Context

ADR-0034's predecessor migration converted legacy model settings, ADR-0037 kept JSON-only jobs executable and derived old schedules in memory, and ADR-0038 ignored per-job fallback after warning. Once the runtime no longer has model compatibility paths, these exception bridges become misleading and costly. The scheduler's safe persistence and autonomous execution are not under review: only obsolete configuration and execution of old representations are retired.

## Considered Options

- Keep automated conversion and runnable read-only JSON: minimizes immediate manual work but retains two job authorities, stale model policy, collision refusals and hidden compatibility.
- Delete the old files or treat the whole scheduler as invalid: simpler code but destroys operator evidence or takes down healthy siblings.
- Reject obsolete fields locally and notify about inert old files (chosen): more manual upgrade work, but no silent conversion, data loss or ambiguity about which jobs execute.

## Decision Outcome

Remove model-specific `migrate_models.py` preview, `--apply` and `--recover`, not the unrelated XDG storage-layout `migrate.py`. Reject top-level `[[models]]` and its old fields, `[agent]` `default_model`, `background_model`, `fallback_model`, `fallback_models`, `ctx_max_tokens`, and non-connection `[providers.<id>]` model/generation/retry fields with key/path and replacement guidance (`[providers.<id>]`, `/models`, new `[agent]` threshold or `[agent]` retry settings as applicable); never ignore or convert them. The retired `models-config.toml` is handled only by ADR-0040's existence notice, not parsed.

For TOML jobs, obsolete `daily`/`interval`, `time`, `hours`, `minutes` or `fallback_models` remain raw and unrewritten but become actionable per-job errors, not derived schedules or fallback. Obsolete schedule values leave that job unscheduled; other obsolete fields fail that job at launch with normal record and alert. Add/modify refuse known-obsolete input before `_on_attempt`; healthy jobs remain runnable. The scheduler's only contact with state-home `scheduler_jobs.json` is an existence check on load/reload, with one actionable path-naming manual `cron`/`once` conversion notice visible in listing; it never reads, runs, rewrites, renames or deletes that file. TOML jobs can mutate/remove without legacy shadow checks. Distinguish this state-home file from pre-XDG `data/scheduled_jobs.json`, which `migrate.py` may still copy unchanged. Keep raw-TOML parsed-value roundtrips, type-strict checks, atomic patch/locking/hash/freshness and last-good/latch semantics, job history/notifications/ownership, historical log and execution-record readers, memory formats, PID lock, permissions and `/stop` unchanged.

## Consequences

- Good: one executable scheduler authority, explicit errors instead of silent fallback or conversion, and unchanged old-file bytes for manual recovery.
- Bad: operators must manually rewrite old config and jobs before those jobs run; notice and backup guidance must make the upgrade actionable.
- Follow-up: update operator documentation/config examples and migrate tests for contained failures without deleting generic persistence/ownership regressions during separately approved apply.
