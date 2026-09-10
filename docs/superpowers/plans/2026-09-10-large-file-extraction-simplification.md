# Large-File Extraction Simplification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shrink the three largest source files by extracting four cohesive, behavior-preserving clusters into their own modules (`tool_formatting.py`, `native_turns.py`, `telegram_mcp_commands.py`, backfill engine → `backfill_graph_memory.py`).

**Architecture:** Pure move refactors — code moves verbatim, no signature changes, no behavior changes. `react_loop.py` keeps canonical loop control flow and re-exports the moved formatting symbols so existing test/telegram imports keep working. The telegram MCP cluster and graph backfill block move with direct import-site updates (no re-export shims — importers are few and in-repo). Import topology stays acyclic: `native_turns → tool_formatting`, `telegram_mcp_commands → telegram_commands`, `backfill_graph_memory → graph_memory`.

**Tech Stack:** Python 3.14, pytest, ruff, vulture, structlog/logging. No new dependencies.

**Spec:** This plan implements items 1, 2, 4, 5 of the @oracle simplification review (2026-09-10, session `ora-1`) — summarized in **Background** below. Item 3 (mcp_client.py OAuth probe extraction) is deliberately deferred. All extraction facts were verified by a read-only recon pass (session `exp-1`); exact line numbers below come from that recon.

## Background (spec summary from the oracle review)

- **Move 1 (High payoff / Low risk):** `react_loop.py` (2110 lines) contains a ~235-line pure-presentation cluster (tool icons + tool-call/brief/result formatters, lines 408–642) with zero control flow and no `ReactContext` dependency. Extract to `tool_formatting.py` with re-exports.
- **Move 5 (Low-Med / Low):** `_append_native_tool_result` (829–849) and `_linearize_native_turns` (852–906) are a distinct "OpenAI native tool-call wire-shape" concern. Fold into the same effort as Move 1 as a sibling module `native_turns.py`.
- **Move 2 (Med-High / Low):** `telegram_commands.py` (1666 lines) contains a fully contiguous, self-contained MCP subcommand cluster (lines 1051–1424). Move to `telegram_mcp_commands.py`, following the existing precedent that split `telegram_callbacks.py` out of `telegram_commands.py`.
- **Move 4 (Med / Low-Med):** `graph_memory.py` (1847 lines) ends with a clean CLI-only backfill tail (lines 1544–1847). Move into the existing dedicated CLI module `backfill_graph_memory.py`.
- **Move 3 (deferred):** `mcp_client.py` OAuth probe machinery — method-bound on `MCPManager`, heavily `patch()`-coupled in `tests/test_mcp_oauth_probe.py`. Do NOT touch in this change.
- **Oracle caveat driving the verification design:** tests import moved symbols by their current module path, and `patch("<module>.<name>")` strings silently stop intercepting when the *internal caller* changes module. Recon verified exactly which seams exist (see Global Constraints).

## Global Constraints

- **Behavior-preserving only.** Move code VERBATIM. No signature changes, no renames, no logic edits, no "improvements" while moving.
- **Never work on `main`.** Work starts by creating `feature/large-file-extraction-simplification` from clean `main` (verified: branch `main`, clean tree).
- **Baseline gate:** `make check` must end at **2097 passed, 1 skipped** (the 1 skip is `test_path_policy.py::TestIsContained::test_normcase_case_insensitive`, platform-specific, pre-existing). Any deviation = investigate, do not proceed.
- **After every code change, run:** `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80` (i.e. `make lint`), plus the task's targeted tests. `make check` before declaring the plan complete.
- **Patch-seam inventory (verified by recon — these are the ONLY ones):**
  - `patch("react_loop.<moved formatting symbol>")` — NONE exist. Moved react_loop symbols are only imported directly. Patched seams (`_dispatch_tool`, `_build_system_prompt`, `_encode_images`, `group_tool_defs_by_server`) all STAY in react_loop.py.
  - `patch("telegram_commands.*")` — NONE exist anywhere.
  - `patch("graph_memory._save_backfill_state")` — 3 sites: `tests/test_graph_memory_backfill.py:546,592,639`. MUST be updated to the new module path in Task 4 (caller and callee move together, so the new patch target intercepts correctly).
