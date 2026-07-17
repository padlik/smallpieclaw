"""Tests for zone-aware confirmation logic in FileTools (builtin_tools/files.py)."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

from builtin_tools.access_control import GrantTracker, ZoneClassification
from builtin_tools.files import FileTools


def _make_file_tools(zone: ZoneClassification) -> tuple[FileTools, MagicMock, MagicMock]:
    checker = MagicMock()
    checker.classify.return_value = zone
    checker.is_write_protected_internal.return_value = False

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
    checker.is_write_protected_internal.return_value = False

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

    def test_file_write_internal_zone_no_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "output.txt")
            ft, owner, _ = _make_file_tools(ZoneClassification.INTERNAL)
            ft._exec_file_write({"path": path, "content": "x"})
            owner._requires_confirmation.assert_not_called()

    def test_file_write_internal_write_protected_confirms(self):
        """FIX 1: writes to write-protected internal dirs (tools/) require confirmation."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tools", "my_tool.sh")
            ft, owner, checker = _make_file_tools(ZoneClassification.INTERNAL)
            checker.is_write_protected_internal.return_value = True
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_write({"path": path, "content": "x"})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert call_kwargs.get("zone_path") == os.path.realpath(path)


class TestFileReadZone:
    def test_file_read_internal_no_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.txt")
            with open(path, "w") as f:
                f.write("hello")
            ft, owner, _ = _make_file_tools(ZoneClassification.INTERNAL)
            result = ft._exec_file_read({"path": path})
            owner._requires_confirmation.assert_not_called()
            assert result["success"] is True

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

    def test_file_patch_internal_no_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp)
            ft, owner, _ = _make_file_tools(ZoneClassification.INTERNAL)
            with patch("builtin_tools.files._is_sensitive_path", return_value=(False, "")):
                result = ft._exec_file_patch({"path": path, "old_str": "hello", "new_str": "hi"})
            owner._requires_confirmation.assert_not_called()
            assert result["success"] is True

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

            def _sensitive(path: str) -> tuple[bool, str]:
                return (True, "sensitive") if path == path_a else (False, "")

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

            def _sensitive(path: str) -> tuple[bool, str]:
                return (True, "sensitive") if path == path_a else (False, "")

            with patch("builtin_tools.files._is_sensitive_path", side_effect=_sensitive):
                result = ft._exec_file_diff({"path_a": path_a, "path_b": path_b})
        owner._requires_confirmation.assert_called_once()
        assert result.get("requires_confirmation") is True


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

    def test_file_send_internal_sensitive_confirms(self):
        """FIX 1: INTERNAL zone no longer bypasses the sensitive path check."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_file(tmp)
            ft, owner, checker = _make_file_tools(ZoneClassification.INTERNAL)
            checker.is_write_protected_internal.return_value = False
            with patch("builtin_tools.files._is_sensitive_path", return_value=(True, "sensitive file")):
                result = ft._exec_file_send({"path": path})
            owner._requires_confirmation.assert_called_once()
            assert result.get("requires_confirmation") is True
            # sensitive (non-UNRECOGNISED) confirm has no zone_path
            call_kwargs = owner._requires_confirmation.call_args.kwargs
            assert not call_kwargs.get("zone_path")
