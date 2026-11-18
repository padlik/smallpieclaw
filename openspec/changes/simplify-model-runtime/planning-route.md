# Planning-only publication route

Approved route, 2026-11-01. Planning is intentionally drafted on `feature/provider-model-catalog-attempts`; this is not permission to implement, commit, merge, or archive.

## Baselines

- Implementation baseline: `a57ab26d9731690725705f6df5aa4f6a059b0918` on the feature branch.
- Predecessor: `provider-model-catalog`, whose implementation and 70 completed tasks are committed on that branch, but whose implementation has not been merged to `main` or archived.
- Canonical specifications: `openspec/specs/` on `main`.
- Pending predecessor requirements: `openspec/changes/provider-model-catalog/specs/`. These are not yet canonical specifications. The new artifacts must explicitly distinguish modifications of canonical requirements from replacements of these pending requirements.

> **Historical status note (2026-11-17).** "Pending" and "not canonical" describe the predecessor at planning time. On `main` the predecessor `provider-model-catalog` has since been archived as `openspec/changes/archive/2026-11-17-provider-model-catalog` and its specifications synchronized into `openspec/specs/`; that integration lives on `main`, not in this feature branch, which carries the planning text as written. Decisions, dependency ordering and headings are unchanged.

## Route and gates

1. Draft and review the new planning artifacts here, one batch at a time. Preserve the predecessor's frozen artifacts and accepted ADRs; scope any supersession explicitly in the new change.
2. After separate approval, publish ONLY the new change's reviewed planning artifacts and any necessary new, scoped ADR records through a planning-only branch based on `main`. Do not merge implementation merely to publish planning, or cherry-pick a mixed planning/code commit.
3. After separate approval, bring the published planning state back into the implementation branch. Confirm that the exact proposal and required artifacts are committed on `main` before apply begins.
4. Implement only after a new explicit apply instruction. Do not start apply as a continuation of proposal authoring.
5. Verify the final combined implementation. Merge only after separate approval. Synchronize/archive on `main` in predecessor-then-follow-up dependency order, checking that final canonical requirements describe the simplified behavior rather than restoring the predecessor's superseded contracts. Each lifecycle operation requires its own authorization.

## Current boundary

Prepare the exploration brief and first proposal batch, review that batch, then pause. Do not advance to design, specs, ADRs, or tasks without the next instruction. No application-code or test edits are authorized here. Preserve the pre-existing `.gitignore` and `openspec/config.yaml` edits.
