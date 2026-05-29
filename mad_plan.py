"""
mad_plan.py
-----------
Model Adaptive Planner (MadPlan) orchestrator.

Decomposes a complex task into atomic sub-tasks, selects the best LLM
and parameters for each sub-task from configured models, and executes
them sequentially via sub-agents.

Key improvement over prior implementations: the decomposition prompt
receives an AGENT CAPABILITIES summary (tools, skills, MCP) so the LLM
avoids over-decomposing tasks the agent can execute in a single run.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MadPlanError(Exception):
    """Base class for MadPlan errors."""


class MadPlanExecutionError(MadPlanError):
    """Raised when a sub-task execution fails."""

    def __init__(self, subtask_id: str, model_name: str, error: Exception):
        self.subtask_id = subtask_id
        self.model_name = model_name
        self.cause = error
        super().__init__(
            f"Sub-task '{subtask_id}' failed on model '{model_name}': {error}"
        )


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM = """\
You are an expert task decomposition specialist. Your goal is to produce the \
MINIMUM number of sub-tasks needed — never more.

## Default behavior
Execute the task as a SINGLE continuous flow unless decomposition is clearly justified.
A single sub-task is correct when all steps share session state, or when the agent's \
available capabilities can handle the full task in one run.

## Agent capabilities
The agent executing each sub-task has the following capabilities available natively. \
Consider these when deciding whether to decompose:

{agent_capabilities}

## When to decompose
Decompose ONLY when the task contains multiple steps where each step:
1. Can be executed in isolation, producing a transferable artifact (file, dataset, \
   message, report) consumed by the next step.
2. Does NOT share live session state (browser context, DOM, form state, open connections).

✅ Decompose when each step operates on portable, storable artifacts:
  Download data → Enrich → Summarize → Send email
  Each step takes the previous step's OUTPUT (a file or dataset), not its live state.

❌ Do NOT decompose when:
- Steps share live session context (browser, terminal session, API auth, open file handle).
- The entire task fits within a single sub-agent run using the listed capabilities above.
- Splitting only adds overhead without isolating genuinely independent work.

## Decision rule
Before splitting a step, ask: "Can step N independently process outputs prepared by \
step N-1 earlier — without re-entering the same session or tool context?"
  YES → candidate for decomposition
  NO  → keep as a single atomic task

## Rules for sub-tasks
- ATOMIC: each has a single, well-defined output.
- NON-OVERLAPPING: no two sub-tasks produce the same artifact.
- MINIMAL: batch small steps within a single sub-task.
- Mark dependencies: if sub-task B requires output from sub-task A, set depends_on=["A_id"].
- Independent sub-tasks have depends_on=[].
- IDs MUST be short verb-noun phrases in snake_case reflecting the action and subject \
  (e.g. "fetch_invoice_data", "enrich_contacts", "send_summary_email"). \
  Never use generic labels like "t1", "t2", "task_3".

Respond with a JSON object only (no markdown fences). Schema:
{{
  "subtasks": [
    {{
      "id": "fetch_invoice_data",
      "name": "Fetch invoice data",
      "description": "What exactly this sub-task produces",
      "depends_on": []
    }}
  ]
}}
"""

_DECOMPOSE_USER_TMPL = """\
Analyze the following task. First decide whether decomposition is warranted: \
if the task is a sequential or session-bound workflow, or can be completed by the \
agent in a single run using its available capabilities, return a SINGLE sub-task \
that covers the full task. Otherwise decompose into minimal atomic sub-tasks.

Task:
{task}"""

_MODEL_SELECT_SYSTEM = """\
You are an expert AI model selector. For each sub-task provided, select the most capable \
and cost-effective model from the CONFIGURED models list and write an execution prompt.

IMPORTANT: You MUST select from the "Configured Models" list below. These are the only \
models available for execution. The "Capabilities Reference" section provides additional \
context for models that have documented capabilities — use it to make better decisions, \
but you cannot select a model that is not in the configured list.

## Instructions

For EACH sub-task in the request:
1. Analyze its requirements: reasoning depth, domain knowledge, instruction-following \
   precision, output format, required context length, latency, and cost sensitivity.
2. Select exactly one model from the configured list. Use its "model" field as model_name. \
   If capabilities data is available, ground your selection in specific evidence. \
   If no capabilities data exists, select based on model name, provider, and general knowledge.
