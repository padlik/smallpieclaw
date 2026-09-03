"""Tests for path_policy.py three-tier classification."""

from __future__ import annotations

import logging
import os

import pytest

from exceptions import ConfigError
from nsjail_config import (
    _BLOCKED_SYSTEM_PREFIXES,
    _SENSITIVE_USER_PREFIXES,
)
from path_policy import (
    HARDCODED_SYSTEM_PREFIXES,
    PathPolicy,
    PathVerdict,
    is_contained,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_policy(
    tmp_path: str,
    *,
    allowed_dirs: list[str] | None = None,
    prohibited_dirs: list[str] | None = None,
    logger: logging.Logger | None = None,
    agent_name: str = "test-agent",
) -> PathPolicy:
    """Create a PathPolicy using temp paths and monkeypatched HOME/XDG env."""
    workspace = os.path.join(tmp_path, "workspace")
    downloads = os.path.join(tmp_path, "downloads")
    data_home = os.path.join(tmp_path, "xdg", "data", agent_name)
    state_home = os.path.join(tmp_path, "xdg", "state", agent_name)
    config_home = os.path.join(tmp_path, "xdg", "config", agent_name)
    vault = os.path.join(tmp_path, "vault.toml")
    config = os.path.join(config_home, "config.toml")
    skills = os.path.join(tmp_path, "skills")
    results = os.path.join(tmp_path, "results")

    for d in (workspace, downloads, data_home, state_home, config_home, skills, results):
        os.makedirs(d, exist_ok=True)
    with open(vault, "w") as f:
        f.write("[secrets]\n")
    with open(config, "w") as f:
        f.write("\n")

    return PathPolicy.create(
        agent_name=agent_name,
        workspace_dir=workspace,
        downloads_dir=downloads,
        tmp_dir=f"/tmp/{agent_name}",
        skills_dir=skills,
        results_dir=results,
        data_home=data_home,
        state_home=state_home,
        config_home=config_home,
        vault_path=vault,
        config_path=config,
        allowed_dirs=allowed_dirs,
        prohibited_dirs=prohibited_dirs,
        logger=logger,
    )


# ---------------------------------------------------------------------------
# Tier classification matrix
# ---------------------------------------------------------------------------


class TestTierClassification:
    def test_workspace_rw(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        target = os.path.realpath(os.path.join(policy.workspace_dir, "notes.txt"))
        verdict, mode = policy.classify(target, "write")
        assert verdict == PathVerdict.ALLOWED
        assert mode == "rw"

    def test_downloads_rw(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        target = os.path.realpath(os.path.join(policy.downloads_dir, "file.zip"))
        verdict, mode = policy.classify(target, "write")
        assert verdict == PathVerdict.ALLOWED
        assert mode == "rw"

    def test_tmp_dir_rw(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        os.makedirs(policy.tmp_dir, exist_ok=True)
        target = os.path.realpath(os.path.join(policy.tmp_dir, "handoff.txt"))
        verdict, mode = policy.classify(target, "write")
        assert verdict == PathVerdict.ALLOWED
        assert mode == "rw"

    def test_skills_read_allowed_write_unrecognised(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        target = os.path.realpath(os.path.join(policy.skills_dir, "someskill", "SKILL.md"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write("skill")

        assert policy.classify(target, "read") == (PathVerdict.ALLOWED, "r")
        assert policy.classify(target, "write") == (PathVerdict.UNRECOGNISED, "")

    def test_results_rw(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        target = os.path.realpath(os.path.join(policy.results_dir, "trace-1", "out.json"))
        verdict, mode = policy.classify(target, "write")
        assert verdict == PathVerdict.ALLOWED
        assert mode == "rw"

    def test_state_home_prohibited(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        target = os.path.realpath(os.path.join(policy.state_home, "logs.sqlite"))
        verdict, reason = policy.classify(target, "read")
        assert verdict == PathVerdict.PROHIBITED
        assert "log_query" in reason or "memory" in reason.lower()

    def test_ssh_home_prohibited_with_reason(self, tmp_path, monkeypatch):
        home = os.path.join(str(tmp_path), "home")
        os.makedirs(home, exist_ok=True)
        monkeypatch.setenv("HOME", home)
        ssh_dir = os.path.join(home, ".ssh")
        os.makedirs(ssh_dir, exist_ok=True)

        policy = _make_policy(str(tmp_path))
        target = os.path.realpath(os.path.join(ssh_dir, "id_ed25519"))
        verdict, reason = policy.classify(target, "read")
        assert verdict == PathVerdict.PROHIBITED
        assert "SSH" in reason or "credential" in reason.lower()

    def test_allowed_dirs_rw(self, tmp_path):
        projects = os.path.join(str(tmp_path), "projects")
        os.makedirs(projects, exist_ok=True)
        policy = _make_policy(str(tmp_path), allowed_dirs=[projects])
        target = os.path.realpath(os.path.join(projects, "myapp", "src", "main.py"))
        assert policy.classify(target, "write") == (PathVerdict.ALLOWED, "rw")

    def test_allowed_dirs_tilde_expansion(self, tmp_path, monkeypatch):
        home = os.path.join(str(tmp_path), "home")
        projects = os.path.join(home, "projects")
        os.makedirs(projects, exist_ok=True)
        monkeypatch.setenv("HOME", home)

        policy = _make_policy(str(tmp_path), allowed_dirs=["~/projects"])
        target = os.path.realpath(os.path.join(projects, "file.txt"))
        assert policy.classify(target, "write") == (PathVerdict.ALLOWED, "rw")

    def test_unrecognised_path(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        target = os.path.realpath("/nonexistent/random/path/file.txt")
        assert policy.classify(target, "read") == (PathVerdict.UNRECOGNISED, "")


# ---------------------------------------------------------------------------
# Boundary / bypass defense
# ---------------------------------------------------------------------------


class TestBoundaryContainment:
    def test_sibling_prefix_bypass(self, tmp_path):
        shared = os.path.join(str(tmp_path), "shared")
        shared_evil = os.path.join(str(tmp_path), "shared-evil")
        os.makedirs(shared, exist_ok=True)
        os.makedirs(shared_evil, exist_ok=True)

        policy = _make_policy(str(tmp_path), allowed_dirs=[shared])
        target = os.path.realpath(os.path.join(shared_evil, "secret.txt"))
        assert policy.classify(target, "read") == (PathVerdict.UNRECOGNISED, "")

    def test_symlink_realpath_resolves_to_prohibited(self, tmp_path):
        """A symlink inside an allowed dir that resolves to a prohibited path
        is denied because callers resolve the realpath before classifying.

        The test uses a synthetic target directory under ``/tmp`` to remain
        portable: it is listed as a config-prohibited dir, and realpath of the
        symlink inside the workspace resolves to that target.
        """
        external = os.path.join(str(tmp_path), "external", "secret")
        os.makedirs(external, exist_ok=True)
        policy = _make_policy(str(tmp_path), prohibited_dirs=[external])
        workspace_link = os.path.join(policy.workspace_dir, "link.txt")
        secret_file = os.path.join(external, "secret.txt")
        with open(secret_file, "w") as f:
            f.write("secret")
        os.symlink(secret_file, workspace_link)
        resolved = os.path.realpath(workspace_link)
        assert policy.classify(resolved, "read") == (
            PathVerdict.PROHIBITED,
            "prohibited directory from config",
        )


# ---------------------------------------------------------------------------
# Inode-alias defense
# ---------------------------------------------------------------------------


class TestInodeAliasDefense:
    def test_hardlink_of_vault_in_allowed_dir_is_prohibited(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        hardlink = os.path.join(policy.workspace_dir, "alias.toml")
        os.link(policy.vault_path, hardlink)
        verdict, reason = policy.classify(os.path.realpath(hardlink), "read")
        assert verdict == PathVerdict.PROHIBITED
        assert "hardlink" in reason


# ---------------------------------------------------------------------------
# Operator recklessness immunity
# ---------------------------------------------------------------------------


class TestRecklessnessImmunity:
    def test_allowed_root_does_not_unlock_ssh(self, tmp_path, monkeypatch):
        home = os.path.join(str(tmp_path), "home")
        os.makedirs(home, exist_ok=True)
        monkeypatch.setenv("HOME", home)
        ssh_dir = os.path.join(home, ".ssh")
        os.makedirs(ssh_dir, exist_ok=True)

        policy = _make_policy(str(tmp_path), allowed_dirs=["/"])
        target = os.path.realpath(os.path.join(ssh_dir, "id_ed25519"))
        verdict, reason = policy.classify(target, "read")
        assert verdict == PathVerdict.PROHIBITED
        assert "SSH" in reason or "credential" in reason.lower()

    def test_allowed_root_does_not_unlock_state_home(self, tmp_path):
        policy = _make_policy(str(tmp_path), allowed_dirs=["/"])
        target = os.path.realpath(os.path.join(policy.state_home, "logs.sqlite"))
        verdict, reason = policy.classify(target, "read")
        assert verdict == PathVerdict.PROHIBITED
        assert policy.state_home in policy._prohibited


# ---------------------------------------------------------------------------
# Frozen immutability
# ---------------------------------------------------------------------------


class TestFrozenImmutability:
    def test_attempting_mutation_raises(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        with pytest.raises((AttributeError, TypeError)):
            policy.workspace_dir = "/other"


# ---------------------------------------------------------------------------
# Conflict validation
# ---------------------------------------------------------------------------


class TestConflictValidation:
    def test_allowed_dir_containing_prohibited_warns_once_and_skips_mount(
        self, tmp_path, caplog
    ):
        caplog.set_level(logging.WARNING)
        work = os.path.join(str(tmp_path), "work")
        vaults = os.path.join(work, "vaults")
        os.makedirs(vaults, exist_ok=True)

        policy = _make_policy(str(tmp_path), allowed_dirs=[work], prohibited_dirs=[vaults])
        assert policy.conflict_skips == frozenset({os.path.realpath(work)})

        # Exactly one warning naming the pair.
        conflict_logs = [
            rec.message for rec in caplog.records if "PathPolicy conflict" in rec.message
        ]
        assert len(conflict_logs) == 1
        assert os.path.realpath(work) in conflict_logs[0]
        assert os.path.realpath(vaults) in conflict_logs[0]

        # File plane still allows access to non-overlapping files.
        report = os.path.realpath(os.path.join(work, "report.txt"))
        assert policy.classify(report, "write") == (PathVerdict.ALLOWED, "rw")

        # Overlapping region remains prohibited.
        secret = os.path.realpath(os.path.join(vaults, "secret.txt"))
        assert policy.classify(secret, "read") == (PathVerdict.PROHIBITED, "prohibited directory from config")

    def test_conflict_skips_does_not_mount_allowed_containing_prohibited(
        self, tmp_path
    ):
        work = os.path.join(str(tmp_path), "work")
        vaults = os.path.join(work, "vaults")
        os.makedirs(vaults, exist_ok=True)
        policy = _make_policy(str(tmp_path), allowed_dirs=[work], prohibited_dirs=[vaults])
        assert os.path.realpath(work) not in policy.tier2_entries()


# ---------------------------------------------------------------------------
# Config validation and warnings
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_nonexistent_prohibited_dir_warns_no_raise(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING)
        missing = os.path.join(str(tmp_path), "missing")
        policy = _make_policy(str(tmp_path), prohibited_dirs=[missing])
        assert missing not in policy._prohibited
        assert any("does not exist" in rec.message for rec in caplog.records)

    def test_nonexistent_allowed_dir_warns_no_raise(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING)
        missing = os.path.join(str(tmp_path), "missing")
        policy = _make_policy(str(tmp_path), allowed_dirs=[missing])
        assert os.path.realpath(missing) not in policy.tier2_entries()
        assert any("does not exist" in rec.message for rec in caplog.records)

    def test_invalid_prohibited_dirs_structure_raises_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="prohibited_dirs must be a list of strings"):
            _make_policy(str(tmp_path), prohibited_dirs="not-a-list")

    def test_invalid_allowed_dirs_structure_raises_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="allowed_dirs must be a list of strings"):
            _make_policy(str(tmp_path), allowed_dirs={"foo": "bar"})

    def test_invalid_allowed_dirs_item_raises_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="allowed_dirs must be a list of strings"):
            _make_policy(str(tmp_path), allowed_dirs=["/ok", 123])


# ---------------------------------------------------------------------------
# Superset containment against legacy blocklists
# ---------------------------------------------------------------------------


class TestSupersetContainment:
    def test_tier0_covers_legacy_blocked_system_prefixes(self, tmp_path):
        policy = _make_policy(str(tmp_path))
        for prefix in _BLOCKED_SYSTEM_PREFIXES:
            assert prefix in HARDCODED_SYSTEM_PREFIXES
            assert prefix in policy._prohibited

    def test_tier0_covers_sensitive_user_prefixes(self, tmp_path, monkeypatch):
        home = os.path.join(str(tmp_path), "home")
        os.makedirs(home, exist_ok=True)
        monkeypatch.setenv("HOME", home)
        policy = _make_policy(str(tmp_path))

        for rel in _SENSITIVE_USER_PREFIXES:
            resolved = os.path.realpath(os.path.join(home, rel))
            os.makedirs(resolved, exist_ok=True)
            target = os.path.realpath(os.path.join(resolved, "secret"))
            verdict, _ = policy.classify(target, "read")
            assert verdict == PathVerdict.PROHIBITED, f"{rel} should be prohibited"

    def test_tier0_covers_agent_scoped_rw_blocked_prefixes(self, tmp_path):
        """Agent's own .local/.config/.cache are covered via XDG home entries."""
        policy = _make_policy(str(tmp_path))
        for path in (policy.data_home, policy.state_home, policy.config_home):
            verdict, _ = policy.classify(os.path.realpath(os.path.join(path, "x")), "read")
            assert verdict == PathVerdict.PROHIBITED, f"{path} should be prohibited"


# ---------------------------------------------------------------------------
# is_contained public helper
# ---------------------------------------------------------------------------


class TestIsContained:
    def test_child_is_contained(self):
        assert is_contained("/tmp/myzone/file.txt", "/tmp/myzone") is True

    def test_exact_path_is_contained(self):
        assert is_contained("/tmp/myzone", "/tmp/myzone") is True

    def test_sibling_prefix_not_contained(self):
        assert is_contained("/tmp/myzone-evil/file.txt", "/tmp/myzone") is False

    def test_normcase_case_insensitive(self, tmp_path):
        if os.path.normcase("A") == "A":
            pytest.skip("normcase is a no-op on this filesystem")
        assert is_contained("/tmp/MyZone/file.txt", "/tmp/myzone") is True
