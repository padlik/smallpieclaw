"""
Unit tests for strategy_memory.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from strategy_memory import (
    Strategy,
    StrategyMemory,
    classify_task_type,
    format_strategies_for_prompt,
)


class TestStrategy:
    """Tests for the ``Strategy`` dataclass."""

    def test_dataclass_fields(self) -> None:
        """All declared fields are exposed as attributes."""
        strategy = Strategy(
            task_type="pdf-processing",
            approach="Use pdftotext for scanned PDFs.",
            confidence=0.85,
            success_count=5,
            failure_count=1,
            last_used="2026-01-01T00:00:00+00:00",
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert strategy.task_type == "pdf-processing"
        assert strategy.approach == "Use pdftotext for scanned PDFs."
        assert strategy.confidence == 0.85
        assert strategy.success_count == 5
        assert strategy.failure_count == 1
        assert strategy.last_used == "2026-01-01T00:00:00+00:00"
        assert strategy.created_at == "2026-01-01T00:00:00+00:00"

    def test_to_dict_roundtrip(self) -> None:
        """
        ``to_dict`` and ``from_dict`` are inverse operations.
        """
        original = Strategy(
            task_type="ocr-task",
            approach="Use tesseract with deskew preprocessing.",
            confidence=0.92,
            success_count=10,
            failure_count=0,
            last_used="2026-06-25T12:00:00+00:00",
            created_at="2026-06-01T12:00:00+00:00",
        )
        data = original.to_dict()
        restored = Strategy.from_dict(data)
        assert restored == original


class TestClassifyTaskType:
    """Tests for the heuristic task-type classifier."""

    def test_pdf_processing(self) -> None:
        assert classify_task_type("convert scanned pdf") == "pdf-processing"

    def test_container_management(self) -> None:
        assert classify_task_type("docker status") == "container-management"

    def test_backup_task(self) -> None:
        assert classify_task_type("run backup") == "backup-task"

    def test_ocr_task(self) -> None:
        assert classify_task_type("scan document") == "ocr-task"

    def test_media_conversion(self) -> None:
        assert classify_task_type("convert video") == "media-conversion"

    def test_general_default(self) -> None:
        assert classify_task_type("hello world") == "general-task"


class TestStrategyMemory:
    """Tests for the persistent strategy store."""

    @pytest.fixture
    def strategy_factory(self):
        """Return a helper that builds a Strategy with a fixed timestamp."""
        def _make(
            task_type: str,
            approach: str,
            confidence: float,
            success_count: int = 0,
            failure_count: int = 0,
        ) -> Strategy:
            return Strategy(
                task_type=task_type,
                approach=approach,
                confidence=confidence,
                success_count=success_count,
                failure_count=failure_count,
                last_used="2026-06-25T00:00:00+00:00",
                created_at="2026-06-25T00:00:00+00:00",
            )

        return _make

    def test_add_and_get(self, tmp_path, strategy_factory) -> None:
        memory = StrategyMemory(str(tmp_path))
        strategy = strategy_factory("pdf-processing", "Use pdftotext.", 0.8, 3, 1)
        memory.add(strategy)
        results = memory.get("pdf-processing")
        assert len(results) == 1
        assert results[0].approach == "Use pdftotext."

    def test_upsert_merges_counts(self, tmp_path, strategy_factory) -> None:
        memory = StrategyMemory(str(tmp_path))
        first = strategy_factory("pdf-processing", "Use pdftotext.", 0.75, 3, 1)
        second = strategy_factory("pdf-processing", "Use pdftotext.", 0.50, 2, 0)
        memory.add(first)
        memory.add(second)
        results = memory.get("pdf-processing")
        assert len(results) == 1
        assert results[0].success_count == 5
        assert results[0].failure_count == 1
        assert results[0].confidence == pytest.approx(5 / 6)

    def test_get_sorted_by_confidence(self, tmp_path, strategy_factory) -> None:
        memory = StrategyMemory(str(tmp_path))
        low = strategy_factory("backup-task", "Rsync locally.", 0.4, 1, 0)
        high = strategy_factory("backup-task", "Rsync to cloud.", 0.9, 5, 0)
        mid = strategy_factory("backup-task", "Use restic.", 0.6, 2, 0)
        for s in (low, high, mid):
            memory.add(s)
        results = memory.get("backup-task")
        confidences = [s.confidence for s in results]
        assert confidences == sorted(confidences, reverse=True)
        assert results[0].approach == "Rsync to cloud."

    def test_get_top_k(self, tmp_path, strategy_factory) -> None:
        memory = StrategyMemory(str(tmp_path))
        for idx, conf in enumerate((0.9, 0.8, 0.5, 0.3)):
            memory.add(
                strategy_factory(
                    "ocr-task", f"Approach {idx}", conf, success_count=1
                )
            )
        top = memory.get_top_k("ocr-task", k=2)
        assert len(top) == 2
        assert top[0].confidence == 0.9
        assert top[1].confidence == 0.8

    def test_get_top_k_with_conflict(self, tmp_path, strategy_factory) -> None:
        memory = StrategyMemory(str(tmp_path))
        first = strategy_factory("media-conversion", "FFmpeg CRF 23.", 0.85, 5, 0)
        second = strategy_factory(
            "media-conversion", "FFmpeg two-pass.", 0.80, 4, 0
        )
        third = strategy_factory("media-conversion", "HandBrake preset.", 0.5, 1, 0)
        for s in (first, second, third):
            memory.add(s)
        top = memory.get_top_k("media-conversion", k=1)
        assert len(top) == 2
        assert {s.approach for s in top} == {"FFmpeg CRF 23.", "FFmpeg two-pass."}

    def test_decay_reduces_confidence(self, tmp_path, strategy_factory, monkeypatch) -> None:
        memory = StrategyMemory(str(tmp_path))
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        fixed_now = datetime(2026, 1, 31, tzinfo=timezone.utc)  # 30 days later

        class MockDateTime:
            @classmethod
            def now(cls, _tz=None):
                return fixed_now

            @classmethod
            def fromisoformat(cls, s):
                return datetime.fromisoformat(s)

        monkeypatch.setattr("strategy_memory.datetime", MockDateTime)

        strategy = strategy_factory(
            "ocr-task", "Tesseract deskew.", 1.0, 5, 0
        )
        strategy.last_used = old_time.isoformat()
        memory.add(strategy)

        memory.decay_all()
        results = memory.get("ocr-task")
        assert len(results) == 1
        assert results[0].confidence == pytest.approx(0.9)
        assert results[0].last_used == fixed_now.isoformat()

    def test_archive_low_confidence(self, tmp_path, strategy_factory) -> None:
        memory = StrategyMemory(str(tmp_path))
        keep = strategy_factory("pdf-processing", "Keep me.", 0.5, 2, 0)
        archive = strategy_factory("pdf-processing", "Archive me.", 0.15, 0, 1)
        memory.add(keep)
        memory.add(archive)
        memory.archive_low_confidence(threshold=0.2)
        results = memory.get("pdf-processing")
        assert len(results) == 1
        assert results[0].approach == "Keep me."

    def test_save_and_load(self, tmp_path, strategy_factory) -> None:
        memory = StrategyMemory(str(tmp_path))
        strategy = strategy_factory(
            "container-management", "Check docker logs.", 0.7, 3, 1
        )
        memory.add(strategy)

        reloaded = StrategyMemory(str(tmp_path))
        results = reloaded.get("container-management")
        assert len(results) == 1
        assert results[0].approach == "Check docker logs."
        assert results[0].confidence == pytest.approx(0.7)

    def test_load_missing_file_creates_empty(self, tmp_path) -> None:
        memory = StrategyMemory(str(tmp_path))
        assert memory.get("anything") == []

    def test_save_atomic_write(self, tmp_path, strategy_factory, monkeypatch) -> None:
        memory = StrategyMemory(str(tmp_path))
        strategy = strategy_factory("backup-task", "Atomic write test.", 0.6, 1, 0)
        memory.add(strategy)

        replace_calls = []
        original_replace = __import__("os").replace

        def spy_replace(src, dst):
            replace_calls.append((src, dst))
            return original_replace(src, dst)

        monkeypatch.setattr("strategy_memory.os.replace", spy_replace)

        memory.save()

        assert len(replace_calls) == 1
        tmp_file, final_file = replace_calls[0]
        assert tmp_file.endswith(".tmp")
        assert ".strategies.json." in tmp_file
        assert final_file == str(tmp_path / "strategies.json")

        payload = json.loads((tmp_path / "strategies.json").read_text())
        assert payload["strategies"][0]["approach"] == "Atomic write test."
        assert payload["archived"] == {}


class TestFormatStrategies:
    """Tests for ``format_strategies_for_prompt``."""

    def test_single_strategy_format(self) -> None:
        strategy = Strategy(
            task_type="pdf-processing",
            approach="use pdftotext",
            confidence=0.85,
            success_count=1,
            failure_count=0,
            last_used="2026-06-25T00:00:00+00:00",
            created_at="2026-06-25T00:00:00+00:00",
        )
        text = format_strategies_for_prompt([strategy])
        assert "For pdf-processing, use pdftotext (confidence: 0.85)" in text

    def test_conflict_note(self) -> None:
        strategies = [
            Strategy(
                task_type="ocr-task",
                approach="tesseract",
                confidence=0.85,
                success_count=1,
                failure_count=0,
                last_used="2026-06-25T00:00:00+00:00",
                created_at="2026-06-25T00:00:00+00:00",
            ),
            Strategy(
                task_type="ocr-task",
                approach="easyocr",
                confidence=0.80,
                success_count=1,
                failure_count=0,
                last_used="2026-06-25T00:00:00+00:00",
                created_at="2026-06-25T00:00:00+00:00",
            ),
        ]
        text = format_strategies_for_prompt(strategies)
        assert "evaluate which applies" in text
