## proposal Round 1 — 2026-07-22

### 🔴 Outstanding
- None.

### 🟡 Addressed
- **Gap 6 `get_server_info()` extra item**: Verified against tests — `test_get_server_info_found`
  (line 814) only tests `active` state; disabled ("off") and error-wrapper states are genuinely
  untested. Item retained as non-redundant.
- **Brief↔proposal gap-2 wording**: Explore-brief says "30s hang" risk; proposal correctly says
  "regression guard, not bug fix." Proposal wording is accurate (source-confirmed: exception
  propagates to `_ready_future.set_exception` at line 237, resolves immediately). Brief was
  written with exploratory precision; proposal takes precedence. Design/spec authors must NOT
  introduce a code change for this path — test only.

**Verdict: proposal.md FROZEN.**

---

## design Round 1 — 2026-07-22

### 🔴 Outstanding
- None.

### 🟡 Fixed
- **`set_enabled` already-connected test not in frozen proposal**: Added declaratively to
  proposal.md "What Changes" minor edge cases list (soft freeze — no decision change).
- **D4 missing "Alternative considered"**: Added "vulture whitelist — rejected" subsection.
- **Migration step 5 vulture rationale inaccurate**: Corrected to explain that
  `_SdkClientWrapper.last_error` attribute suppresses the warning via name-matching.

**Verdict: design.md FROZEN.**

---

## specs+adr Round 1 — 2026-07-22

### 🔴 Outstanding
- None.

### 🟡 Resolved
- **`set_enabled` scenario not in proposal (reviewer stale cache)**: Reviewer had cached
  proposal.md from before the soft-freeze edit. `set_enabled(True)` short-circuit is confirmed
  at proposal.md:23. Not a real gap — all three frozen artifacts are consistent.
- **Status requirement overlaps existing "Server status reporting"**: Accepted as-is. The
  all-ADDED modeling approach is deliberate; no contradictions exist between the delta and the
  existing spec.

**Verdict: specs/mcp-transport/spec.md + adr.md FROZEN.**

---

## tasks Round 1 — 2026-07-22

### 🔴 Outstanding
- None.

### 🟡 Fixed
- **Tasks 3.1/3.2 missing `last_error` assertion**: Added `wrapper.last_error` non-empty
  check to both tasks to fully cover the frozen spec's THEN clause.
- **Task 6.6 enabled flag under-specified**: Added `enabled=True` to setup description so
  the test deterministically produces `status == "error"` and not `"off"`.

**Verdict: tasks.md FROZEN. All artifacts complete — ready for /opsx-apply.**

---

## tasks Round 1 — 2026-07-22

### 🔴 Outstanding
- None.

### 🟡 Fixed
- **Tasks 3.1/3.2 missing `last_error` assertion**: Added `wrapper.last_error` non-empty
  assertion to match the frozen spec's THEN ("descriptive `last_error` message").
- **Task 6.6 enabled flag under-specified**: Added `enabled=True` to setup so the test
  deterministically produces `status == "error"` rather than `"off"`.

**Verdict: tasks.md FROZEN. All artifacts complete — ready for /opsx-apply.**

---

## final Round 1 — 2026-07-22

### 🔴 Outstanding
- None. All 12 spec scenarios verified against mcp_client.py source — every asserted
  behavior already exists. Change is purely dead-code removal + tests + docstrings.

### 🟡 Fixed
- **Task 6.4 timing race**: `_start_loop()` idempotency guard keys on `_loop.is_running()`
  (mcp_client.py:320), not thread liveness. Back-to-back calls may create a second loop
  before the first thread enters `run_forever`. Task updated to assert object identity or
  synchronize on `_loop.is_running()`.
- **Task 6.7 fixture under-specified**: `set_enabled` returns False when name not in `_cfgs`
  (line 418). Task updated to require registering cfg in `_cfgs` AND wrapper in `_wrappers`.

**Verdict: CLEARED FOR /opsx-apply.**
