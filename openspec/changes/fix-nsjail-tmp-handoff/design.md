## Context

`proposal.md` establishes three changes: (0) remove the `paths.tmp_dir` config override entirely, so `/tmp/{agent_name}` is the single, unconditional resolution used everywhere; against the `nsjail-shell-sandboxing` capability, (A) mount `/tmp/{agent_name}` read-write inside the jail at its real host path, immediately after the existing `/tmp` scratch mount; (B) inject `TMPDIR`/`TMP`/`TEMP` as base envars set to `/tmp`.

Relevant in-force ADRs: `ADR-0012` (nsjail shell isolation baseline), `ADR-0017` (session_logs read-only system-mount precedent — a mount that is granted by construction, not through the operator's zone-confirmation approval flow, and doesn't require operator approval). This change follows the exact same "system mount" shape ADR-0017 already established, just read-write instead of read-only. It does not touch nsjail's own configuration state (`trusted_dirs.json`, or anything `ADR-0015` governs) and does not add a new validation or approval framework. It does, however, add a writable mount outside the two categories ADR-0016's Decision 3 named ("`/tmp` scratch" and "explicitly-approved trusted RW directories") — see `adr.md` for the resolution: `adr/0018-mount-default-trusted-zones-into-nsjail.md` partially supersedes that decision's wording to recognize the pre-existing default-trusted-zone tier `tmp_dir` already belongs to.

### Component view (ASCII, component-level only)

```
main.py (composition root)
  tmp_dir = f"/tmp/{app_cfg.agent.agent_name}"
  (always this — no other way to configure it; already created via
   os.makedirs at startup, before the shell tool is ever invoked)
        |
        v
  BuiltinExecutor(tmp_dir=..., nsjail_session_tmpdir=..., ...)
        |
        v
  NsjailConfigBuilder(tmp_dir=...)
        |
        v  build(), per shell call
  nsjail config (.cfg):
    envar: PATH/HOME/LANG/TERM/TMPDIR=/tmp/TMP=/tmp/TEMP=/tmp
    mount: session_tmpdir -> "/tmp"        (unchanged, scratch, first)
    mount: tmp_dir -> tmp_dir (src==dst, rw, mandatory: true)
           (NEW, emitted immediately after /tmp)
        |
        v
  nsjail JAIL
    /tmp            = scratch (throwaway, unchanged)
    {tmp_dir}       = real host dir, RW, visible outside the jail via file_read
```

## Goals / Non-Goals

**Goals:**
- Close the stuck-loop bug: a file written under `tmp_dir` inside the jail is readable by `file_read` outside, and vice versa.
- Keep this minimal: no new config, no new validation layer, no change to the ephemeral scratch mount's lifecycle.

