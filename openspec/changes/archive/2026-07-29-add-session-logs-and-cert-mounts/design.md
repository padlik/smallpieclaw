## Context

The agent currently stores shell command output artifacts in `data/shell_logs/` — a flat directory inside the agent installation that accumulates forever with no cleanup and is invisible to sandboxed shell commands. The main agent's chat history (`short_term`) is lost on every process restart — only sub-agents persist context (via `context_io._save_context` / `_load_context` to `data/job_contexts/<key>.json`). When `allow_net=true` is enabled, TLS-dependent tools (`curl`, `git`, Python `ssl`) fail inside the nsjail jail because `/etc/ssl/certs` is not mounted — the CA bundle is unreachable, and the compiled-in OpenSSL symlink chain (`/usr/lib/ssl/cert.pem → /etc/ssl/certs/ca-certificates.crt`) is broken because `/etc` is not mounted.

In-force ADRs that constrain this design:
- **ADR-0012**: nsjail shell isolation with configurable confirmation. Unaffected — this change adds mounts and env vars, not confirmation logic.
- **ADR-0015**: nsjail configuration state must reside outside the sandbox's write scope. `session_logs` is agent-written state, not sandbox configuration — but the principle (sandbox cannot write to agent-controlled state) is respected: the mount is read-only.
- **ADR-0016**: project_dir removed from sandbox; all agent state consolidated under `~/.local/state/<agent>/`. This change extends that consolidation: `session_logs` and `conversations/` move there too.

## Goals / Non-Goals

**Goals:**
- Move shell output logs from `data/shell_logs/` to `~/.local/state/<agent>/session_logs/<conversation_id>/`, grouped by conversation.
- Make `session_logs/<conversation_id>/` readable inside the nsjail jail at the same host path (read-only bind mount, src==dst).
- Persist the main agent's `short_term` (chat history) across restarts, keyed by a `conversation_id` that survives restarts and rotates on `/reset`.
- Mount the system CA certificate store read-only inside the jail when `allow_net=true`, with distro-aware path detection and env-var injection to bypass the broken-symlink problem.
- Add age-based retention cleanup for old `session_logs` folders.

**Non-Goals:**
- Persisting `working` memory (task state) across restarts — restart always starts a fresh task.
- Read-write access to `session_logs` from inside the jail — the mount is read-only; the agent writes outside the jail.
- Migrating existing `data/shell_logs/` files — old logs are unattributable to any conversation and are left in place.
- A periodic checkpoint mechanism for `short_term` — save happens on shutdown and `/reset` only. The hard-crash gap (SIGKILL/OOM loses the unsaved tail) is accepted, matching the existing sub-agent precedent.
- Changing the `shell_nsjail_confirm_mode` logic (ADR-0012) — unaffected.

## Decisions

### Decision 1: `session_logs` mounted read-only at the same host path inside the jail

**Choice:** Bind-mount `~/.local/state/<agent>/session_logs/<conversation_id>/` inside the nsjail jail with `src == dst`, `is_bind: true`, `rw: false`, `mandatory: false`.

**Rationale:** The agent (outside the jail) writes large shell outputs to this directory. A later sandboxed shell command may need to read a prior large output without the agent piping it back through `file_read → shell`. Mounting the directory read-only at the same host path means the LLM sees one absolute path that works with both `file_read` (agent-side) and `cat` (shell-side). No jail-internal vs host-internal path mapping is needed.

**Alternatives considered:**
- *RW mount (rejected):* Would let sandboxed scripts write/spam/fill-disk. Reverses the archived "shell_logs not mounted" decision with a larger attack surface.
- *Jail-internal path `/session_logs` (rejected):* Requires the LLM to know two paths. Prompt must explain the mapping. More cognitive load.
- *No mount (rejected):* Shell cannot read prior outputs directly; the agent must pipe them back through `file_read`. Defeats the purpose.

