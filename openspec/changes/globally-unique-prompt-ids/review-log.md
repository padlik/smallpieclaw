## proposal Round 1 — 2026-07-23
### 🔴 Fixed
- Rejected alternatives, chosen ULID approach, ID-format mapping table, cross-module
  data flow, all four open-question decisions, and both affected specs from
  explore-brief.md are all faithfully captured in proposal.md — no contradictions found.
- Modified Capabilities (`prompt-tracking`, `runtime-log-introspection`) both exist in
  `openspec/specs/` and are the correct targets. `telegram-command-surface` is correctly
  NOT listed (brief marks it unchanged).
- Impact section covers every file in the brief's cross-module data flow
  (telegram_interface, agent_controller, sub_agent_supervisor, sub_agent_registry,
  builtin_tools/agents, telegram_commands, log_query) plus builtin_executor.py and
  builtin_tools/schemas.py — more thorough than the brief.

### 🟡 Addressed
- Full-vs-truncated display: brief open-question #4 decides "show full ULID (truncation
  risks ambiguity, operator copy-pastes for log queries)". proposal What Changes says only
  "shows the prompt ID" without committing to full display. Non-blocking — capture the
  "full ULID, no truncation" contract explicitly in design.md / spec scenarios so an
  implementer doesn't truncate.
- Rejected alternatives (doc-only, full UUID, date-prefixed sequential) are not restated
  in proposal.md. Acceptable for a proposal; belongs in design.md if one is produced.

### 🔴 Outstanding
- (none)

## design Round 1 — 2026-07-23
### 🔴 Fixed
- Faithful to explore-brief: all four open questions resolved in Decisions 1/2/4 (inline generator, legacy tolerance, `_next_id` removal, full display); rejected-alternatives list preserved and expanded (adds `ulid-py` lib and short-hex). The proposal Round-1 🟡 (commit to full, no-truncation display) is now explicitly captured in Decision 2.
- Consistent with frozen proposal.md — no contradictions with What Changes / Modified Capabilities / Impact. int→str type change, inline ULID, `/prompts` text+timestamp, and legacy tolerance all align.
- ADR coherence verified. ADR-0004: `prompt_id` stays an opaque observability field and `bind_run_context` already stringifies, so the log path needs no logic change. ADR-0011: verified against the archived prompt-scoped-approvals design + source — `_current_prompt_id` is a per-prompt scope marker, NOT the approval-set key (the set is `set[str]` of tool names), so int→str is safe. New ADR correctly deferred to the `adr` step.
- All three operator requirements addressed: (a) Decision 3 shows start timestamp + truncated text; (b) Decisions 1/2 make the full ULID the single operator handle (no `<id><traceid>`); (c) Decision 1's ULID (48-bit ms timestamp + 80-bit random) is globally unique across days.

### 🟡 Addressed
- Inline-generator primitive is mis-stated. brief Q1, proposal L34, and design Decision 1 all say `secrets.token_hex`. `token_hex(10)` returns a 20-char hex string; base32-encoding raw ULID bytes needs `secrets.token_bytes(10)` (6 timestamp bytes + 10 random bytes = 16 bytes → 26 Crockford-base32 chars). Taken literally, `token_hex` produces a wrong-length ID. The design's authoritative "48-bit ts + 80-bit random = 16 bytes" is correct; clarify the primitive is `token_bytes`. (proposal is frozen with the same phrasing — declarative, soft-freeze-correctable, no unfreeze required.)
- Migration Plan rollback overstates the failure mode. It claims old replay "reads prompt_id as int — this would break." Actual current code (`prompt_registry.py:84`) guards `if prompt_id is None or not isinstance(prompt_id, int): continue` — old code SILENTLY SKIPS ULID str records; it does not crash. Rollback is therefore safer than described (ULID prompts become invisible until aged out, no exception). Correct the wording so nobody over-engineers the rollback.
- Cross-day `log_query` scope (informational). Global uniqueness + persistent `prompts.jsonl` satisfies "resolve a prompt to its record," but `log_query` stays active-day-only (Non-Goal), so a cross-midnight prompt's log lines in the prior day's `agent.jsonl` remain unqueryable. Consistent across brief + design; flagging so this is a conscious product acceptance, not an implementer trap.

