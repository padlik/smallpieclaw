"""
prompt_builder.py
-----------------
System prompt construction for the ReAct agent.

Builds the full system prompt by assembling sections: memory, tools, skills,
models, file storage paths, and agent log configuration.  All functions are
pure (no side effects) and accept their data as parameters.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

# Token estimation lives in token_estimator.py. Re-exported here so existing
# `from prompt_builder import estimate_tokens` callers keep working.
from token_estimator import estimate_messages_tokens, estimate_tokens

if TYPE_CHECKING:
    from model_registry import CatalogView, ModelRecord

# Re-exported for backward compatibility; listed so linters treat them as used.
__all__ = ["estimate_tokens", "estimate_messages_tokens"]

# Built-in tools whose descriptions carry the compact model-catalog summary.
MODEL_AWARE_TOOL_NAMES = frozenset({"spawn_agent", "schedule"})

_LIMIT_FIELDS = (
    ("context_tokens", "context"),
    ("input_tokens", "input"),
    ("output_tokens", "output"),
)
_CAPABILITY_FIELDS = (
    ("vision", "vision"),
    ("tool_calling", "tool calling"),
    ("reasoning", "reasoning"),
)
_CATALOG_DISCLAIMER = (
    "Listing does not guarantee that a model is callable or that the account has access; "
    "it is not a routing promise."
)


# ---------------------------------------------------------------------------
# Model catalog presentation (whitelisted fields only)
# ---------------------------------------------------------------------------

def _one_line(text: str) -> str:
    """Collapse whitespace so provider-supplied text cannot break line structure."""
    return " ".join(str(text).split())


def _format_model_entry(model: ModelRecord) -> str:
    """Render one model as a single line: id, display name, limits, capabilities."""
    metadata = model.metadata
    parts: list[str] = []
    for field_name, label in _LIMIT_FIELDS:
        value = getattr(metadata, field_name)
        if value is None:
            parts.append(f"{label} unknown")
        else:
            parts.append(f"{label} {value:,}")
    for field_name, label in _CAPABILITY_FIELDS:
        value = getattr(metadata, field_name)
        if value is None:
            parts.append(f"{label} unknown")
        else:
            state = "supported" if value else "unsupported"
            parts.append(f"{label} {state}")
    display = _one_line(model.display_name)
    name = f"{model.id} ({display})" if display and display != model.upstream_id else model.id
    return f"{name}: {'; '.join(parts)}"


def _format_pin(view: CatalogView) -> str:
    """Describe the pinned execution model and whether it is still discovered."""
    pin = view.pinned_model
    if pin is None:
        return "none"
    inventory_ids = {model.id for model in view.models}
    where = (
        "still in current inventory, whose current metadata may differ"
        if pin.id in inventory_ids
        else "no longer in current inventory"
    )
    return f"{_format_model_entry(pin)} ({where}; metadata recorded at admission time, not current discovery)"


def _stale_providers(view: CatalogView) -> set[str]:
    """Return the ids of providers whose last discovery attempt failed."""
    return {state.provider_id for state in view.providers if state.status == "stale"}


def _format_current_entry(model: ModelRecord, stale: set[str]) -> str:
    """Render a current inventory record, marking it when its provider is stale."""
    marker = f" (provider {model.provider_id} stale)" if model.provider_id in stale else ""
    return f"{_format_model_entry(model)}{marker}"


def _current_projection(
    view: CatalogView, max_models: int
) -> tuple[ModelRecord | None, list[ModelRecord], int]:
    """Split current inventory into active record, capped other records, omitted count.

    The pinned model never substitutes for a current record: if its id is still
    listed, the current record stays in the capped inventory like any other.
    """
    active = None
    if view.selection_status == "active" and view.active_model_id:
        active = next((m for m in view.models if m.id == view.active_model_id), None)
    listable = sorted(
        (m for m in view.models if active is None or m.id != active.id), key=lambda m: m.id
    )
    shown = listable[: max(max_models, 0)]
    return active, shown, len(listable) - len(shown)


def _format_active_id(view: CatalogView) -> str:
    """Describe the active selection for future runs by id and status only."""
    if view.selection_status == "active" and view.active_model_id:
        return f"{view.active_model_id} (status active)"
    if view.selection_status == "saved_selection_unavailable":
        remembered = view.remembered_model_id or "unknown"
        return f"none (remembered selection {remembered} is unavailable)"
    return "none (no model selected)"


def _format_providers(view: CatalogView) -> str:
    """Return provider freshness without any connection or error details."""
    if not view.providers:
        return "none configured"
    return "; ".join(f"{state.provider_id} {state.status}" for state in view.providers)


def format_model_catalog_summary(view: CatalogView | None, *, max_models: int = 20) -> str:
    """Return the compact one-line catalog summary used in tool descriptions.

    Carries the historical pin (admission-time metadata), the current active
    record and a capped projection of other current records, each with its own
    current limits, capabilities and stale-provider marker.
    """
    if view is None:
        return ""
    stale = _stale_providers(view)
    active, shown, omitted = _current_projection(view, max_models)
    parts = [f"[Model catalog — pinned execution model for this run: {_format_pin(view)}."]
    if active is not None:
        parts.append(
            "Active selection for future runs (status active, current metadata): "
            f"{_format_current_entry(active, stale)}."
        )
    else:
        parts.append(f"Active selection for future runs: {_format_active_id(view)}.")
    if shown:
        entries = " | ".join(_format_current_entry(model, stale) for model in shown)
        parts.append(f"Other current models (current metadata): {entries}.")
    if omitted:
        parts.append(f"{omitted} more current models omitted.")
    parts.append(f"Provider freshness: {_format_providers(view)}. {_CATALOG_DISCLAIMER}]")
    return " ".join(parts)


def format_model_catalog_section(view: CatalogView | None, *, max_models: int = 20) -> str:
    """Return the MODEL CATALOG prompt section for a sanitized catalog view.

    The pinned entry (admission-time metadata) and the current active record
    are rendered outside the inventory cap; the other current records,
    including a still-listed pinned id, are listed in deterministic id order up
    to ``max_models`` entries and the count of unlisted ones is reported.
    Returns ``""`` when *view* is ``None``.
    """
    if view is None:
        return ""
    stale = _stale_providers(view)
    provider_lines = [
        f"{state.provider_id} ({state.adapter}) {state.status}, "
        f"last successful discovery {state.last_success_at or 'never'}"
        for state in view.providers
    ]
    lines = [
        "MODEL CATALOG (discovered provider models; canonical ids are provider-id/upstream-id):",
        f"- {_CATALOG_DISCLAIMER}",
        f"- Provider freshness: {'; '.join(provider_lines) or 'none configured'}",
        f"- Pinned execution model for this run: {_format_pin(view)}",
    ]
    active, shown, omitted = _current_projection(view, max_models)
    if active is not None:
        lines.append(
            f"- Active selection for future runs (status active): {_format_current_entry(active, stale)}"
        )
    else:
        lines.append(f"- Active selection for future runs: {_format_active_id(view)}")

    lines.append(
        f"- Current inventory ({len(view.models)} models, current discovery metadata; "
        "the active entry is listed above):"
    )
    for model in shown:
        lines.append(f"  {_format_current_entry(model, stale)}")
    if omitted > 0:
        lines.append(f"  ... {omitted} more inventory models omitted")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section formatters
# ---------------------------------------------------------------------------

def format_tools(tools, model_catalog_view: CatalogView | None = None) -> str:
    """Format a list of tool objects for inclusion in the system prompt.

    With *model_catalog_view*, built-in ``spawn_agent``/``schedule`` lines get
    the compact catalog summary appended; tool objects are never mutated.
    """
    if not tools:
        return "No additional tools registered."
    summary = format_model_catalog_summary(model_catalog_view)
    lines = []
    for t in tools:
        line = f"  {t.name}: {t.description}"
        if summary and t.name in MODEL_AWARE_TOOL_NAMES and not getattr(t, "is_mcp", False):
            line += f" {summary}"
        lines.append(line)
    return "\n".join(lines)


def format_skills(skill_registry) -> str:
    """Return the AVAILABLE SKILLS prompt section, or empty string if no skills."""
    if not skill_registry:
        return ""
    skills = skill_registry.all()
    if not skills:
        return ""
    lines = ["AVAILABLE SKILLS (read SKILL.md via file_read to activate a skill):"]
    for s in skills:
        lines.append(f"  {s.name}")
        lines.append(f"    SKILL.md: {s.skill_md_path}")
        lines.append(f"    Skill dir: {s.path}/")
        lines.append(f"    {s.description}")
    lines.append("")
    return "\n".join(lines)


def format_models(llm, model_catalog_view: CatalogView | None = None) -> str:
    """Return the models prompt section.

    With *model_catalog_view* the shared MODEL CATALOG section is returned;
    otherwise the legacy AVAILABLE MODELS listing of configured models.
    """
    if model_catalog_view is not None:
        section = format_model_catalog_section(model_catalog_view)
        return f"{section}\n\n" if section else ""
    try:
        models = llm._models
    except AttributeError:
        return ""
    if not models:
        return ""
    active_model = llm.llm_cfg.get("model", "")
    lines = ["AVAILABLE MODELS (use the 'model' parameter in spawn_agent to select):"]
    for m in models:
        name = m.get("name", "")
        model_id = m.get("model", "")
        marker = " ← active" if model_id == active_model else ""
        display = f"  {model_id}"
        if name:
            display += f" ({name})"
        display += marker
        lines.append(display)
    lines.append("")
    return "\n".join(lines) + "\n"


def format_log_section(log_file: str, log_backup_count: int) -> str:
    """Build the AGENT LOG section for the system prompt."""
    log_abs = os.path.abspath(log_file)
    lines = [
        f"- Active log (always current session): {log_abs}",
        "- Rotation: nightly at 00:00 local time. Rotated files use numbered suffixes:",
        f"    {log_abs}    ← today (active)",
        f"    {log_abs}.1  ← yesterday",
        f"    {log_abs}.2  ← 2 days ago  … up to .{log_backup_count}",
        f"- To read recent log entries:  file_read(path='{log_abs}', offset=-10000)",
        f"- To read yesterday's log:     file_read(path='{log_abs}.1')",
        "- Always use file_read with a negative offset (e.g. -20000) to read the tail of large logs.",
        "- Do NOT use 'tail' or 'journalctl' for agent logs — use file_read on the paths above.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """
