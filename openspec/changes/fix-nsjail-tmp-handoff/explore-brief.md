# Explore Brief: nsjail /tmp handoff

## Problem

Two unrelated "/tmp" universes exist and nothing bridges them:

1. **Jail scratch `/tmp`**: `nsjail_config.py` bind-mounts an anonymous
   `tempfile.mkdtemp(prefix="nsjail-tmp-")` directory (created once per
   process in `main.py:409`) to `/tmp` inside the sandbox
   (`nsjail_config.py:388`). It is rmtree'd at shutdown (`main.py:725`).
2. **Agent handoff dir**: `paths.tmp_dir` (default `/tmp/{agent_name}`,
   `config_schema.py:503`/`666`) is a real, predictable host path already
   in the default trusted-dir list used by `file_read`/`file_write`
   (`access_control.py:118-123`). The prompt/tool docs already teach this
   convention — `builtin_tools/descriptors.py:110` example: "transcript
   already saved at `/tmp/piclaw/clean_transcript.txt`".

When a shell script running inside the jail writes to
`/tmp/{agent_name}/report.md`, it silently lands in the anonymous scratch
dir (since `/tmp` is bind-mounted from there), never in the real host
`/tmp/{agent_name}` that `file_read` checks outside the jail. The agent
retries the read, finds nothing, loops.

## Alternatives Considered

- **Repoint jail `/tmp` itself at `tmp_dir`** (drop the anonymous mkdtemp
  entirely; bind-mount `tmp_dir` as `/tmp`). Rejected: merges two
  different concerns — throwaway sandbox scratch (should be wiped every
  run) and the deliberate, persistent agent↔host handoff directory
  (already in the trusted zone). Every stray shell temp file would
  silently become part of the trusted/visible zone; also entangles
  cleanup-at-shutdown semantics between "random scratch" and "meaningful
  output."