**Non-Goals:**
- Blocklist validation, viability checks, or missing-directory handling for this mount — not needed. `tmp_dir` is always `f"/tmp/{app_cfg.agent.agent_name}"`, with no other way to configure it, and `os.makedirs` already guarantees the directory exists (created at agent startup) before the shell tool is ever invoked. A malformed `agent.agent_name` is an existing configuration problem outside this change's scope, not something this mount adds handling for.
- Any change to the per-session scratch `/tmp` mount's *mechanism* — its creation, cleanup, and mount semantics are untouched, no relocation logic, no sweep, no lock-guarding added. (Its on-disk *location* does shift as a side effect of D0/D2, since it's created via `mkdtemp()` honoring the process's `TMPDIR` — see Risks.)
- `file_read`/`file_write` themselves, or the prompt conventions that already assume `/tmp/{agent_name}` is the shared drop zone (unaffected — this change only removes an override that could make the *resolved value* diverge, it doesn't change the mechanism).
- The raw-host-`/tmp` mount alternative (rejected in `proposal.md` — IPC socket exposure).

## Decisions

### D0 — `paths.tmp_dir` config override removed entirely; `access_control.py` and `main.py` both switch to the single hardcoded resolution

Today, two different, buggy computations of "the agent's temp directory" exist: `access_control.py`'s `TrustedZoneChecker` (`paths_config.tmp_dir` if an operator set it, else `f"/tmp/{agent_name}"`) and `main.py`'s own separate computation (previously cwd-basename-keyed, with a broken unset-fallback — see `proposal.md`'s Why; see D5 below for the follow-on decision that removed the cwd-basename `agent_name` source entirely). Rather than reconciling these two, the override that makes reconciliation necessary is removed: `PathsConfig.tmp_dir` (the config field), its parsing in `config_schema.py`, `access_control.py`'s fallback branch, and the `config.toml.example` documentation for it are all deleted. `access_control.py` and `main.py` both compute the exact same one-line expression, `f"/tmp/{app_cfg.agent.agent_name}"`, with no branching. This is what makes D2 below possible without reintroducing any divergence risk.

**Alternative considered**: keep `paths.tmp_dir` as a real override and make this new mount, `access_control.py`, and `main.py` all honor it consistently. Rejected: there is no legitimate use case for this directory to be anything other than `/tmp/{agent_name}`, and keeping the override alive means every future consumer of "the agent's temp directory" has to remember to resolve it the same way — exactly the kind of latent divergence that caused this change to exist in the first place.

### D1 — Mount emitted in `build()`, immediately after the `/tmp` scratch mount

This is a single, fixed mount — not derived from operator-managed `trusted_dirs.json` — so it is its own step inside `NsjailConfigBuilder.build()`, positioned immediately after the existing "# Session mounts" block (the `/tmp` scratch mount) so it is not shadowed and line order is deterministic.

### D2 — `main.py` passes the same D0 resolution to the mount; no separate computation for it

`main.py` computes `tmp_dir = f"/tmp/{app_cfg.agent.agent_name}"` (the same expression D0 puts in `access_control.py`) and passes it to `BuiltinExecutor`/`NsjailConfigBuilder`. `main.py`'s previous cwd-basename computation (`main.py:281-282`) is deleted, not patched — replaced by this one-line expression. There is exactly one place in the whole codebase this value is computed conceptually; `main.py` and `access_control.py` each evaluate the same literal expression rather than sharing a helper function, since introducing a shared helper for a single-line, dependency-free expression would be pure indirection.

**Alternative considered**: extract a shared helper (e.g. `resolve_tmp_dir(app_cfg)`) so `main.py` and `access_control.py` call the same function instead of duplicating the expression. Rejected as unnecessary: the expression is one line with no logic to drift, and a shared helper would be a module-boundary decision (which module owns it, what it imports) for no behavioral benefit.

**Rule (not optional)**: `tmp_dir` MUST NOT be re-derived anywhere inside `builtin_executor.py` or `nsjail_config.py` from `BuiltinExecutor`'s own `agent_name` parameter (used for unrelated things — the nsjail state directory, `trusted_dirs.json`, the conversation store). Even after D5 unifies `agent_name` to a single config-sourced value everywhere, `tmp_dir` stays an explicit, separately-threaded constructor argument, computed once in `main.py` and passed straight through — not reconstructed from `agent_name` inside a nested module. This keeps the two parameters decoupled at the module boundary even though they now always resolve consistently, so a future change to one doesn't have to reason about implicitly reconstructing the other.

### D5 — `agent_name` itself is unified to a single, config-sourced value everywhere; the cwd-basename source is deleted

Before this change, `main.py` computed a *second*, independent `agent_name` (`os.path.basename(os.path.abspath("."))`) used for the nsjail state directory, `trusted_dirs.json`'s location, the conversation store, and vault migration paths — distinct from `app_cfg.agent.agent_name` (the config-driven value D0/D2 use for `tmp_dir` and that already drove log paths and the vault). This was flagged during apply as exactly the kind of duplicate-source-of-truth problem this change exists to eliminate — the fact that it happened to be for a *different* directory (XDG state, not `tmp_dir`) doesn't make a second `agent_name` source any less of a footgun. The cwd-basename computation at `main.py:304` (formerly `main.py:281-282`/`:305`) is deleted; `_run()`'s `agent_name` parameter is now always `app_cfg.agent.agent_name`, and every consumer of it (`nsjail_state_dir`, `trusted_dirs_path`, `conversation_id`/`conversations_dir`, vault migration paths, `BuiltinExecutor`'s `agent_name` param, `TrustedZoneChecker`'s `agent_name` param) reads that single value. This makes the D2 "Rule" above about keeping `tmp_dir` and `agent_name` decoupled *more* important, not less: with only one `agent_name` left, it would be tempting to derive `tmp_dir` from it inline instead of passing it explicitly — D2 still requires the explicit, separately-threaded parameter.

**Alternative considered**: leave the cwd-basename `agent_name` in place since it's a different directory than `tmp_dir` and was out of this change's original stated scope. Rejected per explicit user direction during apply: "agent_name should be taken from the configuration file only" — there is no legitimate reason for the agent's identity to be keyed by the process's launch directory in one place and by config in another.

### D3 — `mandatory: true`

Same as the existing `/tmp` scratch mount and `skills_dir` (both already `mandatory: true` in this file) — not the `mandatory: false` mounts (`session_logs_dir`, CA certs, `/dev` nodes). The directory is guaranteed to exist by the time `build()` runs (created via `os.makedirs` at agent startup, before the shell tool is ever invoked), so there is no legitimate case where the mount should fail. If it somehow does fail, that indicates agent startup itself is broken, and the shell call should fail loudly rather than silently degrade — silently tolerating a missing mandatory directory would mask a real bug instead of surfacing it. Verified on a real nsjail binary: a `mandatory: true` mount with a missing source causes the whole jail launch to fail cleanly (nonzero exit, no partial/degraded jail) rather than a silent or partial failure.

**Alternative considered**: `mandatory: false` (warn and skip). Rejected: there is nothing to gracefully degrade to here — a missing directory at this point is not a normal configuration case, it's a broken invariant, and failing the shell call surfaces that immediately instead of reproducing a silent version of the original stuck-loop bug.

### D4 — `TMPDIR`/`TMP`/`TEMP` set to `/tmp`, unconditionally injected alongside `PATH`/`HOME`/`LANG`/`TERM`

Since `tmp_dir` is always a real, already-guaranteed-to-exist path and this envar assignment points at the ephemeral `/tmp` scratch mount (not at `tmp_dir` itself), there is nothing to gate on — these three envars are added to the same unconditional base-envar block as the existing four. Like the existing base envars, they remain overridable per call via `shell_env_set`'s `-E` flags, which take precedence over config `envar` entries as they already do today.

## Risks / Trade-offs

- **Risk**: `agent.agent_name` is set to something unusual (e.g. containing path separators). This mount derives its path directly from that value, with no additional check.
  → **Mitigation**: none needed — this is a pre-existing configuration concern, not something introduced by this mount, and out of scope for this change to fix.
- **Risk**: `mandatory: true` means a bug that somehow prevents the directory from existing at `build()` time (e.g. it was deleted after startup) breaks every subsequent shell call, rather than degrading gracefully.
  → **Accepted**: this is the intended behavior (see D3) — a missing directory here is a broken invariant, not a configuration case to tolerate silently.
- **Risk**: removing `paths.tmp_dir` (D0) is a breaking config change for any operator who had explicitly set it to something other than `/tmp/{agent_name}` — their setting is silently ignored after this change (falls back to `/tmp/{agent_name}` instead).
  → **Accepted**: there is no migration path that preserves an arbitrary custom value while also guaranteeing it's mounted into the jail without reintroducing validation machinery; this is judged an acceptable one-time breaking change for a config key with no legitimate reason to differ from its default.
- **Risk**: because `main.py`'s scratch-directory creation (`tempfile.mkdtemp()`, no explicit `dir=`) honors the process's `TMPDIR`, and `TMPDIR` is now reliably `/tmp/{agent_name}` (D0), the scratch directory's on-disk location moves to under `/tmp/{agent_name}` — i.e. inside the new mount. Verified on a real nsjail run: this does not create a live recursive bind mount or an infinite-loop hazard for tools like `find`/`du`/`tar` (bind mounts don't recursively re-trigger at arbitrary depth) — but it does mean the scratch directory's backing store becomes a second, writable path reachable from inside the jail (via `/tmp/{agent_name}/nsjail-tmp-XXXX/`, in addition to `/tmp`), and nsjail leaves a harmless empty placeholder directory behind as a mountpoint-creation side effect on every run.
  → **Accepted**: not fixed by this change (would require an explicit `dir=` on the existing `mkdtemp()` call, decoupling scratch creation from `TMPDIR` — out of scope, no functional bug demonstrated, and the script already has RW access to `/tmp` regardless of this path).
- **Risk** (found by code review during apply): `_load_trusted_mounts()` already skipped `trusted_dirs.json` entries nested under `session_tmpdir` to avoid a duplicate mount stanza for the same destination, but had no equivalent skip for entries nested under the new `tmp_dir` mount. A `trusted_dirs.json` entry pointing at or under `tmp_dir` (not reachable through the normal zone-confirmation approval flow, since `tmp_dir` already auto-classifies TRUSTED, but reachable via direct file edit or a migrated trust store) would have produced two `mount: { ... dst: <same path> ... }` stanzas for the same destination.
  → **Fixed**: `_load_trusted_mounts()` now also skips entries under `self.tmp_dir`, mirroring the existing `session_tmpdir` check (`nsjail_config.py`).
- **Risk** (found by code review during apply): `BuiltinExecutor.__init__`'s `tmp_dir` parameter defaulted to `""` with no validation, unlike `nsjail_session_tmpdir` which already gates nsjail activation on being truthy. A caller that omitted `tmp_dir` while enabling the nsjail backend would have gotten `mount: { src: "" dst: "" ... mandatory: true }` in the generated config, which nsjail rejects at startup, silently breaking every shell call for that caller.
  → **Fixed**: the nsjail-activation condition in `BuiltinExecutor.__init__` now also requires `tmp_dir` truthy (`shell_backend == "nsjail" and nsjail_session_tmpdir and tmp_dir`), matching the existing `nsjail_session_tmpdir` gating pattern — a caller that omits it simply doesn't get the nsjail backend activated, rather than getting a broken mount.

## Migration Plan

No data migration. Land `config_schema.py`/`access_control.py`/`config.toml.example`/`nsjail_config.py`/`builtin_executor.py`/`main.py` changes together (interdependent — the config key removal and the mount must ship in the same release). No operator action required for the common case (`paths.tmp_dir` unset). An operator who had explicitly set `paths.tmp_dir` will silently stop seeing it honored — not detected or warned about, per the accepted risk above. Rollback = revert the commit(s); nothing depends on the new mount, envars, or the removed config key.

## Open Questions

None.
