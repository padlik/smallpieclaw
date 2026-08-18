## 1. Brief formatter (`react_loop.py`)

- [ ] 1.1 Add `fmt_tool_brief(tool_name, args, is_mcp=False, server_name="")` function in `react_loop.py` after `fmt_tool_call()` (line 381). Include per-tool branches for all 21 built-in tools per the design Decision 2 table, a generic fallback for unknown tools, and `[MCP:{server_name}]` suffix for MCP tools. Truncate brief to ~35 chars with `…`.
- [ ] 1.2 Add shell wrapper stripping helper (Layer 1): strip `^(sh|bash|zsh)\s+-c\s+"(.+)"$`, `^cd\s+\S+\s+&&\s+`, `^export\s+\w+=\S+\s+&&\s+` in order before truncating. Call from the `shell` branch of `fmt_tool_brief()`.
- [ ] 1.3 Add `file_patch` `+N -M` line-count derivation: `len(new_str.splitlines())` → `+N`, `len(old_str.splitlines())` → `-M`. Call from the `file_patch` branch.
- [ ] 1.4 Add `file_diff` dual-basename derivation: `os.path.basename(path_a) ↔ os.path.basename(path_b)`. Call from the `file_diff` branch.
- [ ] 1.5 Add `file_write` content-length derivation: `len(content)` displayed as `(N)` with no unit suffix. Call from the `file_write` branch.
- [ ] 1.6 Add `wait_for_any_agent` count logic: if `len(agent_ids) > 2` show `[N agents]`, else show comma-joined IDs. Call from the `wait_for_any_agent` branch.
- [ ] 1.7 Add `schedule` action-based formatting: `list` → `schedule list`; `add` → `schedule add "{tag}" {cron}`; `remove`/`pause`/`resume`/`run_now` → `schedule {action} "{tag}"`. Call from the `schedule` branch.
- [ ] 1.8 Add `memory_write` action+key formatting: `memory_write {action} "{key}"`. Call from the `memory_write` branch.
- [ ] 1.9 Add `memory_graph_store` content truncation: `"content"` truncated to ~30 chars with `…`. Call from the `memory_graph_store` branch.
- [ ] 1.10 Add `shell_env_list` static brief: `list env vars`. Call from the `shell_env_list` branch.
- [ ] 1.11 Add secrets-safe branches: `secret_get` shows `key` only; `shell_env_set` shows `key` only (never value). Verify no branch leaks `value`/`content`/`old_str`/`new_str` raw content.

## 2. MCP server-name lookup (`react_loop.py`)

- [ ] 2.1 At `react_loop.py:1393` (TOOL_START emission), before building the progress string, check `ctx.mcp_manager.has_tool(tool_name)`. If True, look up `ToolRegistry.get(tool_name)` to get `server_name` and set `is_mcp=True`.
- [ ] 2.2 Pass `is_mcp` and `server_name` into `fmt_tool_brief()` and emit the brief in the "Running tool:" progress string: `f"{icon} Running tool: \`{name}\`\n{brief}"`.

## 3. Panel step model (`telegram_interface.py`)

- [ ] 3.1 Change `_steps` type from `list[tuple[float, str]]` to `list[tuple[float, str, str | None]]` (add tag field). Update `__init__` annotation at line 142.
- [ ] 3.2 Change `_MAX_STEPS` from 10 to 5 at line 116.
- [ ] 3.3 Update `build_panel()` (line 197-211) to unpack the 3-tuple: `for step_elapsed, rendered, _tag in visible:`. Rendering logic unchanged.
- [ ] 3.4 Update `dispatch_progress()` normal path (line 305-308) to append `(elapsed, html, tag)` instead of `(elapsed, html)`.

## 4. Panel classify — keep brief (`telegram_interface.py`)

- [ ] 4.1 Modify `classify()` (line 165-169) to parse the tool name AND the brief from the "Running tool:" message. Return `(html, "TOOL_RUNNING")` instead of just `html`. The HTML should render as `f"{icon} Running: <code>{name}</code> {brief_html}"` where `brief_html` is the brief HTML-escaped and truncated.
- [ ] 4.2 Update `classify()` return type to `tuple[str, str | None]` and update all callers in `dispatch_progress()` to unpack `(html, tag)`.
- [ ] 4.3 Handle the `__SHELL_CHUNK__` path (line 289-304): update the last step's HTML to preserve the brief and append the live tail, keeping the tag as `"TOOL_RUNNING"`.

## 5. Merge TOOL_END into last step (`telegram_interface.py`)

- [ ] 5.1 In `dispatch_progress()`, add handling for TOOL_END messages (the `✅`/`❌` + `**tool_name**` pattern, currently at classify lines 170-192). Instead of appending a new step, find the last step with tag `"TOOL_RUNNING"` and update its HTML in place to append ` ✅` or ` ❌`.
- [ ] 5.2 If no `TOOL_RUNNING` step is found (edge case), fall back to appending a new step with the result (current behavior).
- [ ] 5.3 Drop the shell tail from the step HTML when TOOL_END arrives (replace tail with ✅/❌, keeping the brief).

## 6. Thinking duration retroactive patch (`telegram_interface.py`)

- [ ] 6.1 In `dispatch_progress()`, when a new step arrives and the last step has tag `"THINKING"`, compute duration = `new_elapsed - thinking_elapsed`. Patch the last step's HTML to include ` Ns` (e.g. `⚙️ <i>Thinking… 5s</i>`). Update the step tuple in place.
- [ ] 6.2 Set tag `"THINKING"` on Thinking steps in `classify()` (line 162-164).

## 7. Tests

- [ ] 7.1 Add unit tests for `fmt_tool_brief()` covering all 21 built-in tools: verify each branch produces the expected brief format. Include edge cases (empty args, missing keys, very long commands).
- [ ] 7.2 Add unit tests for shell wrapper stripping: verify `sh -c`, `cd &&`, `export &&` patterns are stripped; verify commands without wrappers are unchanged; verify truncation at ~35 chars.
- [ ] 7.3 Add unit tests for MCP marking: verify `[MCP:server_name]` suffix is appended when `is_mcp=True`; verify no marker when `is_mcp=False`.
- [ ] 7.4 Add unit tests for `_ProgressPanel` merged-line behavior: verify TOOL_END updates the last `TOOL_RUNNING` step in place; verify fallback when no `TOOL_RUNNING` step exists.
- [ ] 7.5 Add unit tests for Thinking duration retroactive patch: verify duration is computed and patched when next step arrives; verify no duration when Thinking is the last step.
- [ ] 7.6 Add unit tests for `_MAX_STEPS=5`: verify only 5 steps are visible when more than 5 exist.
- [ ] 7.7 Add unit tests for `__SHELL_CHUNK__` preserve-brief behavior: verify brief is preserved while tail updates; verify tail is dropped on TOOL_END.
- [ ] 7.8 Run `make check` (lint + test) and verify all tests pass with no new warnings.

## 8. Validation

- [ ] 8.1 Run `openspec validate telegram-tool-brief-panel --type change --strict` and verify it passes.
- [ ] 8.2 Manual Telegram test: run an agent, observe the panel shows briefs, merged ✅/❌, Thinking duration, and `[MCP:server]` markers. Verify 5-step limit.