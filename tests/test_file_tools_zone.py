"""Tests for zone-aware confirmation logic in FileTools (builtin_tools/files.py)."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

from builtin_executor import BuiltinExecutor
from builtin_tools.access_control import GrantTracker, ZoneClassification
from builtin_tools.files import FileTools


def _make_file_tools(zone: ZoneClassification) -> tuple[FileTools, MagicMock, MagicMock]:
    checker = MagicMock()
    checker.classify.return_value = zone

    owner = MagicMock()
    owner.trusted_zone_checker = checker
    owner.grant_tracker = GrantTracker()
    owner._requires_confirmation.return_value = {"requires_confirmation": True, "token": "tok"}

    return FileTools(owner), owner, checker


def _make_file_tools_diff(
    zone_a: ZoneClassification, zone_b: ZoneClassification
) -> tuple[FileTools, MagicMock]:
    """Factory for file_diff tests where path_a and path_b may have different zones."""
    checker = MagicMock()
    checker.classify.side_effect = [zone_a, zone_b]

    owner = MagicMock()
    owner.trusted_zone_checker = checker
    owner.grant_tracker = GrantTracker()
    owner._requires_confirmation.return_value = {"requires_confirmation": True, "token": "tok"}

    return FileTools(owner), owner


def _make_no_checker_file_tools() -> tuple[FileTools, MagicMock]:
    """Factory for checker=None (legacy) tests."""
    owner = MagicMock()
    owner.trusted_zone_checker = None
    owner.grant_tracker = GrantTracker()
    owner._requires_confirmation.return_value = {"requires_confirmation": True, "token": "tok"}

    return FileTools(owner), owner


class TestFileWriteZone:
    def test_file_write_trusted_zone_no_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "output.txt")
            ft, owner, _ = _make_file_tools(ZoneClassification.TRUSTED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_write({"path": path, "content": "x"})
            owner._requires_confirmation.assert_not_called()
            assert result["success"] is True

    def test_file_write_unrecognised_zone_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "output.txt")
            ft, owner, _ = _make_file_tools(ZoneClassification.UNRECOGNISED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_write({"path": path, "content": "x"})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert call_kwargs.get("zone_path") == os.path.realpath(path)

    def test_file_write_sensitive_in_trusted_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "output.txt")
            ft, owner, _ = _make_file_tools(ZoneClassification.TRUSTED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(True, "sensitive")):
                result = ft._exec_file_write({"path": path, "content": "x"})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert "zone_path" not in call_kwargs

    def test_file_write_read_only_trusted_confirms(self):
        """Write to a read-only trusted dir requires confirmation."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ft, owner, checker = _make_file_tools(ZoneClassification.TRUSTED)
            # Override: make checker return UNRECOGNISED for write to r-mode dir
            checker.classify.return_value = ZoneClassification.UNRECOGNISED
            path = os.path.join(tmp_dir, "file.txt")
            args = {"path": path, "content": "data"}
            result = ft._exec_file_write(args)
            assert result.get("requires_confirmation") is True


