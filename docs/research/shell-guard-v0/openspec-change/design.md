## Context

The current shell safety path is concentrated in `builtin_executor.py`: `_exec_shell()` checks `_DANGEROUS_SHELL_PATTERNS`, sends matching depth-0 commands through the existing confirmation flow, sends matching headless/sub-agent shell commands through a fail-closed deny branch, then dispatches to subprocess or PTY execution. `react_loop.py` handles `requires_confirmation` and currently has a broad tool-scoped approve-all path. Telegram renders generic confirmation prompts and callbacks. This change replaces the broad approve-all path with prompt/run-scoped approval leases for eligible non-shell tools.

Shell Guard v0.1 adds an operator-facing guard for interactive depth-0 shell calls only. It should improve classification, explanation, policy reuse, and telemetry without isolating the agent from the host, replacing the user's shell, or changing scheduled/headless behavior.

In-force ADR constraints:

- ADR-0003: TOML is preferred for operator-managed configuration-like files.
- ADR-0004: structured-primary logging uses a shared redaction processor for normal logs; Shell Guard's separate metadata sink must preserve equivalent redaction behavior and reference normal log identity.
- Known secret values originate from the ADR-0003 TOML vault; Shell Guard redaction should reuse the same secret source.

Lightweight C4-inspired component view:

```text
┌─────────────────────┐
│ ReAct loop           │
│ react_loop.py        │
└──────────┬──────────┘
           │ shell tool call, trace, depth
           ▼
┌─────────────────────┐       ┌──────────────────────┐
│ BuiltinExecutor      │──────▶│ Shell Guard runtime   │
│ shell preflight      │       │ parse/classify/policy │
└──────────┬──────────┘       └─────┬───────┬────────┘
           │                        │       │
           │ allow                  │       ├──▶ TOML policy
           ▼                        │       │
┌─────────────────────┐             │       └──▶ JSONL metadata/artifacts
│ subprocess / PTY     │             │
│ existing execution   │             │ ask/deny
└─────────────────────┘             ▼
                           ┌──────────────────────┐
                           │ Telegram approval     │
                           │ callbacks + details   │
                           └──────────────────────┘
```

Classify-to-policy flow:

```text
classify JSONL events
  └─▶ python -m shell_guard policy candidates
       └─▶ grouped shapes + evidence
            └─▶ policy draft/apply
                 └─▶ timestamped backup + TOML policy update
```

## Goals / Non-Goals

**Goals:**

- Guard operator-attended interactive depth-0 shell calls with `transparent`, `classify`, and `active` modes.
- Use a mature parser (`bashlex`) plus Aegish-inspired canonicalization and LLM decision-tree classification.
- Use local TOML policy rules to avoid repeated LLM calls for known command shapes.
- Provide Telegram ask/deny/allow UX for one pending command, including details and persistent rule creation.
- Record detailed JSONL metadata for classify/active decisions and provide a CLI path to convert observations into policy candidates.
- Preserve existing headless/sub-agent dangerous-shell deny behavior and replace broad approve-all semantics with prompt/run-scoped approval leases for eligible non-shell tools.

**Non-Goals:**

- No OS/process sandboxing, Landlock, container isolation, or shell replacement.
- No scheduled-job enforcement, prompting, classify telemetry, or policy restriction in v0.1.
- No automatic classify-to-policy promotion.
- No classification-time shell execution, including command-substitution execution.
- No bulk policy curation in Telegram.

## Decisions

### Decision 1: Place Shell Guard at the interactive shell preflight boundary

