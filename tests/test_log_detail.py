"""Tests for the ``log_detail`` built-in tool.

Covers full-fidelity reads, identity against ``log_query`` previews, EMPTY
payloads for missing/unreadable/invalid cases, and the oversized-record dump
path with trace validation, redaction, and degraded fallback behavior.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest
import structlog

from builtin_tools.descriptors import BUILTIN_TOOLS
from builtin_tools.schemas import BUILTIN_TOOL_SCHEMAS
from config_schema import ExecutorPaths
from sqlite_log import SCHEMA_DDL, record_to_row

TRACE_A = "r-aaaaaaaa"
TRACE_VALID = "r-1a2b3c4d"
TRACE_CURRENT = "r-c0ffee01"


@pytest.fixture(autouse=True)
def _clear_contextvars():
    """Keep structlog contextvars isolated between tests."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _insert_records(path: str, records: list[dict]) -> None:
    """Create schema and insert records using the standard row layout."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_DDL)
    if records:
        rows = [record_to_row(rec) for rec in records]
        conn.executemany(
            """
            INSERT INTO events
            (ts, level, logger, agent, trace, prompt_id, event_type, msg, extra, search_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.commit()
    conn.close()


def _detail(exc, record_id: int) -> dict:
    """Call log_detail through public execute() and parse the payload."""
    result = exc.execute("log_detail", {"id": record_id})
    assert result["success"] is True
    assert result["exit_code"] == 0
    return json.loads(result["output"])


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Create a symlink or skip where the runtime does not permit symlinks."""
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")


# ---------------------------------------------------------------------------
# 5.5 — full fidelity + identity
# ---------------------------------------------------------------------------

class TestLogDetailFullFidelity:
    def test_full_err_returned_untruncated(self, make_builtin_executor, tmp_path):
        """log_detail returns a >500-char err field verbatim."""
        err_text = "E" * 600
        records = [
            {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "error",
             "event_type": "TOOL_FAILED", "tool": "shell", "err": err_text},
        ]
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, records)
        exc = make_builtin_executor(log_store_path=path)

        payload = _detail(exc, 1)
        assert payload["found"] is True
        assert payload["dumped"] is False
        assert payload["dump_path"] is None
        assert payload["record"]["err"] == err_text
        assert "…[" not in payload["record"]["err"]

    def test_identity_against_log_query_preview(self, make_builtin_executor, tmp_path):
        """log_detail's record is the same row as log_query's preview."""
        err_text = "X" * 600
        records = [
            {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "error",
             "event_type": "TOOL_FAILED", "tool": "shell", "err": err_text},
        ]
        path = str(tmp_path / "identity.sqlite")
        _insert_records(path, records)
        exc = make_builtin_executor(log_store_path=path)

        q = json.loads(exc.execute("log_query", {
            "trace": TRACE_A, "level": "DEBUG",
        })["output"])
        preview = q["records"][0]
        detail = _detail(exc, preview["id"])
        record = detail["record"]

        assert record["id"] == preview["id"]
        assert record["trace"] == preview["trace"] == TRACE_A
        assert record["event_type"] == preview["event_type"] == "TOOL_FAILED"
        assert record["ts"] == preview["ts"]
        assert preview["err"].endswith(" chars]")
        assert record["err"] == err_text
        assert record["err"].startswith(preview["err"][:20])


# ---------------------------------------------------------------------------
# 5.6 — EMPTY payloads
# ---------------------------------------------------------------------------

class TestLogDetailEmpty:
    EMPTY = {"found": False, "record": None, "dumped": False, "dump_path": None}

    def _assert_empty(self, payload: dict) -> None:
        keys = {"found", "record", "dumped", "dump_path"}
        assert {k: payload[k] for k in keys} == self.EMPTY

    def test_not_found_id(self, make_builtin_executor, tmp_path):
        path = str(tmp_path / "empty.sqlite")
        _insert_records(path, [])
        exc = make_builtin_executor(log_store_path=path)
        self._assert_empty(_detail(exc, 999999))

    def test_missing_store_path(self, make_builtin_executor, tmp_path):
        """An empty log_store_path yields an EMPTY payload."""
        exc = make_builtin_executor(log_store_path="")
        self._assert_empty(_detail(exc, 1))

    def test_unreadable_store_file(self, make_builtin_executor, tmp_path):
        path = str(tmp_path / "notadb.sqlite")
        Path(path).write_bytes(b"this is not a database")
        exc = make_builtin_executor(log_store_path=path)
        self._assert_empty(_detail(exc, 1))

    @pytest.mark.parametrize(
        "bad_id",
        ["abc", True, False, 0, -1, [], {}, 2**63, 2**64],
    )
    def test_invalid_id(self, make_builtin_executor, tmp_path, bad_id):
        path = str(tmp_path / "invalid.sqlite")
        _insert_records(path, [])
        exc = make_builtin_executor(log_store_path=path)
        self._assert_empty(_detail(exc, bad_id))


# ---------------------------------------------------------------------------
# 5.7 — oversized dump
# ---------------------------------------------------------------------------

class TestLogDetailOversized:
    def _make_executor(self, make_builtin_executor, tmp_path, results_dir: str, **extra):
        path = str(tmp_path / "detail.sqlite")
        return make_builtin_executor(
            max_output=4000,
            paths=ExecutorPaths(log_store_path=path, results_dir=results_dir, **extra),
        )

    def _oversized_record(self, trace: str | None = TRACE_VALID) -> dict:
        rec: dict = {
            "ts": "2026-07-05T10:00:00",
            "level": "error",
            "event_type": "TOOL_FAILED",
            "tool": "shell",
            "err": "E" * 70000,
        }
        if trace is not None:
            rec["trace"] = trace
        return rec

    def test_oversized_dumped_with_record_trace(self, make_builtin_executor, tmp_path):
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [self._oversized_record(TRACE_VALID)])
        exc = make_builtin_executor(
            paths=ExecutorPaths(results_dir=results_dir, log_store_path=path),
        )

        payload = _detail(exc, 1)
        assert payload["found"] is True
        assert payload["dumped"] is True
        assert payload["dump_path"]
        assert payload["dump_path"].startswith(os.path.abspath(results_dir))
        assert "log-detail-1.json" in payload["dump_path"]
        assert payload["record"]["err"].endswith(" chars]")

        with open(payload["dump_path"], encoding="utf-8") as fh:
            dumped = json.load(fh)
        assert dumped["err"] == "E" * 70000
        assert dumped["trace"] == TRACE_VALID

    def test_oversized_no_trace_uses_current_trace(self, make_builtin_executor, tmp_path):
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [self._oversized_record(trace=None)])
        structlog.contextvars.bind_contextvars(trace=TRACE_CURRENT)
        exc = make_builtin_executor(
            paths=ExecutorPaths(results_dir=results_dir, log_store_path=path),
        )

        try:
            payload = _detail(exc, 1)
        finally:
            structlog.contextvars.clear_contextvars()

        assert payload["found"] is True
        assert payload["dumped"] is True
        expected_dir = os.path.join(results_dir, TRACE_CURRENT)
        assert payload["dump_path"].startswith(os.path.abspath(expected_dir))
        assert os.path.isdir(expected_dir)

    def test_malformed_trace_falls_back_to_current(self, make_builtin_executor, tmp_path):
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [self._oversized_record(trace="../../evil")])
        structlog.contextvars.bind_contextvars(trace=TRACE_CURRENT)
        exc = make_builtin_executor(
            paths=ExecutorPaths(results_dir=results_dir, log_store_path=path),
        )

        try:
            payload = _detail(exc, 1)
        finally:
            structlog.contextvars.clear_contextvars()

        assert payload["found"] is True
        assert payload["dumped"] is True
        assert payload["dump_path"].startswith(os.path.abspath(results_dir))
        subdirs = [n for n in os.listdir(results_dir) if os.path.isdir(os.path.join(results_dir, n))]
        assert subdirs == [TRACE_CURRENT]

    def test_redaction_preserved_in_dump(self, make_builtin_executor, tmp_path):
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        secret = "supersecret"
        rec = self._oversized_record(TRACE_VALID)
        rec["err"] = ("A" * 1000) + secret + ("B" * 66000)
        _insert_records(path, [rec])
        exc = make_builtin_executor(
            paths=ExecutorPaths(
                results_dir=results_dir, log_store_path=path, vault_secrets=[secret]
            ),
        )

        payload = _detail(exc, 1)
        assert payload["dumped"] is True
        with open(payload["dump_path"], encoding="utf-8") as fh:
            dumped_text = fh.read()
        assert secret not in dumped_text
        assert "[REDACTED]" in dumped_text

    def test_quoted_secret_absent_from_dump_in_key_and_json_value(
        self, make_builtin_executor, tmp_path
    ):
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        secret = 'pa"ss\\word99'
        rec = self._oversized_record(TRACE_VALID)
        rec["request"] = {secret: 1, "blob": json.dumps({"k": secret})}
        _insert_records(path, [rec])
        exc = make_builtin_executor(
            paths=ExecutorPaths(
                results_dir=results_dir, log_store_path=path, vault_secrets=[secret]
            ),
        )

        payload = _detail(exc, 1)

        assert payload["dumped"] is True
        with open(payload["dump_path"], encoding="utf-8") as fh:
            dumped_text = fh.read()
        assert secret not in dumped_text
        assert json.dumps(secret)[1:-1] not in dumped_text

    @pytest.mark.parametrize(
        "secrets", [["1"], ['"'], ["id"], ["pid"], ["id", "pid"]]
    )
    def test_dump_stays_valid_json_with_schema_keys_for_hostile_secret(
        self, make_builtin_executor, tmp_path, secrets
    ):
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        rec = self._oversized_record(TRACE_VALID)
        rec["request"] = {"id": 7, "pid": 8, "note": f"a{secrets[0]}b"}
        _insert_records(path, [rec])
        exc = make_builtin_executor(
            paths=ExecutorPaths(
                results_dir=results_dir, log_store_path=path, vault_secrets=secrets
            ),
        )

        payload = _detail(exc, 1)

        assert payload["dumped"] is True
        with open(payload["dump_path"], encoding="utf-8") as fh:
            text = fh.read()

        def no_duplicates(pairs):
            keys = [k for k, _ in pairs]
            assert len(keys) == len(set(keys)), keys
            return dict(pairs)

        dumped = json.loads(text, object_pairs_hook=no_duplicates)
        assert dumped["id"] == 1
        assert {"trace", "event_type", "err", "request"} <= set(dumped)
        request_values = list(dumped["request"].values())
        assert len(request_values) == 3
        assert 7 in request_values and 8 in request_values
        assert all(secret not in str(v) for secret in secrets for v in request_values)

    def test_dump_permissions_owner_only(self, make_builtin_executor, tmp_path):
        """The dump artifact is 0600 inside a 0700 trace directory (POSIX)."""
        if os.name == "nt":
            return
        import stat
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [self._oversized_record(TRACE_VALID)])
        exc = make_builtin_executor(
            paths=ExecutorPaths(results_dir=results_dir, log_store_path=path),
        )

        payload = _detail(exc, 1)
        assert payload["dumped"] is True
        file_mode = stat.S_IMODE(os.stat(payload["dump_path"]).st_mode)
        dir_mode = stat.S_IMODE(os.stat(os.path.dirname(payload["dump_path"])).st_mode)
        assert file_mode == 0o600
        assert dir_mode == 0o700

    def test_dump_collision_counter_never_overwrites(self, make_builtin_executor, tmp_path):
        """A pre-existing log-detail-<id>.json shifts the dump to the -1 suffix
        and leaves the pre-existing file untouched."""
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [self._oversized_record(TRACE_VALID)])
        exc = make_builtin_executor(
            paths=ExecutorPaths(results_dir=results_dir, log_store_path=path),
        )
        trace_dir = os.path.join(results_dir, TRACE_VALID)
        os.makedirs(trace_dir, exist_ok=True)
        preexisting = os.path.join(trace_dir, "log-detail-1.json")
        Path(preexisting).write_text("pre-existing artifact")

        payload = _detail(exc, 1)
        assert payload["dumped"] is True
        assert payload["dump_path"].endswith("log-detail-1-1.json")
        # The pre-existing file is never overwritten.
        with open(preexisting, encoding="utf-8") as fh:
            assert fh.read() == "pre-existing artifact"

    def test_dump_temp_symlink_never_alters_outside_target(
        self, make_builtin_executor, tmp_path
    ):
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [self._oversized_record(TRACE_VALID)])
        exc = make_builtin_executor(
            paths=ExecutorPaths(results_dir=results_dir, log_store_path=path),
        )
        trace_dir = Path(results_dir) / TRACE_VALID
        trace_dir.mkdir(parents=True)
        sentinel = tmp_path / "outside-sentinel"
        sentinel.write_text("outside remains unchanged", encoding="utf-8")
        sentinel_mode = stat.S_IMODE(sentinel.stat().st_mode)
        tmp_link = trace_dir / "log-detail-1.json.tmp"
        _symlink_or_skip(tmp_link, sentinel)

        payload = _detail(exc, 1)

        expected_path = trace_dir / "log-detail-1.json"
        assert payload["dumped"] is True
        assert payload["dump_path"] == str(expected_path.resolve())
        assert json.loads(expected_path.read_text(encoding="utf-8")) == {
            **self._oversized_record(TRACE_VALID),
            "id": 1,
        }
        assert sentinel.read_text(encoding="utf-8") == "outside remains unchanged"
        assert stat.S_IMODE(sentinel.stat().st_mode) == sentinel_mode
        assert tmp_link.is_symlink()
        assert tmp_link.readlink() == sentinel
        assert not expected_path.is_symlink()
        assert list(trace_dir.glob(".log-detail-*.tmp")) == []

    def test_dump_ignores_dangling_temp_symlink(
        self, make_builtin_executor, tmp_path
    ):
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [self._oversized_record(TRACE_VALID)])
        exc = make_builtin_executor(
            paths=ExecutorPaths(results_dir=results_dir, log_store_path=path),
        )
        trace_dir = Path(results_dir) / TRACE_VALID
        trace_dir.mkdir(parents=True)
        outside_target = tmp_path / "missing-outside-target"
        tmp_link = trace_dir / "log-detail-1.json.tmp"
        _symlink_or_skip(tmp_link, outside_target)

        payload = _detail(exc, 1)

        expected_path = trace_dir / "log-detail-1.json"
        assert payload["dumped"] is True
        assert payload["dump_path"] == str(expected_path.resolve())
        assert json.loads(expected_path.read_text(encoding="utf-8")) == {
            **self._oversized_record(TRACE_VALID),
            "id": 1,
        }
        assert not outside_target.exists()
        assert tmp_link.is_symlink()
        assert tmp_link.readlink() == outside_target
        assert not expected_path.is_symlink()
        assert list(trace_dir.glob(".log-detail-*.tmp")) == []

    def test_dump_dedup_reuses_identical_artifact(self, make_builtin_executor, tmp_path):
        """A second call for the same oversized record reuses the existing dump.

        Records are immutable, so a repeat call produces byte-identical
        content: the returned dump_path must be the base name (no collision
        suffix) and the artifact content must match the record.
        """
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [self._oversized_record(TRACE_VALID)])
        exc = make_builtin_executor(
            paths=ExecutorPaths(results_dir=results_dir, log_store_path=path),
        )

        first = _detail(exc, 1)
        assert first["dumped"] is True
        assert first["dump_path"].endswith("log-detail-1.json")

        second = _detail(exc, 1)
        assert second["dumped"] is True
        assert second["dump_path"] == first["dump_path"]
        assert not second["dump_path"].endswith("log-detail-1-1.json")

        with open(second["dump_path"], encoding="utf-8") as fh:
            dumped = json.load(fh)
        assert dumped["err"] == "E" * 70000
        # No collision-suffixed sibling was created.
        trace_dir = os.path.join(results_dir, TRACE_VALID)
        assert sorted(os.listdir(trace_dir)) == ["log-detail-1.json"]

    def test_results_dir_unset_returns_preview_note(self, make_builtin_executor, tmp_path):
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [self._oversized_record(TRACE_VALID)])
        exc = make_builtin_executor(log_store_path=path)
        # Default ExecutorPaths has empty results_dir.
        assert not exc._paths.results_dir

        payload = _detail(exc, 1)
        assert payload["found"] is True
        assert payload["dumped"] is False
        assert payload["dump_path"] is None
        assert payload["record"]["err"].endswith(" chars]")
        assert payload.get("note")
        assert "65536" in payload["note"]

    def test_dump_write_failure_returns_preview_note(self, make_builtin_executor, tmp_path):
        results_dir = str(tmp_path / "afile" / "results")
        Path(tmp_path / "afile").write_text("I am a file, not a directory")
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [self._oversized_record(TRACE_VALID)])
        exc = make_builtin_executor(
            paths=ExecutorPaths(results_dir=results_dir, log_store_path=path),
        )

        payload = _detail(exc, 1)
        assert payload["found"] is True
        assert payload["dumped"] is False
        assert payload["dump_path"] is None
        assert payload["record"]["err"].endswith(" chars]")
        assert payload.get("note")

    def test_dump_encoding_failure_cleans_temporary_file(
        self, make_builtin_executor, tmp_path
    ):
        results_dir = str(tmp_path / "results")
        path = str(tmp_path / "detail.sqlite")
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA_DDL)
        extra = json.dumps(
            {"tool": "shell", "err": "E" * 70000, "invalid": "\ud800"}
        )
        conn.execute(
            """
            INSERT INTO events
            (ts, level, logger, agent, trace, prompt_id, event_type, msg, extra, search_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-07-05T10:00:00",
                "error",
                None,
                None,
                TRACE_VALID,
                None,
                "TOOL_FAILED",
                None,
                extra,
                "oversized surrogate record",
            ),
        )
        conn.commit()
        conn.close()
        exc = make_builtin_executor(
            paths=ExecutorPaths(results_dir=results_dir, log_store_path=path),
        )
        trace_dir = Path(results_dir) / TRACE_VALID
        trace_dir.mkdir(parents=True)
        sentinel = tmp_path / "outside-encoding-sentinel"
        sentinel.write_text("outside remains unchanged", encoding="utf-8")
        tmp_link = trace_dir / "log-detail-1.json.tmp"
        _symlink_or_skip(tmp_link, sentinel)

        payload = _detail(exc, 1)

        assert payload["found"] is True
        assert payload["dumped"] is False
        assert payload["dump_path"] is None
        assert payload.get("note")
        assert sentinel.read_text(encoding="utf-8") == "outside remains unchanged"
        assert tmp_link.is_symlink()
        assert tmp_link.readlink() == sentinel
        assert list(trace_dir.glob(".log-detail-*.tmp")) == []


