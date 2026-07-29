# Explore Brief — add-session-logs-and-cert-mounts

## Alternatives Rejected

### session_logs mount: RW vs RO
- **RW (rejected):** Would let sandboxed scripts write/spam/fill-disk in session_logs. Reverses the archived "shell_logs not mounted" decision with a larger attack surface.
- **RO (chosen):** Agent writes outside the jail; shell reads inside. Read-only mount. Safer reversal — a sandboxed script can read prior outputs but cannot write there.

### session_logs mount point: jail-internal path vs same-as-host
- **Jail-internal `/session_logs` (rejected):** Would require the LLM to know two paths (host path for file_read, jail path for shell). Prompt must explain the mapping. More cognitive load.
- **Same-as-host path (chosen):** Bind mount with src==dst. Agent and shell see the identical absolute path. LLM sees one path, works with both file_read and shell. No mapping to explain.

### conversation_id plumbing: mutable builder attr vs per-call kwarg
- **Mutable builder attribute (rejected):** `/reset` would need to reach through controller → builtin_executor → nsjail_builder.session_logs_dir. Two layers of hidden mutable state to sync.
- **Per-call kwarg (chosen):** `builder.build()` already takes per-call data (shell_env). Adding `session_logs_dir` as a kwarg is consistent. Builder stays stateless. BuiltinExecutor holds conversation_id (like it holds _shell_env). One update on /reset.

### conversation persistence scope: short_term + working vs short_term only
- **short_term + working (rejected):** Reloading working memory (mid-task state) after a crash is fragile — tool state is gone, plan may be stale, agent must re-derive where it was.
- **short_term only (chosen):** Chat history is unambiguous. Reloading it gives conversational continuity. Working memory is always fresh on restart. Matches mental model: restart = fresh start on the same conversation.

### CA cert mount gating: always vs only-when-allow_net
- **Always (rejected):** Harmless but pointless when there's no network. Adds mount overhead for the no-network case.
- **Only when allow_net=true (chosen):** Mount + env vars are conditional on networking being enabled. No mount, no env vars, no network → no certs needed.

## Labels / Dimensions / Mapping Tables

### XDG state home layout
```
~/.local/state/<agent>/
  ├── conversation_id              # tiny file, the active conversation label
  ├── conversations/
  │   └── <conv_id>.json           # persisted short_term (chat history)
  ├── session_logs/
  │   └── <conv_id>/               # grouped by conversation
  │       ├── shell-<ts>-<hex>.log
  │       └── shell-<ts>-<hex>.log
  ├── trusted_dirs.json            # already here
  ├── secrets.toml                 # already here
  └── logs/                        # already here (agent.jsonl + agent.log)
```

### CA cert path detection (distro-aware)
| Distro | capath (dir) | cafile (file) |
|---|---|---|
| Debian/Ubuntu | /etc/ssl/certs | /etc/ssl/certs/ca-certificates.crt |
| Alpine | (none) | /etc/ssl/cert.pem |
| Fedora/RHEL | /etc/pki/tls/certs | /etc/pki/tls/certs/ca-bundle.crt |

### CA cert env vars (injected when allow_net=true)
| Env var | Value | Honored by |
|---|---|---|
| SSL_CERT_FILE | <detected cafile> | Python ssl, OpenSSL, curl, git, httpx |
| SSL_CERT_DIR | <detected capath> | Python ssl, OpenSSL, curl, git, httpx |

certifi ignores these (uses own bundle under /usr — already mounted). requests/httpx use certifi by default, so they already work.

### conversation_id lifecycle
| Event | Action |
|---|---|
| First startup | Generate id, persist to conversation_id file |
| Normal restart | Read existing conversation_id, load conversations/<id>.json |
| /reset | Save current conversation, generate new id, write conversation_id file |
| /reset discard | Generate new id without saving |
| Hard crash (SIGKILL/OOM) | Unsaved tail lost (same gap as sub-agents today) |

## Cross-Module Data Flows

### conversation_id flow
```
main.py startup
  → read ~/.local/state/<agent>/conversation_id (or generate if absent)
  → load conversations/<id>.json into ShortTermMemory (or fresh if missing)
  → construct AgentController(short_term=loaded)
  → set builtin_executor.conversation_id = id

/reset (agent_controller.reset_task)
  → save current short_term to conversations/<old_id>.json
  → generate new conversation_id
  → write conversation_id file
  → set builtin_executor.conversation_id = new_id
  → clear short_term + working (existing behavior)

shutdown (main.py finally: block)
  → save short_term to conversations/<id>.json
```

### session_logs flow (shell tool)
```
shell call (builtin_tools/shell.py)
  → _open_shell_log: log_dir = ~/.local/state/<agent>/session_logs/<conv_id>/
  → write large output to shell-<ts>-<hex>.log
  → _finalize_shell_log: keep if output > max_output, else delete
  → tool output notice: "[full output saved to: <path>]"
    (path is the real host path — same path shell sees inside jail)

nsjail shell call (nsjail_config.py build)
  → builder.build(command, timeout, shell_env, session_logs_dir=<conv_dir>)
  → mount: src=<conv_dir> dst=<conv_dir> is_bind: true rw: false
  → (bypasses _BLOCKED_SYSTEM_PREFIXES — system mount, not trusted-dir mount)
```

### CA cert flow (nsjail_config.py build, only when allow_net=true)
```
build()
  → detect cafile/capath (Debian | Alpine | Fedora)
  → mount detected path RO, mandatory: false
  → add envar: SSL_CERT_FILE=<cafile>
  → add envar: SSL_CERT_DIR=<capath>
```

## Open Questions

1. **Retention policy for old session_logs folders** — delete by age (configurable, default 7 days)? Delete on /reset? Manual only? Current code never cleans up shell_logs. Any policy is an improvement. Lean: by-age cleanup pass on startup, configurable, default 7 days.

2. **Sub-agents and session_logs** — sub-agents share the main conversation's session_logs folder (they're part of the same conversation). They use the same conversation_id. No separate sub-agent session_logs. Confirm in design.

3. **conversation_id format** — UUID hex? Short slug? Lean: `uuid4().hex[:12]` (12 chars, collision-safe, no user-facing meaning).

4. **Migration of existing shell_logs/** — on first startup after upgrade, move data/shell_logs/*.log into session_logs/<current_conv_id>/? Or leave old logs in place and just start fresh? Lean: leave old logs, start fresh (old logs are unattributable to any conversation).