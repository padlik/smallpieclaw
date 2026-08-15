"""Tests for the /prompts Telegram command."""

from __future__ import annotations

import asyncio
import re as _re
from unittest.mock import AsyncMock, MagicMock

import pytest

from prompt_registry import PromptRecord, PromptRegistry, SearchPage
from telegram_commands import cmd_help, cmd_prompts


def _make_iface(registry=None):
    """Build a minimal TelegramInterface stand-in."""
    from telegram_interface import TelegramInterface

    config = {
        "telegram": {
            "bot_token": "fake:token",
            "security_mode": "allowlist",
            "allowed_user_ids": [42],
        }
    }
    iface = TelegramInterface.__new__(TelegramInterface)
    iface._config = config
    iface.security_mode = "allowlist"
    iface.allowed_ids = {42}
    iface._prompt_registry = registry
    return iface


def _make_registry(tmp_path):
    """Create a PromptRegistry without triggering the unfinished backfill path.

    The registry backfill only runs when ``prompts_archive.jsonl`` is missing.
    Creating an empty archive file lets the existing list-recent tests work while
    the archive/search implementation is finished in another lane.
    """
    archive = tmp_path / "prompts_archive.jsonl"
    archive.write_text("", encoding="utf-8")
    return PromptRegistry(data_dir=str(tmp_path))


def _run_cmd(iface, args=None):
    """Invoke cmd_prompts with the given args and return all reply_text calls."""
    sent_texts: list[str] = []

    async def _run():
        mock_message = MagicMock()
        mock_message.reply_text = AsyncMock(
            side_effect=lambda text, **kw: sent_texts.append(text)
        )
        mock_user = MagicMock()
        mock_user.id = 42
        mock_update = MagicMock()
        mock_update.effective_user = mock_user
        mock_update.effective_message = mock_message
        mock_ctx = MagicMock()
        mock_ctx.args = args or []
        await cmd_prompts(iface, mock_update, mock_ctx)

    asyncio.run(_run())
    return sent_texts


class TestPromptsCommand:
    def test_lists_recent_prompts(self, tmp_path):
        registry = _make_registry(tmp_path)
        r1 = registry.start("r-aaaa", "first task")
        registry.add_sub_agent(r1.prompt_id, "sa-1")
        registry.finish(r1.prompt_id, "done")
        r2 = registry.start("r-bbbb", "second task")

        iface = _make_iface(registry)
        texts = _run_cmd(iface)

        full = "\n".join(texts)
        assert r1.prompt_id in full
        assert r2.prompt_id in full
        assert "first task" in full
        assert "second task" in full
        assert _re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", full)
        assert "done" in full
        assert "running" in full
        assert "1 sub-agent" in full
        assert "0 sub-agent" not in full  # second prompt has none
        assert "/prompts search" in full
        assert "/prompts show" in full

    def test_empty_registry(self, tmp_path):
        registry = _make_registry(tmp_path)
        iface = _make_iface(registry)
        texts = _run_cmd(iface)
        assert "No prompts recorded yet" in "\n".join(texts)

    def test_registry_unavailable(self):
        iface = _make_iface(registry=None)
        texts = _run_cmd(iface)
        assert "Prompt registry not available" in "\n".join(texts)

    def test_status_icons(self, tmp_path):
        registry = _make_registry(tmp_path)
        registry.start("r-a", "task")
        r2 = registry.start("r-b", "task")
        registry.finish(r2.prompt_id, "failed")
        r3 = registry.start("r-c", "task")
        registry.finish(r3.prompt_id, "cancelled")

        iface = _make_iface(registry)
        texts = _run_cmd(iface)
        full = "\n".join(texts)
        assert "🔄" in full or "running" in full
        assert "❌" in full or "failed" in full
        assert "🛑" in full or "cancelled" in full

    def test_unknown_subcommand_lists_recent(self, tmp_path):
        registry = _make_registry(tmp_path)
        r1 = registry.start("r-a", "fallback task")

        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["foobar"])
        full = "\n".join(texts)
        assert r1.prompt_id in full
        assert "fallback task" in full