- **Re-export style** (only react_loop uses re-exports): plain `from tool_formatting import ...` lines in `react_loop.py`; names that react_loop itself no longer uses get `# noqa: F401` on the import line plus a bare-name entry in `vulture_whitelist.py` following the established pattern (`from react_loop import (...)` + bare references).
- **vulture_whitelist.py pattern** (established, follow exactly):
  ```python
  from <module> import (  # noqa: E402
      Name1,
      Name2,
  )
  Name1
  Name2
  ```
- **Do-not-touch list (from oracle review):** `config_schema.py`; `llm_client.py`/`providers/` dispatch; `memory_store.py` tier classes; `builtin_executor.py` facade; exception hierarchy, enums, Protocols; `react_loop.py` control-flow core (`_run_single_step`, `_dispatch_action`, `_request_turn`, `react_loop`, `_LoopState`/`_Turn`/`StepResult`, `ToolTrace`, `ReactContext`, `extract_json_candidates`, `parse_json`, and everything else not enumerated as moving); `mcp_client.py` OAuth block (Move 3, deferred); `cfg`/`AppConfig` coexistence.
- **Commits:** conventional style like repo history (`refactor: ...`), one commit per task, only intended files staged. Git discipline per `openspec-git-discipline` skill: proposal artifacts / branch / merge timing.
- **Commit approval:** user has approved planning; commits happen as part of the approved execution of this plan. No merges to `main` without explicit user approval.

---

### Task 1: Branch setup + extract `tool_formatting.py` from `react_loop.py` (Move 1)

**Files:**
- Create: `tool_formatting.py` (repo root)
- Modify: `react_loop.py` — delete lines 65–89 (`_TOOL_ICONS` through `_tool_icon`, but NOT `_coerce_args` at 92–104), delete lines 408–642 (formatting cluster), delete lines 1753–1764 (`_compact_args_repr`), add import blocks
- Modify: `vulture_whitelist.py` — add react_loop re-export whitelist entries at the end of the file

**Interfaces:**
- Consumes: nothing new (moved code needs only `json`, `os`, `re`, `Callable` from stdlib/typing).
- Produces: module `tool_formatting` exporting `_TOOL_ICONS`, `_DEFAULT_TOOL_ICON`, `_tool_icon(name: str) -> str`, `_compact_args_repr(tool_name: str, args: dict, max_len: int = 200) -> str`, `fmt_tool_call(tool_name: str, args: dict) -> str`, `_BRIEF_MAX`, `_truncate_brief`, `_strip_shell_wrapper`, all `_format_*` helpers, `_BRIEF_FORMATTERS`, `fmt_tool_brief`, `fmt_tool_result_progress`, `format_tool_result` — all importable from `react_loop` unchanged (re-export).

**Symbols that MOVE (verbatim, with their exact current line ranges):**

| Symbol | react_loop.py lines |
|---|---|
| `_TOOL_ICONS` dict | 65–77 |
| `_DEFAULT_TOOL_ICON` | 78 |
| `_tool_icon` | 88–89 |
| `fmt_tool_call` | 408–425 |
| `_BRIEF_MAX` | 428 |
| `_truncate_brief` | 431–435 |
| `_strip_shell_wrapper` | 438–451 |
| `_format_path` … `_format_generic` (all `_format_*`) | 454–544 |
| `_BRIEF_FORMATTERS` | 548–570 |
| `fmt_tool_brief` | 573–587 |
| `fmt_tool_result_progress` | 590–617 |
| `format_tool_result` | 620–642 |
| `_compact_args_repr` | 1753–1764 |

