# Explore Brief: Guard Shell Tool Execution

## Problem shape

Agents can overuse the shell tool while trying to solve problems. Existing confirmation prompts catch some obvious dangerous patterns, but they are regex-based and mostly cosmetic. The desired “Shell Guard” should make shell execution more explainable and controllable without trying to become a perfect sandbox in version 0.1.

The first version should guard interactive shell tool calls, gather real command telemetry, and support user decisions with concise LLM advice. It should not attempt to solve unattended scheduled jobs, full OS sandboxing, or automatic policy promotion.

## Rejected or deferred alternatives

- Full process sandboxing in v0.1: deferred and not a current goal. The agent is intentionally used to control the box where it is installed, so Shell Guard should not isolate the agent from the local system in v0.1.
- Global subprocess wrapping: deferred. The current shell tool boundary is a clearer first choke point and can explain decisions to the agent/user.
- Shell replacement: rejected. Aegish demonstrates a useful monitored-shell approach, but Shell Guard should guard the existing built-in shell tool rather than replace the user's shell.
- Landlock/kernel enforcement from Aegish: deferred. It is a Linux kernel sandboxing mechanism and is not needed for the v0.1 usability and policy experiment. Revisit OS-level isolation only as a separate future topic.
- Regex-only command classification: rejected as the primary mechanism. Shell Guard should build its own policy/semantic model and should not rely on the existing dangerous regex list as a foundation.
- Classification-time command execution: rejected. Aegish resolves allowed command substitutions by executing them, but Shell Guard's classifier/advisor should remain read-only in v0.1 and should not execute inner substitutions merely to classify a command.
- Auto-promoting classify telemetry to allow rules: rejected. Classify mode may suggest policy, but user/admin review must promote rules.
- Scheduled job enforcement or prompting: out of scope. Scheduled jobs are independent agents without reliable live approval UI; existing behavior remains unchanged in v0.1.
- Bulk policy curation through Telegram: deferred. Telegram is appropriate for single pending-command approval, but poor for reviewing many candidate rules from classify telemetry. v0.1 should include a CLI-oriented conversion path for classify observations to policy candidates/drafts.

## Final approach commitments

Shell Guard v0.1 applies to interactive shell tool calls at the built-in shell execution boundary.

Modes:

| Mode | Behavior |
| --- | --- |
| `transparent` | Shell Guard does not affect behavior; existing safety checks continue as they do today. |
| `classify` | Shell Guard parses/classifies/logs what active mode would have done, but does not prompt, block, mutate policy, or otherwise enforce new decisions. |
| `active` | Shell Guard enforces allow/ask/deny policy for interactive shell calls. Unknown commands ask with LLM advice. |

Rule decisions:

| Decision | Meaning |
| --- | --- |
| `allow` | Command shape may run without prompting unless a stronger semantic deny/ask signal applies. |
| `ask` | Command shape requires user confirmation with advisor text. |
| `deny` | Command shape is blocked and a reason is returned to the agent/user. |

Rule scopes:

| Scope | Example | Notes |
| --- | --- | --- |
| Exact command | `opencode session list` | Narrowest persistent rule. |
| Binary plus argv prefix | `opencode session list *` | Useful for commands such as `opencode session list` or `make test`; preferred over binary-global for most tools. |
| Binary globally | `agent-browser *` | Rare; unavailable for high/critical/unknown-risk commands and should not override critical semantic denies. |
| Semantic category | `remote_download_to_interpreter`, `host_namespace_escape` | Used for structural risks that are more important than binary names. |

Risk/precedence commitments:

