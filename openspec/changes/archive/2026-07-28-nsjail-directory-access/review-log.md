## proposal Round 1 — 2026-07-28

### 🔴 Fixed
- (none yet — first round)

### 🟡 Addressed
- (none yet — first round)

### 🔴 Outstanding
- **Missing modified capability: `secure-secret-resolution`**: The existing spec references the old vault path `~/.local/share/<agent_name>/secrets.toml` in two scenarios. When the vault path changes to `~/.local/state/<agent_name>/secrets.toml`, this spec must also be updated.
- **Missing modified capability: `file-access-zones`**: The existing spec references the old vault path in a scenario. When the vault path changes, this scenario's GIVEN clause must be updated.
- **Rejected alternatives not documented in proposal**: The explore brief lists 3 rejected alternatives with clear reasoning. The proposal's "Why" section doesn't enumerate what was considered and rejected.
- **Vault migration edge case unspecified**: The proposal says "if the old path exists and the new path doesn't, copy it" but doesn't define behavior when both paths exist.
- **Open questions from brief not explicitly addressed**: The explore brief has 3 open questions with resolutions. The proposal doesn't explicitly reference them.

## proposal Round 2 — 2026-07-28

### 🔴 Fixed
- **Missing `secure-secret-resolution`**: Now listed under Modified Capabilities with accurate description of the vault path update needed in two scenarios.
- **Missing `file-access-zones`**: Now listed under Modified Capabilities with description of the vault path update needed.
- **Rejected alternatives not documented**: "Alternatives Considered" subsection added, covering all three rejected approaches from the explore brief with their reasoning.
- **Vault migration edge case unspecified**: Now defined — "If both paths exist, prefer the new path and log a warning that the old path is stale and can be removed manually."

### 🟡 Addressed
- **Open questions from brief not explicitly referenced**: Resolutions are implicitly reflected in the design (skills_dir stays as-is, workspace/downloads not auto-added, test impact covered in Impact section). Minor traceability gap, not a blocker.

### 🔴 Outstanding
*(none — proposal passes, frozen)*

## design Round 1 — 2026-07-28

### 🔴 Fixed
*(none — first design round)*

### 🟡 Addressed
*(none — first design round)*

### 🔴 Outstanding
*(none — design passes, frozen)*

### 💡 Optional Suggestions
- D3 carve-out generalizes beyond the brief's narrower scope (project_dir only) to any known mounted directory — intentional refinement, noted for implementation clarity.
- ADR-0015's `_AGENT_DIR` blocklist entry becomes a no-op after removing `project_dir` — keep as defense-in-depth or remove as dead code (implementation detail).

## specs+adr Round 1 — 2026-07-28

### 🔴 Fixed
*(none — first specs+adr round)*

### 🟡 Addressed
*(none — first specs+adr round)*

### 🔴 Outstanding
*(none — specs and ADR pass, frozen)*

### 💡 Optional Suggestions
- shell-env-management: "AND" after "THEN" in one scenario is a formatting nit — intent is clear.
- ADR-0016 title is narrower than its content (also covers /home blocklist + vault consolidation) — cosmetic.

## tasks Round 1 — 2026-07-28

### 🔴 Fixed
*(none — first tasks round)*

### 🟡 Addressed
*(none — first tasks round)*

### 🔴 Outstanding
- **Missing task: system prompt update for cwd change**: The design's Open Questions section deferred this to the tasks phase. No task existed.
- **Missing test tasks for new skills_dir behavior**: The skills-dir-sandbox-mount delta spec adds two new scenarios (under /home accepted, blocked sensitive path rejected) with no corresponding test tasks.

## tasks Round 2 — 2026-07-28

### 🔴 Fixed
- **Missing task: system prompt update for cwd change**: Now task 1.5 — update system prompt to inform LLM that cwd is /tmp and host file access requires trusted-dir approval.
- **Missing test tasks for new skills_dir behavior**: Now task 4.5 — covers skills_dir under /home accepted (RO mount) and skills_dir on blocked user prefix rejected.

### 🟡 Addressed
- **Task 3.3 wording clarified**: Changed from "spec references" to "vault path comment/docstring in config_schema.py" — unambiguous.
- **Task 1.4 line number caveat added**: Notes that line numbers will shift after task 1.1, directs implementer to search for `os.path.commonpath`.

### 🔴 Outstanding
*(none — tasks pass, frozen)*

## final cross-artifact review — 2026-07-28

### 🔴 Fixed
*(none — all prior issues resolved in earlier rounds)*

### 🟡 Addressed
*(none)*

### 🔴 Outstanding
*(none)*

### ✅ Verdict

**PASS — all artifacts are consistent and complete. The change is ready for apply.**

Cross-artifact consistency: 9/9 dimensions pass (proposal→specs, design→tasks, specs→tasks, ADR→design, vault path consistency, /home blocklist consistency, project_dir removal consistency, already-implemented items, no contradictions).

Review history: 8 rounds across 4 artifact batches + 1 final cross-artifact review. All issues resolved.