**Symbols that STAY in react_loop.py (do not move):** `_coerce_args` (92–104, dispatch-path helper, used at 1437/2035), `_is_mcp_auth_failure` (107), `_handle_mcp_auth_failure` (128), `_format_parent_context` (146), `_truncate_context_payload` (169), `ToolTrace` (193), `ReactContext` (204), `extract_json_candidates` (330), `parse_json` (364), everything ≥ 804 except `_compact_args_repr`.

- [ ] **Step 1: Create feature branch**

```bash
git checkout main && git pull --ff-only && git checkout -b feature/large-file-extraction-simplification
git status --porcelain   # must be empty before starting
```

- [ ] **Step 2: Create `tool_formatting.py`**

Create the new module with this structure — module docstring, imports, then the moved code verbatim (bodies copied exactly, including comments, in original relative order: `_TOOL_ICONS` block first, then `_tool_icon`, then the 408–642 cluster, then `_compact_args_repr`):

```python
"""Pure presentation helpers for tool calls and results.

Extracted from ``react_loop.py``: tool icons, tool-call/brief/result
formatters, and the compact args repr used by traces. No ReAct-loop state,
no ``ReactContext`` dependency — everything takes tool_name/args/outcome
and returns display strings.

``react_loop`` re-exports the public names for backward compatibility
(tests and ``telegram_interface`` import them from there).
"""

import json
import os
import re
from typing import Callable

# ... moved code verbatim, in this order:
# _TOOL_ICONS (was 65-77), _DEFAULT_TOOL_ICON (was 78)
# _tool_icon (was 88-89)
# _BRIEF_MAX / _truncate_brief / _strip_shell_wrapper /
# _format_* helpers / _BRIEF_FORMATTERS /
# fmt_tool_call / fmt_tool_brief / fmt_tool_result_progress / format_tool_result
# (was 408-642)
# _compact_args_repr (was 1753-1764)
```

Note: the moved code is pasted verbatim — do not reformat, reorder bodies, or "fix" anything while moving.

- [ ] **Step 3: Delete moved code from `react_loop.py`**

Remove the four line ranges listed in the table above (icons block 65–89, formatting cluster 408–642, `_compact_args_repr` 1753–1764). Leave everything else untouched, including `_coerce_args` immediately below `_tool_icon`.

- [ ] **Step 4: Add import blocks to `react_loop.py`**

Immediately after the existing import block (after line 44, `from prompt_loader import ...`):

```python
from tool_formatting import (
    _compact_args_repr,
    _tool_icon,
    fmt_tool_brief,
    fmt_tool_call,
    format_tool_result,
)

# Backward-compat re-exports: these helpers moved to tool_formatting.py;
# tests and telegram_interface still import them from react_loop.
from tool_formatting import (  # noqa: F401
    _strip_shell_wrapper,
    _truncate_brief,
    fmt_tool_result_progress,
)
```

These import lines double as the re-export shim AND supply react_loop's own internal call sites (`fmt_tool_call` @ ~804/2087, `fmt_tool_brief` @ ~2041, `format_tool_result` @ ~1459, `_tool_icon` @ ~2042, `_compact_args_repr` @ ~1780 — all line numbers shift up after the deletions). If ruff's isort rules complain about placement/order, follow its instruction — keep both import blocks.

- [ ] **Step 5: Update `vulture_whitelist.py`**

Append at the end of the file, following the established `# noqa: E402` + bare-name pattern:

```python
# react_loop.py — formatting helpers re-exported from tool_formatting.py
from react_loop import (  # noqa: E402
    _strip_shell_wrapper,
    _truncate_brief,
    fmt_tool_result_progress,
)
_strip_shell_wrapper
_truncate_brief
fmt_tool_result_progress
```

- [ ] **Step 6: Lint**

Run: `make lint`
Expected: PASS. If ruff flags now-unused imports in `react_loop.py` (candidates: `secrets` if only used by moved code — verify against remaining code before removing; `Callable` stays, `ReactContext` uses it), remove exactly what ruff identifies as unused and re-run. Do NOT remove imports still referenced by the file.

- [ ] **Step 7: Targeted tests**