- Unknown commands in active mode default to `ask` with LLM advice.
- Binary-global allow is rare and should be unavailable for high, critical, or unknown-risk commands.
- Broad allow rules must not override hard semantic denies such as host namespace escape.
- The existing dangerous regex list should not be treated as the source of truth. Shell Guard should define its own classifications and may only use existing patterns as incidental compatibility/reference material during migration.
- The parser dependency should be proven and mature. Parser alternatives were explored; `bashlex` is the preferred v0.1 parser because it is mature, pure Python, simple to install, and already used successfully by Aegish. `sh-guard` may be revisited later if semantic risk scoring/MITRE mapping is needed. `tree-sitter-bash` is not preferred for v0.1 because its extra runtime complexity and crash/edge-case risk do not buy enough value for non-incremental command parsing.
- Local validation in ignored `.shellguard-lab/` with `bashlex==0.18` showed that bashlex can extract useful command units from representative Shell Guard cases: simple commands, pipelines, `&&`/`||` lists, redirects, command substitutions, process substitution, loops, and sourced scripts. Observed parser limitations include arithmetic expansion raising `NotImplementedError` and some heredoc syntax being parse-sensitive. Shell Guard should treat parser failures as `unknown`/ask and preserve enough raw command context for LLM advisor analysis. Bashlex is only the structure parser; semantic risk classification is expected to happen in the final LLM decision-tree stage, so lack of built-in semantic risk scoring in bashlex is acceptable.
- Aegish's MIT-licensed validation approach is a strong conceptual baseline. Shell Guard should adapt its canonicalization, Bash AST analysis, decomposition, command-substitution inspection, decision-tree prompt, and audit-shape ideas to the built-in shell tool context.
- Shell Guard policy should persist in a separate policy file, not directly in the main application configuration file.

## Aegish findings and adaptation decisions

Aegish (`github.com/GuidoBergman/aegish`) is an MIT-licensed LLM-powered shell that validates commands before execution. Its architecture is close to the desired Shell Guard protection model, but its shell-replacement and kernel-enforcement scope is broader than v0.1 needs.

Aegish validation pipeline observed in source:

```text
raw command
  -> canonicalization
  -> static block checks
  -> bashlex AST analysis
  -> compound command decomposition
  -> command-substitution resolution/inspection
  -> LLM classification using a natural-language decision tree
```

Shell Guard should adopt the shape of this pipeline with modifications:

| Aegish concept | Shell Guard adaptation |
| --- | --- |
| Shell replacement | Do not replace shell; guard the built-in shell tool boundary. |
| `bashlex` parser | Use as the preferred mature parser candidate. |
| Canonicalization | Reuse/adapt ideas: ANSI-C quote decoding, quote normalization, backtick conversion, brace expansion, glob annotations, here-string extraction. |
| Static blocklist | Use only as reference/test inspiration, not as the policy foundation. |
| Compound decomposition | Validate/explain subcommands independently with most-restrictive-wins aggregation. |
| Command substitution handling | Extract and classify substitutions, but do not execute substitutions during classification in v0.1. |
| LLM decision tree | Adapt the 13-rule `allow`/`warn`/`block` prompt to agent shell-tool context. |
| Inline shell prompt | Replace with Telegram async confirmation for `ask` decisions. |
| Audit JSONL | Reuse the simple structured audit shape as inspiration for Shell Guard metadata logs. |
| Landlock sandbox | Defer; not part of v0.1, because Shell Guard should not isolate the agent from the controlled host in this version. |

Aegish action mapping:

| Aegish action | Shell Guard decision |
| --- | --- |
| `allow` | `allow` unless a stronger local policy or semantic rule requires ask/deny. |
| `warn` | `ask` with LLM advisor text. |
| `block` | `deny` by default; broad allow rules must not override hard block/critical semantic categories. |

The final LLM stage is mandatory for semantic classification in v0.1 unless an explicit local policy rule or hard deterministic denial is sufficient. Parser output should enrich the LLM prompt with structure, normalized command units, substitutions, scripts, and annotations; it should not be expected to decide semantic risk alone.

LLM use is expected to be front-loaded for first-time or unknown command shapes. If the LLM classifies a shell construct and the user decides the case is obvious, the user may choose to create an allow/ask/deny rule so the same command shape can be handled by local policy next time without another LLM call. The UI should make rule creation available, but should not force it. In steady state, common safe/known command shapes may naturally migrate to local policy; the LLM remains for new, changed, or ambiguous shapes.