### 🔴 Outstanding
- (none)

## design Round 2 — 2026-07-23
### 🔴 Fixed
- **Blocker resolved — `list_recent` sort contradiction is gone.** Decision 1 now explicitly states the current impl sorts by `prompt_id` keys and this change **requires** `list_recent` to sort by `started_at` descending, because mixed legacy-int + ULID-str IDs raise `TypeError` on comparison. The Risk section matches exactly — "required code change, not pre-existing behavior." Verified against `prompt_registry.py:211`. An implementer no longer ships a crash-on-`/prompts`.

### 🟡 Addressed
- **Rollback wording corrected (round-1 🟡).** Migration Plan now says old replay logic "silently skips" ULID str records, citing the guard at `prompt_registry.py:84` and "no crash occurs." Verified against source.
- **Generator primitive corrected (round-1 🟡).** Decision 1 now reads `secrets.token_bytes(10)` (6 ts + 10 random = 16 bytes → 26 Crockford base32 chars). Risk section also corrected to `token_bytes(10)`.

### 🔴 Outstanding
- (none)

## specs Round 1 — 2026-07-23
### 🔴 Fixed
- (first specs round — nothing prior to fix)

### 🟡 Addressed
- Stale base Purpose: after archive, `prompt-tracking` Purpose still reads "monotonic ... Prompt #N" (base lines 3-5), contradicting the new globally-unique requirement. Deltas can't edit Purpose — reconcile manually at sync/archive.
- `runtime-log-introspection` example IDs (`01JARYN6R0`, `01JARYZ3W2`) are 10 chars, not 26-char ULIDs. Illustrative only; prefer full-length examples or `01J...` elision for consistency with design Decision 2. (optional)
- Verified good: round-1 design blocker (`list_recent` sort by `started_at`) is now a spec scenario; ULID 26-char, no-truncation display, `/prompts` timestamp+text, legacy-int tolerance, and cross-day unambiguous filtering all captured; `runtime-log-introspection` header matches base exactly; duplicate "Filter by prompt id" scenario collapsed; Gherkin structure clean.

### 🔴 Outstanding
- **Both `prompt-tracking` MODIFIED requirement headers were renamed — breaks `MODIFIED` matching / produces a contradictory archived spec.** `intent-driven` schema has no `RENAMED` op; archive matches `MODIFIED` blocks by `### Requirement:` header. Deltas renamed "Prompt registry assigns monotonic prompt IDs" → "...globally-unique prompt IDs" and "Operator can list recent prompts" → "...with recognizable details". Neither new header exists in the base spec, so archive either errors under `--strict` or appends the new requirements while leaving the stale `monotonic` / list-recent requirements. Fix: keep the exact original headers and edit only the body/scenarios. (`runtime-log-introspection` header is correct — unaffected.)

## specs Round 2 — 2026-07-23
### 🔴 Fixed
- **Round-1 blocker resolved — both `prompt-tracking` MODIFIED headers now match the base exactly.** `### Requirement: Prompt registry assigns monotonic prompt IDs` (delta L3 == base L9) and `### Requirement: Operator can list recent prompts` (delta L48 == base L40). Only bodies/scenarios were edited. `MODIFIED` matching resolves cleanly; archive will not produce orphaned/duplicate requirements. `runtime-log-introspection` header unchanged and still matches base (delta L3 == base L9).

