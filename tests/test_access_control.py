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
        data_dir=os.path.join(tmp, "data"),
        skills_dir=os.path.join(tmp, "skills"),
        prompts_dir=os.path.join(tmp, "prompts"),
        downloads_dir=os.path.join(tmp, "downloads"),
        workspace_dir=workspace or os.path.join(tmp, "workspace"),
    )


def _make_checker(tmp: str, workspace: str | None = None) -> TrustedZoneChecker:
    paths = _make_paths(tmp, workspace)
    for d in [
        paths.data_dir,
        paths.skills_dir,
        paths.prompts_dir,
        paths.downloads_dir,
        paths.workspace_dir,
    ]:
        os.makedirs(d, exist_ok=True)
    return TrustedZoneChecker(paths_config=paths, data_dir=paths.data_dir, agent_name="test-agent")


class TestClassify:
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

    def test_vault_file_is_unrecognised(self):
        """Vault file must not be auto-allowed even though it lives in an agent-internal path."""
        with tempfile.TemporaryDirectory() as tmp:
            vault_file = os.path.join(tmp, "vault.toml")
            checker = TrustedZoneChecker(
                paths_config=_make_paths(tmp),
                data_dir=os.path.join(tmp, "data"),
                agent_name="test-agent",
                vault_path=vault_file,
            )
            # Even if the vault dir happens to be inside an internal dir, the specific
            # vault file must return UNRECOGNISED to force confirmation on reads.
            assert checker.classify(vault_file) == ZoneClassification.UNRECOGNISED

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

    def test_vault_file_at_xdg_state_path_is_unrecognised(self):
        """Vault under ~/.local/state/<agent>/secrets.toml must not be auto-allowed."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = os.path.join(tmp, ".local", "state", "test-agent")
            os.makedirs(state_dir, exist_ok=True)
            vault_file = os.path.join(state_dir, "secrets.toml")
            with open(vault_file, "w") as f:
                f.write("secret")
            checker = _make_checker(tmp)
            # Even if state dir happens to be reachable, the specific vault file
            # must be UNRECOGNISED to force confirmation on reads.
            assert checker.classify(vault_file) == ZoneClassification.UNRECOGNISED

    def test_rw_trusted_dir_allows_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir, mode="rw")
                path = os.path.join(some_dir, "file.txt")
                assert checker.classify(path, operation="write") == ZoneClassification.TRUSTED

    def test_read_only_trusted_dir_allows_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir, mode="r")
                path = os.path.join(some_dir, "file.txt")
                assert checker.classify(path, operation="read") == ZoneClassification.TRUSTED

    def test_read_only_trusted_dir_blocks_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir, mode="r")
                path = os.path.join(some_dir, "file.txt")
                assert checker.classify(path, operation="write") == ZoneClassification.UNRECOGNISED

    def test_operation_default_is_write(self):
        """Default operation is 'write' (fail-safe)."""
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir, mode="r")
                path = os.path.join(some_dir, "file.txt")
                # no operation kwarg — defaults to "write", read-only dir should block
                assert checker.classify(path) == ZoneClassification.UNRECOGNISED

    def test_trusted_parent_does_not_unlock_vault(self):
        """Vault remains UNRECOGNISED even when its parent dir is added to trusted."""
        with tempfile.TemporaryDirectory() as tmp:
            vault_file = os.path.join(tmp, "vault_dir", "secrets.toml")
            os.makedirs(os.path.dirname(vault_file), exist_ok=True)
            paths = _make_paths(tmp)
            for d in [paths.data_dir, paths.skills_dir, paths.prompts_dir,
                      paths.downloads_dir, paths.workspace_dir]:
                os.makedirs(d, exist_ok=True)
            checker = TrustedZoneChecker(
                paths_config=paths,
                data_dir=paths.data_dir,
                agent_name="test-agent",
                vault_path=vault_file,
            )
            # Add the vault's parent dir to trusted — vault must still be UNRECOGNISED
            checker.add_trusted(os.path.dirname(vault_file))
            assert checker.classify(vault_file) == ZoneClassification.UNRECOGNISED

    def test_overlapping_trusted_most_restrictive_wins(self):
        """When a path matches both rw and r trusted entries, r wins for writes."""
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as parent:
                child = os.path.join(parent, "secret")
                os.makedirs(child)
                checker = _make_checker(tmp)
                checker.add_trusted(parent, mode="rw")
                checker.add_trusted(child, mode="r")
                # Read inside child: TRUSTED (both match, read allowed)
                assert checker.classify(os.path.join(child, "f.txt"), operation="read") == ZoneClassification.TRUSTED
                # Write inside child: UNRECOGNISED (r-mode match blocks write)
                assert checker.classify(os.path.join(child, "f.txt"), operation="write") == ZoneClassification.UNRECOGNISED
                # Write inside parent (not child): TRUSTED (only rw matches)
                assert checker.classify(os.path.join(parent, "other.txt"), operation="write") == ZoneClassification.TRUSTED

    def test_trusted_parent_does_not_unlock_trust_store(self):
        """trusted_dirs.json remains UNRECOGNISED even when data/ is added to trusted."""
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            paths = _make_paths(tmp)
            # Add data/ dir to trusted
            checker.add_trusted(paths.data_dir)
            trusted_dirs_json = os.path.join(paths.data_dir, "trusted_dirs.json")
            assert checker.classify(trusted_dirs_json) == ZoneClassification.UNRECOGNISED

    def test_hardlink_of_vault_in_trusted_dir_is_unrecognised(self, tmp_path):
        """Hardlink alias of vault inside trusted dir must not classify as TRUSTED."""
        tmp = str(tmp_path)
        paths = _make_paths(tmp)
        data_dir = paths.data_dir
        workspace_dir = paths.workspace_dir
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(workspace_dir, exist_ok=True)

        vault = os.path.join(data_dir, "secrets.toml")
        with open(vault, "w") as f:
            f.write("secret")

        checker = TrustedZoneChecker(
            paths_config=paths,
            data_dir=data_dir,
            agent_name="test",
            vault_path=vault,
        )
        # workspace is default-trusted; hardlink of vault inside it must still be UNRECOGNISED
        hardlink = os.path.join(workspace_dir, "alias.toml")
        os.link(vault, hardlink)
        result = checker.classify(hardlink, operation="read")
        assert result == ZoneClassification.UNRECOGNISED


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

                    real_a = os.path.realpath(dir_a)
                    real_b = os.path.realpath(dir_b)
                    first_sorted, second_sorted = sorted([real_a, real_b])

                    removed = checker.remove_trusted(1)
                    assert removed == first_sorted
                    remaining = [e.path for e in checker.list_user_trusted()]
                    assert first_sorted not in remaining
                    assert second_sorted in remaining

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

    def test_remove_trusted_uses_sorted_index_not_insertion_order(self):
        """Regression: /dir del N must remove the N-th sorted entry, not N-th inserted."""
        with tempfile.TemporaryDirectory() as tmp:
            z_dir = os.path.join(tmp, "zzz_dir")
            a_dir = os.path.join(tmp, "aaa_dir")
            os.makedirs(z_dir)
            os.makedirs(a_dir)

            checker = _make_checker(tmp)
            # Insert zzz first, aaa second → insertion order [zzz, aaa]
            checker.add_trusted(z_dir)
            checker.add_trusted(a_dir)

            # list_user_trusted() → sorted: #1=aaa, #2=zzz
            # /dir del 1 must remove aaa (sorted index), not zzz (insertion index)
            removed = checker.remove_trusted(1)
            assert removed == os.path.realpath(a_dir), (
                f"Expected aaa_dir removed (#1 sorted), got {removed}"
            )
            remaining = [e.path for e in checker.list_user_trusted()]
            assert os.path.realpath(a_dir) not in remaining
            assert os.path.realpath(z_dir) in remaining

    def test_missing_trusted_dirs_file_loads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            assert checker.list_user_trusted() == []

    def test_add_trusted_with_mode_r_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir, mode="r")
                # Reload from disk and verify mode persisted
                checker2 = _make_checker(tmp)
                entries = checker2.list_user_trusted()
                assert len(entries) == 1
                assert entries[0].mode == "r"

    def test_add_trusted_default_mode_is_rw(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir)
                entries = checker.list_user_trusted()
                assert entries[0].mode == "rw"

    def test_load_backward_compat_missing_mode(self):
        """Entries without 'mode' field in JSON default to 'rw'."""
        import json
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                # Write JSON without mode field (legacy format)
                data_dir = _make_paths(tmp).data_dir
                os.makedirs(data_dir, exist_ok=True)
                legacy_data = [{"path": os.path.realpath(some_dir), "added": "2024-01-01T00:00:00+00:00"}]
                with open(os.path.join(data_dir, "trusted_dirs.json"), "w") as f:
                    json.dump(legacy_data, f)
                checker2 = _make_checker(tmp)
                entries = checker2.list_user_trusted()
                assert len(entries) == 1
                assert entries[0].mode == "rw"

    def test_add_trusted_invalid_mode_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = _make_checker(tmp)
            with pytest.raises(ValueError):
                checker.add_trusted("/some/path", mode="ro")


class TestReload:
    def test_reload_picks_up_externally_written_entry(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                assert checker.list_user_trusted() == []

                data_dir = _make_paths(tmp).data_dir
                entry = {"path": os.path.realpath(some_dir), "added": "2024-01-01T00:00:00+00:00", "mode": "rw"}
                with open(os.path.join(data_dir, "trusted_dirs.json"), "w") as f:
                    json.dump([entry], f)

                n = checker.reload_user_trusted()
                assert n == 1
                paths = [e.path for e in checker.list_user_trusted()]
                assert os.path.realpath(some_dir) in paths

    def test_reload_raises_on_malformed_json_and_keeps_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir)
                original = checker.list_user_trusted()

                data_dir = _make_paths(tmp).data_dir
                with open(os.path.join(data_dir, "trusted_dirs.json"), "w") as f:
                    f.write("not valid json{{{")

                with pytest.raises(Exception):
                    checker.reload_user_trusted()

                assert checker.list_user_trusted() == original

    def test_load_user_trusted_returns_empty_on_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = _make_paths(tmp).data_dir
            os.makedirs(data_dir, exist_ok=True)
            with open(os.path.join(data_dir, "trusted_dirs.json"), "w") as f:
                f.write("not valid json{{{")
            checker = _make_checker(tmp)
            assert checker.list_user_trusted() == []

    def test_null_added_field_loads_as_empty_string(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                data_dir = _make_paths(tmp).data_dir
                os.makedirs(data_dir, exist_ok=True)
                entry = {"path": os.path.realpath(some_dir), "added": None, "mode": "rw"}
                with open(os.path.join(data_dir, "trusted_dirs.json"), "w") as f:
                    json.dump([entry], f)
                checker = _make_checker(tmp)
                entries = checker.list_user_trusted()
                assert len(entries) == 1
                assert entries[0].added == ""

    def test_reload_raises_on_wrong_json_type(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir)
                original = checker.list_user_trusted()

                data_dir = _make_paths(tmp).data_dir
                with open(os.path.join(data_dir, "trusted_dirs.json"), "w") as f:
                    json.dump("this is not a list", f)

                with pytest.raises(ValueError, match="must be a JSON array"):
                    checker.reload_user_trusted()

                assert checker.list_user_trusted() == original

    def test_integer_added_field_loads_as_empty_string(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                data_dir = _make_paths(tmp).data_dir
                os.makedirs(data_dir, exist_ok=True)
                entry = {"path": os.path.realpath(some_dir), "added": 20260722, "mode": "rw"}
                with open(os.path.join(data_dir, "trusted_dirs.json"), "w") as f:
                    json.dump([entry], f)
                checker = _make_checker(tmp)
                entries = checker.list_user_trusted()
                assert len(entries) == 1
                assert entries[0].added == ""
                # Ensure slicing (as done in /dir list) does not crash
                assert entries[0].added[:10] == ""

    def test_reload_missing_file_resets_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as some_dir:
                checker = _make_checker(tmp)
                checker.add_trusted(some_dir)
                assert len(checker.list_user_trusted()) == 1

                data_dir = _make_paths(tmp).data_dir
                os.remove(os.path.join(data_dir, "trusted_dirs.json"))

                n = checker.reload_user_trusted()
                assert n == 0
                assert checker.list_user_trusted() == []