**ADR-0015 coherence:** ADR-0015 requires that sandbox *configuration* state reside outside the sandbox's *write* scope. `session_logs` is not configuration — it is agent-written output. The mount is read-only, so the sandbox cannot modify it. The principle (sandbox cannot write to agent-controlled state) is respected.

### Decision 2: `conversation_id` as a per-call kwarg to `NsjailConfigBuilder.build()`

**Choice:** `build()` gains a `session_logs_dir: str = ""` kwarg. The `BuiltinExecutor` holds `conversation_id` (like it holds `_shell_env`), computes the session_logs dir from it, and passes it per-call. The builder remains stateless.

**Rationale:** The builder already takes per-call data (`shell_env` flows through explicitly). Adding `session_logs_dir` as a kwarg is consistent. With a mutable builder attribute, `/reset` would need to reach through `controller → builtin_executor → nsjail_builder.session_logs_dir` — two layers of hidden mutable state to sync. Per-call keeps the builder pure and the update path to one hop (`controller → builtin_executor.conversation_id`).

**Alternatives considered:**
- *Mutable builder attribute (rejected):* Hidden mutable state, two layers to sync on `/reset`.

### Decision 3: Persist `short_term` only, not `working`

**Choice:** On shutdown and `/reset` (save), serialize `short_term` to `~/.local/state/<agent>/conversations/<conversation_id>.json` using the existing `context_io._save_context` atomic-write pattern. On startup, load it if the file exists. `working` memory is always fresh (`WorkingMemory()`).

**Rationale:** Chat history is unambiguous — it's "what we talked about." Reloading it gives conversational continuity. Working memory is "I'm in the middle of running `shell` step 3 of a 5-step plan." Reloading that after a crash is fragile — the tool state is gone, the plan may be stale, and the agent would need to re-derive where it was. Restart = "fresh start on the same conversation," not "resume the exact interrupted task."

**Alternatives considered:**
- *Persist short_term + working (rejected):* Fragile mid-task resume after crash.

### Decision 4: CA cert mount + env vars gated on `allow_net=true`

**Choice:** When `self.allow_net` is True, `build()` detects the CA cert path (distro-aware), mounts it read-only with `mandatory: false`, and injects `SSL_CERT_FILE` / `SSL_CERT_DIR` env vars into the nsjail config `envar` lines.

**Rationale:** The mount provides the file; the env vars tell programs where to look, bypassing the `/usr/lib/ssl/cert.pem → /etc/ssl/certs/...` broken-symlink chain. The env vars are honored by Python `ssl`, OpenSSL, `curl`, `git`, `httpx`. `certifi` ignores them (uses its own bundle under `/usr`, already mounted) — so `requests`/`httpx` already work without this change. The env vars go in the config `envar` lines (alongside `PATH`, `HOME`, `LANG`, `TERM`), not in per-call `-E` flags, because they are static system env vars, not user-set session vars.

**Distro detection:**
| Distro | capath (dir) | cafile (file) |
|---|---|---|
| Debian/Ubuntu | `/etc/ssl/certs` | `/etc/ssl/certs/ca-certificates.crt` |
| Alpine | (none) | `/etc/ssl/cert.pem` |
| Fedora/RHEL | `/etc/pki/tls/certs` | `/etc/pki/tls/certs/ca-bundle.crt` |

Detection tries Debian first, then Alpine, then Fedora. `mandatory: false` ensures the jail still starts if the path is absent (e.g., minimal container).

**Alternatives considered:**
- *Always mount (rejected):* Harmless but pointless when there's no network.
- *Mount only, no env vars (rejected):* Leaves the broken-symlink edge case where a program reads `/usr/lib/ssl/cert.pem` directly and hits a broken link because `/etc` is not mounted.

### Decision 5: `conversation_id` format and lifecycle

**Choice:** `uuid4().hex[:12]` (12 hex chars, collision-safe, no user-facing meaning). Persisted to `~/.local/state/<agent>/conversation_id` (a tiny text file).