Shell Guard integrates in `BuiltinExecutor._exec_shell()` before subprocess/PTY dispatch. It must use an explicit run-origin/operator-attended signal in addition to `caller_depth`. Current scheduled jobs usually run through `SubAgentRunner` at `caller_depth >= 1`, but caller depth alone is not a reliable long-term discriminator because legacy fallback or future unattended entry points can be depth 0. The run context should carry a small typed signal such as `run_origin = interactive | scheduled | subagent | other` plus `operator_attended: bool`, threaded from interactive entry points, scheduler entry points, sub-agent context, `ReactContext`, and `BuiltinExecutor.execute()` into `_exec_shell()`. Shell Guard active/classify may apply only when `run_origin = interactive`, `operator_attended = true`, and `caller_depth = 0`. For `caller_depth >= 1`, the current regex-gated dangerous-shell deny behavior remains intact and Shell Guard active/classify is not applied in v0.1. For scheduled or otherwise unattended runs, Shell Guard active/classify is bypassed before parsing, policy matching, LLM classification, metadata emission, or remote inspection, and existing behavior is preserved.

Depth-0 mode control flow:

- `transparent`: legacy regex dangerous-shell confirmation remains the only guard path; Shell Guard does not parse, classify, prompt, or block.
- `classify`: for operator-attended interactive depth-0 runs only, legacy regex dangerous-shell confirmation remains unchanged and continues to gate execution exactly as today; Shell Guard observes alongside it and records a best-effort `decision_if_active`, but Shell Guard itself does not prompt, block, mutate policy, or enforce decisions. For non-remote command shapes, classify mode may invoke the LLM using local command structure and local read-only context, and metadata records whether the LLM was used. To keep classify mode suitable for observation, it does not perform remote-script downloads or web/docs lookups by default; it records that active mode would require remote inspection when applicable.
- `active`: Shell Guard is the depth-0 shell decision-maker. Legacy regex signals may be used as compatibility/reference input, but Shell Guard decisions must not be routed through the legacy generic shell confirmation path because that path cannot carry Shell Guard-specific decision context, details, policy actions, or metadata semantics. Active-mode `ask` uses Shell Guard-specific confirmation semantics, and active-mode `deny` returns a direct failure result.

Alternatives considered:

- Wrapping `subprocess.Popen()` / PTY spawn: rejected because it loses tool context and is too late for Telegram approval.
- Implementing in `react_loop.py`: rejected because shell-specific parsing/policy would leak into orchestration and complicate non-shell tools.
- Global process sandboxing: rejected as a non-goal.

### Decision 2: Use a layered Aegish-inspired validation pipeline without classification-time execution

The runtime pipeline is:

```text
raw command
  -> canonicalize text and collect annotations
  -> parse with bashlex
  -> extract command units, redirects, substitutions, pipelines/lists
  -> inspect referenced local scripts and identify intended remote URL access when applicable
  -> match hard local policy / deterministic semantic categories where possible
  -> LLM decision-tree classification allow/warn/block when no policy decision is sufficient
  -> map allow/warn/block to allow/ask/deny
```

`bashlex` parse failure produces `unknown`; active mode asks, classify mode records `decision_if_active = ask`. Parser output enriches the LLM prompt but does not replace semantic reasoning.

The deterministic category layer is a careless-operation floor, not a complete security taxonomy. It must preserve at least today's dangerous-shell regex confirmation behavior and add hard-ask/hard-deny categories for destructive local operations: destructive recursive delete, raw device/disk writes, filesystem format commands, critical-path clobbering redirects, broad permission/ownership changes, history rewrites or forced pushes, and broad absolute-path mutations. For compound shell commands, Shell Guard uses most-restrictive-wins across command units.

Alternatives considered:

- Tree-sitter bash: rejected for v0.1 due runtime complexity and edge/crash risk.
- `sh-guard`: deferred; useful later if semantic scoring/MITRE mapping becomes valuable.
- Aegish substitution execution: rejected because the Shell Guard advisor must be read-only.

### Decision 3: Separate policy file with in-memory indexes

Shell Guard policy lives in a separate TOML file, configured from the main config. Use one `[[rules]]` list with `decision`, `scope`, `reason`, provenance fields, and matcher-specific fields. Load the policy once at startup/reload and build in-memory indexes for exact command, binary, argv-prefix, and semantic-category matching.

Precedence:

1. hard deterministic semantic deny;
2. policy deny by most specific match;
3. exact-command policy allow that satisfies a hard deterministic semantic ask;
4. hard deterministic semantic ask;
5. policy ask by most specific match;
6. policy allow by most specific match;
7. defaults (`parse_error = ask`, `unknown = ask`).