The Aegish prompt should be adapted rather than copied verbatim. Important prompt changes:

- Replace “bypasses aegish monitoring” with Shell Guard-specific language about bypassing or escaping agent shell-tool supervision.
- Treat commands as requested by an AI agent, not directly by a human sysadmin.
- Remove or neutralize Aegish cron/scheduled-task guidance for v0.1 because scheduled jobs are out of scope.
- Preserve the core decision tree around shell escapes, arbitrary execution, reverse/bind shells, critical file reads/writes, privilege escalation, exfiltration, library injection, download-and-execute, recon, downloads, benign writes, and normal safe operations.

The Aegish benchmark/article supports the general approach: static validation catches deterministic threats, while the LLM handles command-intent reasoning. The benchmark used GTFOBins-derived malicious commands and harmless command sets; v0.1 should consider Aegish/GTFOBins-style cases as validation inputs for Shell Guard behavior.

## Telegram runtime UX commitments

Telegram is the v0.1 interface for one pending interactive command decision.

Default prompt always shows compact reasoning:

- command text;
- risk level;
- short advice;
- strongest risk/safety signals;
- buttons.

Primary buttons:

- `Allow this time` — execute once and consume the pending token;
- `Deny this time` — cancel once and consume the pending token;
- `Create allow rule…` — choose rule scope, save rule, then ask whether to run the current command now;
- `Create ask rule…` — choose rule scope, save persistent ask rule, show a message that the rule was created, then re-show the current decision prompt;
- `Create deny rule…` — choose rule scope, save rule, and deny the current command;
- `Details` — send a separate details message.

Details behavior:

- Details sends a new Telegram message, not an in-place edit of the original prompt.
- Details do not consume or regenerate the shell request.
- The same pending token remains valid.
- The details message includes `Back to decision`.
- `Back to decision` sends a fresh compact prompt using the same token.

Details content should include parsed command units, matched rules, risk signals, LLM advisor notes, safer alternatives where available, and possible rule scopes.

## LLM advisor commitments

The Shell Guard LLM advisor helps the user decide; it does not approve its own recommendation.

The advisor may automatically use appropriate read-only tools, such as:

- reading local scripts referenced by the shell command;
- fetching intended remote script/content for inspection when the command itself intends to fetch it;
- parsing downloaded remote scripts as ordinary scripts before producing advisor text;
- using web/docs/MCPs to understand command meaning;
- checking remote script/source context through internet research where useful;
- inspecting Shell Guard policy/log context.

The advisor must not execute shell commands, edit files, change policy directly, or make final approval decisions.

The advisor/classifier can use an Aegish-inspired action vocabulary internally (`allow`, `warn`, `block`) and map it to Shell Guard decisions (`allow`, `ask`, `deny`). The user-facing Telegram prompt should use Shell Guard language, not Aegish terminology.

## Classify-mode telemetry commitments

Classify mode is field-study mode for real shell usage. It records enough data to later decide whether policy review belongs in Telegram, a `shellguard` CLI, a generated policy draft, or another interface.

Classify events should preserve:

- raw command;
- normalized command shape;
- parsed command units;
- risk level;
- `decision_if_active`;
- reason;
- matched rules;
- semantic flags;
- suggested policy candidate, if any;
- trace/tool-call context;
- timestamp.

Classify mode may later support aggregation into policy candidates, but v0.1 does not require bulk promotion UI or automatic policy mutation.

Risk taxonomy should be derived from the adapted Aegish decision tree and the LLM classifier's confidence. The working labels are `low`, `medium`, `high`, `critical`, and `unknown`. The prompt examples and confidence value should give the user enough context to decide. Telegram prompts should include a small safety hint: if the user is not sure, choose deny.

Risk examples to guide the proposal:

