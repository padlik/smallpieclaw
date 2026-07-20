"""
prompt_loader.py
----------------
Load, validate, and render Jinja2-based prompt sections from
``prompts/system/*.md`` files.

Each markdown file is expected to start with YAML frontmatter delimited by
``---``. The frontmatter describes the section's metadata; the remainder of the
file is a Jinja2 template that can be rendered with runtime variables.

Example ``prompts/system/identity.md``::

    ---
    section: identity
    order: 0
    required: true
    mode: all
    variables:
      - models_section
    ---
    You are a home-server management agent running on a Raspberry Pi.
    {{ models_section }}

Public API:

* :class:`PromptSection` — metadata + compiled Jinja2 template for a single
  prompt section.
* :class:`PromptLoader` — discovers, parses, compiles, validates, and renders
  prompt sections.
* :func:`build_system_prompt` — high-level replacement for the legacy builder in
  ``prompt_builder.py``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import jinja2
import yaml

from exceptions import ConfigError
from token_estimator import estimate_tokens

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mode defaults
# ---------------------------------------------------------------------------

DEFAULT_MODES = {"default", "planner", "explorer", "resilient"}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PromptLoaderError(ConfigError):
    """Base exception for prompt loading/validation failures."""


class MissingRequiredSectionError(PromptLoaderError):
    """A required prompt section is missing for the active mode."""


class UnresolvedVariableError(PromptLoaderError):
    """A template variable required by a section was not provided."""


class DuplicateOrderError(PromptLoaderError):
    """Two or more sections share the same ordering value."""


class ModeConflictError(PromptLoaderError):
    """Two active sections conflict according to ``conflicts_with``."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PromptSection:
    """A single parsed prompt section.

    Attributes:
        section: Unique identifier for this section.
        order: Sorting order; lower values appear earlier in the prompt.
        required: Whether this section must be present for valid configuration.
        mode: Mode(s) under which this section is included. ``"all"`` means
            every mode.
        variables: Names of variables referenced by the Jinja2 template.
        template: Compiled Jinja2 template.
        raw_content: Template source string (everything after frontmatter).
        conflicts_with: Other section names that cannot coexist with this one.
    """

    section: str
    order: int
    required: bool
    mode: str | list[str]
    variables: list[str]
    template: jinja2.Template
    raw_content: str
    conflicts_with: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------

