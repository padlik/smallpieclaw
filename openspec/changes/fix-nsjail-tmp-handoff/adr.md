# ADR Review Manifest

- Status: completed
- Review date: 2026-07-31

## Review Summary

ADR review completed for this change. Every ADR under `adr/` was read. An earlier draft of this manifest claimed ADR-0016 was "not implicated" — that was wrong: ADR-0016's Decision 3 confines the sandbox's writable scope to `/tmp` scratch and explicitly-approved trusted RW directories, and this change's new mount is neither (it's a default-trusted zone, not operator-approved). A new ADR was written to record the actual boundary rather than leave the architecture record asserting something the code contradicts.

## In-Force ADRs Reviewed

- ADR-0012 (use nsjail for shell isolation) — reviewed; this change extends the nsjail backend ADR-0012 established with one additional mount and three additional base envars, without altering any of its decisions.
- ADR-0015 (nsjail state outside sandbox write scope) — reviewed; not implicated. This change does not touch `trusted_dirs.json` or any nsjail configuration state, and does not relocate or add to `nsjail_state_dir`.
- ADR-0016 (remove project_dir from nsjail sandbox) — reviewed; **partially superseded**. Decision 3 ("writable scope confined to `/tmp` scratch and explicitly-approved trusted RW directories only") didn't account for `access_control.py`'s pre-existing default-trusted tier (`workspace_dir`/`downloads_dir`/`tmp_dir` — all auto-allow, no operator action, same tier as directories an operator approves through the zone-confirmation flow). See the new ADR below.
- ADR-0017 (mount session_logs read-only in nsjail) — reviewed; not directly applicable as precedent here, since its safety argument is conditioned on the mount being read-only, and this new mount is read-write.
- All other ADRs (0001–0011, 0013–0014) — reviewed; no interaction with this change.

## New Durable ADRs Created

- `adr/0018-mount-default-trusted-zones-into-nsjail.md` — records that the sandbox's writable scope (ADR-0016 Decision 3) includes the agent's default-trusted zones (`workspace_dir`, `downloads_dir`, `tmp_dir`) when a specific mount for one is defined in the `nsjail-shell-sandboxing` spec, not only `/tmp` scratch and operator-approved directories. `fix-nsjail-tmp-handoff` mounts exactly one of these zones (`tmp_dir`); `workspace_dir`/`downloads_dir` remain unmounted. See the ADR file itself for full Context/Decision/Consequences — not duplicated here.