# ---------------------------------------------------------------------------
# 5.10 — descriptor / schema content assertions
# ---------------------------------------------------------------------------

class TestLogDetailDescriptorSchema:
    def test_log_query_descriptor_teaches_budget_interplay(self):
        desc = BUILTIN_TOOLS["log_query"].description
        assert "raising 'limit' alone does not return more records" in desc
        assert "limit: 1" in desc
        assert "COUNT-probe" in desc
        assert "before_id paging" in desc
        assert "log_detail" in desc
        assert "ends when a page returns no records" in desc
        assert "NOT when retrieved counts reach total_matched" in desc
        assert "prompt_id or an explicit trace" in desc
        assert "auto-widen to ALL traces" in desc

    def test_log_detail_descriptor_covers_behavior(self):
        desc = BUILTIN_TOOLS["log_detail"].description
        assert "65536" in desc
        assert "dump_path" in desc
        # The descriptor uses typographic double quotes around the EMPTY example.
        assert "found" in desc and "false" in desc
        assert '{"id": 3108}' in desc

    def test_log_detail_schema_requires_id_integer(self):
        schema = BUILTIN_TOOL_SCHEMAS["log_detail"]
        props = schema["parameters"]["properties"]
        assert schema["parameters"]["required"] == ["id"]
        assert props["id"]["type"] == "integer"

    def test_log_query_schema_has_before_id_integer(self):
        props = BUILTIN_TOOL_SCHEMAS["log_query"]["parameters"]["properties"]
        assert "before_id" in props
        assert props["before_id"]["type"] == "integer"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestLogDetailReadTimeRedaction:
    """log_detail redacts against the CURRENT vault, not only the ingestion snapshot."""

    SECRET = "LateAddedCred42"

    def _executor(self, make_builtin_executor, tmp_path, records):
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, records)
        vault = tmp_path / "vault.toml"
        vault.write_text(f'late_key = "{self.SECRET}"\n')
        return make_builtin_executor(
            paths=ExecutorPaths(
                results_dir=str(tmp_path / "results"),
                log_store_path=path,
                vault_path=str(vault),
            ),
        )

    def test_full_fidelity_record_redacted_against_current_vault(
        self, make_builtin_executor, tmp_path
    ):
        rec = {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "error",
               "msg": f"login failed with {self.SECRET}", "err": self.SECRET}
        exc = self._executor(make_builtin_executor, tmp_path, [rec])

        payload = _detail(exc, 1)

        assert payload["dumped"] is False
        assert self.SECRET not in json.dumps(payload)

    def test_oversized_preview_redacted_against_current_vault(
        self, make_builtin_executor, tmp_path
    ):
        rec = {"ts": "2026-07-05T10:00:00", "trace": TRACE_VALID, "level": "error",
               "msg": f"login failed with {self.SECRET}", "err": "E" * 70000}
        exc = self._executor(make_builtin_executor, tmp_path, [rec])

        payload = _detail(exc, 1)

        assert payload["dumped"] is True
        assert self.SECRET not in json.dumps(payload)
        with open(payload["dump_path"], encoding="utf-8") as fh:
            assert self.SECRET not in fh.read()

    def test_unreadable_vault_falls_back_to_startup_secrets(
        self, make_builtin_executor, tmp_path
    ):
        path = str(tmp_path / "detail.sqlite")
        _insert_records(path, [{"ts": "2026-07-05T10:00:00", "trace": TRACE_A,
                                "level": "error", "msg": f"leak {self.SECRET}"}])
        exc = make_builtin_executor(
            paths=ExecutorPaths(
                log_store_path=path,
                vault_path=str(tmp_path / "missing.toml"),
                vault_secrets=[self.SECRET],
            ),
        )

        payload = _detail(exc, 1)

        assert self.SECRET not in json.dumps(payload)