| Risk | Example command shapes | Intent |
| --- | --- | --- |
| `low` | `df -h`, `git status`, `opencode session list`, `python --version` | Read-only, local, non-sensitive inspection. |
| `medium` | `make test`, `pytest tests/`, `rm ./tmp/generated.log`, `pip install -r requirements-dev.txt` | Local/project changes, test/build execution, or bounded modification. |
| `high` | `curl -O URL`, `docker run ...`, `make deploy`, `opencode session delete <id>`, `chmod -R ...`, `kill -HUP <pid>` | Network, package/container/system operations, deletion, broad mutation, or process control. |
| `critical` | `curl URL \| bash`, `docker run --privileged --pid=host ... nsenter ...`, `rm -rf /`, `cat ~/.ssh/id_rsa`, `nc -e /bin/bash ...`, writes to `/etc/sudoers` | Remote-code execution, host escape, credential exposure, privilege escalation, reverse shell, or destructive system action. |
| `unknown` | custom binary with obscure args, obfuscated shell, dynamic command name, missing referenced script | Parser/advisor cannot confidently determine intent. |

Known binary does not imply known safety. For example, `make test` may be medium/allow-candidate, while `make deploy` is high/ask; `docker ps` may be low/medium, while privileged host namespace entry is critical.

Policy candidate aggregation/conversion is required in v0.1 as a CLI-oriented workflow. The initial version does not need a polished Telegram review flow, but should provide a tool that reads classify-mode observations, groups command shapes, and produces reviewable policy candidates or a policy draft.

Classify-to-policy conversion commitments:

- Input: detailed Shell Guard JSONL observations from classify mode.
- Grouping: aggregate by normalized command shape, binary/argv prefix, semantic category, and exact command where appropriate.
- Output: human-reviewable TOML policy draft or patch-like proposal, not automatic policy mutation by default.
- Review path: CLI can support explicit promotion/apply after the user reviews candidates.
- Safety: never auto-promote to broad binary-global allow; prefer exact or argv-prefix allow candidates for common low-risk shapes, ask rules for broad tools, and deny rules for critical semantic categories.
- Evidence: include observed count, sample commands, LLM action/risk/confidence summary, first/last seen timestamps, and reason/provenance in generated candidates.

## Policy and logging commitments

Shell Guard policy should live in a separate policy file. The main configuration may enable Shell Guard and point to this policy file, but persistent allow/ask/deny rules should not be embedded directly in the main config.

An initial CLI tool may manage policy review/promotion from classify observations. Telegram remains the runtime approval UI, while the CLI is the better v0.1 interface for bulk policy curation and debugging.

Preferred CLI surface:

```text
python -m shell_guard policy candidates
python -m shell_guard policy draft
python -m shell_guard policy apply
```

CLI behavior commitments:

- `policy candidates` shows grouped observations and recommendations.
- `policy draft` generates a reviewable TOML policy draft from classify observations.
- `policy apply` mutates the active policy file.
- `policy apply` is interactive by default and asks `yes`/`no` for each candidate.
- `policy apply --all` / `policy apply -a` applies all candidates as-is.
- Each completed apply creates a backup copy of the previous policy file, preferably date/timestamped.

Logging should have two levels:

- Basic Shell Guard information appears in the normal application log stream so ordinary runs remain understandable.
- Detailed guard metadata is written to a dedicated append-only JSONL metadata log for performance and debugging. Avoid many tiny per-command artifact files in v0.1 unless later evidence shows they are needed. Normal logs should reference the metadata id/path/offset where appropriate.

The separate policy file should optimize for runtime loading, debugging, and copy/paste into prompts. TOML is the selected default because this repo already uses TOML and Python has good TOML support. Use a single `[[rules]]` list with explicit `decision`, `scope`, and matching fields rather than separate allow/ask/deny tables.

Policy schema direction:

```toml
version = 1

[defaults]
unknown = "ask"
parse_error = "ask"

[[rules]]
id = "allow-opencode-session-list"
decision = "allow"
scope = "argv_prefix"
binary = "opencode"
argv_prefix = ["session", "list"]
reason = "Read-only opencode session listing."
created_by = "user"
created_at = "2026-07-08T12:00:00Z"

[[rules]]
id = "ask-make"
decision = "ask"
scope = "binary"
binary = "make"
reason = "Make targets can execute arbitrary project-defined commands."
created_by = "user"

[[rules]]
id = "deny-host-namespace-escape"
decision = "deny"
scope = "semantic"
category = "host_namespace_escape"
reason = "Privileged container with host namespace entry."
created_by = "builtin"
```