Run:
```bash
pytest tests/test_tool_brief_panel.py tests/test_react_loop.py tests/test_builtin_executor.py tests/test_react_loop_error_recovery.py tests/test_context_payload.py -v --tb=short
```
Expected: all PASS (these import `fmt_tool_brief`, `_tool_icon`, `fmt_tool_call`, `_strip_shell_wrapper`, `_truncate_brief`, `fmt_tool_result_progress`, `format_tool_result`, `_compact_args_repr` from `react_loop` — all still valid via the re-export).

- [ ] **Step 8: Extraction completeness grep**

```bash
rg -n "def fmt_tool_brief|def _tool_icon|def fmt_tool_call|def format_tool_result|def _compact_args_repr" react_loop.py
```
Expected: NO matches (definitions now live only in `tool_formatting.py`).

```bash
rg -c "_tool_icon|fmt_tool_brief|format_tool_result" tool_formatting.py
```
Expected: matches (definitions + internal uses present).

- [ ] **Step 9: Commit**

```bash
git add react_loop.py tool_formatting.py vulture_whitelist.py
git diff --cached --stat   # exactly 3 files
git commit -m "refactor: extract tool formatting cluster from react_loop.py into tool_formatting.py"
```

---

### Task 2: Extract `native_turns.py` from `react_loop.py` (Move 5)

**Files:**
- Create: `native_turns.py` (repo root)
- Modify: `react_loop.py` — delete lines ~829–906 region (`_append_native_tool_result`, `_linearize_native_turns`; exact numbers shifted by Task 1 deletions — locate by symbol name), add import line
- No vulture_whitelist change (both moved names are used internally by react_loop, so the import line doubles as re-export and vulture sees usage)

**Interfaces:**
- Consumes: `tool_formatting._compact_args_repr`, `interfaces.ToolCall`, stdlib `secrets`.
- Produces: module `native_turns` exporting `_append_native_tool_result(messages: list[dict], tc: ToolCall, content: str) -> None` and `_linearize_native_turns(messages: list[dict]) -> list[dict]` — both importable from `react_loop` unchanged.

**Symbols that MOVE:** `_append_native_tool_result` (was 829–849), `_linearize_native_turns` (was 852–906). Both stay in original relative order.

- [ ] **Step 1: Create `native_turns.py`**

```python
"""OpenAI native tool-call wire-shape helpers.

Extracted from ``react_loop.py``: appending native tool-result messages
and linearizing interleaved native tool-call turns into a flat
assistant/tool message list. ``react_loop`` re-exports both names for
backward compatibility.
"""

import secrets

from interfaces import ToolCall
from tool_formatting import _compact_args_repr

# ... moved code verbatim:
# _append_native_tool_result (was react_loop.py 829-849)
# _linearize_native_turns (was react_loop.py 852-906)
```

- [ ] **Step 2: Delete moved code from `react_loop.py` and import it back**

Delete the two functions (locate by `def _append_native_tool_result` / `def _linearize_native_turns`). Add to the import block from Task 1:

```python
from native_turns import _append_native_tool_result, _linearize_native_turns
```

Both are used internally (call sites at former lines 1411 and 1369 — `_append_native_tool_result` in the native-turns dispatch path, `_linearize_native_turns` before the LLM call), so no `# noqa: F401` and no whitelist entry needed. `tests/test_react_loop.py` imports `_linearize_native_turns` from `react_loop` — the import line keeps that working.

- [ ] **Step 3: Lint + remove imports ruff flags as now-unused**

Run: `make lint`
If `secrets` (and only if) is now unused in `react_loop.py`, remove it per ruff. Expected: PASS.

- [ ] **Step 4: Targeted tests**

Run:
```bash
pytest tests/test_react_loop.py tests/test_native_intercepts.py tests/test_vision_query_confirm.py tests/execution_harness.py -v --tb=short
```
Expected: all PASS (`test_native_intercepts.py` exercises the native-turns path via `patch("react_loop._dispatch_tool")`, which stays in react_loop and now calls the imported `_append_native_tool_result`).

