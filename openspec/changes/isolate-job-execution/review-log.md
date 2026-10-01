## proposal Round 1 — 2026-09-30

### 🔴 Fixed
- The existing job log truncates results to 2,000 characters; proposal now explicitly scopes full-result storage within existing bounded retention and defines expired details as unavailable.
- Scheduled runs are sub-agents under the current supervision spec; proposal now expressly modifies that capability to exclude scheduled runs from shared approvals and prompts.

### 🟡 Addressed
- Expandable scheduled output is identified as a deliberate reversal of current plain text; silent delivery is identified as new behavior.
- Delivery failure/restart remains best-effort, with retained job history authoritative.
- Parallel overlap with preserved context remains compatible, with lost-update risk noted.
- `run_now` remains asynchronous and its `notify`/`show` wording is carried forward.
- Turn-end rejection when owned children remain is delegated to the design and specs.

### 🔴 Outstanding
- None after edits; awaiting reviewer confirmation.

## proposal Round 2 — 2026-09-30

### 🔴 Fixed
- Reviewer confirmed both Round 1 contract issues resolved.

### 🟡 Addressed
- Failure notices also follow quiet idle delivery while preserving error classification and next-run content; `llm-error-recovery` is now an explicitly modified capability.

### 🔴 Outstanding
- None; awaiting re-review of the new scope clarification before freeze.

## proposal Round 3 — 2026-09-30

### 🔴 Fixed
- Reviewer confirmed the failure-notification scope clarification and all earlier findings are resolved; `proposal.md` is frozen.

### 🟡 Addressed
- Spec drafting must distinguish silent Telegram delivery (no sound) from failure suppression (which remains prohibited).

### 🔴 Outstanding
- None.

## proposal Decision-level clarification — 2026-09-30

The architecture check surfaced ambiguous failure-notification wording after proposal freeze. The operator confirmed that `notify=false` suppresses both success and failure run-result notices, while existing launch-failure alerts remain. Proposal was reopened before design/specs/tasks existed; its delivery and `llm-error-recovery` wording and the exploration brief were updated. Re-review required before re-freeze.

## proposal Round 4 — 2026-09-30

### 🔴 Fixed
- Reviewer confirmed the amended `notify=false`/launch-failure distinction and all other proposal commitments. Proposal re-frozen.

### 🟡 Addressed
- Delta specs must change the unconditional scheduled-failure Telegram-notify language in `llm-error-recovery` without dropping classification, retained records, or launch-failure alerts.

### 🔴 Outstanding
- None.

## proposal Decision-level expansion — 2026-10-01

The operator approved read-only `/jobs history <tag>` and `/jobs result <execution-id>` so `notify=false` results can be fetched without an agent turn. Proposal and dependent design were reopened; proposal now lists `telegram-command-surface` as modified. Re-review this batch before revising design, ADR, specs, or tasks.

## proposal Round 5 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed the read-only `/jobs` additions, their new capability mapping, and all prior frozen decisions; proposal re-frozen.

### 🟡 Addressed
- Design/spec drafting must specify run-ID and legacy-entry behavior, tag reuse, long-result transport, missing-argument/unknown-result errors, and documentation for new help entries. No proposal change needed.

### 🔴 Outstanding
- None.

## design Round 1 — 2026-10-01

### 🔴 Fixed
- Shared executor memory and graph-memory tools could still reach operator stores: design now denies all scheduled `memory_write` and `memory_graph_*` operations and removes the proposed new job KV store.
- Proposal did not extend quiet idle treatment to launch failures, reminders, or long-running warnings: design now preserves their existing delivery paths.
- Overly broad result/cancellation narrowing conflicted with council control: design preserves model-facing cancellation for managed sources while limiting only result retrieval to the caller's own spawned children.

### 🟡 Addressed
- Legacy direct-agent scheduler fallback fails closed for agentic tasks; launch failures receive records/IDs, skipped triggers do not.
- Legacy `success` booleans display as partial done/failed; expiry and reused tag behavior are explicit.
- `schedule show.is_running` remains true until every parallel run ends; Telegram character limit is evaluated after entity parsing.
- Removed vague job-log secret-redaction promise and clarified removed once-job history handling.

### 🔴 Outstanding
- None after edits; awaiting reviewer confirmation.

## design Round 2 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed all three Round 1 issues resolved: scheduled memory tools denied, non-result alerts unchanged, and cancellation compatibility retained.

### 🟡 Addressed
- Cancelled run-result notices follow the same idle queue except existing timeout-cancel suppression; empty-task reminders have no run ID.
- Operator prompt history removal, graph-memory nulls, and non-owned result-tool responses are explicit.
- Log tools remain a distinct default-available observability surface; failed history writes set an actionable job status and truthful notice.

### 🔴 Outstanding
- None; awaiting re-review of the final design clarifications before freeze.

## design Round 3 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed all design clarifications compatible with frozen proposal and canonical requirements; `design.md` is frozen.

### 🟡 Addressed
- Spec drafting must keep unknown-ID result errors compatible with council control and cover persistence-error precedence and delayed scheduled cancellation notice.