class TestPromptsSearchArgParsing:
    def test_search_query_only(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search", "PTO"])
        assert called["query"] == "PTO"
        assert called.get("days") is None

    def test_search_query_with_time_window_days(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search", "PTO", "7d"])
        assert called["query"] == "PTO"
        assert called["days"] == 7

    def test_search_only_time_window(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search", "7d"])
        assert called["query"] == ""
        assert called["days"] == 7

    def test_search_no_args(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search"])
        assert called["query"] == ""
        assert called.get("days") is None

    def test_search_with_hours_window(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search", "worklogs", "12h"])
        assert called["query"] == "worklogs"
        assert called["days"] == 0.5

    def test_search_status_valid(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search", "PTO", "--status=failed"])
        assert called["status"] == "failed"

    def test_search_status_invalid(self, tmp_path):
        registry = MagicMock()
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["search", "PTO", "--status=unknown"])
        full = "\n".join(texts)
        assert "Invalid status" in full
        assert "running, done, failed, cancelled" in full

    def test_search_trace(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search", "PTO", "--trace=r-abc123"])
        assert called["trace_id"] == "r-abc123"

    def test_search_since_until_valid(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search", "deploy", "--since=2026-08-01", "--until=2026-08-15"])
        assert called["since"] == "2026-08-01"
        assert called["until"] == "2026-08-15"

    def test_search_since_invalid_iso(self, tmp_path):
        registry = MagicMock()
        registry.search = lambda **kwargs: (_ for _ in ()).throw(ValueError("Invalid ISO"))
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["search", "deploy", "--since=not-a-date"])
        full = "\n".join(texts)
        assert "Invalid timestamp" in full

    def test_search_page_valid(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search", "worklogs", "--page=2"])
        assert called["offset"] == 20

    def test_search_page_non_integer(self, tmp_path):
        registry = MagicMock()
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["search", "worklogs", "--page=abc"])
        full = "\n".join(texts)
        assert "Invalid page number" in full

    def test_search_page_zero_rejected(self, tmp_path):
        registry = MagicMock()
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["search", "worklogs", "--page=0"])
        full = "\n".join(texts)
        assert "Invalid page number" in full

    def test_search_page_negative_rejected(self, tmp_path):
        registry = MagicMock()
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["search", "worklogs", "--page=-1"])
        full = "\n".join(texts)
        assert "Invalid page number" in full

    def test_search_combined_flags(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search", "PTO", "--status=failed", "--trace=r-abc"])
        assert called["query"] == "PTO"
        assert called["status"] == "failed"
        assert called["trace_id"] == "r-abc"

    def test_search_unknown_flag_as_query(self, tmp_path):
        called = {}
        registry = MagicMock()
        registry.search = lambda **kwargs: called.update(kwargs) or SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        _run_cmd(iface, args=["search", "--verbose", "PTO"])
        assert called["query"] == "--verbose PTO"


class TestPromptsShowArgParsing:
    def test_show_with_id(self, tmp_path):
        record = PromptRecord(
            prompt_id="01HSHOW001",
            trace_id="r-a",
            text="show me",
            started_at=1000000000.0,
            ended_at=1000000005.0,
            status="done",
            sub_agent_ids=["sa-1"],
        )
        registry = MagicMock()
        registry.show = lambda prompt_id: record

        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["show", "01HSHOW001"])
        full = "\n".join(texts)
        assert "Prompt <code>01HSHOW001</code>" in full
        assert "show me" in full
        assert "sa-1" in full

    def test_show_without_id(self, tmp_path):
        registry = MagicMock()
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["show"])
        assert "Usage: /prompts show" in "\n".join(texts)

    def test_show_not_found(self, tmp_path):
        registry = MagicMock()
        registry.show = lambda prompt_id: None
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["show", "01HNONEXIST"])
        full = "\n".join(texts)
        assert "not found" in full


class TestPromptsSearchRendering:
    def test_page_1_of_2_with_next_tail(self, tmp_path):
        records = [
            PromptRecord(
                prompt_id=f"01H{i:04d}",
                trace_id="r-a",
                text="worklogs entry",
                started_at=1000000000.0 + i,
                ended_at=1000000001.0 + i,
                status="done",
                sub_agent_ids=[],
            )
            for i in range(30)
        ]
        records.reverse()

        registry = MagicMock()
        registry.search = lambda **kwargs: SearchPage(
            results=records[kwargs.get("offset", 0) : kwargs.get("offset", 0) + 20],
            total_matched=30,
        )
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["search", "worklogs"])
        full = "\n".join(texts)
        assert "🔍 Search results" in full
        assert "(30)" in full
        assert "Page 1 of 2" in full
        assert "use --page=2 for next" in full

    def test_page_2_of_2_no_tail(self, tmp_path):
        records = [
            PromptRecord(
                prompt_id=f"01H{i:04d}",
                trace_id="r-a",
                text="worklogs entry",
                started_at=1000000000.0 + i,
                ended_at=1000000001.0 + i,
                status="done",
                sub_agent_ids=[],
            )
            for i in range(30)
        ]
        records.reverse()

        registry = MagicMock()
        registry.search = lambda **kwargs: SearchPage(
            results=records[kwargs.get("offset", 0) : kwargs.get("offset", 0) + 20],
            total_matched=30,
        )
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["search", "worklogs", "--page=2"])
        full = "\n".join(texts)
        assert "Page 2 of 2" in full
        assert "use --page=3 for next" not in full

    def test_single_page_results(self, tmp_path):
        records = [
            PromptRecord(
                prompt_id=f"01H{i:04d}",
                trace_id="r-a",
                text="worklogs entry",
                started_at=1000000000.0 + i,
                ended_at=1000000001.0 + i,
                status="done",
                sub_agent_ids=[],
            )
            for i in range(5)
        ]
        records.reverse()

        registry = MagicMock()
        registry.search = lambda **kwargs: SearchPage(results=records, total_matched=5)
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["search", "worklogs"])
        full = "\n".join(texts)
        assert "Page 1 of 1" in full
        assert "use --page=2 for next" not in full

    def test_out_of_range_page(self, tmp_path):
        registry = MagicMock()
        registry.search = lambda **kwargs: SearchPage(results=[], total_matched=30)
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["search", "worklogs", "--page=5"])
        full = "\n".join(texts)
        assert "Page 5 is past the last page (2 pages total)" in full
        assert "No prompts matching" not in full

    def test_empty_results(self, tmp_path):
        registry = MagicMock()
        registry.search = lambda **kwargs: SearchPage(results=[], total_matched=0)
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["search", "nonexistent"])
        full = "\n".join(texts)
        assert "No prompts matching" in full