- [ ] **Step 5: Extraction completeness grep**

```bash
rg -n "def _append_native_tool_result|def _linearize_native_turns" react_loop.py
```
Expected: NO matches.

- [ ] **Step 6: Commit**

```bash
git add react_loop.py native_turns.py
git diff --cached --stat   # exactly 2 files
git commit -m "refactor: extract native-turn helpers from react_loop.py into native_turns.py"
```

---

### Task 3: Extract `telegram_mcp_commands.py` from `telegram_commands.py` (Move 2)

**Files:**
- Create: `telegram_mcp_commands.py` (repo root)
- Modify: `telegram_commands.py` — delete the contiguous block lines 1051–1424
- Modify: `telegram_interface.py` — import block at 46–52 (remove `cmd_mcp` from the `telegram_commands` import) + add new import; registration at line 678 unchanged
- Modify: `tests/test_telegram_command_surface.py` — replace every local `from telegram_commands import cmd_mcp` (lines 179, 214, 249, 289, 332, 362, 396, 430)
- Modify: `tests/test_refresh_tool_defs_snapshot.py:8` — `from telegram_commands import _refresh_tool_defs_snapshot`
- No re-export shim (avoids an import cycle); no vulture_whitelist change (all symbols are used within the new module or by telegram_interface).

**Interfaces:**
- Consumes: `telegram_commands._require_auth` (decorator, stays at telegram_commands.py:41–52). New module imports it — this is one-directional; `telegram_commands` must NOT import from `telegram_mcp_commands`.
- Produces: module `telegram_mcp_commands` exporting `cmd_mcp` (the `@_require_auth`-decorated dispatcher), `_refresh_tool_defs_snapshot`, and the internal `_mcp_*`/`_fmt_mcp_token_info`/`_MCP_DISPATCH` symbols. `telegram_interface` registers `CommandHandler("mcp", partial(cmd_mcp, self))` at line 678 from the new module.

**Symbols that MOVE (contiguous block, react lines 1051–1424, no non-MCP symbols interleaved — verified):**

| Symbol | telegram_commands.py lines |
|---|---|
| `_fmt_mcp_token_info` | 1051–1075 |
| `_mcp_auth_status` | 1078–1105 |
| `_mcp_auth_revoke` | 1108–1142 |
| `_refresh_tool_defs_snapshot` | 1145–1194 |
| `_mcp_on` | 1197–1220 |
| `_mcp_off` | 1223–1243 |
| `_mcp_info` | 1246–1284 |
| `_mcp_list` | 1287–1312 |
| `_mcp_auth` | 1315–1396 |
| `_MCP_DISPATCH` | 1399–1405 |
| `cmd_mcp` | 1408–1424 |

**Symbols that STAY:** `_require_auth` (41–52), `_truncate_desc` (55–60), `logger` (38), and every other command.

- [ ] **Step 1: Create `telegram_mcp_commands.py`**

```python
"""MCP subcommands for the Telegram bot (the /mcp command family).

Extracted from ``telegram_commands.py`` along the same seam as
``telegram_callbacks.py``. Self-contained cluster: token-info formatting,
the _mcp_* subcommand handlers, the dispatch table, and cmd_mcp.
"""

import asyncio
import html
import logging
from dataclasses import replace as dataclass_replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from builtin_tools.schemas import build_tool_definitions, builtin_tool_names
from context_monitor import (
    compute_danger_level,
    compute_headroom_real,
    group_tool_defs_by_server,
)
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from telegram_commands import _require_auth

if TYPE_CHECKING:
    from telegram_interface import TelegramInterface

logger = logging.getLogger(__name__)

# ... moved code verbatim (lines 1051-1424 of telegram_commands.py, in order)
```

