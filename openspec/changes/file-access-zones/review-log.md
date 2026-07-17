# Review Log: file-access-zones

## Round 1 — 2026-07-17

**Reviewer:** openspec-reviewer  
**Verdict:** `needs-revision`

---

## 🔴 Critical (blockers before apply)

**1. Checker wiring gap — BuiltinExecutor vs ReactContext**
`files.py` accesses `self._owner` (which is `BuiltinExecutor`), but tasks 4.1/4.3 only add `trusted_zone_checker` to `ReactContext`. Nothing puts the checker on `BuiltinExecutor`. Fix: construct one `TrustedZoneChecker` in `main.py` and inject the same instance into both `BuiltinExecutor` (for `classify()`) and `ReactContext` (for `reset_request_grants()`). Update design §main.py wiring and §ReactContext.

**2. `file_diff` and `file_send` bypass zone model entirely**
Both read files (and `file_send` egresses to Telegram) with "no change" in the design. This leaves two open doors in the read-confinement guarantee. Fix: route both through `classify()`, or add explicit design + ADR justification for the exemption.

**3. `classify() -> AccessDecision{ALLOW, CONFIRM}` too lossy for button visibility**
Task 5.3 requires different buttons for out-of-zone vs sensitive-in-trusted prompts, but a two-value enum cannot distinguish them. Fix: return zone/reason from `classify()` so the callback layer knows why it is confirming. Update task 2.3 and spec scenario.

**4. Delta spec headings — `openspec validate --strict` will fail**
Both specs use `## Requirements` instead of `## ADDED Requirements`. Fix: rename in both spec files.

---

## 🟡 Should Fix (settle before implementation)

**5. Sub-agent / headless + concurrency unspecified**
Sub-agents silently write to trusted zones with no operator bridge. The in-memory grant set is shared — concurrent sub-agents call `reset_request_grants()` clearing each other's grants. Fix: add design section on headless policy and per-agent checker scoping or locking.

**6. Trust store poisoning**
`data/trusted_dirs.json` is inside `data/` (INTERNAL → auto-allow), so the agent can silently overwrite the trust list and inject arbitrary trusted dirs. Fix: special-case `trusted_dirs.json` to CONFIRM, or document the risk in ADR-0010.

**7. Prefix containment bug**
`startswith(trusted_dir)` matches `/srv/shared-evil` against `/srv/shared`. Fix: use separator-boundary containment (`resolved == trusted or resolved.startswith(trusted + os.sep)`). Add sibling-prefix scenario to spec.

**8. Sensitive-pattern stacking on INTERNAL is undefined**
Vault file is INTERNAL and matches `secrets.*`. Does sensitive gate stack on INTERNAL? Two implementers will diverge. Fix: add explicit spec scenario.

**9. `workspace_dir = ~/Documents` includes agent source tree**
Default makes `~/Documents/develop/smallpieclaw` silently writable. Fix: narrow default or add ADR consequence naming self-modification risk.

---

## ✅ What's Done Well

- Six-gate contract (`builtin-tool-execution` spec) correctly preserved — condition change is delegated to tool capability, no contradiction.
- `realpath()` mandate with concrete symlink-escape scenario in spec.
- `trusted-dir-management` spec is thorough (empty state, invalid index, renumber, create-on-first-add).
- ADR-0010 consequences are honest about UX inversion and maintenance burden.

---

## 💡 Optional

- macOS case-insensitivity: `realpath()` doesn't normalize case — note the decision.
- `add_trusted` dedup/normalization against duplicate and trailing-slash variants.
- Proposal could cite `builtin-tool-execution` "condition owned by tool capability" clause to make "Modified Capabilities: none" self-evident.

---

## Round 2 — 2026-07-17

**Reviewer:** openspec-reviewer  
**Verdict:** `approve-with-notes`

### Per-Finding Verification

| # | Finding | Status |
|---|---------|--------|
| 1 | Checker wiring (Executor vs ReactContext) | ✅ resolved |
| 2 | `file_diff`/`file_send` bypass | ✅ resolved |
| 3 | `classify()` too lossy | ✅ resolved (minor residual — see note) |
| 4 | Delta spec headings | ✅ resolved |
| 5 | Sub-agent/headless + concurrency | ✅ resolved |
| 6 | Trust store poisoning | ✅ resolved |
| 7 | Separator-boundary containment | ✅ resolved |
| 8 | Sensitive-pattern stacking on INTERNAL | ✅ resolved |
| 9 | `workspace_dir` self-modification risk | ✅ resolved |

### Residual (non-blocking)

Tasks 3.2–3.4 and design comparison table still reference `AccessDecision.CONFIRM` instead of `ZoneClassification.UNRECOGNISED`. Intent is unambiguous to an implementer; does not block `--strict` validation. Cleaned up in post-round-2 patch.

### Summary

All 4 critical blockers and all 5 should-fix items substantively resolved. Delta specs will pass strict validation. Change is **ready to commit and apply**.

---

## Round 3 — 2026-07-17

**Reviewer:** openspec-reviewer  
**Verdict:** `approve-with-notes`

