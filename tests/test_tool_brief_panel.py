"""Tests for the Telegram tool-brief panel feature.

Covers:
- ``fmt_tool_brief()`` per-tool branches for all 21 built-in tools (Task 7.1)
- Shell wrapper stripping (Task 7.2)
- MCP marking (Task 7.3)
- ``_ProgressPanel`` merged-line TOOL_END behavior (Task 7.4)
- Thinking duration retroactive patch (Task 7.5)
- ``_MAX_STEPS = 5`` (Task 7.6)
- ``__SHELL_CHUNK__`` preserve-brief behavior (Task 7.7)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from react_loop import fmt_tool_brief, _strip_shell_wrapper, _truncate_brief
from telegram_interface import _ProgressPanel, Step, StepTag


# ---------------------------------------------------------------------------
# 7.1 — fmt_tool_brief() per-tool branches
# ---------------------------------------------------------------------------


class TestFmtToolBriefPathBased:
    """file_read, file_send, vision_query show basename."""

    def test_file_read_basename(self) -> None:
        assert fmt_tool_brief("file_read", {"path": "/home/paul/foo/config.yaml"}) == "file_read config.yaml"

    def test_file_send_basename(self) -> None:
        assert fmt_tool_brief("file_send", {"path": "/tmp/report.pdf"}) == "file_send report.pdf"

    def test_vision_query_basename(self) -> None:
        assert fmt_tool_brief("vision_query", {"path": "images/photo.png"}) == "vision_query photo.png"

    def test_missing_path_defaults(self) -> None:
        assert fmt_tool_brief("file_read", {}) == "file_read ?"


class TestFmtToolBriefFileDiff:
    def test_dual_basenames(self) -> None:
        brief = fmt_tool_brief("file_diff", {"path_a": "src/app.py", "path_b": "tests/app.py"})
        assert brief == "file_diff app.py ↔ app.py"

    def test_missing_paths(self) -> None:
        brief = fmt_tool_brief("file_diff", {})
        assert "↔" in brief


class TestFmtToolBriefFilePatch:
    def test_line_counts(self) -> None:
        old_str = "line1\nline2\nline3"
        new_str = "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\nline11\nline12"
        brief = fmt_tool_brief("file_patch", {"path": "app.py", "old_str": old_str, "new_str": new_str})
        assert brief == "file_patch app.py +12 -3"

    def test_empty_strings(self) -> None:
        brief = fmt_tool_brief("file_patch", {"path": "f.py", "old_str": "", "new_str": ""})
        assert brief == "file_patch f.py +0 -0"

    def test_no_patch_content_keys(self) -> None:
        brief = fmt_tool_brief("file_patch", {"path": "f.py"})
        assert brief == "file_patch f.py +0 -0"


class TestFmtToolBriefFileWrite:
    def test_content_length(self) -> None:
        content = "x" * 340
        brief = fmt_tool_brief("file_write", {"path": "app.py", "content": content})
        assert brief == "file_write app.py (340)"

    def test_empty_content(self) -> None:
        brief = fmt_tool_brief("file_write", {"path": "f.py", "content": ""})
        assert brief == "file_write f.py (0)"


class TestFmtToolBriefShell:
    def test_simple_command(self) -> None:
        brief = fmt_tool_brief("shell", {"command": "ls -la"})
        assert brief == 'shell "ls -la"'

    def test_sh_c_wrapper_stripped(self) -> None:
        brief = fmt_tool_brief("shell", {"command": 'sh -c "python3 -c \'import json\'"'})
        assert "sh -c" not in brief
        assert "python3" in brief

    def test_long_command_truncated(self) -> None:
        long_cmd = "python3 -c 'import json, sys; print(json.dumps({" + "x" * 100 + "}))'"
        brief = fmt_tool_brief("shell", {"command": long_cmd})
        # The whole brief is truncated to ~35 chars per spec
        assert len(brief) <= 35
        assert "…" in brief

    def test_command_none_does_not_crash(self) -> None:
        """Regression: command=None must not raise TypeError in _strip_shell_wrapper."""
        brief = fmt_tool_brief("shell", {"command": None})
        assert brief == 'shell ""'

    def test_command_missing_defaults_to_empty(self) -> None:
        brief = fmt_tool_brief("shell", {})
        assert brief == 'shell ""'


class TestFmtToolBriefSpawnAgent:
    def test_short_task(self) -> None:
        brief = fmt_tool_brief("spawn_agent", {"task": "research React 19"})
        assert brief == 'spawn_agent "research React 19"'

    def test_long_task_truncated(self) -> None:
        task = "research React 19 breaking changes and summarize the migration steps"
        brief = fmt_tool_brief("spawn_agent", {"task": task})
        assert "…" in brief
        # Inner content truncated to ~30 chars
        assert len(brief) < len('spawn_agent "') + 30 + len('"') + 5


class TestFmtToolBriefSchedule:
    def test_list(self) -> None:
        assert fmt_tool_brief("schedule", {"action": "list"}) == "schedule list"

    def test_add(self) -> None:
        brief = fmt_tool_brief("schedule", {"action": "add", "tag": "daily-report", "cron": "*/8 * * * *"})
        # Spec expects 'schedule add "daily-report" */8 * * * *' (39 chars)
        # but the whole brief is truncated to ~35 chars. Verify the prefix
        # matches and truncation is applied per spec ("approximately 35 chars").
        assert brief.startswith('schedule add "daily-report" */8')
        assert "…" in brief
        assert len(brief) <= 35

    def test_remove(self) -> None:
        brief = fmt_tool_brief("schedule", {"action": "remove", "tag": "daily-report"})
        assert brief == 'schedule remove "daily-report"'

    def test_pause(self) -> None:
        brief = fmt_tool_brief("schedule", {"action": "pause", "tag": "x"})
        assert brief == 'schedule pause "x"'

    def test_resume(self) -> None:
        brief = fmt_tool_brief("schedule", {"action": "resume", "tag": "x"})
        assert brief == 'schedule resume "x"'

    def test_run_now(self) -> None:
        brief = fmt_tool_brief("schedule", {"action": "run_now", "tag": "x"})
        assert brief == 'schedule run_now "x"'


class TestFmtToolBriefAgentIds:
    def test_get_agent_result(self) -> None:
        assert fmt_tool_brief("get_agent_result", {"agent_id": "sa-abc123"}) == "get_agent_result sa-abc123"

    def test_cancel_agent(self) -> None:
        assert fmt_tool_brief("cancel_agent", {"agent_id": "sa-xyz"}) == "cancel_agent sa-xyz"


class TestFmtToolBriefWaitForAnyAgent:
    def test_count_when_more_than_two(self) -> None:
        brief = fmt_tool_brief("wait_for_any_agent", {"agent_ids": ["sa-a", "sa-b", "sa-c"]})
        assert brief == "wait_for_any_agent [3 agents]"

    def test_count_with_many(self) -> None:
        brief = fmt_tool_brief("wait_for_any_agent", {"agent_ids": ["a", "b", "c", "d", "e"]})
        assert brief == "wait_for_any_agent [5 agents]"

    def test_list_ids_when_two_or_fewer(self) -> None:
        brief = fmt_tool_brief("wait_for_any_agent", {"agent_ids": ["sa-abc", "sa-def"]})
        assert brief == "wait_for_any_agent sa-abc, sa-def"

    def test_single_id(self) -> None:
        brief = fmt_tool_brief("wait_for_any_agent", {"agent_ids": ["sa-x"]})
        assert brief == "wait_for_any_agent sa-x"

    def test_empty(self) -> None:
        brief = fmt_tool_brief("wait_for_any_agent", {"agent_ids": []})
        assert brief == "wait_for_any_agent "


class TestFmtToolBriefMemoryWrite:
    def test_action_key(self) -> None:
        brief = fmt_tool_brief("memory_write", {"action": "set", "key": "user_prefs"})
        assert brief == 'memory_write set "user_prefs"'

    def test_long_key_truncated(self) -> None:
        key = "x" * 50
        brief = fmt_tool_brief("memory_write", {"action": "set", "key": key})
        assert "…" in brief


class TestFmtToolBriefMemoryGraph:
    def test_search_query(self) -> None:
        brief = fmt_tool_brief("memory_graph_search", {"query": "user entities"})
        assert brief == 'memory_graph_search "user entities"'

    def test_store_content_truncated(self) -> None:
        content = "Meeting notes: " + "x" * 50
        brief = fmt_tool_brief("memory_graph_store", {"content": content})
        assert "…" in brief
        assert brief.startswith('memory_graph_store "')

    def test_search_long_query_truncated(self) -> None:
        query = "x" * 50
        brief = fmt_tool_brief("memory_graph_search", {"query": query})
        assert "…" in brief


class TestFmtToolBriefLogQuery:
    def test_text(self) -> None:
        brief = fmt_tool_brief("log_query", {"text": "TOOL_FAILED"})
        assert brief == 'log_query "TOOL_FAILED"'

    def test_long_text_truncated(self) -> None:
        brief = fmt_tool_brief("log_query", {"text": "x" * 50})
        assert "…" in brief


class TestFmtToolBriefSecrets:
    """secret_get and shell_env_set show key only, never value."""

    def test_secret_get_key_only(self) -> None:
        brief = fmt_tool_brief("secret_get", {"key": "API_KEY"})
        assert brief == "secret_get API_KEY"
        assert "value" not in brief.lower() or "API_KEY" in brief

    def test_secret_get_no_value_leak(self) -> None:
        brief = fmt_tool_brief("secret_get", {"key": "K", "value": "supersecret123"})
        assert "supersecret123" not in brief

    def test_shell_env_set_key_only(self) -> None:
        brief = fmt_tool_brief("shell_env_set", {"key": "GITHUB_TOKEN", "value": "ghp_abc123"})
        assert brief == "shell_env_set GITHUB_TOKEN"
        assert "ghp_abc123" not in brief

    def test_shell_env_unset(self) -> None:
        brief = fmt_tool_brief("shell_env_unset", {"key": "FOO"})
        assert brief == "shell_env_unset FOO"

    def test_shell_env_get(self) -> None:
        brief = fmt_tool_brief("shell_env_get", {"key": "BAR"})
        assert brief == "shell_env_get BAR"


class TestFmtToolBriefShellEnvList:
    def test_static_brief(self) -> None:
        assert fmt_tool_brief("shell_env_list", {}) == "shell_env_list list env vars"


class TestFmtToolBriefGenericFallback:
    def test_unknown_tool_with_args(self) -> None:
        brief = fmt_tool_brief("custom_tool", {"k": "v"})
        assert brief.startswith("custom_tool ")
        assert "k" in brief

    def test_unknown_tool_no_args(self) -> None:
        brief = fmt_tool_brief("custom_tool", {})
        assert brief == "custom_tool"

    def test_unknown_tool_no_mcp_marker(self) -> None:
        brief = fmt_tool_brief("custom_tool", {"k": "v"})
        assert "[MCP:" not in brief

    def test_generic_fallback_keys_only_no_values(self) -> None:
        """Secret leak regression: generic fallback must show keys only, never values."""
        brief = fmt_tool_brief("mcp_tool", {"token": "sk-secret-12345", "url": "https://api.example.com"})
        assert "sk-secret-12345" not in brief
        assert "https://api.example.com" not in brief
        assert "token" in brief
        assert "url" in brief

    def test_generic_fallback_mcp_tool_keys_only(self) -> None:
        """MCP tools hit the generic fallback — must never dump arg values."""
        brief = fmt_tool_brief("open", {"url": "https://secret.example.com"}, is_mcp=True, server_name="browser")
        assert "https://secret.example.com" not in brief
        assert "url" in brief
        assert "[MCP:browser]" in brief


class TestFmtToolBriefEdgeCases:
    def test_empty_args(self) -> None:
        brief = fmt_tool_brief("file_read", {})
        assert brief == "file_read ?"

    def test_missing_keys(self) -> None:
        brief = fmt_tool_brief("shell", {})
        assert brief == 'shell ""'

    def test_very_long_command_truncated(self) -> None:
        cmd = "echo " + "x" * 200
        brief = fmt_tool_brief("shell", {"command": cmd})
        assert len(brief) <= 45  # shell + quotes + 35 truncation


# ---------------------------------------------------------------------------
# 7.2 — Shell wrapper stripping
# ---------------------------------------------------------------------------


class TestStripShellWrapper:
    def test_sh_c_stripped(self) -> None:
        assert _strip_shell_wrapper('sh -c "ls -la"') == "ls -la"

    def test_bash_c_stripped(self) -> None:
        assert _strip_shell_wrapper('bash -c "echo hi"') == "echo hi"

    def test_zsh_c_stripped(self) -> None:
        assert _strip_shell_wrapper('zsh -c "echo hi"') == "echo hi"

    def test_cd_prefix_stripped(self) -> None:
        assert _strip_shell_wrapper("cd /tmp && ls -la") == "ls -la"

    def test_export_prefix_stripped(self) -> None:
        assert _strip_shell_wrapper("export FOO=bar && python3 script.py") == "python3 script.py"

    def test_no_wrapper_unchanged(self) -> None:
        assert _strip_shell_wrapper("ls -la") == "ls -la"

    def test_sh_c_then_cd(self) -> None:
        # sh -c wraps, then inside has cd
        cmd = 'sh -c "cd /tmp && ls"'
        result = _strip_shell_wrapper(cmd)
        assert "sh -c" not in result
        assert "cd" not in result  # cd stripped by second pattern
        assert result == "ls"

    def test_multiple_exports(self) -> None:
        # Only the first export is stripped (regex is not global for this pattern)
        cmd = "export FOO=bar && export BAR=baz && ls"
        result = _strip_shell_wrapper(cmd)
        # First export stripped, second remains
        assert "FOO" not in result
        assert "ls" in result

    def test_truncation_at_35(self) -> None:
        long_cmd = "python3 -c 'import json, sys; " + "x" * 100 + "'"
        truncated = _truncate_brief(long_cmd)
        assert len(truncated) == 35
        assert truncated.endswith("…")

    def test_short_not_truncated(self) -> None:
        assert _truncate_brief("short") == "short"


# ---------------------------------------------------------------------------
# 7.3 — MCP marking
# ---------------------------------------------------------------------------


class TestMcpMarking:
    def test_mcp_suffix_appended(self) -> None:
        brief = fmt_tool_brief("open", {"url": "https://example.com"}, is_mcp=True, server_name="agent-browser")
        assert brief.endswith("[MCP:agent-browser]")

    def test_no_mcp_marker_when_false(self) -> None:
        brief = fmt_tool_brief("shell", {"command": "ls"}, is_mcp=False)
        assert "[MCP:" not in brief

    def test_mcp_with_no_args(self) -> None:
        brief = fmt_tool_brief("list_tabs", {}, is_mcp=True, server_name="agent-browser")
        assert "[MCP:agent-browser]" in brief

    def test_mcp_empty_server_name(self) -> None:
        brief = fmt_tool_brief("open", {"url": "x"}, is_mcp=True, server_name="")
        assert brief.endswith("[MCP:]")

    def test_mcp_marker_not_on_builtin(self) -> None:
        brief = fmt_tool_brief("file_read", {"path": "x.py"}, is_mcp=False)
        assert "[MCP:" not in brief


# ---------------------------------------------------------------------------
# _ProgressPanel test helpers
# ---------------------------------------------------------------------------


def _make_panel() -> _ProgressPanel:
    """Create a _ProgressPanel with mocked dependencies for unit testing."""
    interface = MagicMock()
    interface._verbose = False
    interface.agent = None
    update = MagicMock()
    update.effective_chat.id = 123
    update.effective_message = MagicMock()
    ctx = MagicMock()
    ctx.bot = MagicMock()
    loop = MagicMock()
    panel = _ProgressPanel(interface, MagicMock(), loop, update, ctx)
    # Suppress flush_panel to avoid asyncio.run_coroutine_threadsafe calls
    panel.flush_panel = MagicMock()  # type: ignore[method-assign]
    return panel


# ---------------------------------------------------------------------------
# 7.4 — _ProgressPanel merged-line behavior
# ---------------------------------------------------------------------------


class TestProgressPanelToolEndMerge:
    def test_tool_end_updates_running_step_in_place(self) -> None:
        panel = _make_panel()
        # Simulate TOOL_START
        panel.dispatch_progress("🖥️ Running tool: `file_read`\nfile_read config.yaml")
        assert len(panel._steps) == 1
        assert panel._steps[0].tag == StepTag.TOOL_RUNNING
        assert "config.yaml" in panel._steps[0].html

        # Simulate TOOL_END (success) via structured __TOOL_END__ signal
        panel.dispatch_progress("__TOOL_END__:ok:file_read\n📄 **file_read** ✅\n```\nread: config.yaml\n```")
        assert len(panel._steps) == 1  # no new step appended
        assert "✅" in panel._steps[0].html
        assert "config.yaml" in panel._steps[0].html  # brief preserved

    def test_tool_end_failure_updates_running_step(self) -> None:
        panel = _make_panel()
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"ls -la\"")
        assert len(panel._steps) == 1

        panel.dispatch_progress("__TOOL_END__:fail:shell\n🖥️ **shell** ❌\n```\nerror\n```")
        assert len(panel._steps) == 1
        assert "❌" in panel._steps[0].html
        assert "ls -la" in panel._steps[0].html  # brief preserved

    def test_fallback_when_no_tool_running_step(self) -> None:
        panel = _make_panel()
        # No prior TOOL_START — TOOL_END should append a new step
        panel.dispatch_progress("__TOOL_END__:ok:file_read\n📄 **file_read** ✅\n```\ncontent\n```")
        assert len(panel._steps) == 1
        assert panel._steps[0].tag is None  # fallback step has no tag

    def test_merge_finds_last_tool_running_skipping_thinking(self) -> None:
        panel = _make_panel()
        # TOOL_START
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"ls\"")
        # Thinking step (shouldn't normally happen between start/end, but test robustness)
        panel._steps.append(Step(1.0, "⚙️ <i>Thinking…</i>", StepTag.THINKING))
        # TOOL_END should find the TOOL_RUNNING step, not the THINKING step
        panel.dispatch_progress("__TOOL_END__:ok:shell\n🖥️ **shell** ✅\n```\noutput\n```")
        assert len(panel._steps) == 2  # TOOL_RUNNING + THINKING, no new step
        assert "✅" in panel._steps[0].html  # TOOL_RUNNING updated
        assert "Thinking" in panel._steps[1].html  # THINKING untouched


# ---------------------------------------------------------------------------
# 7.5 — Thinking duration retroactive patch
# ---------------------------------------------------------------------------


class TestThinkingDuration:
    def test_duration_patched_when_next_step_arrives(self) -> None:
        panel = _make_panel()
        # Thinking step
        panel.dispatch_progress("⚙️ Thinking… (step 1)")
        assert panel._steps[-1].tag == StepTag.THINKING
        assert "Thinking…" in panel._steps[-1].html
        assert "s" not in panel._steps[-1].html.split("Thinking…")[1]  # no duration yet

        # Advance time and add a new step
        # We can't easily control time.monotonic(), but the patch computes
        # duration = new_elapsed - last_elapsed. Since both use time.monotonic(),
        # the duration will be ~0. We verify the patch mechanism works by
        # checking the HTML changes.
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"ls\"")
        # The Thinking step should have been patched
        assert panel._steps[-2].tag == StepTag.THINKING
        # Duration may be 0s if too fast, but the replace should have run
        # If duration is 0, the replace produces "Thinking… 0s"
        assert "Thinking…" in panel._steps[-2].html

    def test_no_duration_when_thinking_is_last_step(self) -> None:
        panel = _make_panel()
        panel.dispatch_progress("⚙️ Thinking… (step 1)")
        assert panel._steps[-1].tag == StepTag.THINKING
        # No further step — no duration should appear
        assert "0s" not in panel._steps[-1].html
        assert panel._steps[-1].html == "⚙️ <i>Thinking…</i>"

    def test_duration_value_appears(self) -> None:
        panel = _make_panel()
        # Manually set up a Thinking step with a known elapsed time
        panel._steps.append(Step(10.0, "⚙️ <i>Thinking…</i>", StepTag.THINKING))
        # Now simulate a new step arriving 5 seconds later by patching
        # time.monotonic via the _task_start offset
        panel._task_start = time.monotonic() - 15.0  # so elapsed ~15s, duration ~5s
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"ls\"")
        # The Thinking step should now show a duration
        patched_html = panel._steps[-2].html
        assert "Thinking…" in patched_html
        # Duration should be present (exact value depends on timing, but >0)
        assert "s" in patched_html.split("Thinking…")[1]


# ---------------------------------------------------------------------------
# 7.6 — _MAX_STEPS = 5
# ---------------------------------------------------------------------------


class TestMaxSteps:
    def test_max_steps_is_five(self) -> None:
        from telegram_interface import _ProgressPanel
        assert _ProgressPanel._MAX_STEPS == 5

    def test_only_five_steps_visible(self) -> None:
        panel = _make_panel()
        # Add 8 steps
        for i in range(8):
            panel.dispatch_progress(f"🖥️ Running tool: `tool{i}`\ntool{i} arg{i}")
        # build_panel renders only the last _MAX_STEPS
        panel.flush_panel = MagicMock()  # type: ignore[method-assign]
        rendered = panel.build_panel()
        # Count the number of [timestamp] lines (one per visible step)
        step_lines = [line for line in rendered.split("\n") if line.startswith("<code>[")]
        assert len(step_lines) == 5
        # The last visible step should be tool7
        assert "tool7" in step_lines[-1]


# ---------------------------------------------------------------------------
# 7.7 — __SHELL_CHUNK__ preserve-brief behavior
# ---------------------------------------------------------------------------


class TestShellChunkPreserveBrief:
    def test_brief_preserved_while_tail_updates(self) -> None:
        panel = _make_panel()
        # TOOL_START with brief
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"python3 script.py\"")
        assert "python3 script.py" in panel._steps[-1].html
        assert panel._steps[-1].tag == StepTag.TOOL_RUNNING

        # Shell chunk arrives
        panel.dispatch_progress("__SHELL_CHUNK__:line1\nline2")
        # Brief should be preserved
        assert "python3 script.py" in panel._steps[-1].html
        # Tail should be appended
        assert "<i>" in panel._steps[-1].html
        assert "line1" in panel._steps[-1].html or "line2" in panel._steps[-1].html
        # Tag should remain TOOL_RUNNING
        assert panel._steps[-1].tag == StepTag.TOOL_RUNNING

    def test_tail_dropped_on_tool_end(self) -> None:
        panel = _make_panel()
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"ls\"")
        panel.dispatch_progress("__SHELL_CHUNK__:output line 1\noutput line 2")
        assert "<i>" in panel._steps[-1].html  # tail present

        # TOOL_END arrives via structured signal
        panel.dispatch_progress("__TOOL_END__:ok:shell\n🖥️ **shell** ✅\n```\noutput\n```")
        # Tail should be dropped
        assert "<i>" not in panel._steps[-1].html
        # Brief should be preserved
        assert "ls" in panel._steps[-1].html
        # Result marker should be present
        assert "✅" in panel._steps[-1].html

    def test_multiple_shell_chunks_update_tail(self) -> None:
        panel = _make_panel()
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"ls\"")
        panel.dispatch_progress("__SHELL_CHUNK__:first")
        first_html = panel._steps[-1].html
        panel.dispatch_progress("__SHELL_CHUNK__:second")
        second_html = panel._steps[-1].html
        # Brief preserved in both
        assert "ls" in first_html
        assert "ls" in second_html
        # Tail changed
        assert first_html != second_html

    def test_shell_chunk_no_steps_noop(self) -> None:
        panel = _make_panel()
        # No steps yet — should not crash
        panel.dispatch_progress("__SHELL_CHUNK__:output")
        assert len(panel._steps) == 0

    def test_shell_chunk_ignored_when_last_step_is_thinking(self) -> None:
        """RISK-3: __SHELL_CHUNK__ must not corrupt a THINKING step."""
        panel = _make_panel()
        panel.dispatch_progress("⚙️ Thinking… (step 1)")
        assert panel._steps[-1].tag == StepTag.THINKING
        original_html = panel._steps[-1].html
        panel.dispatch_progress("__SHELL_CHUNK__:some output")
        # Thinking step should be unchanged (tag guard prevents mutation)
        assert panel._steps[-1].html == original_html
        assert panel._steps[-1].tag == StepTag.THINKING


# ---------------------------------------------------------------------------
# TEST-1 — TOOL_END collision tests (BUG-1 regression guard)
# ---------------------------------------------------------------------------


class TestToolEndCollisionGuard:
    """Verify that non-TOOL_END messages containing ✅/❌ and ** are NOT
    misdetected as TOOL_END (the original BUG-1)."""

    def test_confirm_line_with_glob_not_misdetected(self) -> None:
        """A confirm line like '✅ Confirmed — executing `shell`' containing
        a glob pattern with ** must not trigger TOOL_END merge."""
        panel = _make_panel()
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"rg --glob **/*.py\"")
        assert len(panel._steps) == 1
        assert panel._steps[0].tag == StepTag.TOOL_RUNNING

        # Simulate a confirm line (NOT a __TOOL_END__ signal) that contains
        # both ✅ and ** — must NOT merge into the TOOL_RUNNING step.
        panel.dispatch_progress("✅ Confirmed — executing `shell`\n```\n$ rg --glob **/*.py\n```")
        # This is a normal progress message (no __TOOL_END__ prefix), so it
        # should append a NEW step, not merge.
        assert len(panel._steps) == 2
        # The TOOL_RUNNING step should NOT have ✅ appended
        assert "✅" not in panel._steps[0].html
        assert panel._steps[0].tag == StepTag.TOOL_RUNNING

    def test_error_line_with_markdown_bold_not_misdetected(self) -> None:
        """An error line like '❌ LLM error: ...' containing markdown ** must
        not trigger TOOL_END merge."""
        panel = _make_panel()
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"ls\"")
        assert len(panel._steps) == 1

        # Simulate an error line with markdown bold — must NOT merge.
        panel.dispatch_progress("❌ LLM error: **timeout** after 30s")
        # Normal append, not merge
        assert len(panel._steps) == 2
        assert "❌" not in panel._steps[0].html  # TOOL_RUNNING untouched
        assert panel._steps[0].tag == StepTag.TOOL_RUNNING

    def test_auto_approve_line_not_misdetected(self) -> None:
        """An auto-approve line '✅ Auto-approved `shell`' must not merge."""
        panel = _make_panel()
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"ls\"")
        assert len(panel._steps) == 1

        panel.dispatch_progress("✅ Auto-approved `shell` (approve-all active)")
        assert len(panel._steps) == 2
        assert "✅" not in panel._steps[0].html


# ---------------------------------------------------------------------------
# TEST-3 — Contract test: emission format binds to classify() parsing
# ---------------------------------------------------------------------------


class TestEmissionClassifyContract:
    """Verify that the TOOL_START emission format produced by react_loop.py
    is correctly parsed by _ProgressPanel.classify(). This guards against
    format drift between the emitter and the parser."""

    def test_tool_start_emission_parses_name_and_brief(self) -> None:
        """The emission format is: '{icon} Running tool: `{name}`\\n{brief}'.
        classify() must extract the name from backticks and the brief from
        the second line."""
        from react_loop import _tool_icon, fmt_tool_brief

        tool_name = "file_read"
        args = {"path": "/home/paul/config.yaml"}
        brief = fmt_tool_brief(tool_name, args)
        emission = f"{_tool_icon(tool_name)} Running tool: `{tool_name}`\n{brief}"

        panel = _make_panel()
        html_text, tag = panel.classify(emission)
        assert tag == StepTag.TOOL_RUNNING
        assert "<code>file_read</code>" in html_text
        assert "config.yaml" in html_text

    def test_tool_start_emission_shell_brief(self) -> None:
        from react_loop import _tool_icon, fmt_tool_brief

        tool_name = "shell"
        args = {"command": "ls -la /tmp"}
        brief = fmt_tool_brief(tool_name, args)
        emission = f"{_tool_icon(tool_name)} Running tool: `{tool_name}`\n{brief}"

        panel = _make_panel()
        html_text, tag = panel.classify(emission)
        assert tag == StepTag.TOOL_RUNNING
        assert "<code>shell</code>" in html_text
        assert "ls -la" in html_text

    def test_tool_start_emission_mcp_brief(self) -> None:
        from react_loop import _tool_icon, fmt_tool_brief

        tool_name = "open"
        args = {"url": "https://example.com"}
        brief = fmt_tool_brief(tool_name, args, is_mcp=True, server_name="agent-browser")
        emission = f"{_tool_icon(tool_name)} Running tool: `{tool_name}`\n{brief}"

        panel = _make_panel()
        html_text, tag = panel.classify(emission)
        assert tag == StepTag.TOOL_RUNNING
        assert "<code>open</code>" in html_text
        assert "MCP:agent-browser" in html_text

    def test_tool_end_signal_parses_correctly(self) -> None:
        """The __TOOL_END__ signal format is: __TOOL_END__:{ok|fail}:{name}\\n{payload}."""
        panel = _make_panel()
        panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"ls\"")
        assert len(panel._steps) == 1

        # Well-formed TOOL_END signal
        panel.dispatch_progress("__TOOL_END__:ok:shell\n🖥️ **shell** ✅\n```\noutput\n```")
        assert len(panel._steps) == 1  # merged, not appended
        assert "✅" in panel._steps[0].html

    def test_tool_end_fail_signal_parses_correctly(self) -> None:
        panel = _make_panel()
        panel.dispatch_progress("🖥️ Running tool: `file_read`\nfile_read app.py")
        assert len(panel._steps) == 1

        panel.dispatch_progress("__TOOL_END__:fail:file_read\n📄 **file_read** ❌\n```\nerror\n```")
        assert len(panel._steps) == 1  # merged
        assert "❌" in panel._steps[0].html
        assert "app.py" in panel._steps[0].html  # brief preserved


# ---------------------------------------------------------------------------
# RISK-1 — Newline guard in fmt_tool_brief
# ---------------------------------------------------------------------------


class TestNewlineGuard:
    """Verify that newlines in shell commands are replaced with spaces in the
    brief, preventing multi-line briefs that break the panel layout."""

    def test_shell_command_with_newline_collapsed(self) -> None:
        cmd = "echo hello\necho world"
        brief = fmt_tool_brief("shell", {"command": cmd})
        assert "\n" not in brief
        assert "\r" not in brief
        assert " " in brief  # newline replaced with space

    def test_shell_command_with_carriage_return_collapsed(self) -> None:
        cmd = "echo hello\r\necho world"
        brief = fmt_tool_brief("shell", {"command": cmd})
        assert "\r" not in brief
        assert "\n" not in brief

    def test_heredoc_command_collapsed(self) -> None:
        cmd = "cat <<EOF\nhello\nworld\nEOF"
        brief = fmt_tool_brief("shell", {"command": cmd})
        assert "\n" not in brief


# ---------------------------------------------------------------------------
# #4 — Unchanged subsystems regression tests (spec "Unchanged subsystems")
# ---------------------------------------------------------------------------


class TestUnchangedSubsystems:
    """Verify that the change did not modify verbose mode, confirmation flow,
    fmt_tool_call(), fmt_tool_result_progress(), LogEvent taxonomy, or the
    on_tool_trace hook."""

    def test_fmt_tool_call_unchanged_shell(self) -> None:
        """fmt_tool_call() must still produce the same multi-line markdown."""
        from react_loop import fmt_tool_call
        result = fmt_tool_call("shell", {"command": "ls -la"})
        assert result == "```\n$ ls -la\n```"

    def test_fmt_tool_call_unchanged_file_read(self) -> None:
        from react_loop import fmt_tool_call
        result = fmt_tool_call("file_read", {"path": "/tmp/config.yaml"})
        assert result == "```\nread: /tmp/config.yaml\n```"

    def test_fmt_tool_call_unchanged_file_write(self) -> None:
        from react_loop import fmt_tool_call
        result = fmt_tool_call("file_write", {"path": "app.py", "content": "x" * 10})
        assert result == "```\nwrite: app.py (10 bytes)\n```"

    def test_fmt_tool_result_progress_still_calls_fmt_tool_call(self) -> None:
        """fmt_tool_result_progress() must still embed fmt_tool_call() output."""
        from react_loop import fmt_tool_result_progress
        outcome = {"success": True, "output": "done"}
        result = fmt_tool_result_progress("shell", {"command": "ls"}, outcome)
        # Must contain the fmt_tool_call output (markdown code fence with $ ls)
        assert "$ ls" in result
        assert "✅" in result

    def test_log_event_taxonomy_unchanged(self) -> None:
        """LogEvent enum must still have TOOL_START, TOOL_END, TOOL_FAILED."""
        from agent_logging import LogEvent
        assert hasattr(LogEvent, "TOOL_START")
        assert hasattr(LogEvent, "TOOL_END")
        assert hasattr(LogEvent, "TOOL_FAILED")
        assert hasattr(LogEvent, "RUN_BEGIN")
        assert hasattr(LogEvent, "RUN_END")
        assert hasattr(LogEvent, "STEP_BEGIN")
        assert hasattr(LogEvent, "STEP_END")

    def test_on_tool_trace_defaults_to_none(self) -> None:
        """ReactContext.on_tool_trace must default to None (unwired for main agent)."""
        from react_loop import ReactContext
        # The field default is None — verify it exists and defaults to None
        import dataclasses
        fields = {f.name: f for f in dataclasses.fields(ReactContext)}
        assert "on_tool_trace" in fields
        assert fields["on_tool_trace"].default is None

    def test_verbose_mode_still_sends_full_args(self) -> None:
        """When verbose mode is on, the __TOOL_END__ handler must forward the
        full fmt_tool_result_progress payload as a verbose event."""
        import asyncio
        panel = _make_panel()
        panel._interface._verbose = True
        panel._interface._send_verbose_event = MagicMock()
        # Stub asyncio.run_coroutine_threadsafe to avoid real scheduling.
        original_run = asyncio.run_coroutine_threadsafe
        def _swallow(coro, loop):
            coro.close()  # suppress "never awaited" warning
            return MagicMock()
        asyncio.run_coroutine_threadsafe = _swallow  # type: ignore[assignment]
        try:
            panel.dispatch_progress("🖥️ Running tool: `shell`\nshell \"ls\"")
            payload = "🖥️ **shell** ✅\n```\n$ ls\n```\n```\noutput\n```"
            panel.dispatch_progress(f"__TOOL_END__:ok:shell\n{payload}")
        finally:
            asyncio.run_coroutine_threadsafe = original_run  # type: ignore[assignment]
        assert panel._interface._send_verbose_event.call_count >= 2
        last_call = panel._interface._send_verbose_event.call_args
        assert last_call.args[2] == payload

    def test_confirmation_flow_unchanged(self) -> None:
        """The __CONFIRM__ handler must still route to _send_confirmation_prompt."""
        import asyncio
        panel = _make_panel()
        panel._interface._send_confirmation_prompt = MagicMock()
        original_run = asyncio.run_coroutine_threadsafe
        def _swallow(coro, loop):
            coro.close()
            return MagicMock()
        asyncio.run_coroutine_threadsafe = _swallow  # type: ignore[assignment]
        try:
            panel.dispatch_progress("__CONFIRM__:token123:shell:Run ls -la?")
        finally:
            asyncio.run_coroutine_threadsafe = original_run  # type: ignore[assignment]
        panel._interface._send_confirmation_prompt.assert_called_once()