## design Round 5 — 2026-07-31

### 🔴 Outstanding
(none)

### 🟡 Acknowledged
- Review log missing design round history (rounds 1–4 backfilled below)

### ✅ Passed
design.md frozen. Note at line 132 resolves diagram ambiguity. All prior 🔴 resolved across rounds 1–4.

---

## design Round 4 — 2026-07-31

### 🔴 Fixed
- Risks section "Non-skill SKILL.md" still described old single-fallback behavior; updated to say skip substitution entirely for Case (b)

### 🟡 Fixed
- Tier 2 lookbehind updated from `(?<![/\w])` to `(?<![/\w-])` to exclude hyphens, preventing `static-assets/` false-positive

---

## design Round 3 — 2026-07-31

### 🔴 Fixed
- Risks section "Non-skill SKILL.md" bullet contradicted Decision 3 Case (b) — fixed

### 🟡 Fixed
- Word-boundary spec added to Decision 2 (`(?<![/\w-])` lookbehind)

---

## design Round 2 — 2026-07-31

### 🔴 Fixed
- Registry guard now has two explicit cases: (a) registry None → dirname fallback; (b) registry set but skill not found → skip substitution entirely

### 🟡 Fixed
- C4 diagram `or dirname(path)` ambiguity mitigated by adding authoritative note

---

## design Round 1 — 2026-07-31

### 🔴 Fixed
- `registry.get_by_path()` replaced with inline scan via `next((s for s in registry.all() if s.skill_md_path == path), None)` — Option A
- Registry-hit guard added: skip substitution when registry is set but path not found
- `skill.path` annotated as `(skill DIRECTORY, not skill_md_path)` in C4 diagram

---

## proposal Round 1 — 2026-07-31

### 🔴 Outstanding
(none)

### 🟡 Fixed
- Dropped `builtin-tool-execution` from Modified Capabilities (new `skill-path-resolution` capability already owns this behavior; `builtin-tool-execution` spec governs dispatch framework only)
- Added rw trust mode note for `skills_dir` in `file-access-zones` modified capability

### ✅ Passed
Proposal fully aligned with explore-brief. Both bugs covered. Scope correct. Ready to freeze.