Mirror the exact import spellings from `telegram_commands.py`'s own header (lines 3–33) for `Update`/`ParseMode`/`ContextTypes` and the `TYPE_CHECKING` block content — copy, don't guess. The `iface: "TelegramInterface"` string annotations resolve via the TYPE_CHECKING import. If some listed imports turn out unused by the moved block (e.g. a helper the cluster doesn't actually call), drop them per ruff — moved bodies themselves must not change.

- [ ] **Step 2: Delete lines 1051–1424 from `telegram_commands.py`**

Nothing else changes in that file. `_require_auth` stays and is now also consumed by the new module.

- [ ] **Step 3: Rewire `telegram_interface.py`**

In the import block at lines 46–52, remove `cmd_mcp` from the `from telegram_commands import (...)` list and add:

```python
from telegram_mcp_commands import cmd_mcp
```

Line 678 (`app.add_handler(CommandHandler("mcp", partial(cmd_mcp, self)))`) is unchanged.

- [ ] **Step 4: Update test imports**

- `tests/test_telegram_command_surface.py`: replace all 8 local imports `from telegram_commands import cmd_mcp` (lines 179, 214, 249, 289, 332, 362, 396, 430) with `from telegram_mcp_commands import cmd_mcp`.
- `tests/test_refresh_tool_defs_snapshot.py:8`: `from telegram_mcp_commands import _refresh_tool_defs_snapshot`.

- [ ] **Step 5: No-cycle sanity check**

```bash
rg -n "telegram_mcp_commands" telegram_commands.py
```
Expected: NO matches (telegram_commands must not import the new module — that would be a cycle).

- [ ] **Step 6: Lint**

Run: `make lint`
Expected: PASS. Known acceptable cosmetic delta: the module logger name changes from `telegram_commands` to `telegram_mcp_commands` for log records emitted by the moved handlers — this is not asserted by any test (recon found zero `patch("telegram_commands.*")` seams).

- [ ] **Step 7: Targeted tests**

Run:
```bash
pytest tests/test_telegram_command_surface.py tests/test_refresh_tool_defs_snapshot.py -v --tb=short
pytest tests/ -k telegram -v --tb=short
```
Expected: all PASS.

- [ ] **Step 8: Extraction completeness grep**

```bash
rg -n "def cmd_mcp|def _mcp_|def _fmt_mcp_token_info|def _refresh_tool_defs_snapshot|_MCP_DISPATCH" telegram_commands.py
```
Expected: NO matches.

- [ ] **Step 9: Commit**

```bash
git add telegram_mcp_commands.py telegram_commands.py telegram_interface.py tests/test_telegram_command_surface.py tests/test_refresh_tool_defs_snapshot.py
git diff --cached --stat   # exactly 5 files
git commit -m "refactor: extract MCP subcommands from telegram_commands.py into telegram_mcp_commands.py"
```

---

### Task 4: Move graph backfill engine from `graph_memory.py` to `backfill_graph_memory.py` (Move 4)

**Files:**
- Modify: `graph_memory.py` — delete the tail block lines 1544–1847 (section header comment 1544–1546 + everything below; the file ends there)
- Modify: `backfill_graph_memory.py` — insert the moved block after the `logger = logging.getLogger("backfill")` line (55), before the existing CLI helper code; simplify the two local import blocks inside `main()` (lines ~281–286 and ~322–325) to direct references
- Modify: `vulture_whitelist.py` — re-point the backfill import block (lines 129–139)
- Modify: `tests/test_graph_memory_backfill.py` — import block (lines 22–26) + the 3 patch targets (lines 546, 592, 639)

**Interfaces:**
- Consumes (from `graph_memory`, one-directional — `graph_memory` must NOT import from `backfill_graph_memory`): `GraphMemoryStore`, `EXTRACTION_PROMPT`, `parse_extraction`, `build_extraction_llm_call` (stays in graph_memory at line 1483); plus `providers._errors.LLMError`, `httpx`.
- Produces: `backfill_graph_memory` exporting `BackfillEntryResult`, `BackfillResult`, `_entry_checksum`, `_load_backfill_state`, `_save_backfill_state`, `backfill_longterm_to_graph` at their new canonical home. No re-export shim in `graph_memory` — all importers are updated directly (verified complete list below).

**Symbols that MOVE (graph_memory.py):**

| Symbol | lines |
|---|---|
| section header comment | 1544–1546 |
| `BackfillEntryResult` | 1549–1558 |
| `BackfillResult` | 1561–1572 |
| `_entry_checksum` | 1575–1578 |
| `_load_backfill_state` | 1581–1596 |
| `_save_backfill_state` | 1599–1617 |
| `backfill_longterm_to_graph` | 1620–1847 (end of file) |

**Complete importer inventory (recon-verified — nothing else references these symbols):**
- `backfill_graph_memory.py:281–286` (`from graph_memory import BackfillResult, BackfillEntryResult, _load_backfill_state, _entry_checksum` — dry-run path) and `:322–325` (`from graph_memory import GraphMemoryStore, backfill_longterm_to_graph` — live path)
- `vulture_whitelist.py:129–139`
- `tests/test_graph_memory_backfill.py:22–26`
- `main.py` — does NOT import any backfill symbol (verified). `tests/test_graph_memory_e2e.py`/`test_graph_memory_integration.py` — do NOT import backfill symbols.

- [ ] **Step 1: Add module-level imports to `backfill_graph_memory.py`**

Extend the existing import block (file already has `os`, `sys`, `shutil`, `argparse`, `logging`, `Callable` at lines 36–55) with exactly what the moved code needs:

```python
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from graph_memory import (
    EXTRACTION_PROMPT,
    GraphMemoryStore,
    build_extraction_llm_call,
    parse_extraction,
)
from providers._errors import LLMError
```

`backfill_longterm_to_graph` takes `llm_call_fn` as a parameter; the CLI builds it with `build_extraction_llm_call`, which stays in `graph_memory`.

- [ ] **Step 2: Insert the moved block**

Paste lines 1544–1847 of `graph_memory.py` verbatim (section comment included) after the `logger` line, before the existing CLI helpers. Then delete the two now-redundant local import blocks inside `main()` (former lines ~281–286, ~322–325) — their names resolve module-locally now.

- [ ] **Step 3: Delete the tail from `graph_memory.py`**

Remove lines 1544–1847. Then let ruff identify imports that became unused in `graph_memory.py` (candidates: `hashlib`, `httpx`, `LLMError` — but `build_extraction_llm_call`/`create_graph_memory` may still use some of these; remove exactly what ruff proves unused, re-run lint).

- [ ] **Step 4: Update `vulture_whitelist.py` (lines 129–139)**

```python
# backfill engine — moved from graph_memory.py to backfill_graph_memory.py
from backfill_graph_memory import (  # noqa: E402
    BackfillEntryResult,
    BackfillResult,
    backfill_longterm_to_graph,
)
from graph_memory import build_extraction_llm_call  # noqa: E402
BackfillEntryResult
BackfillResult
backfill_longterm_to_graph
build_extraction_llm_call
```

- [ ] **Step 5: Update `tests/test_graph_memory_backfill.py`**

- Import block (lines 22–26) → `from backfill_graph_memory import (...)` (same names).
- The 3 patch targets — `patch("graph_memory._save_backfill_state")` at lines 546, 592, 639 → `patch("backfill_graph_memory._save_backfill_state")`. This is correct because the caller (`backfill_longterm_to_graph`) and callee now live in the same module, so the patch intercepts the internal call. Missing this step = 3 tests silently stop testing what they think (the oracle's key risk — that's why it is its own step).

- [ ] **Step 6: No-cycle + completeness greps**

```bash
rg -n "backfill_graph_memory" graph_memory.py
```
Expected: NO matches (graph_memory must not import its former tail's new home).

```bash
rg -n "def backfill_longterm_to_graph|def _save_backfill_state|class BackfillResult" graph_memory.py
```
Expected: NO matches.

```bash
rg -n 'patch\("graph_memory\._save_backfill_state"\)' tests/
```
Expected: NO matches.

- [ ] **Step 7: Lint**

Run: `make lint`
Expected: PASS.

- [ ] **Step 8: Targeted tests**

Run:
```bash
pytest tests/test_graph_memory_backfill.py tests/test_graph_memory_e2e.py tests/test_graph_memory_integration.py -v --tb=short
```
Expected: all PASS (`test_graph_memory_e2e.py` runs for real — ladybug is installed in the project venv; 6/6 expected).

- [ ] **Step 9: Commit**

```bash
git add graph_memory.py backfill_graph_memory.py vulture_whitelist.py tests/test_graph_memory_backfill.py
git diff --cached --stat   # exactly 4 files
git commit -m "refactor: move graph backfill engine from graph_memory.py to backfill_graph_memory.py"
```

---

### Task 5: Full verification + documentation sync

**Files:**
- Modify: `AGENTS.md` — "Key Modules" table: add rows for `tool_formatting.py`, `native_turns.py`, `telegram_mcp_commands.py`; amend the `backfill_graph_memory.py` row to note it owns the backfill engine, not just the CLI
- No other doc changes required (no user-visible behavior changed; module table is the only architecture contract that shifted)

- [ ] **Step 1: Full gate**

Run: `make check`
Expected: **2097 passed, 1 skipped** (pre-existing platform skip in `test_path_policy.py`). Lint clean. Any deviation: investigate root cause before proceeding — do not adjust tests or lint rules.

- [ ] **Step 2: Update the AGENTS.md Key Modules table**

Add (following existing row style):

```markdown
| `tool_formatting.py` | Pure presentation helpers for tool calls/results (icons, brief/call/result formatters, compact args repr); extracted from `react_loop.py`, re-exported there for backward compat |
| `native_turns.py` | OpenAI native tool-call wire-shape helpers (append tool-result messages, linearize native turns); extracted from `react_loop.py`, re-exported there |
| `telegram_mcp_commands.py` | `/mcp` Telegram subcommand family (status/on/off/info/list/auth, tool-defs snapshot refresh); split from `telegram_commands.py` |
```

Amend the `backfill_graph_memory.py` row to: `| backfill_graph_memory.py | One-time CLI **and backfill engine** — imports data/longterm_memory.json into the graph store (BackfillResult, backfill_longterm_to_graph live here) |`

- [ ] **Step 3: Final size sanity report**

```bash
wc -l react_loop.py tool_formatting.py native_turns.py telegram_commands.py telegram_mcp_commands.py graph_memory.py backfill_graph_memory.py
```
Expected approximate: react_loop ~1775, tool_formatting ~300, native_turns ~100, telegram_commands ~1290, telegram_mcp_commands ~415, graph_memory ~1540, backfill_graph_memory ~730. Exact numbers just need to confirm the big three shrank and the new modules are in the right ballpark.

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md
git commit -m "docs: update Key Modules table for large-file extraction refactor"
```

- [ ] **Step 5: Report**

Report to the user: final `make check` result, size deltas, the commit list, and that the branch `feature/large-file-extraction-simplification` is ready for review/merge upon approval. Do NOT merge without explicit approval.

---

## Self-Review (completed)

- **Spec coverage:** Moves 1, 2, 4, 5 → Tasks 1, 2, 3, 4. Move 3 explicitly deferred (Global Constraints). Oracle's re-export caveat → import shims in Tasks 1–2, direct import updates in Tasks 3–4, patch-seam step in Task 4. ✓
- **Placeholder scan:** every step has exact file/line targets or verbatim code; the "moved code verbatim" steps are moves of enumerated line ranges (the content already exists — copying bodies into the plan would duplicate 1000 lines and drift). ✓
- **Type consistency:** all signatures listed in Interfaces blocks match recon-verified source signatures. Re-export names match test import names exactly (`_strip_shell_wrapper`, `_truncate_brief`, `fmt_tool_result_progress` re-exported because tests import them from react_loop; `_BRIEF_FORMATTERS`/`_format_*`/`_BRIEF_MAX`/`_TOOL_ICONS`/`_DEFAULT_TOOL_ICON` NOT re-exported — nothing external imports them). ✓