class TestPromptsShowRendering:
    def test_show_found_done(self, tmp_path):
        record = PromptRecord(
            prompt_id="01HSHOW002",
            trace_id="r-a",
            text="done task full text",
            started_at=1000000000.0,
            ended_at=1000000005.0,
            status="done",
            sub_agent_ids=["sa-1", "sa-2"],
        )
        registry = MagicMock()
        registry.show = lambda prompt_id: record

        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["show", "01HSHOW002"])
        full = "\n".join(texts)
        assert "Prompt <code>01HSHOW002</code>" in full
        assert "Status:" in full
        assert "Trace: <code>r-a</code>" in full
        assert "Started:" in full
        assert "Ended:" in full
        assert "Sub-agents:" in full
        assert "Full text:" in full
        assert "done task full text" in full

    def test_show_found_running(self, tmp_path):
        record = PromptRecord(
            prompt_id="01HSHOW003",
            trace_id="r-a",
            text="running task",
            started_at=1000000000.0,
            ended_at=None,
            status="running",
            sub_agent_ids=[],
        )
        registry = MagicMock()
        registry.show = lambda prompt_id: record

        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["show", "01HSHOW003"])
        full = "\n".join(texts)
        assert "(running)" in full
        assert "Full text:" in full
        assert "running task" in full

    def test_show_not_found(self, tmp_path):
        registry = MagicMock()
        registry.show = lambda prompt_id: None
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["show", "01HNONEXIST"])
        full = "\n".join(texts)
        assert "❌ Prompt" in full
        assert "not found" in full

    def test_show_missing_arg(self, tmp_path):
        registry = MagicMock()
        iface = _make_iface(registry)
        texts = _run_cmd(iface, args=["show"])
        assert "Usage: /prompts show" in "\n".join(texts)


class TestPromptsHelpText:
    def test_help_includes_prompts(self):
        iface = _make_iface()
        sent_texts: list[str] = []

        async def _run():
            mock_message = MagicMock()
            mock_message.reply_text = AsyncMock(
                side_effect=lambda text, **kw: sent_texts.append(text)
            )
            mock_user = MagicMock()
            mock_user.id = 42
            mock_update = MagicMock()
            mock_update.effective_user = mock_user
            mock_update.effective_message = mock_message
            await cmd_help(iface, mock_update, MagicMock())

        asyncio.run(_run())
        assert any("/prompts" in t for t in sent_texts)

    def test_help_includes_search_and_show_subcommands(self):
        iface = _make_iface()
        sent_texts: list[str] = []

        async def _run():
            mock_message = MagicMock()
            mock_message.reply_text = AsyncMock(
                side_effect=lambda text, **kw: sent_texts.append(text)
            )
            mock_user = MagicMock()
            mock_user.id = 42
            mock_update = MagicMock()
            mock_update.effective_user = mock_user
            mock_update.effective_message = mock_message
            await cmd_help(iface, mock_update, MagicMock())

        asyncio.run(_run())
        help_text = "\n".join(sent_texts)
        assert "/prompts search" in help_text
        assert "/prompts show" in help_text
        assert "--status" in help_text
        assert "--trace" in help_text
        assert "--since" in help_text
        assert "--until" in help_text
        assert "--page" in help_text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