def _query(exc, **filters) -> dict:
    """Call log_query through public execute() and parse the payload."""
    result = exc.execute("log_query", filters)
    assert result["success"] is True
    return json.loads(result["output"])


class TestLogQueryReadTimeRedaction:
    """log_query applies the same current-vault structural redaction as log_detail."""

    SECRET = "LateAddedCred42"

    def _records(self) -> list[dict]:
        return [
            {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "error",
             "event_type": "TOOL_FAILED", "msg": f"login failed with {self.SECRET}",
             "err": f"token={self.SECRET}", "request": {"auth": self.SECRET}},
            {"ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "error",
             "event_type": "TOOL_FAILED", "msg": f"retry with {self.SECRET}"},
        ]

    def _executor(self, make_builtin_executor, tmp_path, vault_text: str):
        path = str(tmp_path / "query.sqlite")
        _insert_records(path, self._records())
        vault = tmp_path / "vault.toml"
        vault.write_text(vault_text)
        exc = make_builtin_executor(
            paths=ExecutorPaths(log_store_path=path, vault_path=str(vault)),
            max_output=1_000_000,
        )
        return exc, vault

    def test_ordinary_query_redacts_current_vault_secret(
        self, make_builtin_executor, tmp_path
    ):
        exc, _ = self._executor(
            make_builtin_executor, tmp_path, f'late_key = "{self.SECRET}"\n'
        )

        payload = _query(exc, trace=TRACE_A, level="error")

        assert payload["count"] == 2
        assert self.SECRET not in json.dumps(payload)

    def test_before_id_pagination_redacts_current_vault_secret(
        self, make_builtin_executor, tmp_path
    ):
        exc, _ = self._executor(
            make_builtin_executor, tmp_path, f'late_key = "{self.SECRET}"\n'
        )

        payload = _query(exc, trace="*", before_id=2, level="error")

        assert payload["count"] == 1
        assert self.SECRET not in json.dumps(payload)

    def test_secret_added_mid_session_is_redacted_on_next_query(
        self, make_builtin_executor, tmp_path
    ):
        exc, vault = self._executor(make_builtin_executor, tmp_path, 'other = "x"\n')
        assert self.SECRET in json.dumps(_query(exc, trace=TRACE_A, level="error"))

        vault.write_text(f'other = "x"\nlate_key = "{self.SECRET}"\n')

        assert self.SECRET not in json.dumps(_query(exc, trace=TRACE_A, level="error"))


