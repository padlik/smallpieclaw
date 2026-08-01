"""Tests for skill path resolution in builtin_tools/files.py.

Covers _expand_skill_paths (unit) and _run_file_read SKILL.md intercept (integration).
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock

from builtin_tools.access_control import GrantTracker
from builtin_tools.files import FileTools, _expand_skill_paths
from skill_registry import Skill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ft_with_registry(skill_dir: str, skill_md_path: str) -> FileTools:
    skill = Skill(name="test-skill", description="test", path=skill_dir, skill_md_path=skill_md_path)
    registry = MagicMock()
    registry.all.return_value = [skill]
    owner = MagicMock()
    owner.skill_registry = registry
    owner.trusted_zone_checker = None
    owner.grant_tracker = GrantTracker()
    return FileTools(owner)


def _make_ft_registry_none() -> FileTools:
    owner = MagicMock()
    owner.skill_registry = None
    owner.trusted_zone_checker = None
    owner.grant_tracker = GrantTracker()
    return FileTools(owner)


def _make_ft_skill_not_found() -> FileTools:
    registry = MagicMock()
    registry.all.return_value = []
    owner = MagicMock()
    owner.skill_registry = registry
    owner.trusted_zone_checker = None
    owner.grant_tracker = GrantTracker()
    return FileTools(owner)


# ---------------------------------------------------------------------------
# Unit: _expand_skill_paths
# ---------------------------------------------------------------------------

class TestExpandSkillPaths:

    def test_tier1_dot_slash_replaced_globally(self):
        content = "Run ./scripts/run.sh and load ./assets/.env"
        result = _expand_skill_paths(content, "/skill")
        assert "/skill/scripts/run.sh" in result
        assert "/skill/assets/.env" in result
        assert "./" not in result

    def test_tier1_absolute_paths_unchanged(self):
        content = "Use /usr/bin/python3"
        assert _expand_skill_paths(content, "/skill") == content

    def test_tier2_scripts_in_code_fence(self):
        content = "```\nscripts/run.sh\n```"
        result = _expand_skill_paths(content, "/skill")
        assert "/skill/scripts/run.sh" in result

    def test_tier2_assets_in_inline_span(self):
        content = "Load `assets/.env` before running."
        result = _expand_skill_paths(content, "/skill")
        assert "`/skill/assets/.env`" in result

    def test_tier2_compound_name_not_matched(self):
        content = "See `static-assets/style.css`"
        result = _expand_skill_paths(content, "/skill")
        assert "static-assets/style.css" in result
        assert "static-/skill/assets" not in result

    def test_tier2_not_applied_in_prose(self):
        content = "The scripts/ directory contains helpers."
        result = _expand_skill_paths(content, "/skill")
        assert "The scripts/ directory" in result

    def test_tier2_all_four_standard_subdirs_in_code_fence(self):
        content = "```\nscripts/a.sh\nassets/b.txt\nreferences/c.md\ntests/d.py\n```"
        result = _expand_skill_paths(content, "/skill")
        assert "/skill/scripts/a.sh" in result
        assert "/skill/assets/b.txt" in result
        assert "/skill/references/c.md" in result
        assert "/skill/tests/d.py" in result


# ---------------------------------------------------------------------------
# Integration: _run_file_read SKILL.md intercept
# ---------------------------------------------------------------------------

class TestRunFileReadSkillMdIntercept:

    def test_tier1_substitution_applied(self):
        with tempfile.TemporaryDirectory() as skill_dir:
            skill_md = os.path.join(skill_dir, "SKILL.md")
            with open(skill_md, "w") as f:
                f.write("Run ./scripts/fetch.py")
            ft = _make_ft_with_registry(skill_dir, skill_md)
            result = ft._run_file_read({"_resolved_path": skill_md})
            assert result["success"] is True
            assert f"{skill_dir}/scripts/fetch.py" in result["output"]
            assert "./" not in result["output"]

    def test_registry_none_fallback_uses_dirname(self):
        """Case (a): registry not yet wired → dirname used, substitution still runs."""
        with tempfile.TemporaryDirectory() as skill_dir:
            skill_md = os.path.join(skill_dir, "SKILL.md")
            with open(skill_md, "w") as f:
                f.write("Run ./scripts/helper.sh")
            ft = _make_ft_registry_none()
            result = ft._run_file_read({"_resolved_path": skill_md})
            assert f"{skill_dir}/scripts/helper.sh" in result["output"]

    def test_non_registered_skill_md_skips_substitution(self):
        """Case (b): registry set but skill not found → raw content returned."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_md = os.path.join(tmp, "SKILL.md")
            with open(skill_md, "w") as f:
                f.write("Run ./scripts/fetch.py")
            ft = _make_ft_skill_not_found()
            result = ft._run_file_read({"_resolved_path": skill_md})
            assert "./scripts/fetch.py" in result["output"]
            assert f"{tmp}/scripts/fetch.py" not in result["output"]

    def test_non_skill_md_file_not_substituted(self):
        """Non-SKILL.md files are passed through unchanged."""
        with tempfile.TemporaryDirectory() as skill_dir:
            readme = os.path.join(skill_dir, "README.md")
            with open(readme, "w") as f:
                f.write("See ./scripts/run.sh for details")
            skill_md = os.path.join(skill_dir, "SKILL.md")
            ft = _make_ft_with_registry(skill_dir, skill_md)
            result = ft._run_file_read({"_resolved_path": readme})
            assert "./scripts/run.sh" in result["output"]