You are a home-server management agent running on a Raspberry Pi.
You help the user control and query their home server via Telegram.

{models_section}
PERSISTENT MEMORY (facts about this system):
{memory}

NOTE: Never store model names, providers, or API configuration in persistent memory — that
information is always injected fresh above and any memory entry about it will be stale.

RELEVANT PAST RESULTS:
{past_results}

{graph_context_section}BUILT-IN TOOLS (always available):
  shell             — execute any shell command on the host system
  file_read         — read a file from the filesystem
  file_write        — write content to a file on the filesystem
  schedule          — manage scheduled jobs and reminders (actions: list, add, remove, pause, resume, run_now)
  spawn_agent       — spawn an isolated sub-agent in the background; accepts response_format ("text"|"json"|"file"); returns agent_id immediately
  get_agent_result  — wait for a sub-agent to finish and retrieve its typed result; args: agent_id (required), timeout (optional seconds), cancel_on_timeout (bool, default true — auto-cancels agent on timeout)
  wait_for_any_agent — wait for the first of a set of sub-agents to finish and return its result. Call repeatedly with remaining ids to collect results in completion order, then decide whether you have enough before calling finish.
  cancel_agent      — cancel a spawned sub-agent you no longer need, or pass "managed"/"all" to stop all of them. Not confirmation-gated.
  memory_write      — read/write the agent's persistent memory (data/memory.json). Actions: set, append, delete, get; value must be a native JSON value (object, array, number, string) — do NOT pre-serialize to a string; do NOT store model or provider configuration here
  vision_query      — ask the LLM to analyse an image file on disk. Args: path (str, required — absolute path to image), question (str, required — what to ask about the image). Use this whenever the user asks about the contents of a photo or image file. Do NOT use shell to base64-encode or manually analyse images.
  file_patch        — make a surgical search-and-replace edit to a file. Args: path (str), old_str (str — exact text to find; include enough context to be unambiguous), new_str (str — replacement, may be empty to delete), occurrence (int, default 1; 0 = replace all). Prefer this over reading and rewriting the whole file for small targeted edits. Returns an error without changing the file if old_str is not found or is ambiguous.
  file_diff         — compare two files and return a traditional unified diff (read-only). Args: path_a (str, required — first/old file), path_b (str, required — second/new file), context_lines (int, default 3), max_bytes (int, default 200000). Returns the unified diff text, or 'Files are identical.' when there are no differences. Prefer this over shelling out to the `diff` command.
  memory_graph_search — search the knowledge graph for facts, people, preferences, or past events. Args: query (str). Only available when graph memory is enabled.
  memory_graph_store  — store an important fact, preference, or relationship in the knowledge graph. Args: content (str), entity_type (str, optional). Only available when graph memory is enabled.
  secret_get        — retrieve a value from the vault by key. Args: key (str, required). Requires user confirmation. Use this when a skill or task references an unbound API key, token, or endpoint variable.
  shell_env_set     — set a session-scoped environment variable for subsequent shell calls. Args: key (str, required), value (str, required). Replaces `export` (which does not persist across isolated shell calls).
  shell_env_unset   — remove a session-scoped shell environment variable. Args: key (str, required).
  shell_env_list    — list all session-scoped shell environment variables as a JSON object.
  shell_env_get     — get the value of one session-scoped shell environment variable. Args: key (str, required). Returns empty string if not set.

