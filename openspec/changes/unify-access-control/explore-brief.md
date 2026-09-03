# Explore Brief — unify-access-control

Date: 2026-09-03
Origin: exploration session 2026-09-01..03. Supersedes the narrower `harden-approval-policy`
brief (kept at `../harden-approval-policy/explore-brief.md` until that scaffold is removed):
its C-16 findings — sink-side grant policy veto (`shell`/`secret_get` never hold standing
grants), staged-token lifecycle (`PendingConfirmations`, run-scoped `reset()`), and the
oracle security-hole ranking — are folded into this design rather than shipped separately.

## Scope lock

**In scope: files & folders access plane** (file_read, file_diff, file_write, file_patch,
file_send, secret_get gating, path classification, nsjail mount table, confirmation grants
over file ops).

**Out of scope: execution plane** — `_DANGEROUS_SHELL_PATTERNS`, `shell_nsjail_confirm_mode`
trigger semantics, PTY/subprocess internals, seccomp enablement, macOS support (later).

## Problem (verified from current code)

1. **Policy is scattered**: 7+ decision lists across 5 files (3 nsjail blocklist tuples in
   `nsjail_config.py:23-43`, zone tiers + inode overrides in `access_control.py`,
   sensitive-path patterns in `patterns.py:28-43`, approve-all allowlist in
   `telegram_callbacks.py:24`, `GrantTracker` ephemeral grants, `auto_approve_tools`).
   No single answer to "what can the agent touch right now?"