3. Set `temperature`, `top_p`, and `max_tokens` appropriate for the task type.
4. Write a complete, self-contained execution prompt that includes all context the model \
   needs and specifies the exact expected output format.

---

Configured Models (select ONLY from this list):
{configured_models_json}

---

Capabilities Reference (enrichment data for models that have documented capabilities):
{capabilities_json}

---

Respond with a JSON object only (no markdown fences). Schema:
{{
  "selections": [
    {{
      "subtask_id": "exact id of the sub-task",
      "model_name": "exact 'model' field from the configured models list",
      "temperature": <number>,
      "top_p": <number>,
      "max_tokens": <integer>,
      "rationale": "2-4 sentences grounding the choice",
      "prompt": "complete ready-to-use execution prompt"
    }}
  ]
}}

Return one entry per sub-task, in the same order as provided.
"""

_MODEL_SELECT_USER_TMPL = """\
Select the best model and write the execution prompt for each sub-task below:

{subtasks_block}"""


# ---------------------------------------------------------------------------
# MadPlanOrchestrator
# ---------------------------------------------------------------------------

class MadPlanOrchestrator:
    """Stateless orchestrator. Instantiate fresh per call."""

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def plan_task(
        self,
        task: str,
        llm_client,
        models_capabilities: list[dict],
        configured_models: Optional[list[dict]] = None,
        agent_capabilities: str = "",
        images: Optional[list[str]] = None,
    ) -> dict:
        """
        Decompose a task and select models for each sub-task.

        Args:
            task: the task description text
            llm_client: LLM client for planning calls
            models_capabilities: capabilities reference data
            configured_models: list from llm_client.list_models()
            agent_capabilities: pre-formatted summary of tools/skills/MCP available to sub-agents
            images: optional list of image descriptions/references

        Returns enriched plan dict:
        {
          "task": str,
          "created_at": ISO-8601 timestamp,
          "subtasks": [
            {
              "id": str, "name": str, "description": str,
              "model_name": str,
              "params": {"temperature": float, "top_p": float, "max_tokens": int},
              "prompt": str, "rationale": str, "depends_on": [str],
            }, ...
          ]
        }
        """
        # Step 1: Decompose
        decompose_task = task
        if images:
            decompose_task += (
                f"\n\n[Note: {len(images)} image attachment(s) are available "
                f"for sub-tasks that require visual analysis.]"
            )

        caps_section = agent_capabilities or "No capability information available."
        decompose_system = _DECOMPOSE_SYSTEM.format(agent_capabilities=caps_section)

        logger.info("MadPlan: decomposing task (%d chars)", len(task))
        raw_decomp = llm_client.chat(
            [{"role": "user", "content": _DECOMPOSE_USER_TMPL.format(task=decompose_task)}],
            system=decompose_system,
            json_mode=True,
        )
        subtasks = self._parse_decomposition(raw_decomp)

        # Step 2: Batch model selection
        configured_models = configured_models or []
        cfg_summary = []
        for m in configured_models:
            entry = {
                "model": m.get("model", ""),
                "name": m.get("name", ""),
                "provider": m.get("provider", ""),
                "vision": m.get("vision", False),
            }
            if m.get("aliases"):
                entry["aliases"] = m["aliases"]
            cfg_summary.append(entry)

        configured_models_json = json.dumps(cfg_summary, indent=2)
        capabilities_json = json.dumps(models_capabilities, indent=2)
        model_system = _MODEL_SELECT_SYSTEM.format(
            configured_models_json=configured_models_json,
            capabilities_json=capabilities_json,
        )

        valid_model_ids = {m.get("model") for m in configured_models if m.get("model")}

        subtask_blocks = []
        for st in subtasks:
            block = (
                f"### Sub-task: {st['id']}\n"
                f"- Name: {st['name']}\n"
                f"- Description: {st['description']}"
            )
            if st.get("depends_on"):
                block += (
                    f"\n- Note: depends on outputs of: {', '.join(st['depends_on'])}. "
                    f"Upstream results will be injected at execution time."
                )
            subtask_blocks.append(block)

        user_msg = _MODEL_SELECT_USER_TMPL.format(
            subtasks_block="\n\n".join(subtask_blocks),
        )

        logger.info(
            "MadPlan: selecting models for %d sub-task(s) in a single call", len(subtasks)
        )
        raw_sel = llm_client.chat(
            [{"role": "user", "content": user_msg}],
            system=model_system,
            json_mode=True,
        )
        selections = self._parse_batch_model_selection(raw_sel, subtasks)

        enriched = []
        for st in subtasks:
            sel = selections.get(st["id"], {})
            selected_model = sel.get("model_name", "")
            if valid_model_ids and selected_model not in valid_model_ids:
                logger.warning(
                    "MadPlan: LLM selected '%s' for sub-task '%s' which is not configured. "
                    "Falling back to first configured model.",
                    selected_model, st["id"],
                )
                selected_model = configured_models[0].get("model", "") if configured_models else ""

            enriched.append({
                "id": st["id"],
                "name": st["name"],
                "description": st["description"],
                "model_name": selected_model,
                "params": {
                    "temperature": sel.get("temperature"),
                    "top_p": sel.get("top_p"),
                    "max_tokens": sel.get("max_tokens"),
                },
                "prompt": sel.get("prompt", st["description"]),
                "rationale": sel.get("rationale", ""),
                "depends_on": st.get("depends_on", []),
            })

        return {
            "task": task,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "subtasks": enriched,
        }

    def _parse_decomposition(self, raw: str) -> list[dict]:
        data = _extract_json(raw)
        subtasks = data.get("subtasks", [])
        if not isinstance(subtasks, list) or not subtasks:
            raise MadPlanError(f"Decomposition returned no sub-tasks. Raw: {raw[:300]}")

        result = []
        id_mapping: dict[str, str] = {}
        for i, st in enumerate(subtasks):
            if not isinstance(st, dict):
                continue
            name = str(st.get("name", f"subtask_{i + 1}"))
            original_id = str(st.get("id", ""))
            raw_id = original_id
            if not raw_id or re.match(r"^t\d+$|^task[_\s]?\d+$", raw_id, re.IGNORECASE):
                slug = re.sub(r"[^\w]+", "_", name.lower()).strip("_")[:40] or f"subtask_{i + 1}"
                raw_id = slug
                if original_id:
                    id_mapping[original_id] = raw_id
            result.append({
                "id": raw_id,
                "name": name,
                "description": str(st.get("description", "")),
                "depends_on": [str(d) for d in st.get("depends_on", [])],
            })
        if not result:
            raise MadPlanError("Decomposition returned empty sub-task list.")

        if id_mapping:
            for st in result:
                st["depends_on"] = [id_mapping.get(dep, dep) for dep in st["depends_on"]]

        return result

    def _parse_batch_model_selection(self, raw: str, subtasks: list[dict]) -> dict:
        """Parse a batched model selection response into a dict keyed by subtask_id."""
        data = _extract_json(raw)
        selections = data.get("selections")
        if selections is None:
            selections = [data]
        elif not isinstance(selections, list):
            selections = [data]

        result: dict[str, dict] = {}
        for sel in selections:
            if not isinstance(sel, dict):
                continue
            entry = {
                "model_name": str(sel.get("model_name", "")),
                "temperature": _to_float(sel.get("temperature")),
                "top_p": _to_float(sel.get("top_p")),
                "max_tokens": _to_int(sel.get("max_tokens")),
                "rationale": str(sel.get("rationale", "")),
                "prompt": str(sel.get("prompt", "")),
            }
            subtask_id = str(sel.get("subtask_id", ""))
            if subtask_id:
                result[subtask_id] = entry

        # Assign remaining unmatched selections in order
        subtask_ids = {st["id"] for st in subtasks}
        unmatched = [st for st in subtasks if st["id"] not in result]
        unassigned = [
            sel for sel in selections
            if isinstance(sel, dict) and str(sel.get("subtask_id", "")) not in subtask_ids
        ]
        for st, sel in zip(unmatched, unassigned):
            result[st["id"]] = {
                "model_name": str(sel.get("model_name", "")),
                "temperature": _to_float(sel.get("temperature")),
                "top_p": _to_float(sel.get("top_p")),
                "max_tokens": _to_int(sel.get("max_tokens")),
                "rationale": str(sel.get("rationale", "")),
                "prompt": str(sel.get("prompt", "")),
            }

        return result

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_plan_html(self, plan: dict) -> str:
        """Render plan as Telegram HTML."""
        task_preview = html.escape(plan["task"][:200])
        created = plan.get("created_at", "")[:19].replace("T", " ")
        subtasks = plan.get("subtasks", [])

        lines = [
            "🧠 <b>MadPlan</b>",
            f"<i>{task_preview}</i>",
            f"<code>{created} UTC</code>",
            "",
            f"<b>{len(subtasks)} sub-task(s):</b>",
        ]

        for st in subtasks:
            dep_str = ""
            if st.get("depends_on"):
                dep_str = f" ← {', '.join(st['depends_on'])}"

            params = st.get("params", {})
            param_parts = []
            if params.get("temperature") is not None:
                param_parts.append(f"t={params['temperature']}")
            if params.get("top_p") is not None:
                param_parts.append(f"top_p={params['top_p']}")
            if params.get("max_tokens") is not None:
                param_parts.append(f"max={params['max_tokens']}")
            param_str = f" ({', '.join(param_parts)})" if param_parts else ""

            lines.append(
                f"\n<b>{html.escape(st['id'])}. {html.escape(st['name'])}</b>"
                f"{html.escape(dep_str)}"
            )
            lines.append(
                f"  🤖 <code>{html.escape(st.get('model_name', 'N/A'))}</code>"
                f"{html.escape(param_str)}"
            )
            if st.get("rationale"):
                short = html.escape(st["rationale"][:120])
                ellipsis = "…" if len(st["rationale"]) > 120 else ""
                lines.append(f"  <i>{short}{ellipsis}</i>")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Save / Load / List
    # ------------------------------------------------------------------

    def save_plan(self, plan: dict, plans_dir: str) -> tuple[str, str]:
        """
        Save plan as Markdown to plans_dir/<plan_name>/plan.md.

        The plan_name is derived from the task text (first ~5 words, slugified, max 50 chars).
        Returns (plan_name, absolute_file_path).
        """
        task_text = plan.get("task", "plan")
        created_at = plan.get("created_at", datetime.now(timezone.utc).isoformat())
        words = re.findall(r"[a-zA-Z0-9]+", task_text)[:5]
        slug = "_".join(w.lower() for w in words)[:50] if words else "plan"

        # Guarantee a unique directory name
        plan_dir = os.path.join(plans_dir, slug)
        if os.path.exists(plan_dir):
            ts_suffix = re.sub(r"[^\d]", "", created_at[:19])
            slug = f"{slug}_{ts_suffix}"
            plan_dir = os.path.join(plans_dir, slug)
            counter = 2
            while os.path.exists(plan_dir):
                new_slug = f"{slug}_{counter}"
                plan_dir = os.path.join(plans_dir, new_slug)
                counter += 1
            slug = os.path.basename(plan_dir)

        os.makedirs(plan_dir, exist_ok=True)
        path = os.path.join(plan_dir, "plan.md")

        lines = [
            f"# Plan: {task_text[:80]}",
            "",
            "## Task Description",
            task_text,
            "",
            "## Created",
            created_at,
            "",
            "## Sub-tasks",
            "",
        ]

        for i, st in enumerate(plan.get("subtasks", []), 1):
            params = st.get("params", {})
            dep_str = ", ".join(st.get("depends_on", [])) or "none"
            lines += [
                f"### {i}. {st['name']}",
                f"- **ID:** {st['id']}",
                f"- **Model:** {st.get('model_name', 'N/A')}",
                (
                    f"- **Parameters:** temperature={params.get('temperature')}, "
                    f"top_p={params.get('top_p')}, max_tokens={params.get('max_tokens')}"
                ),
                f"- **Depends on:** {dep_str}",
                "- **Parallel with:** none",
                "- **Prompt:**",
                f"  {st.get('prompt', '')}",
                "",
            ]

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

        logger.info("MadPlan: plan saved to %s", path)
        return slug, path

    def load_plan(self, plan_name: str, plans_dir: str) -> dict:
        """
        Load a plan from plans_dir/<plan_name>/plan.md.
        Returns a minimal plan dict with task, created_at, and subtasks (prompts only).
        Raises MadPlanError if not found or unreadable.
        """
        path = os.path.join(plans_dir, plan_name, "plan.md")
        if not os.path.exists(path):
            raise MadPlanError(f"Plan '{plan_name}' not found at {path}")
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            raise MadPlanError(f"Could not read plan '{plan_name}': {exc}") from exc

        # Parse task description
        task = ""
        created_at = ""
        subtasks = []

        lines = content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("## Task Description"):
                i += 1
                task_lines = []
                while i < len(lines) and not lines[i].startswith("##"):
                    task_lines.append(lines[i])
                    i += 1
                task = "\n".join(task_lines).strip()
                continue
            if line.startswith("## Created"):
                i += 1
                if i < len(lines):
                    created_at = lines[i].strip()
                i += 1
                continue
            if re.match(r"^### \d+\.", line):
                name = re.sub(r"^### \d+\.\s*", "", line).strip()
                st: dict = {"id": "", "name": name, "description": "", "model_name": "",
                            "params": {}, "prompt": "", "rationale": "", "depends_on": []}
                i += 1
                while i < len(lines) and not re.match(r"^### \d+\.", lines[i]) and not lines[i].startswith("## "):
                    ln = lines[i]
                    if ln.startswith("- **ID:**"):
                        st["id"] = ln.split("**ID:**", 1)[-1].strip()
                    elif ln.startswith("- **Model:**"):
                        st["model_name"] = ln.split("**Model:**", 1)[-1].strip()
                    elif ln.startswith("- **Depends on:**"):
                        deps_str = ln.split("**Depends on:**", 1)[-1].strip()
                        st["depends_on"] = [d.strip() for d in deps_str.split(",") if d.strip() and d.strip() != "none"]
                    elif ln.startswith("  ") and st.get("_in_prompt"):
                        st["prompt"] += ln[2:] + "\n"
                    elif ln.startswith("- **Prompt:**"):
                        st["_in_prompt"] = True
                    else:
                        st["_in_prompt"] = False
                    i += 1
                st.pop("_in_prompt", None)
                st["prompt"] = st["prompt"].strip()
                if not st["id"]:
                    st["id"] = re.sub(r"[^\w]+", "_", st["name"].lower()).strip("_")[:40] or f"subtask_{len(subtasks)+1}"
                subtasks.append(st)
                continue
            i += 1

        return {
            "task": task,
            "created_at": created_at,
            "subtasks": subtasks,
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute_plan(
        self,
        plan: dict,
        sub_agent_factory: Callable,
        notify_fn: Optional[Callable] = None,
    ) -> tuple[list[dict], Optional[dict]]:
        """
        Execute sub-tasks sequentially.

        Returns (completed_results, failure_info).
        failure_info is None on full success, or {subtask_id, model_name, error, remaining}.
        """
        results: dict[str, str] = {}
        output: list[dict] = []
        subtasks = plan.get("subtasks", [])

        for i, st in enumerate(subtasks):
            st_id = st["id"]
            model_name = st.get("model_name", "")
            params = st.get("params", {})
            prompt = st.get("prompt", st.get("description", ""))

            upstream_parts = []
            for dep_id in st.get("depends_on", []):
                if dep_id in results:
                    upstream_parts.append(f"## Result of sub-task {dep_id}\n{results[dep_id]}")
                else:
                    logger.warning(
                        "MadPlan: sub-task '%s' depends on '%s' which has no result",
                        st_id, dep_id,
                    )
                    upstream_parts.append(
                        f"## Result of sub-task {dep_id}\n"
                        f"[WARNING: dependency '{dep_id}' has no result]"
                    )
            if upstream_parts:
                prompt = "\n\n".join(upstream_parts) + "\n\n---\n\n" + prompt

            if notify_fn:
                notify_fn(f"▶ Sub-task {st_id}: {st['name']} [{model_name}]")

            logger.info("MadPlan: executing sub-task '%s' with model '%s'", st_id, model_name)

            try:
                runner = sub_agent_factory(
                    model=model_name or None,
                    label=f"madplan-{st_id}",
                    temperature=params.get("temperature"),
                    top_p=params.get("top_p"),
                    max_tokens=params.get("max_tokens"),
                )
                result = runner.run(prompt)
            except Exception as exc:
                logger.error("MadPlan: sub-task '%s' failed: %s", st_id, exc)
                remaining = [s["id"] for s in subtasks[i + 1:]]
                return output, {
                    "subtask_id": st_id,
                    "model_name": model_name,
                    "error": str(exc),
                    "remaining": remaining,
                }

            results[st_id] = result
            output.append({"id": st_id, "name": st["name"], "result": result})
            logger.info("MadPlan: sub-task '%s' completed", st_id)

        return output, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_models_capabilities(data_dir: str) -> list[dict]:
    """Load models_capabilities.json from data_dir. Returns empty list on error."""
    path = os.path.join(data_dir, "models_capabilities.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("models", []) if isinstance(data, dict) else data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("MadPlan: could not load models_capabilities.json: %s", exc)
        return []


def list_plans(plans_dir: str) -> list[str]:
    """Return list of plan names (subdirectory names that contain plan.md)."""
    if not os.path.isdir(plans_dir):
        return []
    names = []
    for entry in sorted(os.listdir(plans_dir)):
        plan_file = os.path.join(plans_dir, entry, "plan.md")
        if os.path.isdir(os.path.join(plans_dir, entry)) and os.path.exists(plan_file):
            names.append(entry)
    return names


def validate_models_for_mad_plan(
    configured_models: list[dict],
    capabilities: list[dict],
) -> dict:
    """
    Cross-reference configured models against capabilities data.

    Matching priority:
    1. Exact match on model field
    2. Exact match on any alias

    Returns:
        {
            "available": [...],
            "with_capabilities": [...],
            "missing_capabilities": [...],
        }
    """
    cap_names = {cap["model_name"]: cap for cap in capabilities}
    available = []
    with_caps = []
    missing_caps = []

    for m in configured_models:
        model_id = m.get("model", "")
        if not model_id:
            continue

        matched_cap = cap_names.get(model_id)
        if not matched_cap:
            for alias in m.get("aliases", []):
                matched_cap = cap_names.get(alias)
                if matched_cap:
                    break

        entry = {
            "model": model_id,
            "name": m.get("name", ""),
            "provider": m.get("provider", ""),
            "vision": m.get("vision", False),
            "capabilities": matched_cap,
        }
        available.append(entry)
        if matched_cap:
            with_caps.append(entry)
        else:
            missing_caps.append(entry)

    return {
        "available": available,
        "with_capabilities": with_caps,
        "missing_capabilities": missing_caps,
    }


def build_agent_capabilities_summary(
    tool_registry=None,
    skill_registry=None,
    mcp_manager=None,
    builtin_tool_names: Optional[list[str]] = None,
) -> str:
    """
    Build a compact capabilities summary string for the decomposition prompt.

    Args:
        tool_registry: optional ToolRegistry — exposes registered custom tools
        skill_registry: optional SkillRegistry — exposes agent skills
        mcp_manager: optional MCPManager — exposes connected MCP tool names
        builtin_tool_names: override list of built-in tool names (for testing)

    Returns a multi-line string describing what a sub-agent can do natively.
    """
    lines: list[str] = []

    # Built-in tools
    if builtin_tool_names is None:
        builtin_tool_names = [
            "shell", "file_read", "file_write", "file_append",
            "spawn_agent", "get_agent_result", "cancel_agent",
            "web_fetch", "http_request",
        ]
    if builtin_tool_names:
        lines.append("Built-in tools: " + ", ".join(builtin_tool_names))

    # Registered custom tools
    if tool_registry is not None:
        try:
            tools = tool_registry.all() if hasattr(tool_registry, "all") else []
            if tools:
                names = [getattr(t, "name", str(t)) for t in tools]
                lines.append("Registered tools: " + ", ".join(names))
        except Exception:
            pass

    # Skills
    if skill_registry is not None:
        try:
            skills = skill_registry.all() if hasattr(skill_registry, "all") else []
            if skills:
                skill_descs = []
                for s in skills:
                    name = getattr(s, "name", "?")
                    desc = getattr(s, "description", "")
                    skill_descs.append(f"{name} ({desc})" if desc else name)
                lines.append("Skills: " + ", ".join(skill_descs))
        except Exception:
            pass

    # MCP tools
    if mcp_manager is not None:
        try:
            mcp_tools = []
            if hasattr(mcp_manager, "list_tools"):
                mcp_tools = mcp_manager.list_tools() or []
            elif hasattr(mcp_manager, "get_all_tools"):
                mcp_tools = mcp_manager.get_all_tools() or []
            if mcp_tools:
                mcp_names = []
                for t in mcp_tools:
                    if isinstance(t, dict):
                        mcp_names.append(t.get("name", str(t)))
                    else:
                        mcp_names.append(getattr(t, "name", str(t)))
                lines.append("MCP tools: " + ", ".join(mcp_names))
        except Exception:
            pass

    return "\n".join(lines) if lines else "No capability information available."


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict:
    """Extract and parse the first JSON object from a string."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        raise MadPlanError(f"Could not parse JSON from LLM response: {raw[:300]}")


def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