SHELL PERSISTENCE (nsjail backend):
Each shell call runs in a separate jail — `export VAR=value` does NOT persist across calls.
- The working directory inside the sandbox is `/tmp`; host files are not directly accessible
  from shell commands unless their directory has been added to the trusted directories list.
- To read host files, use `file_read`; to write to host directories, they must be approved as
  trusted directories first. Per-session temp files may be written under `/tmp` and survive
  across calls in the same session until the agent shuts down.
- To carry environment variables across shell calls: use shell_env_set once, then the variable
  is injected into every subsequent shell call in this session via nsjail -E flags.
- Do not rely on `export`, `source`, or shell startup files for state between calls.

SUB-AGENT USAGE:
Sub-agents run in complete isolation — they have NO access to your memory, conversation
history, or any files unless you pass them explicitly in the 'task' string.
Write every task as a fully standalone brief using this template:

  Objective : <one-sentence goal>
  Context   : <all paths, extracted data, language requirements, constraints>
  Steps     : <ordered steps if the sequence matters>
  Tools     : <which built-in tools to use — shell, file_read, etc.>
  Output    : <exact format, language, structure, maximum length>

  ✗ Vague:   "Summarise the video in Russian"
  ✓ Explicit: "Summarise the podcast transcript already saved at /tmp/piclaw/clean_transcript.txt
               in Russian. Use file_read to load it. Return three sections: Key Topics,
               Main Arguments, Conclusions. Plain text, maximum 800 words."