class TestReadTimeRedactionKeepsRecordSchema:
    """Short vault values never rename fixed record keys; nested keys still scrub."""

    def _executor(self, make_builtin_executor, tmp_path, secrets_toml: str, rec: dict):
        path = str(tmp_path / "schema.sqlite")
        _insert_records(path, [rec])
        vault = tmp_path / "vault.toml"
        vault.write_text(secrets_toml)
        return make_builtin_executor(
            paths=ExecutorPaths(log_store_path=path, vault_path=str(vault)),
            max_output=1_000_000,
        )

    def _rec(self) -> dict:
        return {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
                "event_type": "TOOL_END", "tool": "shell", "msg": "ok",
                "request": {"idsecret": 1}}

    def test_log_detail_preserves_top_level_keys_for_short_secret(
        self, make_builtin_executor, tmp_path
    ):
        exc = self._executor(
            make_builtin_executor, tmp_path, 'a = "id"\nb = "tool"\n', self._rec()
        )

        record = _detail(exc, 1)["record"]

        assert record["id"] == 1
        assert record["tool"] == "shell"
        assert "[REDACTED]" not in record

    def test_log_query_preserves_top_level_keys_for_short_secret(
        self, make_builtin_executor, tmp_path
    ):
        exc = self._executor(
            make_builtin_executor, tmp_path, 'a = "id"\nb = "tool"\n', self._rec()
        )

        record = _query(exc, trace=TRACE_A)["records"][0]

        assert record["id"] == 1
        assert record["tool"] == "shell"
        assert "[REDACTED]" not in record

    def test_nested_secret_key_is_still_scrubbed(self, make_builtin_executor, tmp_path):
        exc = self._executor(
            make_builtin_executor, tmp_path, 'a = "idsecret"\n', self._rec()
        )

        record = _detail(exc, 1)["record"]

        assert record["request"] == {"[REDACTED]": 1}

    def test_colliding_nested_keys_are_both_kept_with_suffix(
        self, make_builtin_executor, tmp_path
    ):
        rec = self._rec()
        rec["request"] = {"secretone": 1, "secrettwo": 2}
        exc = self._executor(
            make_builtin_executor, tmp_path,
            'a = "secretone"\nb = "secrettwo"\n', rec,
        )

        record = _detail(exc, 1)["record"]

        assert sorted(record["request"].values()) == [1, 2]
        assert sorted(record["request"]) == ["[REDACTED]", "[REDACTED]_1"]