class PromptLoader:
    """Discover, parse, validate and render Jinja2 prompt sections.

    Args:
        prompts_dir: Directory containing prompt variant subdirectories.
        variant: Subdirectory name to load (default: "system").
        cache: If ``True`` (default), compiled templates are kept in memory.
    """

    def __init__(self, prompts_dir: str, variant: str = "system", cache: bool = True) -> None:
        self.prompts_dir: str = prompts_dir
        self.variant: str = variant
        self.system_dir: Path = Path(prompts_dir) / variant
        self.cache: bool = cache
        self._template_cache: dict[str, jinja2.Template] = {}
        self._env: jinja2.Environment = jinja2.Environment(
            autoescape=False,
            loader=jinja2.FileSystemLoader(str(self.system_dir)),
        )

    def _extract_frontmatter(self, content: str) -> tuple[str, dict[str, Any]]:
        """Split frontmatter from body and return (body, metadata dict).

        Frontmatter is delimited by ``---`` at the very start of the file. If
        no frontmatter is present, an empty metadata dict is returned and the
        whole file is treated as the template body.
        """
        if not content.startswith("---"):
            return content, {}
        # Split on the closing --- delimiter, keeping only the first block.
        parts = content.split("---", 2)
        if len(parts) < 3:
            return content, {}
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as exc:
            raise PromptLoaderError(f"Invalid YAML frontmatter: {exc}") from exc
        body = parts[2].strip("\n")
        return body, meta

    def _compile_template(self, name: str, source: str) -> jinja2.Template:
        """Compile *source* into a Jinja2 template, honouring the cache flag."""
        if self.cache and name in self._template_cache:
            return self._template_cache[name]
        template = self._env.from_string(source)
        if self.cache:
            self._template_cache[name] = template
        return template

    def _extract_variables(self, source: str) -> list[str]:
        """Return top-level undeclared variable names referenced by *source*."""
        from jinja2 import meta as jinja_meta
        ast = self._env.parse(source)
        visitor = jinja_meta.find_undeclared_variables(ast)
        return sorted(visitor)

    def _modes_match(self, section_mode: str | list[str], active_mode: str) -> bool:
        """Return True if *active_mode* is included in *section_mode*.

        The special value ``"all"`` always matches.
        """
        if section_mode == "all":
            return True
        if isinstance(section_mode, str):
            return section_mode == active_mode
        return active_mode in section_mode

    def _required_section_names(self, mode: str = "default") -> set[str]:
        """Return section names that are required for *mode*.

        Scans all ``.md`` files in the prompt directory **without** mode-filtering
        and collects the ``section`` names of those that declare ``required: true``
        and whose declared mode matches *mode*. The result is used by
        :func:`build_system_prompt` as the ``expected_sections`` argument to
        :meth:`validate`, making required-section validation effective even for
        sections that are only loaded in specific modes.

        Note: a section whose file is completely absent from disk cannot be
        detected by this method; only sections that exist on disk but might be
        filtered out by mode are checked.

        Args:
            mode: The active creativity/prompt mode.

        Returns:
            Set of section names that must be present after :meth:`load_sections`.
        """
        if not self.system_dir.exists():
            return set()
        required: set[str] = set()
        for path in self.system_dir.glob("*.md"):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            _, meta = self._extract_frontmatter(content)
            if not bool(meta.get("required", False)):
                continue
            section_mode = meta.get("mode", "all")
            if self._modes_match(section_mode, mode):
                section_name = meta.get("section", path.stem)
                required.add(section_name)
        return required

    def load_sections(self, mode: str = "default") -> list[PromptSection]:
        """Discover and parse all ``*.md`` files for *mode*.

        Args:
            mode: Active mode used to filter sections. Must be one of the
                default modes or a custom mode present in section metadata.

        Returns:
            A list of :class:`PromptSection` objects sorted by ``order``.
        """
        if not self.system_dir.exists():
            raise PromptLoaderError(
                f"Prompt system directory not found: {self.system_dir}"
            )

        sections: list[PromptSection] = []
        for path in sorted(self.system_dir.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            body, meta = self._extract_frontmatter(content)
            section_name = meta.get("section", path.stem)
            order = int(meta.get("order", 0))
            required = bool(meta.get("required", False))
            section_mode = meta.get("mode", "all")
            conflicts_with = list(meta.get("conflicts_with", []))

            if not self._modes_match(section_mode, mode):
                continue

            template = self._compile_template(path.name, body)
            variables = self._extract_variables(body)
            sections.append(
                PromptSection(
                    section=section_name,
                    order=order,
                    required=required,
                    mode=section_mode,
                    variables=variables,
                    template=template,
                    raw_content=body,
                    conflicts_with=conflicts_with,
                )
            )

        sections.sort(key=lambda s: s.order)
        return sections

    def render(self, sections: list[PromptSection], variables: dict[str, Any]) -> str:
        """Render *sections* with *variables* and join with ``\\n\\n``.

        Args:
            sections: Ordered list of prompt sections to render.
            variables: Mapping of variable names to values supplied to Jinja2.

        Returns:
            Concatenated prompt text.
        """
        rendered: list[str] = []
        for section in sections:
            try:
                text = section.template.render(**variables)
            except jinja2.UndefinedError as exc:
                raise UnresolvedVariableError(
                    f"Section '{section.section}' missing variable: {exc}"
                ) from exc
            text = text.strip()
            if text:
                rendered.append(text)
        return "\n\n".join(rendered)

    def validate(
        self,
        sections: list[PromptSection],
        provided_vars: set[str],
        *,
        expected_sections: set[str] | None = None,
    ) -> None:
        """Validate loaded *sections* against *provided_vars*.

        Checks performed:

        1. All names in *expected_sections* are present (derived from
           :meth:`_required_section_names` by callers that need startup
           validation — sections that only exist when the file is absent from
           disk cannot be detected here without a manifest).
        2. No template references a variable absent from *provided_vars*.
        3. No two sections share the same ``order`` value.
        4. No pair of active sections conflict according to ``conflicts_with``.

        Args:
            sections: Sections that have been selected for rendering.
            provided_vars: Set of variable names the caller intends to supply.
            expected_sections: Optional canonical set of section names that must
                be present. If provided and a name is missing, a
                :class:`MissingRequiredSectionError` is raised.

        Raises:
            MissingRequiredSectionError: A required section is missing.
            UnresolvedVariableError: A variable is referenced but not provided.
            DuplicateOrderError: Duplicate ``order`` values exist.
            ModeConflictError: Two sections conflict with each other.
        """
        present = {s.section for s in sections}

        # Required sections: verified via the expected_sections set, which callers
        # derive by scanning all files in the prompt directory before mode-filtering
        # (see _required_section_names). Checking only within the already-filtered
        # ``sections`` list would be vacuous since every loaded section is present
        # by construction.
        if expected_sections is not None and not expected_sections.issubset(present):
            missing = expected_sections - present
            raise MissingRequiredSectionError(
                f"Required prompt section missing: {sorted(missing)[0]}"
            )

        # Variables: every variable referenced by every active template must be
        # provided (or have a default inside the template).
        for section in sections:
            for var in section.variables:
                if var not in provided_vars:
                    raise UnresolvedVariableError(
                        f"Section '{section.section}' requires unresolved variable: {var}"
                    )

        # Duplicate order values.
        orders: dict[int, str] = {}
        for section in sections:
            if section.order in orders:
                raise DuplicateOrderError(
                    f"Duplicate prompt order {section.order} between "
                    f"'{orders[section.order]}' and '{section.section}'"
                )
            orders[section.order] = section.section

        # Mode conflicts: if section A lists B in conflicts_with, and both A
        # and B are active, the configuration is invalid.
        active = {s.section for s in sections}
        for section in sections:
            for other in section.conflicts_with:
                if other in active:
                    raise ModeConflictError(
                        f"Section '{section.section}' conflicts with '{other}'"
                    )


# ---------------------------------------------------------------------------
# High-level builder
# ---------------------------------------------------------------------------

def build_system_prompt(
    *,
    tool_index,
    memory,
    results,
    skill_registry,
    llm,
    tmp_dir: str,
    downloads_dir: str,
    workspace_dir: str = "~/Documents",
    log_file: str,
    log_backup_count: int,
    top_tools: int,
    user_goal: str = "(context snapshot)",
    job_history_section: str = "",
    graph_context_section: str = "",
    strategies_section: str = "",
    results_top_k: int = 2,
    mode: str = "default",
    prompts_dir: Optional[str] = None,
    parent_context_section: str = "",
) -> tuple[str, int]:
    """Build the full system prompt for the ReAct agent.

    This function is a drop-in replacement for the legacy
    :func:`prompt_builder.build_system_prompt` with the same keyword-only
    arguments. When ``prompts_dir`` (default ``prompts`` next to this module)
    exists, sections are loaded from ``prompts/system/*.md`` and rendered via
    Jinja2. Otherwise the legacy ``SYSTEM_PROMPT_TEMPLATE`` from
    ``prompt_builder.py`` is used and a deprecation warning is logged.

    Returns:
        ``(prompt_text, estimated_tokens)``.
    """
    if prompts_dir is None:
        prompts_dir = os.path.join(os.path.dirname(__file__), "prompts")

    system_dir = Path(prompts_dir) / "system"

    if not system_dir.exists():
        logger.warning(
            "Legacy prompts directory %s missing; falling back to SYSTEM_PROMPT_TEMPLATE "
            "from prompt_builder.py. This fallback is deprecated.",
            system_dir,
        )
        return _legacy_build_system_prompt(
            tool_index=tool_index,
            memory=memory,
            results=results,
            skill_registry=skill_registry,
            llm=llm,
            tmp_dir=tmp_dir,
            downloads_dir=downloads_dir,
            workspace_dir=workspace_dir,
            log_file=log_file,
            log_backup_count=log_backup_count,
            top_tools=top_tools,
            user_goal=user_goal,
            job_history_section=job_history_section,
            graph_context_section=graph_context_section,
            results_top_k=results_top_k,
            parent_context_section=parent_context_section,
            strategies_section=strategies_section,
            mode=mode,
        )

    # Choose variant directory: sub-agent prompts live under prompts/sub-agent
    variant = mode if mode == "sub-agent" else "system"
    variant_dir = Path(prompts_dir) / variant
    if not variant_dir.exists():
        # Graceful fallback to system prompts when the requested variant is missing
        logger.warning(
            "Prompt variant directory %s missing; falling back to prompts/system", variant_dir
        )
        variant = "system"
        variant_dir = system_dir

    loader_variant = PromptLoader(prompts_dir, variant=variant)

    sections = loader_variant.load_sections(mode=mode)

    # Prepare variable values. These mirror the legacy template substitutions
    # plus the optional job history block.
    from prompt_builder import format_log_section, format_models, format_skills, format_tools

    relevant_tools = tool_index.search(user_goal, top_k=top_tools)
    tools_text = format_tools(relevant_tools)
    memory_text = memory.as_prompt_text()
    if results and results_top_k > 0:
        past_results_text = results.as_prompt_text(user_goal, top_k=results_top_k)
    elif results:
        past_results_text = "(Skipped — semantic recall provided by graph memory below.)"
    else:
        past_results_text = "No past results."
    skills_section = format_skills(skill_registry)
    models_section = format_models(llm)
    file_storage = (
        f"- User workspace (prefer this for files you create or edit for the user):\n"
        f"    {workspace_dir}  ← trusted zone (no confirmation for normal files)\n"
        f"- Permanent downloads (files the user wants to keep):\n"
        f"    {downloads_dir}\n"
        f"- Temporary files (intermediate outputs, anything only needed for this task):\n"
        f"    {tmp_dir}  ← cleaned by OS on reboot\n"
        f"- Use workspace for work files, downloads for files the user keeps, tmp for temporary operations.\n"
        f"- Never write files to the agent script directory."
    )
    log_section = format_log_section(log_file, log_backup_count)
    graph_ctx_block = f"{graph_context_section}\n\n" if graph_context_section else ""
    parent_ctx_block = f"{parent_context_section}\n\n" if parent_context_section else ""

    variables = {
        "memory": memory_text,
        "past_results": past_results_text,
        "tools": tools_text,
        "skills_section": skills_section,
        "models_section": models_section,
        "file_storage": file_storage,
        "log_section": log_section,
        "graph_context_section": graph_ctx_block,
        "job_history_section": job_history_section,
        "user_goal": user_goal,
        # Sub-agent prompt variant uses {{task}} for the delegated task text,
        # which is the sub-agent's user_goal.
        "task": user_goal,
        "parent_context": parent_ctx_block,
        "strategies": strategies_section,
    }

    provided_vars = set(variables.keys())
    expected = loader_variant._required_section_names(mode=mode) or None
    loader_variant.validate(sections, provided_vars, expected_sections=expected)

    prompt = loader_variant.render(sections, variables)

    # Backwards-compatible injection of job history into the rendered prompt.
    if job_history_section and "RESPONSE FORMAT" in prompt:
        prompt = prompt.replace(
            "RESPONSE FORMAT — CRITICAL:",
            f"{job_history_section}\n\nRESPONSE FORMAT — CRITICAL:",
        )

    return prompt, estimate_tokens(prompt)


def _inject_parent_context_legacy(prompt: str, parent_context_section: str) -> str:
    """Legacy fallback injection for static template prompts.

    Places the parent context section just before the persistent memory section
    so sub-agents see it early without breaking the rest of the template.
    """
    if not parent_context_section:
        return prompt
    return prompt.replace(
        "PERSISTENT MEMORY",
        f"{parent_context_section}\n\nPERSISTENT MEMORY",
    )


def build_spawn_context_summary(
    *,
    user_goal: str,
    working,
    memory,
    results=None,
    graph_memory=None,
    graph_memory_max_entries: int = 3,
    tool_results_count: int = 2,
    max_goal_len: int = 200,
    max_value_len: int = 500,
) -> dict:
    """Build an automatic context payload when spawn_agent receives none.

    Captures:
      - the current user goal (truncated)
      - the last *tool_results_count* tool outcomes from working memory
      - relevant memory entries (memory, results, graph memory)

    All values are plain strings so they serialize cleanly and stay within the
    sub-agent context budget.
    """
    summary: dict[str, str] = {}

    goal_text = (user_goal or "").strip()
    if goal_text:
        summary["parent_goal"] = _truncate_text(goal_text, max_goal_len)

    # Last N tool results from working memory
    if working is not None:
        steps = getattr(working, "steps", []) or []
        tool_steps = [
            s for s in steps
            if isinstance(s, dict) and s.get("action") == "tool"
        ]
        recent = tool_steps[-tool_results_count:] if tool_steps else []
        for i, step in enumerate(recent, start=1):
            details = step.get("details", {})
            tool_name = details.get("tool", "unknown")
            success = "success" if details.get("success", False) else "failure"
            args = details.get("args", {})
            arg_text = ""
            if isinstance(args, dict):
                arg_text = ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items())
            value = f"{tool_name} ({success}) args: {arg_text}"
            summary[f"tool_result_{i}"] = _truncate_text(value, max_value_len)

    # Relevant memory entries
    memory_text = ""
    try:
        memory_text = memory.as_prompt_text()
    except Exception:  # noqa: BLE001
        memory_text = ""
    if memory_text and memory_text != "No persistent memory entries.":
        summary["relevant_memory"] = _truncate_text(memory_text, max_value_len)

    if results is not None:
        try:
            past_results_text = results.as_prompt_text(user_goal, top_k=2)
        except Exception:  # noqa: BLE001
            past_results_text = ""
        if past_results_text and past_results_text != "No past results.":
            summary["relevant_results"] = _truncate_text(past_results_text, max_value_len)

    if graph_memory is not None:
        try:
            graph_text = graph_memory.format_for_prompt(user_goal, max_entries=graph_memory_max_entries)
        except Exception:  # noqa: BLE001
            graph_text = ""
        if graph_text:
            summary["relevant_graph"] = _truncate_text(graph_text, max_value_len)

    return summary


