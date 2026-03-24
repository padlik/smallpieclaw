"""
skill_registry.py
-----------------
Discovers and parses Agent Skills from the skills/ directory.

Each skill is a subdirectory containing a SKILL.md file with YAML frontmatter
followed by Markdown instructions. Conforms to the Agent Skills specification:
  https://agentskills.io/specification

Directory structure:
    skills/
        my-skill/
            SKILL.md          ← Required: YAML frontmatter + Markdown body
            scripts/          ← Optional: executable scripts
            references/       ← Optional: reference documents
            assets/           ← Optional: templates, static files

SKILL.md frontmatter fields:
    name          (required) — must match parent directory name
    description   (required) — what the skill does and when to use it
    license       (optional)
    compatibility (optional) — environment requirements
    metadata      (optional) — arbitrary key-value pairs
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Regex to extract YAML frontmatter block between --- delimiters
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    path: str               # absolute path to skill directory
    skill_md_path: str      # absolute path to SKILL.md
    license: str = ""
    compatibility: str = ""
    metadata: dict = field(default_factory=dict)


class SkillRegistry:
    """
    Scans a skills directory and maintains a registry of available skills.
    Parses YAML frontmatter without external dependencies.
    """

    def __init__(self, skills_dir: str):
        self.skills_dir = skills_dir
        self._registry: dict[str, Skill] = {}
        self.refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> int:
        """Rescan the skills directory and rebuild the registry. Returns skill count."""
        self._registry.clear()
        if not os.path.isdir(self.skills_dir):
            logger.debug("Skills directory not found: %s", self.skills_dir)
            return 0

        for entry in sorted(os.listdir(self.skills_dir)):
            skill_dir = os.path.join(self.skills_dir, entry)
            if not os.path.isdir(skill_dir):
                continue
            skill_md = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(skill_md):
                logger.debug("Skipping %s — no SKILL.md found", skill_dir)
                continue
            skill = self._parse_skill(skill_dir, skill_md)
            if skill:
                self._registry[skill.name] = skill

        logger.info("Skill registry refreshed: %d skills loaded", len(self._registry))
        return len(self._registry)

    def all(self) -> list[Skill]:
        return list(self._registry.values())

    def get(self, name: str) -> Optional[Skill]:
        return self._registry.get(name)

    def count(self) -> int:
        return len(self._registry)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_frontmatter(text: str) -> dict:
        """
        Minimal YAML frontmatter parser. Handles simple key: value pairs and
        multi-line strings (block scalars and quoted strings are not supported
        beyond basic line continuation). Good enough for SKILL.md frontmatter.
        """
        result: dict = {}
        for line in text.splitlines():
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip().strip('"').strip("'")
            if key:
                result[key] = value
        return result

    def _parse_skill(self, skill_dir: str, skill_md: str) -> Optional[Skill]:
        """Parse a SKILL.md file and return a Skill object, or None on error."""
        dir_name = os.path.basename(skill_dir)
        try:
            with open(skill_md, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as exc:
            logger.warning("Could not read %s: %s", skill_md, exc)
            return None

        m = _FRONTMATTER_RE.match(content)
        if not m:
            logger.warning("No YAML frontmatter found in %s — skipping", skill_md)
            return None

        fm = self._parse_frontmatter(m.group(1))

        name = fm.get("name", "").strip()
        description = fm.get("description", "").strip()

        if not name:
            logger.warning("Missing 'name' in %s — skipping", skill_md)
            return None
        if not description:
            logger.warning("Missing 'description' in %s — skipping", skill_md)
            return None

        # Validate: name must match directory name
        if name != dir_name:
            logger.warning(
                "Skill name '%s' does not match directory '%s' — skipping",
                name, dir_name,
            )
            return None

        # Validate name format: lowercase alphanumeric + hyphens, no leading/trailing/consecutive hyphens
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name) and name not in ("", ):
            if not re.fullmatch(r"[a-z0-9]", name):  # single char is fine
                if "--" in name or name.startswith("-") or name.endswith("-"):
                    logger.warning("Invalid skill name '%s' — skipping", name)
                    return None

        return Skill(
            name=name,
            description=description,
            path=os.path.abspath(skill_dir),
            skill_md_path=os.path.abspath(skill_md),
            license=fm.get("license", ""),
            compatibility=fm.get("compatibility", ""),
            metadata={k: v for k, v in fm.items()
                      if k not in ("name", "description", "license", "compatibility", "allowed-tools")},
        )
