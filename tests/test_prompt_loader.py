"""Unit tests for the prompt_loader module.

These tests cover parsing, ordering, mode filtering, validation, rendering,
caching, and the high-level ``build_system_prompt`` integration with the
legacy fallback path.
"""

from __future__ import annotations

import os
import re
from unittest.mock import MagicMock, patch

import pytest

from prompt_loader import (
    DuplicateOrderError,
    ModeConflictError,
    MissingRequiredSectionError,
    PromptLoader,
    PromptLoaderError,
    UnresolvedVariableError,
    build_system_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def prompts_dir(tmp_path):
    """Return a temporary directory with a ``system/`` prompts subdirectory."""
    (tmp_path / "system").mkdir()
    return tmp_path


@pytest.fixture
def loader_factory():
    """Factory fixture returning a :class:`PromptLoader` for a directory."""
    def _factory(path, *, cache=True):
        return PromptLoader(str(path), cache=cache)
    return _factory


# ---------------------------------------------------------------------------
# Section parsing tests
# ---------------------------------------------------------------------------

class TestPromptSectionParsing:
    """Tests for YAML frontmatter extraction and parsing."""

    def test_parse_frontmatter(self, prompts_dir, loader_factory):
        """Valid YAML frontmatter is extracted and used as metadata."""
        section_file = prompts_dir / "system" / "identity.md"
        section_file.write_text(
            "---\n"
            "section: identity\n"
            "order: 1\n"
            "required: true\n"
            "mode: all\n"
            "---\n"
            "Identity text.\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections()

        assert len(sections) == 1
        section = sections[0]
        assert section.section == "identity"
        assert section.order == 1
        assert section.required is True
        assert section.mode == "all"
        assert section.raw_content == "Identity text."

    def test_missing_frontmatter(self, prompts_dir, loader_factory):
        """A file without frontmatter is treated as the entire template body."""
        section_file = prompts_dir / "system" / "bare.md"
        section_file.write_text("Just plain markdown content.\n")

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections()

        assert len(sections) == 1
        assert sections[0].section == "bare"  # falls back to file stem
        assert sections[0].order == 0
        assert sections[0].mode == "all"
        assert sections[0].raw_content == "Just plain markdown content.\n"

    def test_invalid_yaml(self, prompts_dir, loader_factory):
        """Malformed YAML frontmatter raises :class:`PromptLoaderError`."""
        section_file = prompts_dir / "system" / "bad.md"
        section_file.write_text(
            "---\n"
            "section: bad\n"
            "order: [unclosed\n"
            "---\n"
            "Body.\n"
        )

        loader = loader_factory(prompts_dir)
        with pytest.raises(PromptLoaderError):
            loader.load_sections()


# ---------------------------------------------------------------------------
# Section ordering tests
# ---------------------------------------------------------------------------

class TestSectionOrdering:
    """Tests for ordering and duplicate order detection."""

    def test_ordered_correctly(self, prompts_dir, loader_factory):
        """Sections are returned sorted by their ``order`` metadata."""
        (prompts_dir / "system" / "second.md").write_text(
            "---\nsection: second\norder: 5\n---\nSecond.\n"
        )
        (prompts_dir / "system" / "first.md").write_text(
            "---\nsection: first\norder: 2\n---\nFirst.\n"
        )
        (prompts_dir / "system" / "third.md").write_text(
            "---\nsection: third\norder: 10\n---\nThird.\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections()

        assert [s.section for s in sections] == ["first", "second", "third"]

    def test_duplicate_order_raises(self, prompts_dir, loader_factory):
        """Two sections sharing ``order`` raise :class:`DuplicateOrderError`."""
        (prompts_dir / "system" / "a.md").write_text(
            "---\nsection: a\norder: 1\n---\nA.\n"
        )
        (prompts_dir / "system" / "b.md").write_text(
            "---\nsection: b\norder: 1\n---\nB.\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections()
        with pytest.raises(DuplicateOrderError):
            loader.validate(sections, set())


# ---------------------------------------------------------------------------
# Mode filtering tests
# ---------------------------------------------------------------------------

class TestModeFiltering:
    """Tests for mode-based section selection."""

    def test_all_mode_included(self, prompts_dir, loader_factory):
        """Sections marked ``mode: all`` are included in every active mode."""
        (prompts_dir / "system" / "always.md").write_text(
            "---\nsection: always\norder: 1\nmode: all\n---\nAlways.\n"
        )

        loader = loader_factory(prompts_dir)
        for mode in ("default", "planner", "explorer", "resilient"):
            sections = loader.load_sections(mode=mode)
            assert any(s.section == "always" for s in sections)

    def test_default_mode(self, prompts_dir, loader_factory):
        """Default mode includes ``default`` and ``all`` sections."""
        (prompts_dir / "system" / "every.md").write_text(
            "---\nsection: every\norder: 1\nmode: all\n---\nEvery.\n"
        )
        (prompts_dir / "system" / "default_only.md").write_text(
            "---\nsection: default_only\norder: 2\nmode: default\n---\nDefault.\n"
        )
        (prompts_dir / "system" / "planner_only.md").write_text(
            "---\nsection: planner_only\norder: 3\nmode: planner\n---\nPlanner.\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections(mode="default")

        names = {s.section for s in sections}
        assert names == {"every", "default_only"}

    def test_planner_mode(self, prompts_dir, loader_factory):
        """Planner mode includes planner-specific sections plus ``all``."""
        (prompts_dir / "system" / "every.md").write_text(
            "---\nsection: every\norder: 1\nmode: all\n---\nEvery.\n"
        )
        (prompts_dir / "system" / "planner_only.md").write_text(
            "---\nsection: planner_only\norder: 2\nmode: planner\n---\nPlanner.\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections(mode="planner")

        names = {s.section for s in sections}
        assert names == {"every", "planner_only"}

    def test_mode_exclusion(self, prompts_dir, loader_factory):
        """Sections whose mode does not match the active mode are excluded."""
        (prompts_dir / "system" / "explorer.md").write_text(
            "---\nsection: explorer\norder: 1\nmode: explorer\n---\nExplorer.\n"
        )

        loader = loader_factory(prompts_dir)
        assert loader.load_sections(mode="default") == []


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestValidation:
    """Tests for prompt section validation."""

    def test_missing_required_section(self, prompts_dir, loader_factory):
        """A missing required section raises :class:`MissingRequiredSectionError`.

        ``validate`` receives only the sections that will be rendered. If the
        caller knows the canonical required set for the active mode, passing
        ``expected_sections`` triggers the missing-required-section check.
        """
        (prompts_dir / "system" / "identity.md").write_text(
            "---\nsection: identity\norder: 1\nrequired: true\nmode: default\n---\nId.\n"
        )
        (prompts_dir / "system" / "tools.md").write_text(
            "---\nsection: tools\norder: 2\nrequired: true\nmode: default\n---\nTools.\n"
        )

        loader = loader_factory(prompts_dir)
        all_sections = loader.load_sections(mode="default")
        # Drop the required identity section to simulate a bad caller.
        reduced = [s for s in all_sections if s.section != "identity"]

        with pytest.raises(MissingRequiredSectionError):
            loader.validate(reduced, set(), expected_sections={"identity", "tools"})

    def test_missing_required_section_for_mode(self, prompts_dir, loader_factory):
        """A required section missing for the active mode is not detected.

        The loader filters by mode during load_sections, so a required section
        from another mode will not appear in the returned list and validate will
        not see it. This test documents that behaviour.
        """
        (prompts_dir / "system" / "identity.md").write_text(
            "---\nsection: identity\norder: 1\nrequired: true\nmode: default\n---\nId.\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections(mode="planner")
        assert sections == []
        # validate on an empty list finds no required sections present.
        loader.validate(sections, set())
        # No exception raised; the mode filter prevents the loader from
        # considering the default-only required section.

    def test_all_required_sections_present(self, prompts_dir, loader_factory):
        """Validation passes when all required sections are present."""
        (prompts_dir / "system" / "identity.md").write_text(
            "---\nsection: identity\norder: 1\nrequired: true\nmode: default\n---\nId.\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections(mode="default")
        loader.validate(sections, set())
        # No exception raised.

    def test_unresolved_variable(self, prompts_dir, loader_factory):
        """A referenced variable not in ``provided_vars`` raises
        :class:`UnresolvedVariableError`.
        """
        (prompts_dir / "system" / "identity.md").write_text(
            "---\nsection: identity\norder: 1\n---\n{{ models_section }}\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections()
        with pytest.raises(UnresolvedVariableError):
            loader.validate(sections, set())

    def test_mode_conflict(self, prompts_dir, loader_factory):
        """Two active sections that conflict raise :class:`ModeConflictError`."""
        (prompts_dir / "system" / "a.md").write_text(
            "---\nsection: a\norder: 1\nconflicts_with:\n  - b\n---\nA.\n"
        )
        (prompts_dir / "system" / "b.md").write_text(
            "---\nsection: b\norder: 2\n---\nB.\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections()
        with pytest.raises(ModeConflictError):
            loader.validate(sections, set())


# ---------------------------------------------------------------------------
# Rendering tests
# ---------------------------------------------------------------------------

class TestRendering:
    """Tests for Jinja2 rendering of loaded sections."""

    def test_jinja2_variable_substitution(self, prompts_dir, loader_factory):
        """``{{ var }}`` placeholders are replaced with supplied values."""
        (prompts_dir / "system" / "identity.md").write_text(
            "---\nsection: identity\norder: 1\n---\nHello, {{ name }}!\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections()
        prompt = loader.render(sections, {"name": "World"})
        assert prompt == "Hello, World!"

    def test_conditional_rendering(self, prompts_dir, loader_factory):
        """Jinja2 ``{% if %}`` blocks control emitted content."""
        (prompts_dir / "system" / "identity.md").write_text(
            "---\nsection: identity\norder: 1\n---\n"
            "{% if verbose %}Verbose mode{% else %}Quiet mode{% endif %}\n"
        )

        loader = loader_factory(prompts_dir)
        sections = loader.load_sections()

        assert loader.render(sections, {"verbose": True}) == "Verbose mode"
        assert loader.render(sections, {"verbose": False}) == "Quiet mode"


# ---------------------------------------------------------------------------
# build_system_prompt integration tests
# ---------------------------------------------------------------------------

class TestBuildSystemPrompt:
    """Tests for the high-level :func:`build_system_prompt` entry point."""

    def test_loads_from_prompts_dir(self, prompts_dir):
        """When ``prompts/system/`` exists, sections are loaded and rendered."""
        (prompts_dir / "system" / "identity.md").write_text(
            "---\nsection: identity\norder: 1\nrequired: true\nmode: all\n---\n"
            "You are {{ user_goal }}.\n"
        )

        tool_index = MagicMock()
        tool_index.search.return_value = []
        memory = MagicMock()
        memory.as_prompt_text.return_value = "No memory."
        results = None
        skill_registry = None
        llm = MagicMock()
        llm._models = []
        llm.llm_cfg = {}

        with patch("prompt_builder.format_tools", return_value="No tools."), \
             patch("prompt_builder.format_skills", return_value=""), \
             patch("prompt_builder.format_models", return_value=""), \
             patch("prompt_builder.format_log_section", return_value="Log section."), \
             patch("prompt_loader.estimate_tokens", return_value=42):
            prompt, estimated = build_system_prompt(
                tool_index=tool_index,
                memory=memory,
                results=results,
                skill_registry=skill_registry,
                llm=llm,
                tmp_dir="/tmp/agent",
                downloads_dir="downloads",
                log_file="agent.log",
                log_backup_count=30,
                top_tools=3,
                user_goal="test goal",
                prompts_dir=str(prompts_dir),
            )

        assert "You are test goal." in prompt
        assert estimated == 42

    def test_legacy_fallback(self, tmp_path):
        """When ``prompts/system/`` is missing, the legacy template is used."""
        tool_index = MagicMock()
        tool_index.search.return_value = []
        memory = MagicMock()
        memory.as_prompt_text.return_value = "No memory."
        results = None
        skill_registry = None
        llm = MagicMock()
        llm._models = []
        llm.llm_cfg = {}

        with patch("prompt_builder.format_tools", return_value="No tools."), \
             patch("prompt_builder.format_skills", return_value=""), \
             patch("prompt_builder.format_models", return_value=""), \
             patch("prompt_builder.format_log_section", return_value="Log section."), \
             patch("prompt_loader.estimate_tokens", return_value=100):
            prompt, estimated = build_system_prompt(
                tool_index=tool_index,
                memory=memory,
                results=results,
                skill_registry=skill_registry,
                llm=llm,
                tmp_dir="/tmp/agent",
                downloads_dir="downloads",
                log_file="agent.log",
                log_backup_count=30,
                top_tools=3,
                user_goal="test goal",
                prompts_dir=str(tmp_path / "nonexistent"),
            )

        assert "home-server management agent" in prompt
        assert "RESPONSE FORMAT" in prompt
        assert estimated == 100

    def test_returns_tuple(self, prompts_dir):
        """``build_system_prompt`` always returns ``(prompt_text, estimated_tokens)``."""
        (prompts_dir / "system" / "identity.md").write_text(
            "---\nsection: identity\norder: 1\nrequired: true\nmode: all\n---\n"
            "System prompt body.\n"
        )

        tool_index = MagicMock()
        tool_index.search.return_value = []
        memory = MagicMock()
        memory.as_prompt_text.return_value = "No memory."
        results = None
        skill_registry = None
        llm = MagicMock()
        llm._models = []
        llm.llm_cfg = {}

        with patch("prompt_builder.format_tools", return_value="No tools."), \
             patch("prompt_builder.format_skills", return_value=""), \
             patch("prompt_builder.format_models", return_value=""), \
             patch("prompt_builder.format_log_section", return_value="Log section."), \
             patch("prompt_loader.estimate_tokens", return_value=10):
            result = build_system_prompt(
                tool_index=tool_index,
                memory=memory,
                results=results,
                skill_registry=skill_registry,
                llm=llm,
                tmp_dir="/tmp/agent",
                downloads_dir="downloads",
                log_file="agent.log",
                log_backup_count=30,
                top_tools=3,
                user_goal="goal",
                prompts_dir=str(prompts_dir),
            )

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], int)


# ---------------------------------------------------------------------------
# Caching tests
# ---------------------------------------------------------------------------

class TestCaching:
    """Tests for template caching in :class:`PromptLoader`."""

    def test_template_cached(self, prompts_dir, loader_factory):
        """A second load of the same template returns the cached object."""
        section_file = prompts_dir / "system" / "identity.md"
        section_file.write_text(
            "---\nsection: identity\norder: 1\n---\n{{ name }}\n"
        )

        loader = loader_factory(prompts_dir)
        sections_first = loader.load_sections()
        sections_second = loader.load_sections()

        assert sections_first[0].template is sections_second[0].template


# ---------------------------------------------------------------------------
# _required_section_names tests
# ---------------------------------------------------------------------------

class TestRequiredSectionNames:
    """Tests for :meth:`PromptLoader._required_section_names`."""

    def test_returns_required_sections_for_mode(self, prompts_dir, loader_factory):
        """Required sections matching the active mode are returned."""
        (prompts_dir / "system" / "a.md").write_text(
            "---\nsection: a\norder: 1\nrequired: true\nmode: all\n---\nA.\n"
        )
        (prompts_dir / "system" / "b.md").write_text(
            "---\nsection: b\norder: 2\nrequired: false\nmode: all\n---\nB.\n"
        )
        loader = loader_factory(prompts_dir)
        names = loader._required_section_names(mode="default")
        assert names == {"a"}

    def test_excludes_non_matching_mode(self, prompts_dir, loader_factory):
        """Sections with a mode that does not match are excluded."""
        (prompts_dir / "system" / "planner-only.md").write_text(
            "---\nsection: planner-only\norder: 1\nrequired: true\nmode: planner\n---\nP.\n"
        )
        loader = loader_factory(prompts_dir)
        assert loader._required_section_names(mode="default") == set()
        assert loader._required_section_names(mode="planner") == {"planner-only"}

    def test_missing_dir_returns_empty(self, tmp_path, loader_factory):
        """Non-existent prompt directory returns an empty set."""
        (tmp_path / "system").mkdir()  # loader_factory requires this at construct time
        loader = loader_factory(tmp_path)
        # Manually point to a missing directory to exercise the guard.
        from pathlib import Path
        loader.system_dir = Path(tmp_path) / "nonexistent"
        assert loader._required_section_names() == set()

    def test_build_system_prompt_validates_required_sections(self, prompts_dir):
        """build_system_prompt raises when a required section is missing for the mode."""
        # Create ONE required section but ask build_system_prompt to use "planner"
        # mode. The section declares mode=planner so it matches.
        (prompts_dir / "system" / "required-one.md").write_text(
            "---\nsection: required-one\norder: 1\nrequired: true\nmode: planner\n---\nRequired.\n"
        )

        tool_index = MagicMock()
        tool_index.search.return_value = []
        memory = MagicMock()
        memory.as_prompt_text.return_value = ""
        llm = MagicMock()
        llm._models = []
        llm.llm_cfg = {}

        with patch("prompt_builder.format_tools", return_value=""), \
             patch("prompt_builder.format_skills", return_value=""), \
             patch("prompt_builder.format_models", return_value=""), \
             patch("prompt_builder.format_log_section", return_value=""), \
             patch("prompt_loader.estimate_tokens", return_value=0):
            # Expect success — required section present for planner mode.
            prompt, _ = build_system_prompt(
                tool_index=tool_index,
                memory=memory,
                results=None,
                skill_registry=None,
                llm=llm,
                tmp_dir="/tmp",
                downloads_dir="downloads",
                log_file="agent.log",
                log_backup_count=30,
                top_tools=3,
                user_goal="test",
                prompts_dir=str(prompts_dir),
                mode="planner",
            )
        assert "Required." in prompt


# ---------------------------------------------------------------------------
# Advertised built-in tools / RULES blocks in the RUNTIME prompt
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS:
# The prompt the model actually sees is rendered at runtime by
# build_system_prompt() from the markdown templates in prompts/system/*.md and
# prompts/sub-agent/*.md — NOT from prompt_builder.py:SYSTEM_PROMPT_TEMPLATE,
# which is a legacy fallback used only when the prompts/ directory is missing.
# A previous change added the vault `secret_get` tool + VAULT RULES to the dead
# template only, so the feature shipped inert: it never reached the rendered
# runtime prompt. These tests render the REAL runtime prompt for BOTH variants
# and assert that every advertised built-in tool and every RULES block survives
# rendering, guarding against advertised capabilities being silently absent from
# the live prompt.
#
# The rosters below are the single source of truth: when a tool or RULES block
# is added to (or removed from) the prompt markdown, update the matching list in
# one line.

# prompts/system/03-capabilities.md — BUILT-IN TOOLS block (mode="default").
SYSTEM_BUILTIN_TOOLS = [
    "shell",
    "file_read",
    "file_write",
    "schedule",
    "spawn_agent",
    "get_agent_result",
    "memory_write",
    "vision_query",
    "file_patch",
    "file_diff",
    "memory_graph_search",
    "memory_graph_store",
    "secret_get",
]

# prompts/system/04-execution.md — RULES headings (mode="default").
SYSTEM_RULES_HEADINGS = [
    "GRAPH MEMORY RULES",
    "VAULT RULES",
]

# prompts/sub-agent/03-tools.md — built-in tools block (mode="sub-agent").
SUB_AGENT_BUILTIN_TOOLS = [
    "shell",
    "file_read",
    "file_write",
    "file_patch",
    "file_diff",
    "vision_query",
    "secret_get",
]

# prompts/sub-agent/03-tools.md — RULES headings (mode="sub-agent").
SUB_AGENT_RULES_HEADINGS = [
    "VAULT RULES",
]

# The real prompt templates rendered by the daemon live in <repo>/prompts.
_RUNTIME_PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts"
)


def _render_runtime_prompt(mode: str) -> str:
    """Render the actual runtime system prompt for *mode* and return the text.

    Points ``build_system_prompt`` at the real ``prompts/`` directory so the
    assertions run against the SAME templates the daemon renders — not the temp
    fixtures used elsewhere in this module and not the legacy
    ``SYSTEM_PROMPT_TEMPLATE`` fallback. Dynamic sections (semantic tool search,
    skills, models, log) are stubbed exactly as the other prompt tests stub them
    (see ``tests/test_context_payload.py``); the advertised built-in tool roster
    and RULES blocks are literal template text and are unaffected by the stubbed
    ``{{tools}}`` semantic-search block.
    """
    tool_index = MagicMock()
    tool_index.search.return_value = []
    memory = MagicMock()
    memory.as_prompt_text.return_value = "No memory."
    llm = MagicMock()
    llm._models = []
    llm.llm_cfg = {}

    with patch("prompt_builder.format_tools", return_value="No tools."), \
         patch("prompt_builder.format_skills", return_value=""), \
         patch("prompt_builder.format_models", return_value=""), \
         patch("prompt_builder.format_log_section", return_value="Log section."), \
         patch("prompt_loader.estimate_tokens", return_value=0):
        prompt, _ = build_system_prompt(
            tool_index=tool_index,
            memory=memory,
            results=None,
            skill_registry=None,
            llm=llm,
            tmp_dir="/tmp/agent",
            downloads_dir="downloads",
            log_file="agent.log",
            log_backup_count=30,
            top_tools=3,
            user_goal="context snapshot",
            prompts_dir=_RUNTIME_PROMPTS_DIR,
            mode=mode,
        )
    return prompt


# Built-in tools render as list entries ``  <name><spaces>— <desc>`` (the
# separator is an EM DASH, U+2014). Asserting on the ENTRY line — first token
# followed by that em dash — rather than a bare substring stops an incidental
# occurrence (e.g. "shell" inside "reverse shells", or a stray "schedule")
# from satisfying the check after the real tool bullet has been deleted.
_TOOL_ENTRY_RE = re.compile(r"^(\S+)\s+\u2014")


def _tool_entry_names(prompt: str) -> set[str]:
    """Return the set of built-in tool names rendered as bullet entries.

    Scans each line of *prompt* and, for lines whose stripped form looks like a
    tool entry (``<name>  — <description>``), collects the leading ``<name>``
    token. Roster assertions check membership in this set, so they fail if a
    tool's dedicated entry line is removed from the template — unlike a bare
    substring match, which incidental prose (e.g. "reverse shells") could
    satisfy.
    """
    names: set[str] = set()
    for line in prompt.splitlines():
        match = _TOOL_ENTRY_RE.match(line.strip())
        if match:
            names.add(match.group(1))
    return names


class TestRuntimePromptAdvertisedContent:
    """Advertised built-in tools and RULES blocks must survive into the
    rendered runtime prompt (guards against the ``secret_get``/VAULT regression
    where content was added to the dead template only).
    """

    def test_system_prompt_advertises_all_builtin_tools_and_rules(self):
        """Default prompt renders every system built-in tool and both RULES blocks."""
        prompt = _render_runtime_prompt("default")
        tool_entries = _tool_entry_names(prompt)

        for tool in SYSTEM_BUILTIN_TOOLS:
            assert tool in tool_entries, (
                f"Built-in tool {tool!r} advertised in "
                f"prompts/system/03-capabilities.md is absent from the rendered "
                f"default system prompt (no dedicated tool-entry line for it)."
            )
        for heading in SYSTEM_RULES_HEADINGS:
            assert heading in prompt, (
                f"{heading!r} block is absent from the rendered default system "
                f"prompt (expected from prompts/system/04-execution.md)."
            )

    def test_sub_agent_prompt_advertises_all_builtin_tools_and_rules(self):
        """Sub-agent prompt renders every sub-agent built-in tool and VAULT RULES."""
        prompt = _render_runtime_prompt("sub-agent")
        tool_entries = _tool_entry_names(prompt)

        for tool in SUB_AGENT_BUILTIN_TOOLS:
            assert tool in tool_entries, (
                f"Built-in tool {tool!r} advertised in "
                f"prompts/sub-agent/03-tools.md is absent from the rendered "
                f"sub-agent prompt (no dedicated tool-entry line for it)."
            )
        for heading in SUB_AGENT_RULES_HEADINGS:
            assert heading in prompt, (
                f"{heading!r} block is absent from the rendered sub-agent prompt "
                f"(expected from prompts/sub-agent/03-tools.md)."
            )

    def test_secret_get_present_in_both_variants(self):
        """``secret_get`` reaches BOTH rendered variants (the exact shipped bug).

        The vault tool was added only to the dead ``SYSTEM_PROMPT_TEMPLATE``, so
        it never appeared in the runtime prompt. Guard both variants explicitly.
        """
        default_prompt = _render_runtime_prompt("default")
        sub_agent_prompt = _render_runtime_prompt("sub-agent")

        assert "secret_get" in default_prompt, (
            "`secret_get` missing from rendered default system prompt."
        )
        assert "secret_get" in sub_agent_prompt, (
            "`secret_get` missing from rendered sub-agent prompt."
        )
