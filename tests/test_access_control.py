"""Tests for builtin_tools/access_control.py zone classification and persistence."""

from __future__ import annotations

import os
import tempfile
import threading

import pytest

from builtin_tools.access_control import (
    GrantTracker,
    TrustedZoneChecker,
    ZoneClassification,
    _is_contained,
)
from config_schema import PathsConfig


def _make_paths(tmp: str, workspace: str | None = None) -> PathsConfig:
    return PathsConfig(
        tools_dir=os.path.join(tmp, "tools"),
        generated_tools_dir=os.path.join(tmp, "tools_generated"),
        data_dir=os.path.join(tmp, "data"),
        skills_dir=os.path.join(tmp, "skills"),
        prompts_dir=os.path.join(tmp, "prompts"),
        downloads_dir=os.path.join(tmp, "downloads"),
        workspace_dir=workspace or os.path.join(tmp, "workspace"),
        tmp_dir=os.path.join(tmp, "tmp"),
    )


def _make_checker(tmp: str, workspace: str | None = None) -> TrustedZoneChecker:
    paths = _make_paths(tmp, workspace)
    for d in [
        paths.tools_dir,
        paths.generated_tools_dir,
        paths.data_dir,
        paths.skills_dir,
        paths.prompts_dir,
        paths.downloads_dir,
        paths.workspace_dir,
        paths.tmp_dir,
    ]:
        os.makedirs(d, exist_ok=True)
    return TrustedZoneChecker(paths_config=paths, data_dir=paths.data_dir, agent_name="test-agent")


class TestClassify:
    def test_internal_path_is_internal(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            path = os.path.join(paths.tools_dir, "my_tool.sh")
            assert checker.classify(path) == ZoneClassification.INTERNAL

    def test_default_trusted_workspace_is_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            path = os.path.join(paths.workspace_dir, "readme.md")
            assert checker.classify(path) == ZoneClassification.TRUSTED

    def test_default_trusted_downloads_is_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            path = os.path.join(paths.downloads_dir, "file.zip")
            assert checker.classify(path) == ZoneClassification.TRUSTED

    def test_unrecognised_path_is_unrecognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            assert checker.classify("/nonexistent/random/path/file.txt") == ZoneClassification.UNRECOGNISED

    def test_user_added_trusted_is_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir)
                path = os.path.join(some_dir, "file.txt")
                assert checker.classify(path) == ZoneClassification.TRUSTED

    def test_request_grant_is_request_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                file_path = os.path.join(some_dir, "file.txt")
                gt = GrantTracker()
                gt.add(file_path)
                assert checker.classify(file_path, request_grants=gt.snapshot()) == ZoneClassification.REQUEST_GRANT

    def test_symlink_outside_trusted_is_unrecognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as external:
                checker = _make_checker(tmp)
                paths = _make_paths(tmp)

                external_file = os.path.join(external, "secret.txt")
                with open(external_file, "w") as f:
                    f.write("secret")

                symlink_path = os.path.join(paths.workspace_dir, "link.txt")
                os.symlink(external_file, symlink_path)

                assert checker.classify(symlink_path) == ZoneClassification.UNRECOGNISED

    def test_dotdot_traversal_is_unrecognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            path = os.path.join(paths.workspace_dir, "..", "..", "etc", "passwd")
            assert checker.classify(path) == ZoneClassification.UNRECOGNISED

    def test_trusted_dirs_json_is_unrecognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            trusted_dirs_json = os.path.join(paths.data_dir, "trusted_dirs.json")
            assert checker.classify(trusted_dirs_json) == ZoneClassification.UNRECOGNISED


class TestSiblingPrefixContainment:
    def test_sibling_with_shared_prefix_is_unrecognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = os.path.join(tmp, "shared")
            shared_evil = os.path.join(tmp, "shared-evil")
            os.makedirs(shared)
            os.makedirs(shared_evil)
            checker = _make_checker(tmp, workspace=shared)
            result = checker.classify(os.path.join(shared_evil, "secret.txt"))
            assert result == ZoneClassification.UNRECOGNISED

    def test_exact_zone_dir_file_is_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = os.path.join(tmp, "shared")
            os.makedirs(shared, exist_ok=True)
            checker = _make_checker(tmp, workspace=shared)
            result = checker.classify(os.path.join(shared, "file.txt"))
            assert result == ZoneClassification.TRUSTED


class TestGrantTracker:
    def test_grant_tracker_add_and_snapshot(self):
        with tempfile.TemporaryDirectory() as some_dir:
            gt = GrantTracker()
            file_path = os.path.join(some_dir, "report.txt")
            gt.add(file_path)
            grants = gt.snapshot()
            assert os.path.dirname(os.path.realpath(file_path)) in grants

    def test_grant_tracker_reset(self):
        with tempfile.TemporaryDirectory() as some_dir:
            gt = GrantTracker()
            gt.add(os.path.join(some_dir, "file.txt"))
            gt.reset()
            assert len(gt.snapshot()) == 0

    def test_grant_tracker_cross_thread(self):
        """Grants written from one thread are visible from another (shared set, not thread-local)."""
        with tempfile.TemporaryDirectory() as some_dir:
            gt = GrantTracker()
            file_path = os.path.join(some_dir, "file.txt")
            result: list = []

            def _add() -> None:
                gt.add(file_path)

            def _read() -> None:
                result.append(gt.snapshot())

            t1 = threading.Thread(target=_add)
            t1.start()
            t1.join()
            t2 = threading.Thread(target=_read)
            t2.start()
            t2.join()

            assert len(result) == 1
            assert os.path.dirname(os.path.realpath(file_path)) in result[0]