Remote script inspection depth for v0.1:

- Required: download the script that the command itself intends to execute, preserve URL/final URL metadata, parse/canonicalize it as shell content, and include script content/summary in LLM advisor context.
- Useful if feasible: record redirect chain, extract additional URLs referenced by the script, and use internet/docs lookup when the source claims to be a known project or install command.
- Out of scope: recursive crawling, full reputation scoring, cryptographic provenance verification, or turning source checks into a package-security scanner.

Aegish code reuse stance: reuse as much as is useful under MIT, but adapt to this project's style, type hints, docstrings, and architecture. Prefer Shell Guard-native APIs and dataclasses. Direct ports of meaningful Aegish code should keep appropriate attribution/license notice; prompt structure and validation ideas may be adapted rather than copied verbatim.

Detailed metadata schema direction:

```json
{
  "schema_version": 1,
  "event_id": "sg-20260708-abc123",
  "timestamp": "2026-07-08T12:00:00Z",
  "trace": "r-12345678",
  "mode": "active",
  "raw_command": "opencode session delete abc123",
  "normalized_command": "opencode session delete abc123",
  "parse_status": "ok",
  "parsed_units": [
    {
      "span": "opencode session delete abc123",
      "binary": "opencode",
      "argv": ["session", "delete", "abc123"],
      "redirects": []
    }
  ],
  "semantic_flags": ["destructive_operation"],
  "matched_rules": [],
  "llm": {
    "used": true,
    "model": "gpt-...",
    "action": "warn",
    "risk": "high",
    "confidence": 0.82,
    "reason": "Deletes local opencode session data."
  },
  "decision": "ask",
  "decision_source": "llm",
  "user_decision": "deny_this_time",
  "policy_change": null
}
```

Large artifacts such as downloaded remote scripts should not be embedded inline in every JSONL event. Store script summaries, hashes, and artifact references in the event, with bulky content under a guard artifact directory when needed.

## Cross-module data flows

Interactive shell call flow:

```text
react_loop.py
  -> BuiltinExecutor.execute("shell", args)
  -> shell preflight in builtin_executor.py
  -> Shell Guard parse/classify/policy decision
  -> allow: run existing shell backend
  -> ask: create pending token and Telegram prompt
  -> deny: return structured denial to agent
```

Telegram ask flow:

```text
Shell Guard ask decision
  -> existing confirmation/token mechanism or Shell Guard-specific token state
  -> telegram_interface.py renders compact prompt
  -> telegram_commands.py callback receives user decision
  -> allow once: resume/execute pending shell command
  -> deny once: cancel pending shell command
  -> details: send separate details message
  -> back: send fresh compact prompt with same token
```

Rule creation flow:

```text
Telegram create-rule callback
  -> scope selection: exact, argv-prefix, binary-global, semantic where applicable
  -> persist policy rule with provenance
  -> allow rule: ask Run now / Do not run
  -> ask rule: show rule-created message, then re-show current decision prompt
  -> deny rule: deny current request
```

Logging/classify flow:

```text
Shell Guard decision
  -> Aegish-inspired canonicalization and bashlex parsing
  -> decompose pipelines/lists/subcommands where applicable
  -> inspect but do not execute command substitutions
  -> LLM decision-tree classification: allow/warn/block
  -> map allow/warn/block to allow/ask/deny
  -> basic guard info in normal structured/prose logs
  -> detailed guard metadata in separate guard metadata log/file
  -> normal log references guard metadata file/id where appropriate
  -> CLI conversion tool groups observations into reviewable policy candidates
```

## Known open questions

- No major product-shaping open questions remain. Proposal/design may still refine field names and exact module boundaries.