@pytest.mark.parametrize("layers", [2, 3, 7])
def test_log_detail_nested_json_credential_not_recoverable(
    make_builtin_executor, tmp_path, layers
):
    """A credential serialized into JSON several times is gone at every decode depth."""
    secret = 'ab12"ba21'
    leaf = f"token={secret}"
    for _ in range(layers):
        leaf = json.dumps(leaf)
    path = str(tmp_path / "nested.sqlite")
    _insert_records(path, [{"ts": "2026-07-05T10:00:00", "trace": TRACE_A,
                            "level": "error", "msg": leaf}])
    vault = tmp_path / "vault.toml"
    vault.write_text(f"late_key = '{secret}'\n")
    exc = make_builtin_executor(
        paths=ExecutorPaths(log_store_path=path, vault_path=str(vault))
    )

    text = _detail(exc, 1)["record"]["msg"]

    for _ in range(16):
        assert secret not in text
        try:
            decoded = json.loads(text)
        except ValueError:
            break
        if not isinstance(decoded, str):
            break
        text = decoded


@pytest.mark.parametrize("layers", [1, 2, 3, 7, 8])
def test_log_detail_unicode_escaped_credential_not_recoverable(
    make_builtin_executor, tmp_path, layers
):
    """Unicode-escape spellings of quotes/backslashes are caught at every depth."""
    secret = 'ab12"ba21'
    leaf = f"token={secret}"
    for _ in range(layers):
        leaf = '"' + leaf.replace("\\", "\\u005c").replace('"', "\\u0022") + '"'
    path = str(tmp_path / "unicode.sqlite")
    _insert_records(path, [{"ts": "2026-07-05T10:00:00", "trace": TRACE_A,
                            "level": "error", "msg": leaf}])
    vault = tmp_path / "vault.toml"
    vault.write_text(f"late_key = '{secret}'\n")
    exc = make_builtin_executor(
        paths=ExecutorPaths(log_store_path=path, vault_path=str(vault))
    )

    text = _detail(exc, 1)["record"]["msg"]

    for _ in range(16):
        assert secret not in text
        try:
            decoded = json.loads(text)
        except ValueError:
            break
        if not isinstance(decoded, str):
            break
        text = decoded


# ---------------------------------------------------------------------------
# Python-repr / Python-escape / URL-percent spellings of a credential
# ---------------------------------------------------------------------------

_PYTHON_SECRET = "abc'def\"ghi"
_QUOTE_SECRET = 'ab12"ba21'