### 🔴 Outstanding
- None.

## adr Round 1 — 2026-10-01

### 🔴 Fixed
- Root-level glossary companion looked like a duplicate numbered ADR-0033; removed it and placed ADR term references in the change-local ADR glossary reference instead.

### 🟡 Addressed
- ADR states its compatibility/amendment relationship to ADR-0005 and ADR-0027, including model-facing result vs cancellation scope.
- Manifest corrects ADR-0001 -> ADR-0002 -> ADR-0003 supersession and names ADR-0016's remaining in-force scope.

### 🔴 Outstanding
- None after edits; awaiting reviewer confirmation.

## adr Round 2 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed the sole numbered ADR-0033, correct manifest, in-force supersession graph, and compatibility with ADR-0005/0027; ADR batch frozen.

### 🟡 Addressed
- Two cosmetic wording/paragraph notes are not decision-level and require no further changes.

### 🔴 Outstanding
- None.

## specs Round 1 — 2026-10-01

### 🔴 Fixed
- None yet. `openspec validate isolate-job-execution --strict` passed after restoring canonical scenario titles, but semantic review found two archival conflicts.

### 🟡 Addressed
- Reviewer identified configurable retention defaults, persistence-error precedence, completed-unretrieved child finish, prompt-registry owner data, command help and unknown/empty examples, explicit fallback record and parallel risk, and glossary-reference formatting for this batch to resolve.

### 🔴 Outstanding
- S-01: `llm-error-recovery` MODIFIED requirement drops canonical failure detection, failure-notice header, and success non-classification.
- S-02: Three unchanged canonical `sub-agent-supervision` requirements still permit non-owned result retrieval or imply old notification behavior; modify whole requirements with their existing scenarios. The prompt-registry admission-source requirement also needs updating.

## specs Round 2 — 2026-10-01

### 🔴 Fixed
- S-01: Failure detection, original error header and success non-classification restored in `llm-error-recovery`.
- S-02 partly: Additional supervision requirements and prompt-registry admission source now reflect owner-specific behavior.

### 🟡 Addressed
- Reviewer requested precise timeout-cancellation, history-write error precedence, configurable retention, duplicate-scenario cleanup, scheduled notification cross-reference, fallback record, council-control error and glossary formatting.

### 🔴 Outstanding
- S-02 remains at `sub-agent-supervision`'s copied "Supervision compatibility remains stable" scenario, which still permits joining scheduled runs through `get_agent_result`.

## specs Round 3 — 2026-10-01

### 🔴 Fixed
- S-02 closed: owning-turn child result retrieval and scheduled callback delivery are distinct; all modified canonical scenarios are preserved without contradictory scheduled joins.
- `openspec validate isolate-job-execution --strict` passed after the Round 2 corrections; six capability delta specs are frozen.

### 🟡 Addressed
- Timeout, fallback-record, and council-control cross-reference notes are wording-level and do not change implementation decisions; the frozen design and other spec requirements already define those behaviors.

### 🔴 Outstanding
- None.

## tasks Round 1 — 2026-10-01

### 🔴 Fixed
- None yet; structure, file paths, validation commands, and planning-only boundaries passed review.

### 🟡 Addressed
- Reviewer identified overlarge task bundles, missing `main.py` history wiring and adjusted test targets, scheduled terminal-notice edge cases, confirmation dispatch paths, help/architecture documentation, and glossary usage for this batch to resolve.

### 🔴 Outstanding
- T-01: no explicit test/implementation tasks prevent scheduled launches from acquiring the operator's automatic parent summary or `context_payload`.
- T-02: no explicit test/implementation tasks suppress on-demand child completion notifications and automatic ResultsMemory publication.

## tasks Round 2 — 2026-10-01

### 🔴 Fixed
- T-01 and T-02 closed: test-first scheduled parent-summary suppression and on-demand completion/recall suppression are now in the checklist.
- Overlarge work bundles were split and the main.py operator-history wiring and dedicated Telegram idle queue test are tracked.

### 🟡 Addressed
- Reviewer requested supervisor notice handoff, `/help` static checks, existing `/jobs` regressions, token/grant cleanup timing, conditional MCP denial evidence, and documentation/task-size refinements.

### 🔴 Outstanding
- T-03: no test/implementation pair after run-ID storage for failed spawn/fail-closed fallback records with admitted ID and error, or for no records on overlap skips and empty-task reminders.

## tasks Round 3 — 2026-10-01

### 🔴 Fixed
- T-03 closed: separate tests and implementation for failed-spawn/fail-closed records with admitted IDs and no records for skips/reminders follow run-ID storage.
- Reviewer confirmed dependency order, sequential checkbox numbering, tracked test/implementation coverage, and strict change validation; `tasks.md` is frozen.

### 🟡 Addressed
- Optional implementation-time notes: if MCP confirmation is present, route its denial through the existing scheduled policy; split the idle-queue handoff task if it exceeds two hours; add targeted secret/grant-veto tests and update architecture notes in AGENTS.md if the resulting contract changes those entries.

### 🔴 Outstanding
- None.