### Findings

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | should-fix | `proposal.md:27` still named `AccessDecision` (stale symbol) | ✅ fixed |
| 2 | should-fix | Sensitive-pattern gate undefined for `file_diff`/`file_send` | ✅ fixed — stacks on all file_* tools, added to design + tasks 3.6/3.7 |

### Consistency confirmed

- Proposal capabilities match spec dir names ✓
- "Modified Capabilities: none" correct — six-gate contract preserved ✓
- `ZoneClassification`/`UNRECOGNISED` consistent across all artifacts ✓
- ADR-0010 consequences complete ✓

### Summary

**Ready to commit and apply.** All findings from rounds 1–3 resolved. Commit proposal artifacts before starting `/opsx-apply`.

---

## Round 4 — 2026-07-18

**Reviewer:** openspec-reviewer
**Verdict:** `needs-revision`

### Findings

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | critical | Sharing model three-way contradiction (tasks 2.10/4.3, ADR:47) | ✅ fixed |
| 2 | should-fix | Grant API naming mismatch (`grant_for_request`/`reset_request_grants` vs `GrantTracker.add()/reset()`) | ✅ fixed |
| 3 | should-fix | `classify()` operation default fail-open (`"rw"` → `"write"`) | ✅ fixed |
| 4 | should-fix | `grant_tracker` on ReactContext undocumented | ✅ fixed |
| 5 | should-fix | `/dir list` mode annotation missing from scenario | ✅ fixed |
| 6 | should-fix | No spec scenarios for `file_diff` dual-path rule | ✅ fixed |
| 7 | should-fix | tasks.md 2.9 still checked (`is_write_protected_internal` still in code) | ✅ fixed |
| 8 | should-fix | ADR "opaque" overstates — internal is confirmation-gated, not hidden | ✅ fixed |

---

## Round 5 — 2026-07-18

**Reviewer:** openspec-reviewer
**Verdict:** `approve-with-notes`

### Round 4 Finding Verification

All 8 Round 4 findings resolved. Two optional items added (vault-UNRECOGNISED scenario, Add-to-trusted always-rw assertion).

### New Finding

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | should-fix | `classify()` self-exclusion for `trusted_dirs.json` + vault not documented in tasks 2.2/2.3 or design | ✅ fixed |

### Summary

**Ready to apply.** All findings from rounds 1–5 resolved. Artifacts are internally consistent and consistent with the code at all `[x]` tasks. `openspec validate --type change --strict` expected to pass. Resolve the trust-store/vault self-exclusion (task 2.2/2.3 + design) before implementing those tasks — done.

---

## Round 6 — 2026-07-18

**Reviewer:** openspec-reviewer
**Verdict:** `needs-revision`

### Findings

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | critical | Vault path hardcoded / `vault_path` arg dropped — breaks `$SPC_VAULT_FILE` override | ✅ fixed |
| 2 | should-fix | No spec scenario / test task for parent-trusted-but-override guarantee | ✅ fixed |

---

## Round 7 — 2026-07-18

**Reviewer:** openspec-reviewer
**Verdict:** `needs-revision`

### Findings

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | critical | `design.md` wiring block omitted `vault_path` — contradicting same file's defense-in-depth prose | ✅ fixed |
| 2 | should-fix | `tasks.md:34` (4.3) dropped required `agent_name` arg from constructor snippet | ✅ fixed |

---

## Round 8 — 2026-07-18

**Reviewer:** openspec-reviewer
**Verdict:** `approve`

### Summary

All findings from rounds 4–7 resolved. Both construction snippets (`design.md:158-163`, `tasks.md:34`) now show the complete 4-argument call matching the real constructor and `main.py`. `openspec validate --type change --strict` expected to pass. Ready to proceed to `/opsx-apply`.

---

## Round 6 — 2026-07-18

**Reviewer:** openspec-reviewer
**Verdict:** `needs-revision`

### Findings

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | critical | Round 5 fix hardcoded vault path (`~/.local/share/<agent>/secrets.toml`) and dropped `vault_path` arg — silently breaks the safeguard under `$SPC_VAULT_FILE` | ✅ fixed — tasks.md 2.2/4.3 + design.md updated to pass resolved `vault_path(cfg)` |
| 2 | should-fix | No spec scenario or test task asserting trust-store/vault remain UNRECOGNISED when parent dir is trusted | ✅ fixed — new scenario added to spec.md:50-54; 7.1 test list extended |

---

## Round 7 — 2026-07-18

**Reviewer:** openspec-reviewer
**Verdict:** `needs-revision`

### Findings

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | critical | `design.md` wiring block (lines 158-162) omitted `vault_path=vault_path(cfg)` — contradicted `design.md:186` and silently reinstated the vault-override gap if copied literally | ✅ fixed — `vault_path=vault_path(cfg),` added to wiring block |
| 2 | should-fix | `tasks.md:34` (4.3) Round 6 edit swapped `agent_name` for `vault_path` instead of keeping both — literal copy would yield `TypeError` | ✅ fixed — both args now shown: `agent_name=app_cfg.agent.agent_name, vault_path=vault_path(cfg)` |