def _recovered_by_oracle_decoders(secret: str, text: str) -> bool:
    """Apply the oracle's literal_eval / unicode_escape / unquote decoders to *text*."""
    import ast
    import codecs
    from urllib.parse import unquote

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        unicode_escaped = codecs.decode(text, "unicode_escape")
    from tests.test_text_utils import _per_run_percent_variants

    candidates = [
        *_per_run_percent_variants(text),
        text,
        unquote(text),
        unquote(text, encoding="latin-1"),
        unicode_escaped,
    ]
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        value = None
    if isinstance(value, dict):
        candidates.extend(str(v) for v in value.values())
    return any(secret in candidate for candidate in candidates)


_ORACLE_CASES = [
    pytest.param(_PYTHON_SECRET, repr({"token": _PYTHON_SECRET}), id="python-repr"),
    pytest.param(_QUOTE_SECRET, r"token=ab12\x22ba21", id="python-hex-escape"),
    pytest.param(_QUOTE_SECRET, "token=ab12%22ba21", id="url-percent"),
]


def _oracle_executor(make_builtin_executor, tmp_path, secret: str, msg: str):
    path = str(tmp_path / "oracle.sqlite")
    _insert_records(path, [{"ts": "2026-07-05T10:00:00", "trace": TRACE_A,
                            "level": "error", "msg": msg}])
    vault = tmp_path / "vault.toml"
    vault.write_text(f"late_key = {json.dumps(secret)}\n")
    return make_builtin_executor(
        paths=ExecutorPaths(log_store_path=path, vault_path=str(vault)),
        max_output=1_000_000,
    )


@pytest.mark.parametrize(("secret", "msg"), _ORACLE_CASES)
def test_log_detail_python_and_url_spellings_not_recoverable(
    make_builtin_executor, tmp_path, secret, msg
):
    assert _recovered_by_oracle_decoders(secret, msg)
    exc = _oracle_executor(make_builtin_executor, tmp_path, secret, msg)

    returned = _detail(exc, 1)["record"]["msg"]

    assert not _recovered_by_oracle_decoders(secret, returned)
    assert secret not in returned


@pytest.mark.parametrize(("secret", "msg"), _ORACLE_CASES)
def test_log_query_python_and_url_spellings_not_recoverable(
    make_builtin_executor, tmp_path, secret, msg
):
    exc = _oracle_executor(make_builtin_executor, tmp_path, secret, msg)

    for filters in ({"level": "DEBUG"}, {"text": "REDACTED"}):
        payload = _query(exc, **filters)
        assert payload["records"], filters
        for record in payload["records"]:
            assert not _recovered_by_oracle_decoders(secret, record["msg"])
            assert secret not in record["msg"]


@pytest.mark.parametrize(("secret", "msg"), _ORACLE_CASES)
def test_oversized_dump_python_and_url_spellings_not_recoverable(
    make_builtin_executor, tmp_path, secret, msg
):
    path = str(tmp_path / "oracle.sqlite")
    _insert_records(path, [{"ts": "2026-07-05T10:00:00", "trace": TRACE_VALID,
                            "level": "error", "msg": msg, "err": "E" * 70_000}])
    vault = tmp_path / "vault.toml"
    vault.write_text(f"late_key = {json.dumps(secret)}\n")
    exc = make_builtin_executor(
        paths=ExecutorPaths(
            results_dir=str(tmp_path / "results"), log_store_path=path, vault_path=str(vault)
        ),
        max_output=1_000_000,
    )

    payload = _detail(exc, 1)

    assert payload["dumped"] is True
    artifact = Path(payload["dump_path"]).read_text(encoding="utf-8")
    assert not _recovered_by_oracle_decoders(secret, json.loads(artifact)["msg"])
    assert secret not in artifact