- **Mount raw host `/tmp` rw into the jail** (maximally simple — no
  special-cased handoff directory at all). Rejected during review: host
  `/tmp` contains live same-uid IPC sockets (tmux control socket,
  `/tmp/.X11-unix/X0`, a standalone ssh-agent's `/tmp/ssh-XXXXXX/agent.NNN`).
  RW access to those from inside the jail is a real sandbox escape (e.g.
  `tmux send-keys` into the user's live session, or using the ssh-agent
  socket to authenticate as the user without ever touching `~/.ssh`,
  bypassing the exact blocklist protection `_blocked_user_prefixes`
  exists for). It also falsifies the existing spec claim "the host
  filesystem outside mounted paths is inaccessible" and deletes the
  spec'd per-session `/tmp` persistence + shutdown-cleanup requirements
  (`openspec/specs/nsjail-shell-sandboxing/spec.md:29-34`, `:70-84`).
- **Do nothing / tell users to avoid /tmp for output** — rejected by user;
  they want the fix to close the loop, not push a workaround into every
  agent prompt.

## Critical Correction Found Mid-Review

`main.py:375-377` sets `os.environ["TMPDIR"] = tmp_dir` (also `TMP`,
`TEMP`) **before** `main.py:409` calls
`tempfile.mkdtemp(prefix="nsjail-tmp-")` with no explicit `dir=`. Since no
earlier code in this repo touches `tempfile` (confirmed by grep — the
only other uses are `nsjail_config.py:475`, which runs later, and
`access_control.py:313`, which passes an explicit `dir=`), the scratch
dir practically always resolves as a **child of `tmp_dir`**
(e.g. `/tmp/{agent_name}/nsjail-tmp-XXXXXX`), not an unrelated location as
originally assumed. This must be fixed as part of this change (see
Requirement C below) or the two spaces stay entangled and the agent sees
its own scratch garbage inside its trusted temp dir.

## Chosen Approach (converged after multi-round review)

Three requirements, framed as behavior (mechanism is a design.md concern):

**Requirement A — the agent temp directory is visible inside the jail at
its real path.** The directory resolved at startup as `paths.tmp_dir` is
bind-mounted **read-write** inside the jail with `src == dst` (own real
absolute path, not remapped to `/tmp`), nested after the existing `/tmp`
scratch mount entry (since the default `tmp_dir` sits under `/tmp`). A
file written there by a sandboxed script is readable by `file_read` at
the identical path outside, and vice versa. The mount source is the
value **resolved once at startup from config** — never re-derived from
the live environment — so `shell_env_set` cannot influence what gets
mounted (a mutable-env-derived mount source would let the agent get an
arbitrary directory bind-mounted rw on the next shell call — a
self-service escape). If the resolved path is rejected by the blocklist
or doesn't exist, the mount is skipped **and a warning is logged**
(silent skip would silently reproduce the original stuck-loop bug).
Validated against the same `_BLOCKED_SYSTEM_PREFIXES` +
`_blocked_user_prefixes` checks `_load_trusted_mounts` applies to other
RW trusted-dir mounts (not the RO-only exemption `skills_dir` gets).

**Requirement B — temp-dir environment variables are injected so scripts
follow the setting.** `TMPDIR`, `TMP`, and `TEMP` are injected as base
config `envar` entries (same layer as `PATH`/`HOME`/`LANG`/`TERM`), all
three set to the mounted temp dir — matching what the agent process
already exports for itself (`main.py:375-377`). Without this, the mount
only helps scripts that hardcode the path; with it, `mktemp`,
`tempfile.mkdtemp`, `mkstemp`, and anything honoring `$TMPDIR` also land
in the mounted directory instead of falling back to ephemeral `/tmp`.
`shell_env_set` may still override these like any base envar — the
override changes only where scripts write, never the mount, so
overridden output does not survive the jail (this should be stated
explicitly, not left implicit).

**Requirement C — the ephemeral scratch and the agent temp dir are
disjoint.** Neither contains the other. Fixes the mid-review correction
above: create the scratch dir at an explicit location that cannot land
inside `tmp_dir` regardless of `TMPDIR`/`TMP`/`TEMP` env state (e.g. pass
an explicit `dir=` to the `mkdtemp` call rather than relying on
env-derived defaults).

### Confirmed decisions (via AskUserQuestion, still valid)

- Mount mode: **read-write** (Requirement A).
- Blocklist: **apply the full `_blocked_user_prefixes` check**, not the
  skills_dir RO-only exemption (Requirement A). Rationale unchanged: the
  `skills_dir` exemption is justified specifically by it being read-only.

### Still open (to resolve in design.md)

- Exact scratch-dir relocation target for Requirement C (candidates
  raised: hardcode `dir="/tmp"`, or root under the existing
  `nsjail_state_dir` XDG state directory, `main.py:337`).
- Whether `TMPDIR`/`TMP`/`TEMP` remain overridable via `shell_env_set`
  (leaning yes, per Requirement B, but state it explicitly).
- `mandatory: true` vs `false` for the new mount. Note: `main.py:330`
  already does `os.makedirs(tmp_dir, exist_ok=True)` before
  `BuiltinExecutor` is constructed (`main.py:424`), which is before the
  nsjail builder is ever asked to build a config — so existence is
  already guaranteed by startup order; `mandatory: true` is viable
  without new directory-creation logic in `nsjail_config.py`.

## Cross-Module Data Flow

```
main.py (startup)
  paths.tmp_dir resolved (config_schema.py) ──┐
  nsjail_session_tmpdir = mkdtemp(...)         │  both passed to
                                                ▼
                                    BuiltinExecutor.__init__
                                    (builtin_executor.py)
                                                │
                                                ▼
                                    NsjailConfigBuilder(
                                      session_tmpdir=...,   # existing, unchanged
                                      tmp_dir=...,           # NEW param
                                      ...
                                    )
                                                │
                                                ▼
                                    build() emits mount lines:
                                      1. /tmp  <- session_tmpdir (unchanged, scratch)
                                      2. {tmp_dir} <- {tmp_dir}  (NEW, RW, nested
                                         under /tmp mount, validated against
                                         _BLOCKED_SYSTEM_PREFIXES +
                                         _blocked_user_prefixes)
```

No other module needs to change. `access_control.py`'s existing trust
classification for `tmp_dir` already covers the host side; this change
only makes that same directory visible from inside the jail.

## Open Questions

- Should the new mount be `mandatory: true` or `false`? (`skills_dir` is
  `true`; `session_logs_dir` is `false`.) Leaning `false` since `tmp_dir`
  may not exist yet on first run — needs `os.makedirs` before mount, or
  `mandatory: false` to tolerate absence gracefully. To resolve during
  design.
- Should `NsjailConfigBuilder` create `tmp_dir` on disk if missing (like
  it might need to), or should `main.py`/`access_control.py` already
  guarantee it exists by the time nsjail builds its config? To resolve
  during design.
