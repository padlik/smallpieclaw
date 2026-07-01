"""Tests for _on_file() caption+artifact handling in TelegramInterface.

Verifies that when a file is uploaded with a caption:
  - Captioned photo → _run_agent_task called with artifact-aware text + images=[dest]
  - Captioned image document → same, preserving document filename
  - Captioned non-image document → _run_agent_task called with artifact-aware text, no images
  - No-caption document → _run_agent_task NOT called; saved confirmation returned
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_deferred_message import _make_ctx, _make_iface


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_photo_update(user_id: int = 42, caption: str = "") -> MagicMock:
    """Return an update whose message has a photo + optional caption."""
    photo = MagicMock()
    photo.file_id = "photo_file_id"
    photo.file_unique_id = "uniq123"

    msg = MagicMock()
    msg.document = None
    msg.photo = [photo]  # list; _on_file takes the last entry
    msg.audio = None
    msg.video = None
    msg.voice = None
    msg.caption = caption or None
    msg.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))

    user = MagicMock()
    user.id = user_id

    update = MagicMock()
    update.effective_user = user
    update.effective_message = msg
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1001
    return update


def _make_document_update(
    user_id: int = 42,
    caption: str = "",
    filename: str = "report.pdf",
    mime_type: str = "application/pdf",
) -> MagicMock:
    """Return an update whose message has a document + optional caption."""
    doc = MagicMock()
    doc.file_id = "doc_file_id"
    doc.file_unique_id = "docuniq456"
    doc.file_name = filename
    doc.mime_type = mime_type

    msg = MagicMock()
    msg.document = doc
    msg.photo = []
    msg.audio = None
    msg.video = None
    msg.voice = None
    msg.caption = caption or None
    msg.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))

    user = MagicMock()
    user.id = user_id

    update = MagicMock()
    update.effective_user = user
    update.effective_message = msg
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1001
    return update


def _run_on_file(iface, update, ctx):
    """Run _on_file synchronously, patching filesystem + Telegram download."""
    tg_file_mock = MagicMock()
    tg_file_mock.download_to_drive = AsyncMock()

    async def _run():
        with (
            patch("os.makedirs"),
            patch("os.path.exists", return_value=False),
            patch("os.path.getsize", return_value=512 * 1024),  # 512 KB
        ):
            ctx.bot.get_file = AsyncMock(return_value=tg_file_mock)
            await iface._on_file(update, ctx)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOnFileCaptionHandling:
    """Unit tests for _on_file caption+artifact propagation."""

    def test_captioned_photo_runs_agent_with_artifact_text(self):
        """Captioned photo → agent receives task text containing caption + artifact path."""
        iface = _make_iface()
        iface._run_agent_task = AsyncMock()

        update = _make_photo_update(caption="Analyse this chart")
        ctx = _make_ctx()
        _run_on_file(iface, update, ctx)

        iface._run_agent_task.assert_awaited_once()
        call_args = iface._run_agent_task.call_args
        task_text: str = call_args.args[2]
        images = call_args.kwargs.get("images") or (call_args.args[3] if len(call_args.args) > 3 else None)

        # Caption is present in the task text
        assert "Analyse this chart" in task_text
        # Artifact path must appear in the task text
        assert "photo_uniq123.jpg" in task_text or "/tmp" in task_text
        # images kwarg must be a list with the dest path
        assert images is not None
        assert len(images) == 1
        assert images[0].endswith(".jpg")

    def test_captioned_photo_task_text_includes_filename_and_path(self):
        """Task text for captioned photo contains filename and path separately."""
        iface = _make_iface()
        iface._run_agent_task = AsyncMock()

        update = _make_photo_update(caption="What do you see?")
        ctx = _make_ctx()
        _run_on_file(iface, update, ctx)

        call_args = iface._run_agent_task.call_args
        task_text: str = call_args.args[2]
        # Should include artifact metadata section
        assert "photo_uniq123.jpg" in task_text
        assert "Uploaded artifact" in task_text or "artifact" in task_text.lower()

    def test_captioned_image_document_runs_agent_with_filename(self):
        """Captioned image document → agent text includes document filename."""
        iface = _make_iface()
        iface._run_agent_task = AsyncMock()

        update = _make_document_update(
            caption="Resize this",
            filename="screenshot.png",
            mime_type="image/png",
        )
        ctx = _make_ctx()
        _run_on_file(iface, update, ctx)

        iface._run_agent_task.assert_awaited_once()
        call_args = iface._run_agent_task.call_args
        task_text: str = call_args.args[2]
        images = call_args.kwargs.get("images") or (call_args.args[3] if len(call_args.args) > 3 else None)

        assert "Resize this" in task_text
        assert "screenshot.png" in task_text
        assert images is not None
        assert len(images) == 1

    def test_captioned_non_image_document_runs_agent(self):
        """Captioned non-image document → agent IS run (currently broken)."""
        iface = _make_iface()
        iface._run_agent_task = AsyncMock()

        update = _make_document_update(
            caption="Summarise this PDF",
            filename="report.pdf",
            mime_type="application/pdf",
        )
        ctx = _make_ctx()
        _run_on_file(iface, update, ctx)

        # Currently _run_agent_task is NOT called for non-image files — this test is RED
        iface._run_agent_task.assert_awaited_once()

    def test_captioned_non_image_document_task_text_has_caption_and_path(self):
        """Captioned non-image document → task text contains caption + artifact metadata."""
        iface = _make_iface()
        iface._run_agent_task = AsyncMock()

        update = _make_document_update(
            caption="Summarise this PDF",
            filename="report.pdf",
            mime_type="application/pdf",
        )
        ctx = _make_ctx()
        _run_on_file(iface, update, ctx)

        call_args = iface._run_agent_task.call_args
        task_text: str = call_args.args[2]

        assert "Summarise this PDF" in task_text
        assert "report.pdf" in task_text
        # images should be None or absent for non-image documents
        images = call_args.kwargs.get("images") or (call_args.args[3] if len(call_args.args) > 3 else None)
        assert not images

    def test_duplicate_document_uses_deduplicated_name_in_task_text(self):
        """When saved path is deduplicated, artifact name must match basename(path)."""
        iface = _make_iface()
        iface._run_agent_task = AsyncMock()
        tg_file_mock = MagicMock()
        tg_file_mock.download_to_drive = AsyncMock()

        update = _make_document_update(
            caption="Summarise this PDF",
            filename="report.pdf",
            mime_type="application/pdf",
        )
        ctx = _make_ctx()

        async def _run():
            with (
                patch("os.makedirs"),
                patch("os.path.exists", side_effect=[True, True, False]),
                patch("os.path.getsize", return_value=512 * 1024),
            ):
                ctx.bot.get_file = AsyncMock(return_value=tg_file_mock)
                await iface._on_file(update, ctx)

        asyncio.run(_run())

        task_text: str = iface._run_agent_task.call_args.args[2]
        assert "- name: report_2.pdf" in task_text
        assert "report_2.pdf" in task_text
        assert "- name: report.pdf" not in task_text

    def test_no_caption_document_does_not_run_agent(self):
        """Document with no caption → _run_agent_task NOT called; path in confirmation."""
        iface = _make_iface()
        iface._run_agent_task = AsyncMock()

        update = _make_document_update(caption="", filename="data.csv")
        ctx = _make_ctx()
        _run_on_file(iface, update, ctx)

        iface._run_agent_task.assert_not_awaited()

    def test_no_caption_photo_does_not_run_agent(self):
        """Photo with no caption → _run_agent_task NOT called."""
        iface = _make_iface()
        iface._run_agent_task = AsyncMock()

        update = _make_photo_update(caption="")
        ctx = _make_ctx()
        _run_on_file(iface, update, ctx)

        iface._run_agent_task.assert_not_awaited()


class TestTaskTextWithArtifact:
    """Unit tests for the helper function _task_text_with_artifact."""

    def test_helper_contains_caption(self):
        from telegram_interface import _task_text_with_artifact
        result = _task_text_with_artifact("my caption", "file.pdf", "/tmp/file.pdf", "64.0 KB")
        assert "my caption" in result

    def test_helper_contains_filename(self):
        from telegram_interface import _task_text_with_artifact
        result = _task_text_with_artifact("my caption", "file.pdf", "/tmp/file.pdf", "64.0 KB")
        assert "file.pdf" in result

    def test_helper_contains_dest_path(self):
        from telegram_interface import _task_text_with_artifact
        result = _task_text_with_artifact("my caption", "file.pdf", "/tmp/file.pdf", "64.0 KB")
        assert "/tmp/file.pdf" in result

    def test_helper_contains_size(self):
        from telegram_interface import _task_text_with_artifact
        result = _task_text_with_artifact("my caption", "file.pdf", "/tmp/file.pdf", "64.0 KB")
        assert "64.0 KB" in result