2. **Dynamic trust store is a bug factory**: `trusted_dirs.json` is read fresh per nsjail
   call; entries can be persisted that the builder then silently refuses to mount
   (persisted-but-never-mounted divergence, `nsjail_config.py:286`); a dead-token
   `cb_zone_trusted` callback permanently trusts a directory (oracle hole #5);
   per-call config rebuilds.
3. **Approval asymmetry (C-16)**: main-agent "Approve all" accepts any tool incl. `shell`
   (`confirmation.py:106`); sub-agent path enforces an allowlist. Inverted trust.
4. **Naming-convention theater**: `_is_sensitive_path` (.env/config.toml/.pem…) prompts on
   filenames inside trusted zones — trivially renamed, protects nothing, adds 93%-fatigue
   noise. Credential *directories* deserve hard denial, not prompts.
5. **Agent state over-exposed**: skills_dir is the only operational reason the XDG data home
   is partially jail-visible (`nsjail_config.py:495-499`); session logs are RO-mounted in
   the jail (exfiltration surface) even though logs are queryable via SQLite (`log_query`).
6. **No large-payload transfer channel**: shell artifacts go to ad-hoc log files;
   sub-agent handoffs beyond context_io size limits have no home.

## Target model — three path tiers + one grant type

```
┌─────────────────────────────────────────────────────────────────────┐
│ TIER 0 — PROHIBITED (absolute priority; hard tool error; never     │
│          grantable; never a prompt; never jail-mounted)             │
│  hardcoded: /etc /proc /sys /dev /boot /usr /bin /sbin /lib /var…  │
│             ~/.ssh ~/.gnupg ~/.aws ~/.kube ~/.docker                │
│  derived:   ~/.config/<agent>   (config holds keys — self-         │
│             protecting; agent cannot read its own config)           │
│             ~/.local/share/<agent>, ~/.local/state/<agent>          │
│             (XDG home FULLY closed; logs via log_query SQL only)    │
│             vault file                                          │
│  config:    prohibited_dirs = [...]  (APPEND-ONLY)                  │
├─────────────────────────────────────────────────────────────────────┤
│ TIER 1 — AGENT-CONTROLLED (hardcoded; operator cannot touch)       │
│  workspace_dir (rw)   downloads_dir (rw)   /tmp/<agent> (rw)        │
│  ~/.<agent>/skills   (r  — moved out of XDG data home)              │
│  ~/.<agent>/results  (rw — NEW: results/<trace-id>/ run-scoped      │
│                      large-payload exchange + shell artifact home)  │
├─────────────────────────────────────────────────────────────────────┤
│ TIER 2 — OPERATOR ALLOWED (config only; static; rw-only; no mode    │
│          syntax; extend-only; cannot remove Tier-1 defaults)       │
│  allowed_dirs = ["~/projects", ...]                                 │
├─────────────────────────────────────────────────────────────────────┤
│ everything else → confirmation prompt                               │
└─────────────────────────────────────────────────────────────────────┘
```

### Tier semantics (all decided)

- **Prohibited is absolute, not a default**: a reckless `allowed_dirs = ["/"]` changes
  nothing for Tier 0 — only removes prompts elsewhere. Denied dirs keep working regardless
  of operator recklessness.
- **Prohibited = hard error, never a prompt**: `Permission denied: prohibited path
  (<reason>)`. Reason strings come from the prohibited dict. No fatigue-press-Yes path.
- **Inode/hardlink defense survives** (generalized from `_collect_override_inodes`):
  prohibited *files* (vault, config) are denied by inode alias too.
- **Entries may be files or dirs** (vault is a file) — containment check keeps its
  exact-match branch.
- **Conflict rule**: allowed_dir containing/underlying a prohibited path → Tier 0 wins.
  Warning emitted ONCE at startup validation (static config makes this detectable
  up-front, not per-operation).
- **Jail mounts = Tier 1 + Tier 2 minus Tier 0 overlap**: a config allowed_dir that
  CONTAINS a prohibited path cannot become a jail mount (no partial bind mounts) —
  skipped with startup warning; file-plane access remains as configured. The jail never
  silently becomes a no-op.
- **nsjail config built ONCE per session** (mount table is a pure function of static
  config); per-call command/env injection (`-E` flags) stays per-command.
- **Skills migration**: one-time copy `~/.local/share/<agent>/skills/` → `~/.<agent>/skills/`
  on first run (content migration is fine; grant migration is not).
- **No trusted_dirs migration**: on startup, existing `data/trusted_dirs.json` triggers a
  loud warning ("dynamic trust store removed; add allowed_dirs to config"); config is
  never auto-written.

### Results dir (decided)

- `~/.<agent>/results/` mounted **rw** in the jail (shell is the main producer of oversized
  payloads); whole dir mounted, not per-trace (jail config is session-static; trace IDs are
  per-run). Mount at real host path (no path rewriting).
- Run-scoped subdirs `results/<trace-id>/` (reuse `trace_context.py` r-<8hex>) — free
  correlation with the SQLite log; natural retention unit.
- `_finalize_shell_log` oversized-output artifacts relocate here; `_vault_secrets`
  redaction carries over.
- Retention: clean orphan dirs (no live trace) at startup; rest kept until operator
  removes. (Fine detail may settle in design.)
- Note in design: everything in results is readable by any later jail process in the
  session — accepted, results IS the designated exchange surface; sensitive payloads
  belong in the vault or outside results.

### Grants & confirmation UX (decided)

```
file-op prompt:   [✅ Confirm]       → grants (tool, dir-of-op) till end of prompt
                  [✅✅ Till /reset]  → grants (tool, dir-of-op) for the session
                  [❌ Deny]          → refuse; agent sees denial, can re-plan
shell prompt:      [✅ Run] [❌ Deny]     ← C-16 rule rides along: NO standing grants
secret_get:        always asks, never grantable
prohibited path:   no prompt — hard error
```

- **One grant type**: `(tool, dir)` — dir-recursive (`/data` ⇒ `/data/**`, matching today's
  zone semantics and jail mount semantics).
- **Two lifetimes**: prompt (= one user-message cycle, the current `GrantTracker.reset()`
  boundary at `react_loop()` entry) and session (= until `/reset`; extend `cmd_reset`).
- **In-memory only, never persisted** — the "persistent trust" concept leaves the codebase.
- **Grant-policy vetoes at the sink** (from C-16/oracle): `shell` and `secret_get` can
  never hold standing grants; enforcement lives in the grant ledger check, not the
  Telegram boundary (crafted-callback-proof, defense-in-depth boundary check retained).
- **Prohibited immune to grants** (scope-collision rule: a per-dir grant never overrides
  a prohibited path).

### What gets DELETED (explicit)

| Deleted | Was |
|---|---|
| `TrustedZoneChecker`, `trusted_dirs.json`, add/remove/list/reload_user_trusted | whole dynamic trust store (access_control.py) |
| Zone buttons (🔓 Allow request / 📁 Add to trusted) | telegram prompt rows + callbacks |
| `GrantTracker` per-message grant set | superseded by prompt-lifetime grants in unified ledger |
| `_is_sensitive_path` file patterns + `sensitive=` gating in files.py | naming-convention theater (credential DIRS move to Tier 0 hard-deny) |
| 3 blocklist tuples in `nsjail_config.py` | collapsed into Tier 0 prohibited dict |
| Per-call nsjail `build()` + trusted_dirs re-read | once-per-session build |
| RO session-logs mount in jail | logs via `log_query` SQL |
| `auto_approve_tools` + `_ALLOWED_APPROVE_ALL_TOOLS` asymmetry | mooted: one grant ledger, sink-side policy vetoes |
| trusted_dirs.json self-protection overrides | nothing to protect (inode defense STAYS for vault + config) |

Kept unchanged: workspace/downloads/tmp defaults, `_vault_secrets` artifact redaction,
`_DANGEROUS_SHELL_PATTERNS` + shell confirm modes (execution plane, out of scope),
token staging/lifecycle (becomes `PendingConfirmations` w/ lock + run-scoped `reset()` —
carried from C-16 plan), `/reset` as session boundary, `secret_get` always-confirm.

## Rejected alternatives (with reasons)

- **Manifest-inversion variants A–D** (pure manifest / per-surface visibility / layered
  manifests / file-tools-in-jail): superseded by the user's three-tier static design —
  simpler, static config enables once-per-session jail builds and up-front conflict
  detection. Variant D (jail everywhere) shelved for any future security rewrite.
- **Full ConfirmationCoordinator merge** (original C-16): rejected — staging vs blocking
  responsibilities genuinely differ; the grant ledger + sink vetoes deliver the security
  without the coupling.
- **Cross-token batch approve-all**: rejected — widens blast radius, fatigue-exploitation
  pattern (OWASP ATR-2026-00118).
- **Sensitive-path overlay kept as "grant veto"** (my earlier #3 suggestion): superseded —
  overlay deleted entirely; credential directories are hard-denied instead, which is
  strictly stronger and noise-free.
- **r-mode syntax in operator config**: rejected — read-only-ness is exclusively
  agent-controlled-tier business; operator config stays rw-only.

## Resolutions (grill-me session 2026-09-03)

1. **Capability decomposition**: 3 new (`path-policy`, `approval-grants`,
   `agent-results-dir`) + modified (`file-access-zones`, `trusted-dir-management`,
   `nsjail-shell-sandboxing`, `skills-dir-sandbox-mount`, `agent-scoped-directories`).
   Design/ADR step will confirm whether `xdg-path-resolution` / `agent-xdg-launch` /
   `vault-config-resolution` statements also need deltas (their resolution logic is
   unchanged; only access policy to those dirs changes — owned by path-policy).
2. **Stale trusted_dirs.json**: SILENTLY IGNORED — no announcement, no log line, no
   detection code at all.
3. **Results retention**: keep until manual clean — no automatic deletion, no TTL.
   results/ is the operator's archive, not a managed store.
4. **Sub-agent grants**: one ledger owned by depth-0; prompt-lifetime grants arising from
   sub-agent confirmations expire when that sub-agent's run completes (scoped annotation,
   no upward privilege leak). Session-lifetime grants behave as usual.
5. **Grant clearing**: /reset clears both lifetimes (it ends the current prompt cycle by
   definition and is the session boundary).
6. **Config reload**: verified — only Scheduler.reload() exists (job map); no command
   reloads security config, so the once-per-session PathPolicy cannot be desynced.

## Carry-forward notes for design phase (from proposal review round 1)

1. **Tier 0 system-dir set must be a superset of the live `_BLOCKED_SYSTEM_PREFIXES`**
   (nsjail_config.py:23-27) — includes `/root`, `/run`, `/lib64` in addition to the
   dirs listed above; the `path-policy` spec must enumerate the full closed set so the
   blocklist collapse does not silently drop those three and regress the sandbox.
2. **Deferred from resolution #1**: decide in design whether `xdg-path-resolution`,
   `agent-xdg-launch`, and `vault-config-resolution` need delta specs — their resolution
   logic is unchanged; only access policy to those dirs changes (owned by `path-policy`).

## Cross-module data flows (target)

```
startup:
  config.toml → [security] prohibited_dirs(append) + allowed_dirs
  + hardcoded _PROHIBITED dict + derived (XDG homes, vault, config home)
  → resolve + conflict-validate (warn once) → PathPolicy (frozen, session-static)
  → NsjailConfigBuilder.build_once(mount table) → cached per session

file tool call:
  _exec_* → PathPolicy.classify(realpath)
      PROHIBITED → hard error (reason)
      ALLOWED    → run
      else       → GrantLedger.check(tool, dir)
                      hit  → run
                      miss → stage (PendingConfirmations) → prompt 3 buttons
                              Confirm/Till-reset → GrantLedger.add((tool,dir), lifetime)
                              Deny → denial result

boundaries:
  prompt-lifetime grants cleared at react_loop() entry (per user message)
  session-lifetime grants cleared by /reset (cmd_reset)
  staged tokens cleared by PendingConfirmations.reset() at depth-0 _run finally
```

## Non-obvious facts to carry into proposal (verified, don't re-derive)

1. Main-agent approve-all today accepts `shell` via a normal UI button
   (telegram_interface.py:989-996); sub-agent path already enforces + tests the allowlist.
2. Interactive depth-0 path bypasses `_coordinator` entirely (react_loop.py:1946);
   the bridge is load-bearing only for headless sub-agents.
3. `log_query` already serves the SQLite store (C-34 done) — closing XDG home loses nothing.
4. `file_patch` builds its confirmation from args only, never reading the file first
   ("content oracle fix", files.py:433) — must be preserved in the new gating.
5. Checker-unwired degradation today: reads → sensitive-only gate; writes → fail closed.
   New model: PathPolicy is constructed from config; "unwired" becomes a startup error
   (fail closed everywhere).
6. Grant checks must run at the sink (grant ledger), not the Telegram boundary —
   crafted-callback defense-in-depth (tested today in test_subagent_approve_all.py).