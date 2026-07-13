## proposal Round 1 — 2026-07-12

### 🔴 Fixed
- Missing compatibility-shim/test-migration decision → proposal now states `_exec_spawn_agent` remains the model-facing compatibility entry point, scheduler launches move to the supervisor seam, and tests should migrate scheduler-control assertions to supervision options.

### 🟡 Addressed
- Incomplete shim/supervisor split → proposal now names context-key validation, depth/friendly policy errors, scheduler rewiring, and result rendezvous compatibility.
- Control fields not enumerated → proposal now lists `_job_tag`, `_finish_cb`, `_result_log_cb`, `_notify`, and `expandable`.
- `sub-agent-context` rationale mismatch → proposal reframes the modified capability around channel separation rather than previously documented scheduler keys.
- `get_agent_result` relationship unstated → proposal now states lookup may use supervisor or shared registry but behavior must remain compatible.

### 🔴 Outstanding

## specs Round 2 — 2026-07-12

### 🔴 Fixed
- None; specs review found no serious outstanding issues.

### 🟡 Addressed
- Graph-memory non-population and model-facing validation preservation scenarios were confirmed resolved; requirement wording was aligned to include model-facing contract validation.

### 🔴 Outstanding
- None.

## adr Round 1 — 2026-07-12

### 🔴 Fixed
- None; ADR review found no serious outstanding issues.

### 🟡 Addressed
- Added a consequence to ADR-0005 noting that supervisor-owned thread-pool submission must preserve ADR-0004's structured logging/trace identity binding obligation at background thread/executor entry points.

### 🔴 Outstanding
- None.

## tasks Round 1 — 2026-07-12

### 🔴 Fixed
- None; tasks review found no serious outstanding issues.

### 🟡 Addressed
- Split the oversized supervisor extraction task into synchronous registration/submission and background lifecycle tasks.
- Added an explicit test task for sub-agent context channel separation.

### 🔴 Outstanding
- None.

## design Round 2 — 2026-07-12

### 🔴 Fixed
- None; prior round's cap/source preservation issue was confirmed resolved.

### 🟡 Addressed
- `_scheduler_finish_cb` fallback ambiguity was confirmed resolved.

### 🔴 Outstanding
- None.

## specs Round 1 — 2026-07-12

### 🔴 Fixed
- None; specs review found no serious outstanding issues.

### 🟡 Addressed
- Added defensive scenarios for model-facing spawn validation compatibility and graph-memory non-admission after result delivery moves into the supervisor.

### 🔴 Outstanding


## proposal Round 2 — 2026-07-12

### 🔴 Fixed
- None; prior round's serious compatibility-shim/test-migration issue was confirmed resolved.

### 🟡 Addressed
- Responsibility split, control-field enumeration, channel-separation framing, and `get_agent_result` relationship were confirmed addressed.

### 🔴 Outstanding
- None.

## design Round 1 — 2026-07-12

### 🔴 Fixed
- Cap/source preservation ambiguity → design now states scheduler launches must preserve the same pre-submit max-subagents check and `SubAgentRecord.source` semantics they currently receive through `_exec_spawn_agent`; changing scheduled cap/visibility behavior is deferred.

### 🟡 Addressed
- `_scheduler_finish_cb` fallback ambiguity → design now requires translating any still-wired shared fallback into per-submission options or removing it only after confirming no current composition path depends on it.

### 🔴 Outstanding
