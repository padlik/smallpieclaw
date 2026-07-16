# Review Log: split-telegram-callbacks

## proposal Round 1 — 2026-07-16

### 🔴 Fixed
_(none — first round)_

### 🟡 Addressed
_(none — first round)_

### 🔴 Outstanding
_(none — proposal passes)_

### 🟡 Findings (non-blocking, for design.md)
- `_ack_query` is listed as a "shared helper that stays" but is used ONLY by `cb_*` handlers. Leaving it in `telegram_commands.py` forces a cross-module private import and risks the vulture gate. design.md must decide: move `_ack_query` into `telegram_callbacks.py` (recommended — callback-only) or keep it shared deliberately. The other 5 helpers (`_require_auth`, `_truncate_desc`, `_redact_env_var`, `_tool_entry`, `_fmt_stat`) are genuinely `cmd_*`-only and correctly stay.
- Tests impact is generic; concrete consumers are `tests/test_deferred_message.py` (cb_deferred) and `tests/test_telegram_mode_selector.py` (cb_mode_switch) — name them in tasks.md.

### ✅ Verified against source
- `telegram_commands.py` = 1364 lines (matches).
- 7 `cb_*` defs at lines 983/1043/1085/1126/1160/1190/1299 (all match).
- `telegram_interface.py` imports+registers `cb_*` (lines 46–47, 247–253); dependency direction is one-way, no circular-import risk.
- No spec-level capability change — pure mechanical refactor, no delta specs needed.

### ⚖️ Verdict: PASS — ready for design.md.

## design Round 1 — 2026-07-17

### 🔴 Fixed
_(none — first design-batch review)_

### 🟡 Addressed
- 7 `cb_*` extraction targets confirmed complete and correct against source.
- 5 command-only helpers correctly identified as staying in `telegram_commands.py`.
- `TYPE_CHECKING` hint for `TelegramInterface` correctly avoids the import cycle.

### 🔴 Outstanding
- `cb_mode_switch` depends on `_apply_mode` and `_MODE_DESCRIPTIONS` (lines 1174, 1177, 1180), which are ALSO used by `cmd_*` — genuinely shared symbols. `design.md` omitted them from the move/shared inventory and falsely claimed "no runtime import from telegram_commands." Also: decision #3 ("callbacks import shared helpers, one-way") directly contradicted the line-35 claim. → **Fixed in Round 2 design.md**: both symbols explicitly inventoried as shared (stay in `telegram_commands`, imported by `telegram_callbacks` at runtime); dependency direction confirmed one-way.
- `_ack_query` placement decision (mandated by proposal Round 1) was not recorded in `design.md`. → **Fixed in Round 2 design.md**: `_ack_query` explicitly listed in "what moves" with rationale (callback-only, all 10 uses inside `cb_*` bodies).

## design Round 1 — 2026-07-17

### 🔴 Fixed
_(none — first design-batch review)_

### 🟡 Addressed
- 7 `cb_*` extraction targets confirmed complete and correct against source.
- 5 command-only helpers correctly identified as staying in `telegram_commands.py`.
- `TYPE_CHECKING` hint for `TelegramInterface` correctly avoids the import cycle.

### 🔴 Outstanding
- `cb_mode_switch` depends on module-level `_apply_mode` and `_MODE_DESCRIPTIONS` (`telegram_commands.py`:1174,1177,1180), which are ALSO used by `cmd_*` handlers — genuinely shared symbols. design.md omitted them from the move/shared inventory, and line 35 claimed "telegram_callbacks.py does not import from telegram_commands at runtime" which is false for this case. Decision #3 also directly contradicted line 35. design.md must (a) inventory `_apply_mode`/`_MODE_DESCRIPTIONS` and (b) state resolution: keep in `telegram_commands.py`, import into callbacks (maintains one-way direction).
- `_ack_query` placement decision (mandated by proposal Round 1) not recorded in design.md — repo grep found token only in `telegram_commands.py` and `review-log.md`. Source confirms callback-only (all 10 uses inside `cb_*` bodies); must add explicit line to design.md's "what moves" list.

## design Round 2 — 2026-07-17

### 🔴 Fixed
- Shared-symbol inventory: `_apply_mode` (def `telegram_commands.py:332`, used by `cmd_*` at :384 AND `cb_mode_switch` at :1177) and `_MODE_DESCRIPTIONS` (def :324, used by `cmd_*` at :375/387/396/403 AND `cb_mode_switch` at :1174/1180) are now explicitly inventoried as shared — stay in `telegram_commands.py`, imported at runtime by `telegram_callbacks.py`. The false "no runtime import" claim is removed.
- `_ack_query` placement: now explicitly moves to `telegram_callbacks.py`. Verified callback-only — def at `telegram_commands.py:42`, all 10 call sites (:1006,1030,1074,1115,1137,1171,1219,1260,1317,1345) inside `cb_*` bodies.

### 🟡 Addressed
- Dependency direction stated unambiguously one-way: `telegram_callbacks → telegram_commands`, no reverse import; consistent with runtime-import list and `TYPE_CHECKING` guard.
- Import topology matches source: `cb_*` symbols confirmed on `telegram_interface.py:46–47`; split target correct.
- Consumer test coverage complete: the only two `cb_*` importers (`test_deferred_message.py` → `cb_deferred`; `test_telegram_mode_selector.py` → `cb_mode_switch`) are both named.

### 🔴 Outstanding
_(none — design passes; ready for adr/specs/tasks. No delta specs required per frozen proposal.)_

## tasks Round 1 — 2026-07-17

### 🔴 Fixed
_(none — first tasks-batch review)_

### 🟡 Addressed
- T5 wording fixed: `test_deferred_message.py` now targets the six `from telegram_commands import cb_deferred` statements (lines 224/245/266/295/389/424) rather than attribute form. `test_telegram_mode_selector.py` now describes splitting the combined import block — keep `_MODE_DESCRIPTIONS` on `telegram_commands`, add `from telegram_callbacks import cb_mode_switch`.

### 🔴 Outstanding
_(none — batch passes.)_

### ✅ Verified against source
- `_ack_query`@42: all 10 call sites inside `cb_*` bodies; no external importers.
- All 7 `cb_*` line refs exact (983/1043/1085/1126/1160/1190/1299).
- `telegram_interface.py` import block 41–48; only two test files import `cb_*`.
- ADR consistent with frozen design (one-way dependency, correct file sizes, no behaviour change).

### ⚖️ Verdict: PASS — all artifacts frozen; ready to apply.

## tasks Round 1 — 2026-07-17

### 🔴 Fixed
_(none — first tasks-batch review)_

### 🟡 Addressed
- T5 `test_telegram_mode_selector.py` reworded: now specifies keeping `_MODE_DESCRIPTIONS` on `telegram_commands` and adding `from telegram_callbacks import cb_mode_switch` (not attribute-style relabel).
- T5 `test_deferred_message.py` reworded: targets the six `from telegram_commands import cb_deferred` statements (224,245,266,295,389,424).

### 🔴 Outstanding
_(none — batch passes. All design changes covered by tasks; line refs verified exact; ADR consistent with frozen design.)_

### ✅ Verified against source
- `_ack_query` at :42; all 10 call sites inside `cb_*` bodies — clean to move.
- `telegram_interface.py` import block 41–48; T4 span accurate.
- Only `test_deferred_message.py` and `test_telegram_mode_selector.py` import `cb_*`; other two test files unaffected.
- ADR consequences internally consistent (1364 − 380 ≈ 984 lines).

### ⚖️ Verdict: PASS — ready for apply.