@pytest.mark.parametrize(
    ("secret", "printed"),
    [
        pytest.param(_QUOTE_SECRET, r"token=ab12\x22ba21", id="python-hex-escape"),
        pytest.param(_QUOTE_SECRET, "token=ab12%22ba21", id="url-percent"),
        pytest.param(_PYTHON_SECRET, repr({"token": _PYTHON_SECRET}), id="python-repr"),
    ],
)
def test_shell_artifact_python_and_url_spellings_not_recoverable(
    make_builtin_executor, tmp_path, monkeypatch, secret, printed
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    exc = make_builtin_executor(
        max_output=50,
        data_dir=str(tmp_path),
        agent_name="testagent",
        vault_secrets=[secret],
    )
    exc.conversation_id = "testconv"
    script = f"print({printed!r}); print('padding ' * 30)"

    result = exc.execute("shell", {"command": f"python3 -c {json.dumps(script)}"})

    assert result["success"] is True
    content = Path(result["full_log_path"]).read_text(encoding="utf-8")
    assert printed.split("=")[0] in content or "[REDACTED]" in content
    assert not _recovered_by_oracle_decoders(secret, content)
    assert secret not in content


def test_short_secret_is_redacted_by_log_detail(make_builtin_executor, tmp_path):
    """No read-time minimum length: a two-character vault value is redacted."""
    exc = _oracle_executor(make_builtin_executor, tmp_path, "ab", "pw=ab ok")

    assert _detail(exc, 1)["record"]["msg"] == "pw=[REDACTED] ok"


def test_short_secret_may_replace_a_whole_coincidentally_encoded_line(
    make_builtin_executor, tmp_path
):
    """Accepted tradeoff: an encoded short secret erases the whole line."""
    exc = _oracle_executor(
        make_builtin_executor, tmp_path, "a\n", r'diagnostic {"key":"\u0061\n"}'
    )

    assert _detail(exc, 1)["record"]["msg"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# Oracle round 6: CR/CRLF continuations, Latin-1 percent, malformed \N{...}
# ---------------------------------------------------------------------------

_CR_SECRET = "ab12ba21"
_LATIN1_SECRET = "p\u00c3\u00a9ssword"

_ROUND6_CASES = [
    pytest.param(_CR_SECRET, "{'token': 'ab12" + "\\" + "\r\n" + "ba21'}", id="crlf-continuation"),
    pytest.param(_CR_SECRET, "{'token': 'ab12" + "\\" + "\r" + "ba21'}", id="cr-continuation"),
    pytest.param(_LATIN1_SECRET, "token=p%C3%A9ssword", id="latin1-percent"),
]
_MALFORMED_MSG = r"ordinary diagnostic \N{\uD800}"


@pytest.mark.parametrize(("secret", "msg"), _ROUND6_CASES)
def test_log_detail_round6_spellings_not_recoverable(make_builtin_executor, tmp_path, secret, msg):
    assert _recovered_by_oracle_decoders(secret, msg)
    exc = _oracle_executor(make_builtin_executor, tmp_path, secret, msg)

    returned = _detail(exc, 1)["record"]["msg"]

    assert not _recovered_by_oracle_decoders(secret, returned)
    assert secret not in returned


@pytest.mark.parametrize(("secret", "msg"), _ROUND6_CASES)
def test_log_query_round6_spellings_not_recoverable(make_builtin_executor, tmp_path, secret, msg):
    exc = _oracle_executor(make_builtin_executor, tmp_path, secret, msg)

    for filters in ({"level": "DEBUG"}, {"text": "REDACTED"}):
        payload = _query(exc, **filters)
        assert payload["records"], filters
        for record in payload["records"]:
            assert not _recovered_by_oracle_decoders(secret, record["msg"])


@pytest.mark.parametrize(("secret", "msg"), _ROUND6_CASES)
def test_oversized_dump_round6_spellings_not_recoverable(
    make_builtin_executor, tmp_path, secret, msg
):
    path = str(tmp_path / "oracle.sqlite")
    _insert_records(path, [{"ts": "2026-07-05T10:00:00", "trace": TRACE_VALID,
                            "level": "error", "msg": msg, "err": "E" * 70_000}])
    vault = tmp_path / "vault.toml"
    vault.write_text(f"late_key = {json.dumps(secret)}\n")
    exc = make_builtin_executor(
        paths=ExecutorPaths(
            results_dir=str(tmp_path / "results"), log_store_path=path, vault_path=str(vault)
        ),
        max_output=1_000_000,
    )

    payload = _detail(exc, 1)

    assert payload["dumped"] is True
    artifact = Path(payload["dump_path"]).read_text(encoding="utf-8")
    assert not _recovered_by_oracle_decoders(secret, json.loads(artifact)["msg"])


@pytest.mark.parametrize(
    ("secret", "printed"),
    [
        pytest.param(_LATIN1_SECRET, "token=p%C3%A9ssword", id="latin1-percent"),
    ],
)
def test_shell_artifact_round6_spellings_not_recoverable(
    make_builtin_executor, tmp_path, monkeypatch, secret, printed
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    exc = make_builtin_executor(
        max_output=50,
        data_dir=str(tmp_path),
        agent_name="testagent",
        vault_secrets=[secret],
    )
    exc.conversation_id = "testconv"
    script = f"print({printed!r}); print('padding ' * 30)"

    result = exc.execute("shell", {"command": f"python3 -c {json.dumps(script)}"})

    assert result["success"] is True
    content = Path(result["full_log_path"]).read_bytes().decode("utf-8")
    assert not _recovered_by_oracle_decoders(secret, content)


def _malformed_name_records(secret: str) -> list[dict]:
    return [
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "error", "msg": _MALFORMED_MSG},
        {"ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "error",
         "msg": json.dumps(_MALFORMED_MSG)},
        {"ts": "2026-07-05T10:00:02", "trace": TRACE_A, "level": "error",
         "msg": f"token={secret}"},
    ]


def _malformed_executor(make_builtin_executor, tmp_path, secret: str):
    path = str(tmp_path / "malformed.sqlite")
    _insert_records(path, _malformed_name_records(secret))
    vault = tmp_path / "vault.toml"
    vault.write_text(f"late_key = {json.dumps(secret)}\n")
    return make_builtin_executor(
        paths=ExecutorPaths(log_store_path=path, vault_path=str(vault)),
        max_output=1_000_000,
    )


def test_log_detail_malformed_named_escape_is_processed(make_builtin_executor, tmp_path):
    exc = _malformed_executor(make_builtin_executor, tmp_path, "ab12ba21")

    assert _detail(exc, 1)["record"]["msg"] == _MALFORMED_MSG
    assert _detail(exc, 2)["record"]["msg"] == json.dumps(_MALFORMED_MSG)


def test_log_query_malformed_named_escape_does_not_break_credential_redaction(
    make_builtin_executor, tmp_path
):
    exc = _malformed_executor(make_builtin_executor, tmp_path, "ab12ba21")

    msgs = [r["msg"] for r in _query(exc, level="DEBUG")["records"]]

    assert "token=[REDACTED]" in msgs
    assert _MALFORMED_MSG in msgs
    assert not any("ab12ba21" in m for m in msgs)


# ---------------------------------------------------------------------------
# Oracle round 7: independent per-run percent readings
# ---------------------------------------------------------------------------

_SPLIT_SECRET = "prefix\u00e9-\u00c3\u00a9suffix"
_SPLIT_MSG = "token=prefix%C3%A9-%C3%A9suffix"


def test_log_detail_independent_percent_run_readings_not_recoverable(
    make_builtin_executor, tmp_path
):
    assert _recovered_by_oracle_decoders(_SPLIT_SECRET, _SPLIT_MSG)
    exc = _oracle_executor(make_builtin_executor, tmp_path, _SPLIT_SECRET, _SPLIT_MSG)

    returned = _detail(exc, 1)["record"]["msg"]

    assert not _recovered_by_oracle_decoders(_SPLIT_SECRET, returned)


def test_log_query_independent_percent_run_readings_not_recoverable(
    make_builtin_executor, tmp_path
):
    exc = _oracle_executor(make_builtin_executor, tmp_path, _SPLIT_SECRET, _SPLIT_MSG)

    for filters in ({"level": "DEBUG"}, {"text": "REDACTED"}):
        payload = _query(exc, **filters)
        assert payload["records"], filters
        for record in payload["records"]:
            assert not _recovered_by_oracle_decoders(_SPLIT_SECRET, record["msg"])


def test_oversized_dump_independent_percent_run_readings_not_recoverable(
    make_builtin_executor, tmp_path
):
    path = str(tmp_path / "oracle.sqlite")
    _insert_records(path, [{"ts": "2026-07-05T10:00:00", "trace": TRACE_VALID,
                            "level": "error", "msg": _SPLIT_MSG, "err": "E" * 70_000}])
    vault = tmp_path / "vault.toml"
    vault.write_text(f"late_key = {json.dumps(_SPLIT_SECRET)}\n")
    exc = make_builtin_executor(
        paths=ExecutorPaths(
            results_dir=str(tmp_path / "results"), log_store_path=path, vault_path=str(vault)
        ),
        max_output=1_000_000,
    )

    payload = _detail(exc, 1)

    assert payload["dumped"] is True
    artifact = Path(payload["dump_path"]).read_text(encoding="utf-8")
    assert not _recovered_by_oracle_decoders(_SPLIT_SECRET, json.loads(artifact)["msg"])


def test_shell_artifact_independent_percent_run_readings_not_recoverable(
    make_builtin_executor, tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    exc = make_builtin_executor(
        max_output=50,
        data_dir=str(tmp_path),
        agent_name="testagent",
        vault_secrets=[_SPLIT_SECRET],
    )
    exc.conversation_id = "testconv"
    script = f"print({_SPLIT_MSG!r}); print('padding ' * 30)"

    result = exc.execute("shell", {"command": f"python3 -c {json.dumps(script)}"})

    assert result["success"] is True
    content = Path(result["full_log_path"]).read_bytes().decode("utf-8")
    assert not _recovered_by_oracle_decoders(_SPLIT_SECRET, content)


def test_log_query_honest_multilingual_url_survives_read_time_redaction(
    make_builtin_executor, tmp_path
):
    url = "GET /wiki/%E6%97%A5%E6%9C%AC%E8%AA%9E/caf%C3%A9?a=%C3%B1o%C3%B1o&b=%E4%B8%AD HTTP/1.1"
    exc = _oracle_executor(make_builtin_executor, tmp_path, _SPLIT_SECRET, url)

    assert _detail(exc, 1)["record"]["msg"] == url


# ---------------------------------------------------------------------------
# Oracle round 8: invalid UTF-8 percent bytes read with U+FFFD replacement
# ---------------------------------------------------------------------------

_REPLACEMENT_CASES = [
    pytest.param("prefix\ufffd\ufffdsuffix", "token=prefix%C0%AFsuffix", id="overlong-C0AF"),
    pytest.param("prefix\ufffdsuffix", "token=prefix%FFsuffix", id="byte-FF"),
    pytest.param("prefix\ufffd\ufffd\ufffdsuffix", "token=prefix%ED%A0%80suffix", id="surrogate"),
    pytest.param(
        "prefix\ufffd\ufffd\ufffd\ufffdsuffix", "token=prefix%F4%90%80%80suffix", id="out-of-range"
    ),
]


@pytest.mark.parametrize(("secret", "msg"), _REPLACEMENT_CASES)
def test_log_detail_replacement_reading_not_recoverable(make_builtin_executor, tmp_path, secret, msg):
    assert _recovered_by_oracle_decoders(secret, msg)
    exc = _oracle_executor(make_builtin_executor, tmp_path, secret, msg)

    returned = _detail(exc, 1)["record"]["msg"]

    assert not _recovered_by_oracle_decoders(secret, returned)


@pytest.mark.parametrize(("secret", "msg"), _REPLACEMENT_CASES)
def test_log_query_replacement_reading_not_recoverable(make_builtin_executor, tmp_path, secret, msg):
    exc = _oracle_executor(make_builtin_executor, tmp_path, secret, msg)

    for filters in ({"level": "DEBUG"}, {"text": "REDACTED"}):
        payload = _query(exc, **filters)
        assert payload["records"], filters
        for record in payload["records"]:
            assert not _recovered_by_oracle_decoders(secret, record["msg"])


@pytest.mark.parametrize(("secret", "msg"), _REPLACEMENT_CASES)
def test_oversized_dump_replacement_reading_not_recoverable(
    make_builtin_executor, tmp_path, secret, msg
):
    path = str(tmp_path / "oracle.sqlite")
    _insert_records(path, [{"ts": "2026-07-05T10:00:00", "trace": TRACE_VALID,
                            "level": "error", "msg": msg, "err": "E" * 70_000}])
    vault = tmp_path / "vault.toml"
    vault.write_text(f"late_key = {json.dumps(secret)}\n")
    exc = make_builtin_executor(
        paths=ExecutorPaths(
            results_dir=str(tmp_path / "results"), log_store_path=path, vault_path=str(vault)
        ),
        max_output=1_000_000,
    )

    payload = _detail(exc, 1)

    assert payload["dumped"] is True
    artifact = Path(payload["dump_path"]).read_text(encoding="utf-8")
    assert not _recovered_by_oracle_decoders(secret, json.loads(artifact)["msg"])


@pytest.mark.parametrize(("secret", "msg"), _REPLACEMENT_CASES)
def test_shell_artifact_replacement_reading_not_recoverable(
    make_builtin_executor, tmp_path, monkeypatch, secret, msg
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    exc = make_builtin_executor(
        max_output=50,
        data_dir=str(tmp_path),
        agent_name="testagent",
        vault_secrets=[secret],
    )
    exc.conversation_id = "testconv"
    script = f"print({msg!r}); print('padding ' * 30)"

    result = exc.execute("shell", {"command": f"python3 -c {json.dumps(script)}"})

    assert result["success"] is True
    content = Path(result["full_log_path"]).read_bytes().decode("utf-8")
    assert not _recovered_by_oracle_decoders(secret, content)


def test_log_detail_malformed_percent_bytes_pass_through_without_a_replacement_secret(
    make_builtin_executor, tmp_path
):
    msg = "GET /x?a=%C0%AF&b=%FF&c=%ED%A0%80 HTTP/1.1"
    exc = _oracle_executor(make_builtin_executor, tmp_path, "ab12ba21", msg)

    assert _detail(exc, 1)["record"]["msg"] == msg