**Lifecycle:**
| Event | Action |
|---|---|
| First startup | Generate id, persist to `conversation_id` file |
| Normal restart | Read existing `conversation_id`, load `conversations/<id>.json` into `ShortTermMemory` |
| `/reset` (save) | Save `short_term` to `conversations/<old_id>.json`, generate new id, write `conversation_id` file, update `builtin_executor.conversation_id` |
| `/reset discard` | Generate new id, write `conversation_id` file, update `builtin_executor.conversation_id` (no save) |
| Hard crash (SIGKILL/OOM) | Unsaved tail lost (same gap as sub-agents today) |

**Sub-agents:** Sub-agents inherit the main conversation's `conversation_id` and share its `session_logs` folder. They do not get a separate id or folder.

### Decision 6: Age-based retention cleanup on startup

**Choice:** On startup, scan `~/.local/state/<agent>/session_logs/` and delete conversation folders whose newest file is older than `session_logs_retention_days` (default 7, configurable via `config.toml`). Also delete the corresponding `conversations/<old_id>.json` file.

**Rationale:** The current code never cleans up `shell_logs`. Any policy is an improvement. Age-based cleanup is gentler than delete-on-`/reset` (which would lose logs the moment you clear the conversation). Running on startup avoids a background timer.

## Risks / Trade-offs

- **[Hard-crash gap]** SIGKILL/OOM/power loss skips the `finally:` block → unsaved `short_term` tail is lost. *Mitigation:* accepted — sub-agents have the same gap today. A periodic checkpoint could close this but adds I/O; deferred.
- **[Archived decision reversal]** The archived `add-shell-isolation-improvements` change (Decision 3: "shell_logs not mounted") is reversed for `session_logs`. *Mitigation:* read-only mount, not read-write. A sandboxed script can read prior outputs but cannot write/spam/fill-disk there.
- **[Distro path absence]** If the detected CA cert path doesn't exist on a particular system, the mount is skipped (`mandatory: false`) but the env vars still point at it → programs may fail with "file not found" instead of "no certs." *Mitigation:* only inject env vars when the path actually exists; if no path is detected, skip both mount and env vars (graceful degradation, same as today).
- **[Stale conversation file after id rotation]** After `/reset`, the old `conversations/<old_id>.json` and `session_logs/<old_id>/` remain on disk until age-based cleanup removes them. *Mitigation:* this is the intended retention behavior — old conversations are available for a configurable window, then cleaned up.
- **[conversation_id file corruption]** If the `conversation_id` file is corrupted or deleted, the agent generates a fresh id and starts a new conversation. *Mitigation:* acceptable — same as a fresh install.

## Migration Plan

1. **No data migration for `shell_logs`** — old `data/shell_logs/` files are left in place (unattributable to any conversation). New logs start fresh in `~/.local/state/<agent>/session_logs/<conversation_id>/`.
2. **No data migration for conversations** — first startup with this change generates a `conversation_id` and starts with an empty `short_term` (no prior conversation to load).
3. **Config addition** — `session_logs_retention_days` is optional with default 7. No config migration needed; existing configs work unchanged.
4. **Rollback** — revert the code. Old `data/shell_logs/` is untouched. New `session_logs/` and `conversations/` directories under XDG state can be manually deleted. No schema changes to existing files.

## Open Questions

- **ADR-0015 supersession?** ADR-0015 states "any file or state that influences nsjail sandbox configuration must reside outside the sandbox's write scope." `session_logs` is not configuration (it's output), and the mount is read-only, so ADR-0015 is not violated. However, the archived `add-shell-isolation-improvements` decision ("shell_logs not mounted") is reversed. The adr step should record a new ADR documenting the read-only reversal and its relationship to ADR-0015.
- **Retention cleanup granularity** — should the cleanup also remove `conversations/<old_id>.json` when the corresponding `session_logs/<old_id>/` is deleted? Lean: yes, they are a pair.