class TestFileReadZone:
    def test_file_read_agent_internal_path_confirms(self):
        """Agent-internal paths (data/, tools/) are UNRECOGNISED in 3-zone model."""
        ft, owner, _ = _make_file_tools(ZoneClassification.UNRECOGNISED)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            args = {"path": path}
            result = ft._exec_file_read(args)
            assert result.get("requires_confirmation") is True
        finally:
            os.unlink(path)

    def test_file_read_trusted_no_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.txt")
            with open(path, "w") as f:
                f.write("hello")
            ft, owner, _ = _make_file_tools(ZoneClassification.TRUSTED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_read({"path": path})
            owner._requires_confirmation.assert_not_called()
            assert result["success"] is True

    def test_file_read_unrecognised_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.txt")
            ft, owner, _ = _make_file_tools(ZoneClassification.UNRECOGNISED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_read({"path": path})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert call_kwargs.get("zone_path")

    def test_file_read_sensitive_trusted_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.txt")
            ft, owner, _ = _make_file_tools(ZoneClassification.TRUSTED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(True, "sensitive")):
                result = ft._exec_file_read({"path": path})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert not call_kwargs.get("zone_path")

    def test_file_read_request_grant_no_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.txt")
            with open(path, "w") as f:
                f.write("hello")
            ft, owner, _ = _make_file_tools(ZoneClassification.REQUEST_GRANT)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_read({"path": path})
            owner._requires_confirmation.assert_not_called()
            assert result["success"] is True

    def test_file_read_no_checker_sensitive_confirms(self):
        ft, owner = _make_no_checker_file_tools()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.txt")
            with patch("builtin_tools.files._is_sensitive_path", return_value=(True, "sensitive")):
                result = ft._exec_file_read({"path": path})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True

    def test_file_read_no_checker_non_sensitive_reads(self):
        ft, owner = _make_no_checker_file_tools()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.txt")
            with open(path, "w") as f:
                f.write("hello")
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_read({"path": path})
        owner._requires_confirmation.assert_not_called()
        assert result["success"] is True


class TestFilePatchZone:
    def _write_file(self, tmp_dir: str, content: str = "hello world") -> str:
        path = os.path.join(tmp_dir, "target.txt")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_file_patch_unrecognised_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp)
            ft, owner, _ = _make_file_tools(ZoneClassification.UNRECOGNISED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_patch({"path": path, "old_str": "hello", "new_str": "hi"})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert call_kwargs.get("zone_path")

    def test_file_patch_trusted_no_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp)
            ft, owner, _ = _make_file_tools(ZoneClassification.TRUSTED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_patch({"path": path, "old_str": "hello", "new_str": "hi"})
            owner._requires_confirmation.assert_not_called()
            assert result["success"] is True

    def test_file_patch_sensitive_trusted_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp)
            ft, owner, _ = _make_file_tools(ZoneClassification.TRUSTED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(True, "sensitive")):
                result = ft._exec_file_patch({"path": path, "old_str": "hello", "new_str": "hi"})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert not call_kwargs.get("zone_path")

    def test_file_patch_no_checker_confirms(self):
        ft, owner = _make_no_checker_file_tools()
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_patch({"path": path, "old_str": "hello", "new_str": "hi"})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True


class TestFileDiffZone:
    def _write_file(self, tmp_dir: str, name: str, content: str = "hello\n") -> str:
        path = os.path.join(tmp_dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_file_diff_both_trusted_no_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_a = self._write_file(tmp, "a.txt", "hello\n")
            path_b = self._write_file(tmp, "b.txt", "hello\n")
            ft, owner = _make_file_tools_diff(ZoneClassification.TRUSTED, ZoneClassification.TRUSTED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
            owner._requires_confirmation.assert_not_called()
            assert result["success"] is True

    def test_file_diff_unrecognised_a_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_a = self._write_file(tmp, "a.txt")
            path_b = self._write_file(tmp, "b.txt")
            ft, owner = _make_file_tools_diff(ZoneClassification.UNRECOGNISED, ZoneClassification.TRUSTED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert call_kwargs.get("zone_path")

    def test_file_diff_unrecognised_b_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_a = self._write_file(tmp, "a.txt")
            path_b = self._write_file(tmp, "b.txt")
            ft, owner = _make_file_tools_diff(ZoneClassification.TRUSTED, ZoneClassification.UNRECOGNISED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert call_kwargs.get("zone_path")

    def test_file_diff_sensitive_trusted_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_a = self._write_file(tmp, "a.txt")
            path_b = self._write_file(tmp, "b.txt")
            ft, owner = _make_file_tools_diff(ZoneClassification.TRUSTED, ZoneClassification.TRUSTED)

            real_path_a = os.path.realpath(path_a)

            def _sensitive(path: str) -> tuple[bool, str]:
                return (True, "sensitive") if path == real_path_a else (False, "")

            with patch("builtin_tools.files._is_sensitive_path", side_effect=_sensitive):
                result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True
        call_kwargs = owner._requires_confirmation.call_args.kwargs
        assert not call_kwargs.get("zone_path")

    def test_file_diff_no_checker_sensitive_confirms(self):
        ft, owner = _make_no_checker_file_tools()
        with tempfile.TemporaryDirectory() as tmp:
            path_a = self._write_file(tmp, "a.txt")
            path_b = self._write_file(tmp, "b.txt")
            real_path_a = os.path.realpath(path_a)

            def _sensitive(path: str) -> tuple[bool, str]:
                return (True, "sensitive") if path == real_path_a else (False, "")

            with patch("builtin_tools.files._is_sensitive_path", side_effect=_sensitive):
                result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True

    def test_file_diff_only_path_b_unrecognised_uses_path_b_zone_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_a = self._write_file(tmp, "a.txt")
            path_b = self._write_file(tmp, "b.txt")
            ft, owner = _make_file_tools_diff(ZoneClassification.TRUSTED, ZoneClassification.UNRECOGNISED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True
        call_kwargs = owner._requires_confirmation.call_args.kwargs
        assert call_kwargs.get("zone_path") == os.path.realpath(path_b)


class TestFileSendZone:
    def _write_file(self, tmp_dir: str, name: str = "file.txt", content: str = "data") -> str:
        path = os.path.join(tmp_dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_file_send_trusted_no_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp)
            ft, owner, _ = _make_file_tools(ZoneClassification.TRUSTED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_send({"path": path})
            owner._requires_confirmation.assert_not_called()
            assert result["success"] is True

    def test_file_send_unrecognised_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp)
            ft, owner, _ = _make_file_tools(ZoneClassification.UNRECOGNISED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_send({"path": path})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert call_kwargs.get("zone_path")

    def test_file_send_sensitive_trusted_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp)
            ft, owner, _ = _make_file_tools(ZoneClassification.TRUSTED)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(True, "sensitive")):
                result = ft._exec_file_send({"path": path})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert not call_kwargs.get("zone_path")

    def test_file_send_no_checker_sensitive_confirms(self):
        ft, owner = _make_no_checker_file_tools()
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(True, "sensitive")):
                result = ft._exec_file_send({"path": path})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True


class TestConfirmRoundTrip:
    """Integration: stage confirmation via execute(), then confirm(token) must use _run_table."""

    def _write_file(self, tmp_dir: str, name: str, content: str = "hello\n") -> str:
        path = os.path.join(tmp_dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def _make_executor(self) -> BuiltinExecutor:
        """Real BuiltinExecutor with a checker that classifies all paths as UNRECOGNISED."""
        from builtin_tools.access_control import GrantTracker
        builtin = BuiltinExecutor()
        checker = MagicMock()
        checker.classify.return_value = ZoneClassification.UNRECOGNISED
        builtin.trusted_zone_checker = checker  # type: ignore[assignment]  # type: ignore[assignment]
        builtin.push_grant_tracker(GrantTracker())
        return builtin

    def test_file_diff_confirm_executes(self):
        """file_diff: confirm(token) must execute successfully, not return 'Unknown built-in'."""
        with tempfile.TemporaryDirectory() as tmp:
            path_a = self._write_file(tmp, "a.txt", "hello\n")
            path_b = self._write_file(tmp, "b.txt", "world\n")
            builtin = self._make_executor()
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                staged = builtin.execute("file_diff", {"path_a": path_a, "path_b": path_b})
            assert staged.get("requires_confirmation") is True
            token = staged["token"]
            result = builtin.confirm(token)
            assert result.get("success") is True
            assert result.get("error") != "Unknown built-in"

    def test_file_send_confirm_executes(self):
        """file_send: confirm(token) must execute successfully, not return 'Unknown built-in'."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp, "data.txt", "payload\n")
            builtin = self._make_executor()
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                staged = builtin.execute("file_send", {"path": path})
            assert staged.get("requires_confirmation") is True
            token = staged["token"]
            result = builtin.confirm(token)
            assert result.get("success") is True
            assert result.get("error") != "Unknown built-in"

    def test_file_write_confirm_executes(self):
        """file_write: sanity check that the existing _run_table entry still works."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.txt")
            builtin = self._make_executor()
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                staged = builtin.execute("file_write", {"path": path, "content": "x"})
            assert staged.get("requires_confirmation") is True
            token = staged["token"]
            result = builtin.confirm(token)
            assert result.get("success") is True
            assert result.get("error") != "Unknown built-in"

    def test_file_patch_confirm_executes(self):
        """file_patch: sanity check that the existing _run_table entry still works."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp, "target.txt", "hello world")
            builtin = self._make_executor()
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                staged = builtin.execute(
                    "file_patch", {"path": path, "old_str": "hello", "new_str": "hi"}
                )
            assert staged.get("requires_confirmation") is True
            token = staged["token"]
            result = builtin.confirm(token)
            assert result.get("success") is True
            assert result.get("error") != "Unknown built-in"