Hard semantic deny always wins. Exact-command allow may satisfy hard semantic ask for a specific reviewed command such as `rm -rf ./build`; broad binary, binary-global, or semantic allow rules must not satisfy hard semantic ask. Broad binary-global allow is unavailable for high/critical/unknown-risk candidates and never overrides hard semantic deny.

### Decision 4: LLM advisor is read-only and front-loaded

The LLM advisor/classifier may use read-only context: parsed command structure, local referenced script contents, URL strings and command intent, policy matches, prior classify observations, and web/docs context when useful in active mode. It must not execute shell commands, write files, change policy, approve its own recommendation, or fetch remote scripts in v0.1. In classify mode, remote downloads and web/docs lookups are forbidden; LLM use is configurable, best-effort, timeout-bound, and local-context-only. If classify-mode LLM is disabled, fails, or times out, Shell Guard records `decision_if_active = ask`, records `llm.used = false` or `llm.error`, and continues without blocking execution. Repeated normalized command shapes may be cached within a run to avoid duplicate classifier calls; metadata records cache use when applied.

When a user chooses to create a rule for an obvious command shape, future matching calls can be handled by policy without LLM. Rule creation remains optional.

### Decision 5: Replace broad approve-all with prompt/run-scoped approval leases

The legacy broad approve-all feature should be replaced with prompt/run-scoped approval leases for eligible non-shell confirmation flows. A lease is bound to the current `AgentController.run()` / trace id, expires at run end or reset, is never durable, and must not apply to scheduled/unattended runs or sub-agents unless a future design explicitly adds that behavior. Shell and Shell Guard `ask` decisions are ineligible for approval leases; they use one-time decisions or Shell Guard policy/rules.

Initial lease-eligible tool is `file_patch` only. `file_write`, `secret_get`, `memory_graph_store`, `shell`, and Shell Guard `ask` decisions are not eligible in v0.1. Shell Guard `deny` returns a direct failure result and never enters confirmation. If active mode produces `ask` but no Shell Guard approval channel is available, Shell Guard fails closed: it denies execution, returns a clear error explaining approval is unavailable, and writes a metadata/log event. Approval channel unavailable includes Telegram not configured, bot unreachable, prompt send failure, and approval timeout/no operator response.

Telegram prompt flow:

```text
compact prompt
  ├─ Allow this time -> execute pending command once
  ├─ Deny this time  -> cancel pending command once
  ├─ Create allow rule -> choose scope -> save -> ask Run now / Do not run
  ├─ Create ask rule   -> choose scope -> save -> re-show decision prompt
  ├─ Create deny rule  -> choose scope -> save -> deny current command
  └─ Details -> send separate details message -> Back sends fresh compact prompt
```

Shell Guard intentionally replaces broad approve-all with explicit one-time decisions and scoped rule creation. Generic confirmation prompts for eligible non-shell tools may offer prompt/run-scoped approval leases; ineligible tools remain per-operation only.

### Decision 6: Detailed metadata uses append-only JSONL plus artifact references

Normal logs include basic Shell Guard summaries and a metadata reference. Detailed events go to a dedicated append-only JSONL file under `~/.local/state/<agent_name>/shell_guard/`, not the repo-relative `data_dir`. Appends should write complete JSONL events as atomic lines. The active policy TOML, policy backups, and bulky local classification evidence artifacts live under `~/.local/share/<agent_name>/shell_guard/`; evidence artifacts are owner-only and referenced by hash/path in JSONL metadata. The daemon and `python -m shell_guard policy ...` CLI must resolve the same agent-scoped locations so policy conversion reads the daemon's classify observations and writes the active policy expected by the daemon.

Metadata and artifacts must pass through the same known-secret redaction source as normal logs, but the current normal-log redactor only scans top-level string fields and cannot be reused as-is for nested Shell Guard events. Implementation should extract a shared string redaction helper from the existing redaction logic, including the minimum needle-length behavior, and apply it recursively to every string before writing nested JSONL metadata or artifact summaries. Unknown secrets embedded in commands, URLs, or local referenced scripts remain a residual risk; files must be owner-only and paths must be agent-scoped.

