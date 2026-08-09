## proposal Round 1 — 2026-08-09
### 🔴 Outstanding
- none
### 🟡 Addressed / Suggestions
- Impact says the two mcp_client.py sites are unaffected "because they never call get_client_info()" — stronger than the brief (which only guarantees they compile unchanged). Safe regardless (default matches build()), but verify the no-call claim or soften the wording.
- Why explains the symptom/mechanism but names the two-object drift root cause only in What Changes; consider a one-clause mention in Why for self-containment.
### Brief coverage
- Two-object drift (client_metadata vs client_info): captured
- Both return paths in get_client_info(): captured
- Fill-if-None for cached path (not force-override): captured
- Defaulted constructor param / zero blast radius: captured
- Source from client_metadata, not a new config field: captured
### Verdict
- PASS — ready to freeze; both suggestions are non-blocking.

## design Round 1 — 2026-08-09
### 🔴 Outstanding
- none
### 🟡 Addressed / Suggestions
- Risk 2 mitigation claimed the two mcp_client.py sites "never call get_client_info()" — unverified and likely wrong. Softened to: even if they reach get_client_info(), the default matches build()'s value, so behavior is unchanged.
- Pre-seed normalization lacked its own Decision heading. Added Decision 4 for traceability and symmetry with Decision 3.
- "Behave unchanged" in Goals was imprecise — the fix changes runtime behavior (sends client_secret). Clarified to "compile without modification; only the OAuth-flow sites gain the corrected auth header."
### Brief coverage
- Two-object drift (client_metadata vs client_info): captured
- Both return paths in get_client_info(): captured (Decision 3 + Decision 4)
- Fill-if-None for cached path (not force-override): captured
- Defaulted constructor param / zero blast radius: captured
- Source from client_metadata, not a new config field: captured
- Alternatives A & B rejected with reasons: captured
- Cross-module data flow: captured (fixed flow diagram)
- Open question (config-driven method = future scope): captured
### Verdict
- PASS — ready to freeze; all suggestions applied.

## specs Round 1 — 2026-08-09
### 🔴 Outstanding
- none
### 🟡 Addressed / Suggestions
- Requirement prose leaked `model_copy(update={...})` implementation detail. Removed — the observable contract is "fill-if-None, preserve-if-set" which the scenarios already state cleanly.
### Brief coverage
- Defaulted constructor param: captured
- build() passes client_metadata.token_endpoint_auth_method: captured
- Pre-seed path adds auth method: captured (modified scenario)
- Cached path fill-if-None (not force-override): captured (two cached scenarios)
- Sourcing from client_metadata (not new config): captured
- Two-object drift addressed: captured ("prevent drift between the two objects")
- Both return paths have scenarios: captured
- Observable fix outcome (client_secret reaches token exchange): captured
### MODIFIED Requirements correctness
- Header match: exact
- Full requirement content (not partial diff): all 5 original scenarios preserved
- Pre-seeded scenario correctly modified
- New cached-path scenarios added
### Verdict
- PASS — ready to freeze; suggestion applied.

## tasks Round 1 — 2026-08-09
### 🔴 Outstanding
- none
### 🟡 Addressed / Suggestions
- Task 1.4 line reference "between line 176 and 178" was fragile to line drift. Reworded to "inside the if cached.client_secret == self.client_secret: block, immediately before return cached".
- Task 3.1 vulture invocation omitted --exclude interfaces.py (drifted from make lint). Aligned with Makefile.
### Coverage check
- Decision 1 (defaulted ctor param): task 1.1 ✓
- Decision 2 (build() sources from client_metadata): task 1.2 ✓
- Decision 3 (fill-if-None on cached path): task 1.4 ✓
- Decision 4 (pre-seed includes auth method): task 1.3 ✓
- Spec scenario: pre-seed returns token_endpoint_auth_method: task 2.1 ✓
- Spec scenario: cached None repaired: task 2.2 ✓
- Spec scenario: cached non-None preserved: task 2.3 ✓
- Backward compat (default value): task 2.4 ✓
- Verification (lint, test, openspec validate): tasks 3.1-3.4 ✓
### Verdict
- PASS — ready to freeze; all suggestions applied.