def _truncate_text(text: str, max_len: int) -> str:
    """Return *text* truncated to *max_len* characters with an ellipsis."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _legacy_build_system_prompt(
    *,
    tool_index,
    memory,
    results,
    skill_registry,
    llm,
    tmp_dir: str,
    downloads_dir: str,
    workspace_dir: str = "~/Documents",
    log_file: str,
    log_backup_count: int,
    top_tools: int,
    user_goal: str = "(context snapshot)",
    job_history_section: str = "",
    graph_context_section: str = "",
    results_top_k: int = 2,
    parent_context_section: str = "",
    strategies_section: str = "",
    mode: str = "default",
) -> tuple[str, int]:
    """Build the system prompt using the legacy static template."""
    from prompt_builder import (
        SYSTEM_PROMPT_TEMPLATE,
        format_log_section,
        format_models,
        format_skills,
        format_tools,
    )

    relevant_tools = tool_index.search(user_goal, top_k=top_tools)
    tools_text = format_tools(relevant_tools)
    memory_text = memory.as_prompt_text()
    if results and results_top_k > 0:
        past_results_text = results.as_prompt_text(user_goal, top_k=results_top_k)
    elif results:
        past_results_text = "(Skipped — semantic recall provided by graph memory below.)"
    else:
        past_results_text = "No past results."
    skills_section = format_skills(skill_registry)
    models_section = format_models(llm)
    file_storage = (
        f"- User workspace (prefer this for files you create or edit for the user):\n"
        f"    {workspace_dir}  ← trusted zone (no confirmation for normal files)\n"
        f"- Permanent downloads (files the user wants to keep):\n"
        f"    {downloads_dir}\n"
        f"- Temporary files (intermediate outputs, anything only needed for this task):\n"
        f"    {tmp_dir}  ← cleaned by OS on reboot\n"
        f"- Use workspace for work files, downloads for files the user keeps, tmp for temporary operations.\n"
        f"- Never write files to the agent script directory."
    )
    log_section = format_log_section(log_file, log_backup_count)
    graph_ctx_block = f"{graph_context_section}\n\n" if graph_context_section else ""

    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        memory=memory_text,
        past_results=past_results_text,
        tools=tools_text,
        skills_section=skills_section,
        models_section=models_section,
        file_storage=file_storage,
        log_section=log_section,
        graph_context_section=graph_ctx_block,
    )

    # Inject strategies if provided (legacy fallback path)
    if strategies_section and "LEARNED STRATEGIES" not in prompt:
        prompt = prompt.replace(
            "PERSISTENT MEMORY",
            f"{strategies_section}\n\nPERSISTENT MEMORY",
        )

    if parent_context_section:
        prompt = _inject_parent_context_legacy(prompt, parent_context_section)

    if job_history_section:
        prompt = prompt.replace(
            "RESPONSE FORMAT — CRITICAL:",
            f"{job_history_section}\n\nRESPONSE FORMAT — CRITICAL:",
        )

    return prompt, estimate_tokens(prompt)