### 🟡 Addressed
- **Header word "monotonic" is now semantically stale vs. its ULID body** (delta L3). This is an accepted, unavoidable artifact of the no-`RENAMED` constraint: keeping the exact original header is required for `MODIFIED` matching, so the header text can't track the body. Same class as the stale base Purpose flagged in specs Round 1 — reconcile the requirement title (and Purpose) manually at sync/archive. Non-blocking.
- Design decisions all verified present: ULID 26-char format (L5), full-ULID no-truncation display (L53/L59), `/prompts` timestamp+text (L60–61), `list_recent` sort by `started_at` desc with no mixed-type `TypeError` (L50/L64–68), legacy-int replay tolerance (L29–34), cross-day unambiguous `prompt_id` filtering (runtime-log L21–25). Gherkin structure clean; MODIFIED requirements show complete bodies.
- Round-1 optional note (short 10-char example IDs in runtime-log-introspection) is still present but remains illustrative-only — not re-raised.

### 🔴 Outstanding
- (none)

## adr Round 1 — 2026-07-23
### 🔴 Fixed
- (none — first review of this batch)

### 🟡 Addressed
- (none)

### 🔴 Outstanding
- (none)

### Notes
Repo-level ADR `adr/0013-use-ulid-for-globally-unique-prompt-ids.md` and change-local manifest `adr.md` both PASS. Single durable decision (ULID for prompt IDs), not a bundle. MADR-minimal structure present. Supersedes: None; verified against ADR-0004 and ADR-0011 — both treat prompt_id as opaque, no conflict. Sequence 0013 correct (highest prior 0012). Decision & Consequences consistent with frozen design.md. Manifest states review completed, lists in-force ADRs reviewed, references the new 0013 file, does not duplicate full ADR body.

## tasks Round 1 — 2026-07-23
### 🔴 Fixed
- Coverage complete against design + frozen artifacts. All 9 Impact source files have a task. All 5 design Decisions covered. Round-1 design blocker (`list_recent` sort by `started_at`) is task 2.5 + tests 3.2/3.3. Legacy int tolerance is 2.4 + tests 3.4/3.5. log_query str-safe filter verified (`secrets_log.py:282`). `bind_run_context` stringification verified (`agent_controller.py:189`). Format/granularity/ordering all sound.

### 🟡 Addressed
- `test_prompt_approval_ttl.py` not in the migration task list — uses int literals against the new str contract. Added task 5.4.
- `_records: dict[int, PromptRecord]` annotation (line 51) not called out. Folded into task 2.1.
- Stale docstrings (lines 4–5, 42) say "monotonic … Prompt #N". Added task 2.8.
- Task 1.2 wording nit re: "monotonically non-decreasing within the same ms" — reworded to "increases across milliseconds".

### 🔴 Outstanding
- **`%d` log format strings in `prompt_registry.py` will silently break once `prompt_id` is a str — no task covered this.** Lines 152, 165, 177, 184, 193 format `prompt_id` with `%d`. With a ULID string, `%d` raises `TypeError` at emit time; Python logging swallows it (no propagation, no test failure), so every prompt lifecycle event ships a corrupted log line. Added task 2.7 to change `%d` → `%s`.

## tasks Round 2 — 2026-07-23
### 🔴 Fixed
- **Round-1 blocker resolved — `%d`→`%s` for `prompt_id` is now task 2.7.** Verified against source: all five `logger.*` calls format `prompt_id` with `%d` at the exact lines cited (L152, L165, L177, L184, L193). Task 2.7 correctly scopes the change to the `prompt_id` specifier only, leaving co-located `%s` for trace/status/agent_id untouched. Accurate and complete.

### 🟡 Addressed
- Stale docstrings (round-1 🟡) → task 2.8, now also covers `_replay` docstring L67–70 (`max(prompt_id)+1` sentence).
- `dict[int, PromptRecord]` annotation → folded into task 2.1.
- `test_prompt_approval_ttl.py` → task 5.4.
- Task 1.2 wording reworded.
- Regression guard for 2.7: task 3.1 now includes `caplog.getMessage()` assertions on `start()`/`finish()` to catch future `%d` reintroduction.

### 🔴 Outstanding
- (none)