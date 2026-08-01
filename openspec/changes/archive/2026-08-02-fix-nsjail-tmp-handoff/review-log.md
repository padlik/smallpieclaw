## proposal Round 1 — 2026-07-30
### 🔴 Fixed
- Proposal's premise that the nsjail scratch dir and `paths.tmp_dir` were unrelated was wrong: `main.py:375-377` sets `TMPDIR`/`TMP`/`TEMP` to `tmp_dir` before the scratch dir is created via env-derived `mkdtemp` (`main.py:409`), so the scratch dir normally nests inside `tmp_dir` → rewrote proposal around a three-requirement design (mount + TMPDIR/TMP/TEMP injection + scratch/tmp_dir disjointness), rejected raw-`/tmp` mount alternative (IPC socket exposure).

## proposal Round 2 — 2026-07-30
### 🔴 Outstanding
- Requirement A's anti-escape constraint (mount source resolved once at startup, never re-derived from live env / `shell_env_set`) is present in explore-brief.md but missing from proposal.md — needed before spec deltas are written, since it's the exact constraint the mount-requirement scenarios must encode.
- Requirement C (scratch/tmp_dir disjointness) has no ADDED/MODIFIED target requirement named, and proposal.md's own "no change to persistence/cleanup behavior" line contradicts it needing to modify "Per-session /tmp persists across nsjail invocations" (`spec.md:70-84`).
### 🟡 Addressed (pending)
- Warn-on-skip behavior for blocklist-rejected/missing tmp_dir not stated (silent skip reproduces the original bug).
- Mount ordering (must be emitted after the `/tmp` scratch mount) not stated as an outcome-level behavior.
- Requirement B override semantics via `shell_env_set` not stated (override changes only where scripts write, not the mount — output doesn't survive the jail).
- `HOME=/tmp` interaction with the new TMPDIR mount not clarified (HOME-derived paths intentionally stay ephemeral).

## proposal Round 3 — 2026-07-30
All round-2 items verified resolved (anti-escape constraint, disjointness MODIFIED target, warn-on-skip, mount ordering, override semantics, HOME interaction).
### 🟡 Addressed
- Skip/envar coupling (when mount skipped, TMPDIR/TMP/TEMP not injected either) — added.
- Symlink/realpath handling for the mount destination — added.
- Duplicate-mount tolerance when tmp_dir is also operator-added as a trusted dir — added (new mount emitted last, takes effect regardless).
- Loose `config_schema.py:503` citation for the `/tmp/{agent_name}` default — corrected to `main.py:282`, `access_control.py:122`.
### 🔴 Outstanding
(none)

**proposal.md is FROZEN as of this round.**

## design Round 1 — 2026-07-30
### 🔴 Fixed
- D3 falsely claimed main.py's raw `tmp_dir` local was already the value `TrustedZoneChecker` consumes — two divergent resolutions actually exist (main.py:282 cwd-basename-keyed vs. access_control.py's agent_name-keyed realpath/expanduser). Pinned a single resolution (the TrustedZoneChecker's), made unifying main.py's computation with it explicitly in-scope.
- `mandatory: true/false` and per-call-vs-startup viability were deferred to design by the brief but never answered. Added D0: `mandatory: false`, viability re-checked every `build()` call, gating both mount and envar injection (D4).
### 🟡 Addressed
- D2's `_is_blocked_path(path)` signature couldn't serve both the full-tuple and system-only-tuple call sites as required — parameterized by `prefixes: tuple`, resolving the previously-open scoping question (skills_dir keeps its narrower tuple, no behavior change).
- Corrected call-site count (2 existing + 1 new, not 3 existing).
- Added mount-point-creation-inside-scratch-/tmp note (precedented by skills_dir/session_logs_dir, recommend real-nsjail integration test).
- Added explicit ADR-0015 carve-out invariant (only the leaf scratch dir is ever a mount source, never the state-dir root) and flagged it under Open Questions for the adr step rather than silently claiming no conflict.
- Risks section: replaced incorrect "relative path causes skip" claim with the accurate enumerated skip-condition list; added stale nsjail-tmp-* accumulation + disk-fill-mode risk with a startup-sweep mitigation; added explicit disjointness-relies-on-relocation-not-assertion risk with rationale for why it's accepted.
- D4 restates shell_env_set override semantics; D3 states empty-string tmp_dir behavior explicitly.
### 🔴 Outstanding
(none — pending round 2 re-review)

## design Round 2 — 2026-07-30
### 🔴 Outstanding — UNFREEZE proposal.md
- Design round 2 review discovered (and orchestrator independently verified against `config_schema.py:666,672` and `main.py:282`) that `paths.tmp_dir`'s unset default silently resolves to the **project/cwd directory**, not `/tmp/{agent_name}` as documented (`config.toml.example:293`) — an empty-string value is mirrored back into the raw config dict, masking the intended fallback. This is a decision-level correction to `proposal.md`'s "Why" (root cause), "What Changes" (needs a 4th item fixing the default), and "Impact" (blast radius is no longer nsjail-only — process-wide TMPDIR/TMP/TEMP, prompt-visible path). User decided (via AskUserQuestion): fix both bugs together in this change.
- **proposal.md UNFROZEN and amended**: Why/What Changes/Impact updated to describe the config_schema.py default-resolution fix alongside the nsjail mount fix; Capabilities left unchanged (no capability governs config-parsing defaults, so this is an Impact-only addition, not a new capability delta).
- design.md's Context "Critical correction" subsection and D5's trade-off analysis are now known to rest on incorrect evidence (assumed the two resolutions merely diverged by cwd-basename vs realpath, when in fact main.py's default resolves to the project dir due to the masking bug) and must be rewritten before design round 3.
### 🟡 Outstanding (carried to design round 3)
- Diagram in design.md contradicts D1 on mount order (new mount shown before `/tmp` mount despite text saying "after").
- Replacement "relative path doesn't trigger skip" claim in Risks is itself wrong under the unified resolution (relative values resolve under `_agent_dir`, which IS blocklisted) — needs correction, not the same fix as before.
- ADR-0016 (sandbox RW scope limited to /tmp + explicitly-approved trusted dirs) is a literal carve-out this change makes, same as ADR-0015 — needs the same explicit-invariant + Open-Questions treatment, not "not touched by this change."
- Existing spec sentence `spec.md:37` about `~/.local` being blocked will read as contradicted by the new mount unless design notes the leaf-only-bind distinction.
- D0's viability predicate ("exists") vs Risks' predicate ("isdir") inconsistent — pin one (recommend isdir, matching skills_dir/session_logs_dir precedent).
- Stale-scratch-dir sweep mitigation needs a concrete, implementable criterion (or scope out) — "older than the current process's own lifetime" isn't realizable as stated.

## proposal Round 4 — 2026-07-30
### 🔴 Fixed
- Two different `{agent_name}` conventions collide (main.py's cwd-basename vs access_control.py's/descriptors.py's app_cfg.agent.agent_name) — pinned the fix to app_cfg.agent.agent_name (config_schema.py already has this exact derivation pattern at :232/:251), which provably leaves access_control.py's resolved trusted value unchanged.
- Impact's claim that access_control.py "already does the right thing" was hand-wavy; corrected to state the resolved value is provably unchanged (proof: fix matches its existing fallback exactly), only main.py's divergent computation was wrong.
### 🟡 Addressed
- Capabilities section: acknowledged file-access-zones/spec.md:20 and trusted-dir-management/spec.md:11,73 name tmp_dir by name (unaffected since value doesn't move); explained why no delta needed.
- Expanded consumer inventory: main.py:330 makedirs semantics, prompt_loader.py, agent_controller.py/react_loop.py/agent_runtime.py inheritance (all consume the single threaded value, no separate fixes needed).
- Added explicit no-migration statement for pre-existing cwd-deposited content.
- Corrected "project/repo directory" framing to "process cwd, normally the repo, but a misconfigured unit could differ."
- Added config.toml.example doc-comment correction to Impact/Code list.
### 🔴 Outstanding
(none — pending round 5 re-review)

## proposal Round 5 — 2026-07-30
### 🔴 Fixed
- Skip conditions for the new mount didn't exclude `tmp_dir == "/tmp"` (or containment with the scratch dir) — since `/tmp` isn't in `_BLOCKED_SYSTEM_PREFIXES`, an operator setting `paths.tmp_dir = "/tmp"` would've gotten real host `/tmp` mounted RW, reopening the exact IPC-socket escape the rejected alternative rules out. Added explicit skip condition + rationale.
### 🟡 Addressed
- Qualified "provably unchanged" claim for the `agent_name = ""` edge case (AgentConfig.agent_name has no `or`, unlike the pinned derivation) — strictly safer but not literally unconditional; documented in Impact.
- Corrected false claim that `descriptors.py`/`prompt_builder.py` read `app_cfg.agent.agent_name` dynamically — they hardcode the literal `"piclaw"`; added as explicit non-goal.
- `main.py`'s fix now explicitly reads `app_cfg.paths.tmp_dir` (not the mirrored `cfg` dict, which only mirrors when `[paths]` exists at all) — closes the "patch config_schema.py only and call it done" gap.
- Stated `PathsConfig.tmp_dir` dataclass default (`:503`) stays `""` — fix lives in `_parse_paths` only, `access_control.py`'s fallback remains as safety net for direct `PathsConfig` construction (tests).
### 🔴 Outstanding
(none — pending round 6 re-review)

## proposal Round 6 — 2026-07-30
Verdict: ready for design round 3, no new blocker introduced across 6 rounds of accumulated edits; all round-5 items verified substantively resolved against source.
### 🟡 Addressed (declarative, folded in without a 7th review round per reviewer's own recommendation)
- Disjointness bullet reworded from absolute invariant to accurate two-part claim (no accidental nesting vs. detected-and-skipped deliberate operator misconfiguration) — matching Capabilities MODIFIED entry reworded to match.
- Added explicit note that `main.py:305`'s separate cwd-basename `agent_name` (which keys `nsjail_state_dir` / trusted_dirs.json / conversations / session logs) is intentionally unchanged and out of scope, distinct from the `app_cfg.agent.agent_name` this proposal pins `tmp_dir` to.
### 🔴 Outstanding
(none)

**proposal.md is RE-FROZEN as of this round** (process note: this batch went through 6 total review rounds — 3 pre-unfreeze, 3 post-unfreeze — exceeding the nominal 5-round soft cap; flagged to user mid-cycle, proceeded since each round found genuine new issues rather than diminishing returns, and round 6 came back clean).

## design Round 3 — 2026-07-30
Rewrote design.md's Context/D0/D5/Risks/Open-Questions against proposal.md's re-frozen (round-6) premise.
### 🔴 Fixed
- Context "Critical correction" section rewritten: replaced the wrong "diverge by cwd-basename vs realpath" framing with the actual config_schema.py empty-string-mirroring bug, pinned the fix to app_cfg.agent.agent_name (matching access_control.py/config_schema.py's existing :232/:251 derivation), removed the now-inapplicable "realpath/expanduser unification" framing (config_schema.py's _expand_path already handles this).
### 🟡 Addressed
- D5's Risks trade-off flipped from "regression: tmpfs → persistent disk" to "strict improvement in the default case" (scratch dir today lands in the project cwd, not a /tmp-rooted location, due to the corrected root cause).
- Added tmp_dir=="/tmp"/containment skip condition to D0 (proposal round 5's fix), reusing nsjail_config.py:247's existing session_tmpdir containment-skip pattern.
- Added ADR-0016 explicit-invariant carve-out (parallel to ADR-0015's), flagged under Open Questions for the adr step.
- Added explicit note that spec.md:37's ~/.local blocklist sentence isn't contradicted (describes trusted-dir mounts, unaffected; scratch /tmp mount has always bypassed blocklist unconditionally, unchanged by relocation).
- Pinned D0's viability predicate to os.path.isdir (matching skills_dir/session_logs_dir), not os.path.exists.
- Corrected "relative path doesn't trigger skip" Risks claim — now correctly states relative values resolve under _agent_dir (blocked).
- Gave concrete stale-sweep criterion: remove all nsjail-tmp-* dirs under nsjail_state_dir except the one just created this run, justified by the existing PID-file-lock singleton guarantee.
- Migration Plan updated to include config_schema.py fix and the stale-sweep behavior.
### 🔴 Outstanding
(none — pending design round 3 re-review)

## design Round 3 re-review — 2026-07-30
Reviewer verdict: proposal.md's realpath requirement for the mount destination was contradicted by design's "no separate normalization step is needed" claim, and — since main.py:281-282's abspath is being deleted per the proposal — this left relative paths.tmp_dir values able to bypass D2's blocklist (all prefixes are absolute).
### 🔴 Fixed
- Pinned single normalization at the composition root: `tmp_dir = os.path.realpath(os.path.abspath(os.path.expanduser(app_cfg.paths.tmp_dir)))`, computed once in main.py, consumed identically by makedirs/envars/BuiltinExecutor/D2 blocklist/D0 checks/mount src+dst. Removed the incorrect "no normalization needed" claim.
### 🟡 Addressed
- Made `tmp_dir == "/tmp"` a standalone skip predicate (not folded into the containment check) — post-D5 the scratch dir lives under nsjail_state_dir, not under /tmp, so a containment check alone wouldn't catch it.
- Hardened the stale-sweep criterion: PID-lock singleton argument doesn't cover the case where two installs share a cwd-basename-derived nsjail_state_dir but different pid_file paths — replaced with a lock/PID-marker-based liveness check instead of the unqualified "except the one just created" claim.
- Added the missing `agent_name = ""` Risks entry that Context's cross-reference pointed to (was previously only in proposal.md's Impact, not design.md).
### 🔴 Outstanding
(none — pending final confirmation review)

## design Round 3 confirmation review — 2026-07-30
Reviewer treated this as a final confirmation pass (review-log was ahead of the brief it was given); flagged the brief/log mismatch but it didn't affect the verdict. All round-1/round-3 items it could verify against source checked out. Found two **new** blockers introduced by the round-3 and round-3-re-review edits themselves.
### 🔴 Fixed
- The pinned normalization formula (`realpath(abspath(expanduser(app_cfg.paths.tmp_dir)))`) had no emptiness guard before `abspath` — `os.path.abspath("")` resolves to the process cwd, silently reintroducing the exact root-cause bug this change fixes and making D0's "empty ⇒ skip" arm unreachable from main.py's only production call path. Added an explicit `if raw_tmp_dir else ""` guard evaluated before `abspath`, pinned in Context with the code snippet inline.
- D5's stale-sweep liveness marker (added in the round-3 re-review) was specified as living *inside* each scratch dir — but the scratch dir is the mount source for the unconditional, `mandatory: true` `/tmp` mount (`nsjail_config.py:387-390`), so a sandboxed script could delete/rewrite the marker and cause one instance's sweep to `rmtree` a second, live instance's mandatory mount out from under it. Promoted the sweep to its own decision (**D6**): the liveness marker is now a **sibling** `.lock` file directly under `nsjail_state_dir`, never inside the bind-mounted leaf — keeping the ADR-0015 carve-out (D5) limited to inert scratch *contents*, not control state.
### 🟡 Addressed
- Fixed the component diagram: mount order now shows `session_tmpdir -> /tmp` emitted *before* the new `tmp_dir` mount (matching D1's "immediately after /tmp" text, previously contradicted); normalization line updated from the stale "no separate resolution here" to the pinned guarded formula.
- Promoted the stale-scratch sweep from a Risks-only mitigation to D6 (own decision, own alternative-considered), added a matching 5th "What Changes" bullet + Impact note to `proposal.md` via soft-freeze (declarative addition, no unfreeze needed) — gives specs/tasks something to trace to.
- Added an explicit note in D0 and a proposal.md soft-freeze edit reconciling the `isdir`-vs-`"does not exist"` predicate mismatch between design.md and proposal.md's wording.
- Fixed stale line-number citations: `nsjail_config.py:395`/`:421` → `:393`/`:416`; `config_schema.py:605` → `:604`.
### 🔴 Outstanding
(none — fixes applied by orchestrator, not yet independently re-reviewed; design round 4 launched to confirm before freezing)

## design Round 4 — 2026-07-30
Confirmed both round-3 blockers were genuinely fixed (verified against actual file content: guarded formula present, D6 exists with sibling-lock marker outside the mounted leaf, diagram order corrected, proposal.md soft-freeze additions genuinely declarative). Found one **new** 🔴, introduced by D6 itself, plus 🟡s.
### 🔴 Fixed
- D6's liveness rule had no arm for the "no `.lock` sibling exists" case — which is the state of every directory the sweep is actually meant to reclaim (all pre-change leaks, and any crash between `mkdtemp` and lock creation). The obvious default reading ("no lock ⇒ delete") reopens the exact live-mount-deletion hazard D6 was created to close; the other reading ("no lock ⇒ leave") makes the sweep a no-op for its stated purpose. Fixed by adding a coarse, blocking `nsjail_state_dir/.sweep.lock` held across the whole scan+create sequence, closing the window in which a legitimate directory could transiently lack its `.lock` sibling — under that lock, "no `.lock` sibling" is now unconditionally safe to delete, and the sweep can reclaim legacy leaks.
- Regression from the round-3 fix pass: `config_schema.py:604` → corrected back to `:605` (`AgentConfig.agent_name` parsing line).
### 🟡 Addressed
- Fixed the two backwards "above"/"below" cross-references between D5's Risks mitigation and D6.
- Added the D6 sweep to `proposal.md`'s MODIFIED `nsjail-shell-sandboxing` Capabilities entry (previously only in "What Changes" prose/Impact — Capabilities is what the specs step derives deltas from).
- Added an explicit one-line statement of `main.py`'s behavior when `tmp_dir` normalizes to `""` (skips `makedirs`/env assignment, still threads `""` through to `BuiltinExecutor` for D0 to skip on).
- Repositioned the 5th "What Changes" bullet (D6 sweep) to sit with the other four bullets instead of after the closing "None of this changes…" paragraph.
- Fixed `main.py:333-335` → `:334-336` citation mismatch in `proposal.md` Impact (design.md already had the correct line).
### 🔴 Outstanding
(none — fixes applied by orchestrator, not yet independently re-reviewed; design round 5 launched to confirm before freezing)

## design Round 5 — 2026-07-30
Confirmed the `.sweep.lock` serialization logic from round 4 is sound on independent re-derivation, and all citations verified correct against source. Found one **new** 🔴: a naming collision between D6's scan pattern and its own lock-file naming.
### 🔴 Fixed
- D6's scan for `nsjail-tmp-*` "entries" matched both scratch directories *and* their `.lock` sibling files (both share the `nsjail-tmp-*` prefix) — the straightforward reading treats a lock file itself as a sweep candidate, unlinks a live process's lock file, and lets a *later* sweep then delete that now-lockless (but still live) scratch directory, reopening the exact hazard rounds 3/4 closed via a namespace collision instead of a logic gap. Fixed by pinning candidates to `os.path.isdir(entry)` directories only, excluding the `.lock` filename suffix explicitly.
### 🟡 Addressed
- Pinned `.sweep.lock` and the per-directory lock to `fcntl.flock`-style advisory locks (matching `_PidFileLock`, `main.py:37`) — explicitly rejecting a "lock file + poll for existence" implementation, which would deadlock all future startups permanently after any crash mid-sweep.
- Added explicit ENOENT-tolerant removal semantics (a target already gone during a concurrent shutdown is treated as success, not an error).
- Added explicit shutdown-unlinks-the-lock-sibling clause, closing an orphan-lock-file accumulation gap.
- Removed the stale Open Question item claiming the D6/proposal soft-freeze additions were still pending — they're present as of round 4.
- Reworded `proposal.md`'s "Three behavioral requirements" lead-in (now describes five bullets: 3 capability requirements + 1 config-parsing fix + 1 capability-folded sweep).
- Qualified the Context diagram's "single-process" Assumptions line, which previously contradicted D5's own Risks entry about `_PidFileLock`/`nsjail_state_dir` keying independently — now explicitly attributes concurrent-process safety to D6's `.sweep.lock` protocol, not a single-process invariant.
### 🔴 Outstanding
(none — fixes applied by orchestrator, not yet independently re-reviewed; design round 6 launched to confirm before freezing)

## design Round 6 (final) — 2026-07-30
Reviewer independently re-derived D6's full candidate-selection + three-case logic end to end with the round-5 directory-only filter in place, verified the flock pinning is unambiguous, confirmed the shutdown-unlink/ENOENT additions don't conflict with the existing `main.py:725` cleanup path, and confirmed both round-5 rewording fixes (proposal.md lead-in, design.md Assumptions line) are internally consistent with the rest of both documents. **No 🔴.**
### 🟡 Folded in before freezing (both declarative, no decision change; the first has real blast radius so applied regardless of process)
- D6's candidate predicate was phrased as two necessary conditions (`isdir` + not-`.lock`) with the `nsjail-tmp-*` prefix demoted to an "i.e." gloss — read literally (missing the prefix as a hard requirement), any directory under `nsjail_state_dir` not ending in `.lock` and with no `.lock` sibling qualifies for case (c) unconditional removal, which would `rmtree` `conversations/` (`main.py:344`) on the next startup. Rewrote as three explicit conjunctive conditions (prefix, not-`.lock`-suffixed, `isdir`), all normative.
- Case (c)'s justification falsely claimed it lets the sweep "reclaim legacy leaks" — pre-this-change leaks were created under the buggy `TMPDIR`-derived cwd location, never under `nsjail_state_dir` (per `proposal.md`'s Impact, which states the sweep never scans the cwd), so there is nothing of that kind here to reclaim. Reworded to describe only the two cases that actually exist: a crash in the `mkdtemp`→lock-creation window, and any future/partial build lacking lock support.
### 💡 Folded in as low-cost hardening (optional, non-blocking per the reviewer)
- Pinned the per-directory liveness probe as a non-blocking (`LOCK_EX | LOCK_NB`) try-acquire, explicit rather than implied.
- Pinned shutdown order: `rmtree` the scratch directory before unlinking its `.lock` sibling, so the directory is never observably lockless while still present; both steps stated as ENOENT-tolerant, matching `main.py:725`'s existing `ignore_errors=True` posture.
### 🔴 Outstanding
(none)

**design.md is FROZEN as of this round.**

## specs Round 1 — 2026-07-30
Reviewer verified MODIFIED-requirement discipline (full baseline blocks + scenarios preserved) and D0/D2/D3/D4's implementation-level judgment calls were correctly reflected. Found 3 🔴 gaps versus frozen design.md.
### 🔴 Fixed
- `mandatory: false` was nowhere in the spec text (only implied) — an implementer could reach for the `skills_dir` `mandatory: true` precedent instead, producing a strictly worse failure mode (every shell call breaks on a misconfigured tmp_dir) than the bug this change fixes. Added explicitly to the requirement text plus a mount-attributes scenario.
- D6's case (c) ("no lock sibling exists ⇒ safe to remove, but only because the coarse `.sweep.lock` rules out the transient-creation race") and the `.sweep.lock` serialization itself were both entirely missing from the spec — the single hardest-won piece of design.md's last 3 rounds. Added full three-case liveness logic and the coarse-lock serialization to the (folded-in) per-session-/tmp MODIFIED requirement, plus scenarios for the lockless-reclaim case and for concurrent-instance safety.
- D6's three-condition candidate filter (name prefix + not-`.lock`-suffixed + `isdir`) had been demoted to a scenario-only parenthetical, reopening the `conversations/`-deletion hazard design round 6 caught. Promoted to normative requirement text; added a scenario asserting unrelated `nsjail_state_dir` entries are never swept.
### 🟡 Addressed
- Added per-call viability re-check scenarios (tmp_dir removed/created mid-session).
- Added ENOENT-tolerant sweep-removal scenario and requirement text.
- Added shutdown ordering (rmtree before lock unlink) to both the requirement text and its scenario.
- Folded the sweep into the MODIFIED "Per-session /tmp" requirement (was a second stray ADDED requirement) to match frozen proposal.md's Capabilities shape (1 ADDED + 2 MODIFIED, sweep folded into the per-session-/tmp MODIFIED entry).
- Scoped "the new mount is emitted last" to the specific trusted-dir-duplicate case, and separately stated D1's absolute ordering (after /tmp, before session_logs_dir/skills_dir) in the main requirement paragraph.
- Stated the mount is a system mount (not requiring `/dir add` operator approval), matching the `session_logs_dir` requirement's precedent.
- Added scenarios for the two previously-untested skip conditions (empty tmp_dir, tmp_dir-is-a-regular-file).
- Sub-agent-sharing open question left unaddressed by design: confirmed proposal.md's Non-Goals already covers this ("inherits whatever sharing model BuiltinExecutor already has for sub-agents") — no spec scenario needed, no action taken.
### 🔴 Outstanding
(none — fixes applied by orchestrator, not yet independently re-reviewed; specs round 2 launched to confirm before freezing)

## specs Round 2 — 2026-07-30
Independently re-verified all three round-1 blocker fixes against the rewritten file (not just the round-1 summary), re-traced D0 through D6 fresh against the spec text, and re-confirmed MODIFIED-block completeness (both original scenarios preserved in each of the two MODIFIED requirements). **No 🔴.**
### 🟡 Folded in before freezing (both declarative, no decision change)
- Added an explicit "non-blocking try-acquire, never wait on the lock" clause to the liveness-check sentence — the loose "if a live process holds that lock" wording didn't rule out a blocking acquire, which would hang the sweep (and thus agent startup) forever against a live holder; this is exactly the deadlock D6 was written to avoid.
- Added an explicit "this mount bypasses the trusted-directory blocklist unconditionally, like `session_logs_dir`" sentence — the scratch directory's new location (rooted under the nsjail state directory) would otherwise read as falling under the targeted user-prefix blocklist if a reader didn't already know the `/tmp` scratch mount has always bypassed it by construction; design.md's own final Open Question flagged this exact gap for the specs step.
- Removed a stray `.design.md.swp` editor artifact from the change directory.
### 🔴 Outstanding
(none)

**specs batch is FROZEN as of this round.**

## MAJOR UNFREEZE — 2026-07-30 (user correction, post-adr-round-1)
User reviewed the accumulated design (D0-D6, blocklist validation, scratch-dir relocation under `nsjail_state_dir`, startup sweep with lock files, ADR-0018 carve-out) and rejected it outright as unnecessary complexity built on wrong premises:
- The "scratch directory relocation + sweep" machinery (D5/D6) was solving a problem that only existed because the design pointed `TMPDIR` at the persisted `tmp_dir`; nsjail's `/tmp` is already ephemeral/self-cleaning by design and needed no changes at all.
- The blocklist/viability/mandatory-flag framework (D0/D2/D3) was defensive engineering against misconfigurations that can't happen: `agent_name` always has a config default, and the directory is already guaranteed to exist before the shell tool runs.
- The `/dir add` trusted-dir-duplicate-mount interaction (D1's tolerance scenario) was flagged as an edge case not worth handling.
- Directive: mount `/tmp/{agent_name}` RW immediately after `/tmp`; inject `TMPDIR`/`TMP`/`TEMP` = `/tmp` (not the persisted dir); no blocklist/validation for this mount; no scratch-dir changes of any kind; no new config.
**`proposal.md`, `design.md`, and the specs delta are UNFROZEN and rewritten from scratch** to this minimal shape. `adr/0018-...md` (drafted, never committed) is deleted — the simplified design no longer contradicts ADR-0015/ADR-0016 in any way requiring a new ADR; it is a direct instance of the existing ADR-0017 system-mount pattern. The change-local `adr.md` manifest is rewritten to reflect "no new ADR needed."
No new review-agent round launched per this cycle — user asked to review the simplified artifacts directly before further token spend on automated review.

## Further user tightening — 2026-07-30
User reviewed the simplified rewrite and requested three more corrections (declarative, no re-unfreeze needed — same batch, not yet re-frozen):
- `tmp_dir` must be exactly `f"/tmp/{app_cfg.agent.agent_name}"` with no operator-override branch at all (dropped the "or paths.tmp_dir if set" fallback from D2 — confirmed there is no legitimate reason for the mounted path and the agent-name-derived path to diverge).
- Confirmed "no checking" is correct, with the reasoning made explicit: the directory is guaranteed to exist by agent startup, so a missing directory at mount time is a broken invariant, not a configuration case — changed D3 from `mandatory: false` to `mandatory: true` so the shell call fails loudly instead of silently degrading.
- Confirmed a malformed `agent.agent_name` is a pre-existing configuration problem out of scope for this change, not something to add handling for.
Applied to `proposal.md`, `design.md`, and the specs delta.

## VM-verified findings + further scope decision — 2026-07-30
Live-tested on the `nsjail-test` Lima VM (real nsjail binary, not theoretical) rather than reasoning abstractly, per user's explicit request:
- Confirmed the core mount fix works end-to-end: a file written inside the jail to `/tmp/piclaw/...` is immediately visible on the host, and a file present on the host beforehand is readable from inside the jail.
- Confirmed `mandatory: true` on a missing mount source fails the whole jail launch cleanly (exit 255, no partial/degraded jail) — matches the "let it fail, don't tolerate configuration issues" directive.
- Found (and confirmed empirically) that the reviewer's "infinite recursion" claim about the scratch-dir-nested-inside-tmp_dir was overstated — it's not a live recursive mount, just a harmless empty leftover directory nsjail creates as a mountpoint side effect. Traced the root cause via `git log -L`: `main.py`'s `os.environ["TMPDIR"] = tmp_dir` (commit `a7cfac0`) exists for an unrelated, legitimate reason (subprocess-backend and MCP-server env inheritance), and the nsjail scratch dir's `mkdtemp()` call just incidentally inherits it since it passes no `dir=`. Decided: kept as a known, accepted cosmetic artifact — no dir= fix added, since the user separately confirmed (via the "who needs scratches" discussion) that the two-mount design (ephemeral `/tmp` scratch unchanged + separate `/tmp/{agent_name}` mount) is correct as-is, and nothing here needs fixing beyond what's already planned.
- **New decision (D0)**: the `paths.tmp_dir` config override is removed entirely — from `config_schema.py`, `access_control.py`'s `TrustedZoneChecker` fallback, and `config.toml.example` — not just ignored by this new mount. `access_control.py` and `main.py` both switch to the single hardcoded `f"/tmp/{app_cfg.agent.agent_name}"` resolution, eliminating any possibility of the trusted directory, the created directory, and the mounted directory disagreeing. This expands the change's blast radius slightly (touches `access_control.py`, a file previously declared out of scope) but was an explicit, deliberate user decision after a concrete divergence example was walked through.
Applied to `proposal.md` and `design.md` (added D0; updated D2's rationale, Risks, Migration Plan, Non-Goals). No spec delta changes needed — `file-access-zones`/`trusted-dir-management` specs reference `tmp_dir` only by name/identity, not by resolution formula, so their requirements are unaffected by this decision.

## Review round (report-only) — 2026-07-31
Full re-review of all four artifacts against current source. Found 2 🔴 + 7 🟡. User addressed each directly:
### 🔴 Fixed
- "No observable change in the common case" (proposal.md Impact) was wrong — traced and confirmed `main.py:282`'s default resolves to the process cwd today (config-mirroring bug), not `/tmp/{cwd_basename}`; D0 actually relocates `TMPDIR`/`TMP`/`TEMP`, the scratch dir's on-disk location, `os.makedirs`'s target, and the prompt-visible path. User: don't over-explain the mechanism — just state tmp_dir=/tmp/{agent_name} at startup, mechanism is an implementation detail; rewrote Why/Impact accordingly, much shorter.
- ADR-0016 conflict (adr.md's "not implicated" claim contradicts ADR-0016 Decision 3's writable-scope wording verbatim; ADR-0017 can't be borrowed as precedent since its rationale is read-only-conditioned) — **not yet resolved, awaiting user decision on approach** (amend/partial-supersede ADR-0016 vs. write a new ADR vs. other).
### 🟡 Addressed
- D3's "unlike skills_dir" framing was backwards (skills_dir and the /tmp scratch mount are both already mandatory:true in real code) — fixed, cited the VM-verified failure behavior directly in the decision text.
- BuiltinExecutor already has its own agent_name param (cwd-basename-sourced, unrelated purpose) that could be mistaken for tmp_dir's source — promoted from a review-only observation to an explicit, non-optional Rule in D2.
- Added the missing spec scenario pinning the mandatory:true failure behavior (VM-verified: shell call fails, no degraded jail).
- Added missing Impact items: tests/test_config_schema.py, tests/test_access_control.py, README.md (all reference paths.tmp_dir and break/go stale without updates).
- Non-Goals' "no change to scratch dir location" was contradicted by D0's actual side effect — reworded to distinguish mechanism (unchanged) from on-disk location (moves, as an accepted D0 side effect) — added as a new Risk with the VM-verified "no infinite loop, but reachable second writable path + harmless placeholder junk" finding.
- explore-brief.md staleness — user: it's a historical record of the exploration phase, was never meant to track the current design; leaving as-is, no action.
### 🔴 Outstanding
(none)

**User's framing for the ADR resolution**: `access_control.py`'s `TrustedZoneChecker` already groups `workspace_dir`/`downloads_dir`/`tmp_dir` in one auto-allow "TRUSTED" tier, same as operator-added `trusted_dirs.json` entries — confirmed by reading `access_control.py:3-6`. `tmp_dir` isn't a new, unapproved category; it's an already-default-trusted zone that ADR-0016's Decision 3 simply didn't enumerate when it was written (it only named `/tmp` scratch + operator-approved dirs). Wrote `adr/0018-mount-default-trusted-zones-into-nsjail.md`, partially superseding ADR-0016 Decision 3 to explicitly recognize the default-trusted-zone tier as mountable (scoped to `tmp_dir` for this change; `workspace_dir`/`downloads_dir` remain unmounted, this just gives future work on them a named precedent). `adr.md` manifest rewritten to reference it accurately instead of claiming "not implicated."

**adr.md is FROZEN** (user confirmed the ADR draft directly — "Okay ADR looks fine").

## tasks Round 1 — 2026-07-31
This round reviewed tasks.md; proposal.md/design.md/specs/adr.md are frozen. (Reviewer note: the project's `.claude/agents/openspec-reviewer.md` is written in OpenCode's subagent format, not registered as a Claude Code agent this session — review was run via a general-purpose agent seeded with that same reviewer definition as its operating instructions, same methodology and report format.)
### 🔴 Outstanding
(none)
### 🟡 Addressed
- Task 5.2's VM-backed test scope only covered file read/write visibility and the missing-mandatory-mount failure — didn't explicitly exercise the two MODIFIED-requirement runtime scenarios (`TMPDIR`/`TMP`/`TEMP` echoed as `/tmp /tmp /tmp` inside the jail; a `shell_env_set` override winning via `-E`). Folded both checks into task 5.2 (declarative addition — task 4.1/D4 already establish the envars are unconditionally injected and overridable, this only adds explicit runtime verification of that existing decision, no new behavior).
### ✅ Findings
- Full coverage confirmed: every file in proposal.md's Impact list, every Decision (D0–D4) including the non-obvious D2 "Rule" (BuiltinExecutor's own `agent_name` must never derive `tmp_dir`, encoded twice — tasks 2.4/3.3), every spec scenario including the ADDED mandatory:true-failure scenario, and the required `openspec validate --strict` gate (task 6.1) are all present. No reintroduction of previously-rejected scope (blocklist validation, scratch-dir relocation/sweep/lock files, shared `resolve_tmp_dir()` helper).

**tasks.md is FROZEN as of this round. All artifacts for `fix-nsjail-tmp-handoff` are now frozen — the change is ready for `/opsx:apply`.**

## Apply-time correction round — 2026-07-31
Implementation completed (19/19 tasks), `/opsx:verify` passed clean, then a `/code-review` background pass on the diff found 2 real issues, and the user gave three follow-on directives while reviewing the findings.
### 🔴 Fixed (code review)
- `nsjail_config.py`'s `_load_trusted_mounts()` skipped `trusted_dirs.json` entries under `session_tmpdir` (avoiding a duplicate mount stanza) but had no equivalent skip for entries under the new `tmp_dir` mount — a stray trust-store entry there would emit two `mount: { ... dst: <same path> ... }` stanzas. Added a matching `tmp_dir` skip.
- `builtin_executor.py`'s `tmp_dir` param defaulted to `""` with no validation, unlike `nsjail_session_tmpdir` which already gates nsjail activation. A caller omitting `tmp_dir` would get a broken empty-path `mandatory: true` mount that fails nsjail startup. Added `tmp_dir` to the existing truthy-gate condition, matching `nsjail_session_tmpdir`'s pattern — omitting it now just skips nsjail activation instead of breaking it.
### 🔴 Fixed (user directive — scope expansion beyond the original design)
- User: "agent_name should be taken from the configuration file only" — the cwd-basename `agent_name` computed separately in `main.py` (keying `nsjail_state_dir`, `trusted_dirs.json`'s location, the conversation store, and vault migration paths) was itself exactly the kind of duplicate-source-of-truth problem this change exists to eliminate, even though it was for a different directory than `tmp_dir` and was explicitly out of the frozen design's stated scope. Deleted the cwd-basename computation entirely; `_run()`'s `agent_name` is now always `app_cfg.agent.agent_name`. Documented as new decision **D5** in `design.md` (with an "Alternative considered: leave it, out of scope" entry, rejected per this directive), and reflected in `proposal.md`'s Impact and `tasks.md` (new task 2.5).
- User: "every code using tmp_dir should refer the same source of truth" — confirmed already true (`main.py` and `access_control.py` both compute the identical `f"/tmp/{app_cfg.agent.agent_name}"` expression, nothing re-derives it elsewhere) and reinforced by the D5 unification above (no second `agent_name` source left to accidentally leak into a `tmp_dir` computation).
- User: "There is no /dir add functionality, stop referring to it, do not make hypothesis based on nothing" — verified against `telegram_commands.py:cmd_dir` (only `list`/`del N`/`reload` subcommands exist) and `telegram_callbacks.py:467` (the actual mechanism: `TrustedZoneChecker.add_trusted()` invoked from an inline Telegram confirmation button when a file operation touches an UNRECOGNISED path, not a slash command). Corrected every reference to `/dir add` in `design.md`, `adr.md`, and `adr/0018-mount-default-trusted-zones-into-nsjail.md` to describe the real zone-confirmation approval flow instead. `review-log.md`'s own historical entries (this file) were left as-is per the standing rule that historical review rounds are a record of what was said at the time, not a live source of truth.
### 🔴 Outstanding
(none — `make check` clean after all fixes: ruff, vulture, 1467 passed/1 skipped)

## Second code-review pass — 2026-07-31
A second background `/code-review` on the updated diff found 2 more real issues, both stemming from the agent_name-unification/code-review-fix round above.
### 🔴 Fixed
- `nsjail_config.py`'s `self.tmp_dir` was stored raw in `__init__`, unlike every sibling path attribute (`session_tmpdir`, `_agent_dir`, `skills_dir`, `trusted_dirs_path`) which are all `realpath`'d/`abspath`'d. On a host where `/tmp` is a symlink (e.g. macOS: `/tmp` → `/private/tmp`), or if `tmp_dir` were ever passed non-canonical, `_load_trusted_mounts()`'s dedup check (`real == self.tmp_dir or real.startswith(...)`, added in the previous round) would compare a realpath'd trusted-dir entry against an un-normalized `self.tmp_dir` and fail to match, producing a second mount stanza for the same effective location. Fixed: `self.tmp_dir = os.path.realpath(os.path.abspath(tmp_dir)) if tmp_dir else ""`, matching the `session_tmpdir` pattern exactly. Updated `tests/test_nsjail_config.py`'s new mount-ordering test to assert against `builder.tmp_dir` (the normalized attribute) instead of the literal input string, matching how the pre-existing `session_tmpdir` test already does it — the literal-string assertion would have failed on macOS once normalization was added.
- `builtin_executor.py`'s nsjail-activation guard (`shell_backend == "nsjail" and nsjail_session_tmpdir and tmp_dir`, added in the previous round) silently fell through to the unsandboxed subprocess backend with no log message when `tmp_dir` was empty — indistinguishable in logs from the binary-not-found case, which does warn. Fixed: restructured into an explicit `if not (nsjail_session_tmpdir and tmp_dir): logger.warning(...)` branch before the nsjail-binary-lookup branch, so a future regression (new construction path, config bug) surfaces in logs instead of silently degrading to an unsandboxed shell.
### 🔴 Outstanding
(none — `make check` clean: ruff, vulture, 1467 passed/1 skipped)