### Decision 7: CLI handles classify-to-policy conversion

Use `python -m shell_guard policy candidates`, `draft`, and `apply` for bulk policy curation. `apply` is interactive by default and confirms each candidate; `--all` / `-a` applies all as-is. Every completed apply creates a timestamped backup of the previous policy file before mutation.

Candidate generation groups classify observations by normalized command shape, exact command, argv prefix, binary, and semantic category. Candidates include evidence: count, sample commands, first/last seen, LLM action/risk/confidence summary, suggested scope, and reason.

## Risks / Trade-offs

- [Risk] LLM classification can be inconsistent or wrong -> Use temperature-zero structured JSON output, examples from Aegish decision tree, confidence display, local policy reuse, and user-facing “if unsure, deny” guidance.
- [Risk] Classify-mode LLM calls add cost, latency, and provider failure modes -> Make classify LLM use configurable, use timeouts, continue execution on failure with `decision_if_active = ask`, record `llm.used`/`llm.error`/duration metadata, and optionally cache repeated normalized shapes within a run.
- [Risk] Remote URL commands can involve SSRF/webhook/tracking/one-time-token side effects -> Shell Guard v0.1 does not attempt deep network-attack protection or pre-approval remote fetching. It treats remote URL access and remote-download-to-interpreter as user-decision points, asks the user in active mode, shows URL/command context and LLM advice where available, and leaves final execution decision to the user. Classify mode MUST NOT perform remote fetches.
- [Risk] Policy writes and metadata reads can race between daemon and CLI -> Policy apply writes a timestamped backup before atomic replace; runtime reads only complete policy snapshots and defines reload semantics; CLI observation readers ignore incomplete trailing JSONL lines and never mutate metadata.
- [Risk] Metadata/artifacts may contain sensitive data -> Apply known-secret redaction, owner-only permissions, compact normal-log references, and document residual unknown-secret risk.
- [Risk] Removing broad approve-all worsens UX for repeated confirmations -> Replace it with prompt/run-scoped leases for eligible non-shell tools only; Shell uses scoped rules and explicit decisions.
- [Risk] v0.1 scope is large -> Implement in thin modules: config/policy, parser/canonicalizer, validator/advisor, metadata, Telegram callbacks, CLI conversion.
- [Risk] `bashlex` parse gaps produce noisy asks -> Treat parse failures as unknown/ask, preserve raw command for LLM context, and use classify telemetry to tune policy.
- [Risk] Shell Guard could be weaker than the legacy regex confirmation path -> Seed deterministic careless-operation categories from the existing dangerous-shell patterns and require active mode to ask or deny anything the legacy gate would have confirmed.

## Migration Plan

1. Add Shell Guard config with `enabled = false` or equivalent transparent default so existing behavior remains unchanged.
2. Add dependency and parsing/validation modules behind the Shell Guard mode gate.
3. Add policy/metadata paths under agent-scoped state/data locations with owner-only permissions.
4. Add classify mode and JSONL metadata first; verify Shell Guard records decisions only for operator-attended interactive depth-0 runs, without itself blocking, prompting, mutating policy, or performing remote fetches, while the existing regex dangerous-command confirmation continues to gate execution.
5. Replace legacy approve-all with prompt/run-scoped leases for eligible non-shell tools and add active mode Telegram ask/deny/allow flow.
6. Add CLI policy candidate/draft/apply flow with policy backups.
7. Rollback by disabling Shell Guard or setting transparent mode; existing shell execution and regex confirmation remain available.

## Open Questions

- Exact module names may change during implementation, but module boundaries should remain small.
- Whether future changes should supersede an ADR to formalize Shell Guard's policy/log storage conventions after v0.1 stabilizes.
- What follow-up UX improvements, if any, should expand prompt/run-scoped approval leases without reintroducing broad approval risk?