class TestClassifyWithRequestGrants:
    def test_classify_request_grant_via_param(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as external:
                checker = _make_checker(tmp)
                file_path = os.path.join(external, "file.txt")
                gt = GrantTracker()
                gt.add(file_path)
                result = checker.classify(file_path, request_grants=gt.snapshot())
                assert result == ZoneClassification.REQUEST_GRANT

    def test_classify_no_request_grant_unrecognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as external:
                checker = _make_checker(tmp)
                file_path = os.path.join(external, "file.txt")
                result = checker.classify(file_path, request_grants=frozenset())
                assert result == ZoneClassification.UNRECOGNISED

    def test_grant_active_during_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as external:
                checker = _make_checker(tmp)
                path = os.path.join(external, "report.txt")
                gt = GrantTracker()
                gt.add(path)
                assert checker.classify(path, request_grants=gt.snapshot()) == ZoneClassification.REQUEST_GRANT

    def test_grant_cleared_after_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as external:
                checker = _make_checker(tmp)
                path = os.path.join(external, "report.txt")
                gt = GrantTracker()
                gt.add(path)
                gt.reset()
                assert checker.classify(path, request_grants=gt.snapshot()) == ZoneClassification.UNRECOGNISED

    def test_grant_does_not_cover_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as external:
                checker = _make_checker(tmp)
                reports_dir = os.path.join(external, "reports")
                os.makedirs(reports_dir, exist_ok=True)
                q1_path = os.path.join(reports_dir, "q1.txt")
                other_path = os.path.join(external, "other.txt")
                gt = GrantTracker()
                gt.add(q1_path)
                assert checker.classify(other_path, request_grants=gt.snapshot()) == ZoneClassification.UNRECOGNISED


class TestWriteProtectedInternal:
    def test_is_write_protected_internal_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            path = os.path.join(paths.tools_dir, "my_tool.sh")
            assert checker.is_write_protected_internal(os.path.realpath(path)) is True

    def test_is_write_protected_internal_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            # data/ is INTERNAL but NOT in the write-protected set
            path = os.path.join(paths.data_dir, "some_file.json")
            assert checker.is_write_protected_internal(os.path.realpath(path)) is False

    def test_is_write_protected_internal_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            path = os.path.join(paths.prompts_dir, "system.md")
            assert checker.is_write_protected_internal(os.path.realpath(path)) is True

    def test_is_write_protected_internal_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            path = os.path.join(paths.skills_dir, "myplugin", "SKILL.md")
            assert checker.is_write_protected_internal(os.path.realpath(path)) is True


class TestWriteProtectedIntegration:
    def test_generated_tools_dir_is_internal_and_write_protected(self):
        """Confirm the conjunction (INTERNAL and write-protected) can actually fire."""
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            tool_path = os.path.join(paths.generated_tools_dir, "gen_tool.py")
            real_path = os.path.realpath(tool_path)
            assert checker.classify(tool_path) == ZoneClassification.INTERNAL
            assert checker.is_write_protected_internal(real_path) is True


class TestIsContained:
    def test_child_is_contained(self):
        assert _is_contained("/tmp/myzone/file.txt", "/tmp/myzone") is True

    def test_exact_path_is_contained(self):
        assert _is_contained("/tmp/myzone", "/tmp/myzone") is True

    def test_sibling_prefix_not_contained(self):
        assert _is_contained("/tmp/myzone-evil/file.txt", "/tmp/myzone") is False

    def test_normcase_case_insensitive(self):
        # On case-insensitive filesystems (macOS/Windows) normcase lowercases paths
        if os.path.normcase("A") == "A":
            pytest.skip("normcase is a no-op on this filesystem")
        assert _is_contained("/tmp/MyZone/file.txt", "/tmp/myzone") is True


class TestPersistence:
    def test_add_trusted_persistence_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir)

                checker2 = _make_checker(tmp)
                paths = [e.path for e in checker2.list_user_trusted()]
                assert os.path.realpath(some_dir) in paths

    def test_remove_trusted_valid_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as dir_a:
                with tempfile.TemporaryDirectory() as dir_b:
                    checker = _make_checker(tmp)
                    checker.add_trusted(dir_a)
                    checker.add_trusted(dir_b)

                    removed = checker.remove_trusted(1)
                    assert removed == os.path.realpath(dir_a)
                    remaining = [e.path for e in checker.list_user_trusted()]
                    assert os.path.realpath(dir_a) not in remaining
                    assert os.path.realpath(dir_b) in remaining

    def test_remove_trusted_invalid_index_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as dir_a:
                checker = _make_checker(tmp)
                checker.add_trusted(dir_a)
                with pytest.raises(IndexError):
                    checker.remove_trusted(5)

    def test_list_user_trusted_sorted_by_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            z_dir = os.path.join(tmp, "zzz_dir")
            a_dir = os.path.join(tmp, "aaa_dir")
            os.makedirs(z_dir)
            os.makedirs(a_dir)

            checker = _make_checker(tmp)
            checker.add_trusted(z_dir)
            checker.add_trusted(a_dir)

            trusted = checker.list_user_trusted()
            result_paths = [e.path for e in trusted]
            assert result_paths == sorted(result_paths)
            assert result_paths[0] == os.path.realpath(a_dir)
            assert result_paths[1] == os.path.realpath(z_dir)

    def test_missing_trusted_dirs_file_loads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            assert checker.list_user_trusted() == []
