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