- Choose the model deliberately: fast/cheap for data extraction, smarter for analysis.
- Spawn sub-agents concurrently when their tasks are independent of each other.
- Always follow spawn_agent with get_agent_result or wait_for_any_agent to collect results before finishing.
- Confirmation grants are scoped to one tool and one directory. "Confirm" grants last for the current task and cover only the requester (you, or the specific sub-agent run that asked); "Till /reset" grants last for the session and cover you and your sub-agents. Scheduled runs never receive grants.


AVAILABLE TOOLS:
{tools}

{skills_section}FILE STORAGE:
{file_storage}

AGENT LOG:
{log_section}

RESPONSE FORMAT — CRITICAL:
- You MUST respond with ONLY a single valid JSON object. Nothing else.
- No markdown, no prose, no explanation, no ```json fences. Just the raw JSON object.
- Invalid responses waste a step and delay the user.

Possible actions:

1. Execute a tool (built-in or registered):
   {{"action": "tool", "tool": "<tool_name>", "args": {{}}}}

   CORRECT:   {{"action": "tool", "tool": "shell", "args": {{"command": "df -h"}}}}
   WRONG:     {{"action": "shell", "command": "df -h"}}
   WRONG:     {{"action": "tool", "tool": "shell", "args": ["df -h"]}}

2. Finish and return an answer to the user:
   {{"action": "finish", "result": "<your answer>"}}

Rules:
- If the user says "use skill <name>" or the task clearly matches a listed skill, read its SKILL.md first using file_read, then follow the instructions inside.
- SKILL.md files describe *how* to accomplish tasks using shell commands and other means. Any "tools" or sub-commands mentioned inside a SKILL.md are descriptions of functionality — they are NOT registered tools you can call. Do not call them with {{"action": "tool", ...}}. Use shell or file_read to implement the instructions described in the skill.
- file_read automatically rewrites `./`-prefixed paths anywhere in SKILL.md content, and the standard `scripts/`, `assets/`, `references/`, `tests/` subdirs when they appear inside a fenced code block or inline code span, into absolute paths. Any other reference — a bare filename with no `./`, one of those subdirs mentioned only in prose (no backticks/fence), or a subdirectory outside that list — is NOT rewritten; resolve it yourself by joining it to the skill's own directory (`cd <skill_dir> && ...`) before running it.
- Never include dangerous commands (rm -rf /, sudo, eval, reverse shells, etc.).
- If a tool fails, try a different approach or explain the issue.
- Always end with a "finish" action.

GRAPH MEMORY RULES (applies only when memory_graph_search / memory_graph_store are listed above):
- ALWAYS call memory_graph_search BEFORE answering any question that might involve information
  from a prior conversation: people, their preferences, tools they use, past events, rules.
- Do NOT say "I don't have that information" without first calling memory_graph_search.
- Use memory_graph_store when the user shares important facts, preferences, or rules that should
  be remembered across sessions.
- Graph memory persists across conversations — facts survive restarts.

VAULT RULES:
- When a SKILL.md or task references an unbound API key, token, endpoint, or other configuration
  variable (e.g. "Set OLLAMA_HOST to your endpoint" or "use your API_KEY"), use the secret_get
  tool to retrieve it from the vault.
- Do NOT guess values. If a vault key is missing, report the error and stop.
- Vault keys are case-sensitive and match the names in the vault exactly.
- The secret_get tool requires user confirmation before accessing the vault.
""".strip()


# ---------------------------------------------------------------------------
# Main builder
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
    graph_context_section: str = "",
    results_top_k: int = 2,
    model_catalog_view: CatalogView | None = None,
) -> tuple[str, int]:
    """Build the full system prompt for the ReAct agent.

    Returns (prompt_text, estimated_tokens).

    ``results_top_k`` controls how many ResultsMemory entries are injected.
    Pass ``0`` to suppress ResultsMemory recall entirely — callers do this when
    graph memory has already supplied richer semantic recall for this turn,
    avoiding redundant/overlapping recall context in the prompt.

    ``model_catalog_view`` (optional) replaces the legacy models listing with
    the shared MODEL CATALOG section and decorates model-aware tool lines.
    """
    relevant_tools = tool_index.search(user_goal, top_k=top_tools)
    tools_text = format_tools(relevant_tools, model_catalog_view)
    memory_text = memory.as_prompt_text()
    if results and results_top_k > 0:
        past_results_text = results.as_prompt_text(user_goal, top_k=results_top_k)
    elif results:
        past_results_text = "(Skipped — semantic recall provided by graph memory below.)"
    else:
        past_results_text = "No past results."
    skills_section = format_skills(skill_registry)
    models_section = format_models(llm, model_catalog_view)
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

    # Format graph context: inject as a block followed by a newline, or empty
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
    return prompt, estimate_tokens(prompt)
