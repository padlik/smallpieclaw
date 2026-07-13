## proposal Round 1 — 2026-07-13

### 🔴 Fixed
- None; proposal review found no critical issues.

### 🟡 Addressed
- Carry forward to design/specs: graph/strategy post-init memory preservation, `_on_step` ordering after `register_run()`, runner-shaped product compatibility surface, scheduler construction impact, and avoiding no-op modified capability deltas.

### 🔴 Outstanding
- None.

## design Round 1 — 2026-07-13

### 🔴 Fixed
- None; design review found no critical issues.

### 🟡 Addressed
- Design closes proposal-round advisories for graph/strategy post-init preservation, `_on_step` ordering, runner-shaped product surface, construction-only scope, and profile/source separation.
- Carry forward to specs/tasks: express equivalence against product surface rather than concrete return type, and pin scheduler construction site in tasks.

### 🔴 Outstanding
- None.

## specs Round 1 — 2026-07-13

### 🔴 Fixed
- None; specs review found no critical issues.

### 🟡 Addressed
- Added explicit preservation assertions for `usage_registry`, caller tagging, and `max_iterations`.
- Removed empty operation sections from delta specs.

### 🔴 Outstanding
- None.

## adr Round 1 — 2026-07-13

### 🔴 Fixed
- None; ADR review found no critical issues.

### 🟡 Addressed
- Added an ADR-0007 consequence noting that centralized trace/cancel wiring must preserve ADR-0004 trace propagation and thread/executor trace-identity binding guarantees.

### 🔴 Outstanding
- None.

## tasks Round 1 — 2026-07-13

### 🔴 Fixed
- None; tasks review found no critical issues.

### 🟡 Addressed
- Added explicit task 4.6 for preserving `_on_step` ordering so runtime construction does not clobber registry-installed callbacks.

### 🔴 Outstanding
- None.

## implementation review note — 2026-07-13

### 🟡 Addressed
- Clarified `agent-runtime-construction` spec wording so `MAIN` is covered by runtime-owned per-run context assembly while top-level main controller construction remains outside this change.
