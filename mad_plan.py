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
import shutil
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

_STRATEGY_SYSTEM = """\
You are an expert task analyst and execution strategist. Your job is to deeply \
understand a task before any execution begins — to think through HOW it should \
be done, not just DO it.

## Your role
Given a task and the available agent capabilities, produce a concrete execution \
strategy that answers:
1. What exactly does this task require? (clarify ambiguity, identify the real goal)
2. What constraints or unknowns could affect execution? \
   (permissions, OS, API availability, data access, rate limits, etc.)
3. What is the best primary approach — and why?
4. What are the fallback approaches if the primary fails?
5. What information needs to be DISCOVERED at runtime before the main work can proceed?
6. What are the natural phases of work, and which can run independently vs. sequentially?

## Available agent capabilities
The agent that will execute sub-tasks has these capabilities:

{agent_capabilities}

## Output format
Respond with a JSON object only (no markdown fences):
{{
  "task_summary": "One sentence restating the task in concrete terms",
  "short_name": "3-5 word snake_case slug for the plan directory, e.g. check_ufw_without_root or docker_iptables_probe (no generic words like task/write/check at the start — use the domain noun)",
  "constraints": ["each constraint or unknown as a short phrase"],
  "primary_approach": "Detailed description of the recommended execution strategy",
  "fallback_approaches": ["fallback 1 if primary fails", "fallback 2", ...],
  "discovery_needed": ["things the agent must discover at runtime before acting"],
  "execution_phases": [
    {{
      "phase": "short name",
      "goal": "what this phase produces or achieves",
      "depends_on_discovery": true/false,
      "can_run_independently": true/false
    }}
  ],
  "notes": "any other important observations about executing this task"
}}
"""

_STRATEGY_USER_TMPL = """\
Analyze the following task and produce a complete execution strategy.

Task:
{task}"""

_DECOMPOSE_SYSTEM = """\
You are an expert task decomposition specialist. Your goal is to produce the \
MINIMUM number of sub-tasks needed — never more.

## Input
You receive:
1. The original user task
2. An execution strategy already prepared by a task analyst — use it as your \
   primary guide for decomposition decisions

## Default behavior
Return a SINGLE sub-task covering the full task unless decomposition is clearly \
justified by the execution strategy. A single sub-task is correct when:
- All work shares the same execution context (terminal session, browser, open connection)
- OR the strategy identifies no distinct independent phases

## Agent capabilities
The agent executing each sub-task has the following capabilities available natively. \
Consider these when deciding whether to decompose:

{agent_capabilities}

## When to decompose
Decompose when the execution strategy identifies phases where:
1. A phase produces a portable artifact (file, dataset, report, findings) consumed \
   by the next phase.
2. The phase does NOT require shared live session state from another phase.

✅ Decompose following the strategy's execution_phases when phases are independent:
  "Discover accessible methods" → "Execute using discovered method" → "Report results"

❌ Do NOT decompose when:
- Phases share a live session (browser, terminal, API auth, open file handle).
- The strategy marks all phases as NOT independently runnable.
- Splitting adds overhead without isolating genuinely separate work.

## Decision rule
Before splitting a step, ask: "Does the execution strategy identify this as a \
separate phase with a distinct output?" AND "Can this phase independently process \
a portable artifact from the previous phase?"
  YES to both → split it
  NO to either → keep as one task

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
Task:
{task}

Execution Strategy (use this to guide decomposition):
{strategy}

Decide: can this task be handled in a single sub-task, or does the strategy \
identify distinct independent phases that should be split? Return the minimum \
number of sub-tasks."""

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
4. Write a complete, self-contained execution prompt that includes:
   - Embeds ALL relevant context from the execution strategy (constraints, fallbacks, \
     discovery needs) so the agent does not need to re-derive the approach
   - Specifies the exact expected output format
   - For discovery/research sub-tasks: lists ALL approaches to try in priority order, \
     instructs the agent to test each and produce a structured report of findings \
     (what worked, what failed, why) for use by downstream sub-tasks
   - For execution sub-tasks that depend on upstream results: explicitly describes \
     how to interpret the upstream findings and which approach to use based on them
   - For sub-tasks involving system commands or uncertain operations: lists multiple \
     approaches in priority order with fallback instructions
   - Is tailored to the model's known strengths and limitations

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
Execution Strategy (background context for writing prompts):
{strategy}

---

Select the best model and write the execution prompt for each sub-task below:

{subtasks_block}"""

_REVISE_SYSTEM = """\
You are an expert task planner. A MadPlan has already been prepared for a task. \
The user has reviewed the plan and provided feedback. Your job is to incorporate \
the feedback and produce a complete, revised plan.

## Agent capabilities
The agent executing each sub-task has the following capabilities available. \
Use this to decide whether tasks require decomposition and to write accurate \
execution prompts:

{agent_capabilities}

## What you receive
- The original task
- The current execution strategy
- The current sub-tasks with their model selections and execution prompts
- The user's feedback / additional information

## What you produce
A fully revised plan in a single JSON response. Update only what the feedback \
requires — preserve unchanged sections as-is. The response must always include \
all three sections even if unchanged.

Respond with a JSON object only (no markdown fences):
{{
  "strategy": {{
    "task_summary": "...",
    "short_name": "3-5 word snake_case slug, e.g. ufw_check_without_root (update if the approach changed significantly)",
    "constraints": [...],
    "primary_approach": "...",
    "fallback_approaches": [...],
    "discovery_needed": [...],
    "execution_phases": [
      {{"phase": "...", "goal": "...", "depends_on_discovery": true/false, "can_run_independently": true/false}}
    ],
    "notes": "..."
  }},
  "subtasks": [
    {{
      "id": "snake_case_verb_noun",
      "name": "Short name",
      "description": "What this sub-task produces",
      "depends_on": []
    }}
  ],
  "selections": [
    {{
      "subtask_id": "exact id from subtasks",
      "model_name": "exact model field from configured models",
      "temperature": <number>,
      "top_p": <number>,
      "max_tokens": <integer>,
      "rationale": "2-4 sentences",
      "prompt": "complete ready-to-use execution prompt"
    }}
  ]
}}

Rules for sub-task IDs: short verb-noun phrases in snake_case \
(e.g. "fetch_invoice_data", "run_docker_probe"). Never use "t1", "t2", etc.
"""

_REVISE_USER_TMPL = """\
Original task:
{task}

Current strategy:
{strategy}

Current sub-tasks and model selections:
{subtasks_block}

---

User feedback / new information:
{feedback}

---

Configured models (select ONLY from this list for model_name fields):
{configured_models_json}

Produce the revised plan incorporating the feedback above."""


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
        Research, decompose, and assign models for a task.

        Pipeline:
          1. Strategy  – analyse the task: constraints, approach, fallbacks, phases
          2. Decompose – split the plan into minimal independent sub-tasks
          3. Select    – choose best model + write execution prompt per sub-task

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
          "strategy": dict,          # Phase 1 output
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
        caps_section = agent_capabilities or "No capability information available."

        full_task = task
        if images:
            full_task += (
                f"\n\n[Note: {len(images)} image attachment(s) are available "
                f"for sub-tasks that require visual analysis.]"
            )

        # Step 1: Research & strategy
        strategy_system = _STRATEGY_SYSTEM.format(agent_capabilities=caps_section)
        logger.info("MadPlan: building execution strategy for task (%d chars)", len(task))
        raw_strategy = llm_client.chat(
            [{"role": "user", "content": _STRATEGY_USER_TMPL.format(task=full_task)}],
            system=strategy_system,
            json_mode=True,
        )
        strategy = self._parse_strategy(raw_strategy)
        strategy_text = json.dumps(strategy, indent=2)
        logger.info(
            "MadPlan: strategy ready — %d constraint(s), %d phase(s)",
            len(strategy.get("constraints", [])),
            len(strategy.get("execution_phases", [])),
        )

        # Step 2: Decompose (guided by strategy)
        decompose_system = _DECOMPOSE_SYSTEM.format(agent_capabilities=caps_section)
        logger.info("MadPlan: decomposing task")
        raw_decomp = llm_client.chat(
            [{"role": "user", "content": _DECOMPOSE_USER_TMPL.format(
                task=full_task,
                strategy=strategy_text,
            )}],
            system=decompose_system,
            json_mode=True,
        )
        subtasks = self._parse_decomposition(raw_decomp)

        # Step 3: Batch model selection (informed by strategy)
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
            strategy=strategy_text,
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
            "strategy": strategy,
            "subtasks": enriched,
        }

    def revise_plan(
        self,
        plan: dict,
        feedback: str,
        llm_client,
        models_capabilities: list[dict],
        configured_models: Optional[list[dict]] = None,
        agent_capabilities: str = "",
    ) -> dict:
        """
        Revise an existing plan using user feedback in a single LLM call.

        The LLM receives the full current plan (strategy + subtasks with prompts)
        and the user's feedback, then returns a revised strategy, subtask list,
        and model selections all at once.

        Returns a new plan dict with the same schema as plan_task(), preserving
        any internal metadata keys (_plan_name, _saved_path) from the original.
        """
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

        valid_model_ids = {m.get("model") for m in configured_models if m.get("model")}

        current_strategy = json.dumps(plan.get("strategy", {}), indent=2)

        subtasks_block_parts = []
        for st in plan.get("subtasks", []):
            dep_str = f" ← {', '.join(st['depends_on'])}" if st.get("depends_on") else ""
            params = st.get("params", {})
            param_str = ", ".join(
                f"{k}={v}" for k, v in params.items() if v is not None
            )
            block = (
                f"### {st['id']}: {st['name']}{dep_str}\n"
                f"Description: {st['description']}\n"
                f"Model: {st.get('model_name', 'N/A')} ({param_str})\n"
                f"Prompt: {st.get('prompt', '')[:300]}"
                + ("…" if len(st.get("prompt", "")) > 300 else "")
            )
            subtasks_block_parts.append(block)

        user_msg = _REVISE_USER_TMPL.format(
            task=plan.get("task", ""),
            strategy=current_strategy,
            subtasks_block="\n\n".join(subtasks_block_parts),
            feedback=feedback,
            configured_models_json=json.dumps(cfg_summary, indent=2),
        )

        logger.info("MadPlan: revising plan with user feedback (%d chars)", len(feedback))
        caps_section = agent_capabilities or "No capability information available."
        revise_system = _REVISE_SYSTEM.format(agent_capabilities=caps_section)
        raw = llm_client.chat(
            [{"role": "user", "content": user_msg}],
            system=revise_system,
            json_mode=True,
        )
        revised = self._parse_revision(raw, plan, valid_model_ids, configured_models)

        # Preserve internal metadata from the original plan
        for key in ("_plan_name", "_saved_path"):
            if key in plan:
                revised[key] = plan[key]

        return revised

    def _parse_revision(
        self,
        raw: str,
        original_plan: dict,
        valid_model_ids: set,
        configured_models: list[dict],
    ) -> dict:
        """Parse the combined revision JSON; fall back gracefully on partial failures."""
        try:
            data = _extract_json(raw)
        except MadPlanError:
            logger.warning("MadPlan: could not parse revision JSON, returning original plan")
            return dict(original_plan)
        if not isinstance(data, dict):
            logger.warning("MadPlan: revision JSON was not an object, returning original plan")
            return dict(original_plan)

        strategy = self._parse_strategy(json.dumps(data.get("strategy", {})))

        subtasks_raw = data.get("subtasks", [])
        if isinstance(subtasks_raw, list) and subtasks_raw:
            try:
                subtasks = self._parse_decomposition(
                    json.dumps({"subtasks": subtasks_raw})
                )
            except MadPlanError:
                logger.warning("MadPlan: revision subtask parse failed, keeping original subtasks")
                subtasks = original_plan.get("subtasks", [])
        else:
            subtasks = original_plan.get("subtasks", [])

        selections_raw = data.get("selections", [])
        if isinstance(selections_raw, list) and selections_raw:
            selections = self._parse_batch_model_selection(
                json.dumps({"selections": selections_raw}), subtasks
            )
        else:
            # Partial response: preserve original model/prompt/params keyed by subtask id
            selections = {
                st["id"]: {
                    "model_name": st.get("model_name", ""),
                    "temperature": st.get("params", {}).get("temperature"),
                    "top_p": st.get("params", {}).get("top_p"),
                    "max_tokens": st.get("params", {}).get("max_tokens"),
                    "prompt": st.get("prompt", ""),
                    "rationale": st.get("rationale", ""),
                }
                for st in original_plan.get("subtasks", [])
            }

        enriched = []
        for st in subtasks:
            sel = selections.get(st["id"], {})
            selected_model = sel.get("model_name", "")
            if valid_model_ids and selected_model not in valid_model_ids:
                logger.warning(
                    "MadPlan revision: LLM selected '%s' for '%s' which is not configured, "
                    "falling back to first configured model.",
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
                "prompt": sel.get("prompt", st.get("description", "")),
                "rationale": sel.get("rationale", ""),
                "depends_on": st.get("depends_on", []),
            })

        return {
            "task": original_plan.get("task", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy,
            "subtasks": enriched,
        }

    def _parse_strategy(self, raw: str) -> dict:
        """Parse strategy JSON; return empty-but-valid dict on failure."""
        try:
            data = _extract_json(raw)
        except MadPlanError:
            logger.warning("MadPlan: could not parse strategy JSON, using empty strategy")
            data = {}
        if not isinstance(data, dict):
            logger.warning(
                "MadPlan: strategy JSON was not an object (got %s), using empty strategy",
                type(data).__name__,
            )
            data = {}
        required_keys = {
            "task_summary", "short_name", "constraints", "primary_approach",
            "fallback_approaches", "discovery_needed", "execution_phases", "notes",
        }
        for key in required_keys:
            if key not in data:
                data[key] = [] if key in (
                    "constraints", "fallback_approaches",
                    "discovery_needed", "execution_phases",
                ) else ""
        # Normalise short_name: slugify whatever the LLM returned
        if data.get("short_name"):
            data["short_name"] = re.sub(r"[^\w]+", "_", str(data["short_name"]).lower()).strip("_")[:50]
        return data

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
        strategy = plan.get("strategy", {})

        lines = [
            "🧠 <b>MadPlan</b>",
            f"<i>{task_preview}</i>",
            f"<code>{created} UTC</code>",
        ]

        if strategy.get("primary_approach"):
            lines += [
                "",
                "<b>Strategy:</b>",
                f"<i>{html.escape(strategy['primary_approach'][:300])}</i>",
            ]
            if strategy.get("constraints"):
                constraints_str = " · ".join(
                    html.escape(c) for c in strategy["constraints"][:4]
                )
                lines.append(f"⚠️ <i>{constraints_str}</i>")

        lines += [
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

    def save_plan(
        self,
        plan: dict,
        plans_dir: str,
        overwrite: bool = False,
        target_slug: str = "",
    ) -> tuple[str, str]:
        """
        Save plan as Markdown to plans_dir/<plan_name>/plan.md.

        The plan_name is derived from the task text (first ~5 words, slugified, max 50 chars),
        unless target_slug is provided, in which case that directory is used directly.
        If overwrite=True and the directory already exists, the files are overwritten in place.
        Returns (plan_name, absolute_file_path).
        """
        task_text = plan.get("task", "plan")
        created_at = plan.get("created_at", datetime.now(timezone.utc).isoformat())

        if target_slug:
            slug = target_slug
            plan_dir = os.path.join(plans_dir, slug)
        else:
            # Prefer the LLM-generated short_name from the strategy when available
            short_name = plan.get("strategy", {}).get("short_name", "")
            if short_name:
                base_slug = re.sub(r"[^\w]+", "_", str(short_name).lower()).strip("_")[:50] or "plan"
            else:
                words = re.findall(r"[a-zA-Z0-9]+", task_text)[:5]
                base_slug = "_".join(w.lower() for w in words)[:50] if words else "plan"
            slug = base_slug
            plan_dir = os.path.join(plans_dir, slug)
            if os.path.exists(plan_dir) and not overwrite:
                # Guarantee a unique directory name
                ts_suffix = re.sub(r"[^\d]", "", created_at[:19])
                slug = f"{base_slug}_{ts_suffix}"
                plan_dir = os.path.join(plans_dir, slug)
                counter = 2
                while os.path.exists(plan_dir):
                    new_slug = f"{base_slug}_{ts_suffix}_{counter}"
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
        ]

        strategy = plan.get("strategy", {})
        if strategy:
            lines += [
                "## Strategy",
                "",
            ]
            if strategy.get("task_summary"):
                lines += [f"**Summary:** {strategy['task_summary']}", ""]
            if strategy.get("primary_approach"):
                lines += [f"**Primary approach:** {strategy['primary_approach']}", ""]
            if strategy.get("constraints"):
                lines += ["**Constraints:**"]
                lines += [f"- {c}" for c in strategy["constraints"]]
                lines.append("")
            if strategy.get("fallback_approaches"):
                lines += ["**Fallbacks:**"]
                lines += [f"- {f}" for f in strategy["fallback_approaches"]]
                lines.append("")
            if strategy.get("discovery_needed"):
                lines += ["**Discovery needed:**"]
                lines += [f"- {d}" for d in strategy["discovery_needed"]]
                lines.append("")
            if strategy.get("notes"):
                lines += [f"**Notes:** {strategy['notes']}", ""]

        lines += ["## Sub-tasks", ""]

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

        strategy = plan.get("strategy")
        if strategy:
            strategy_path = os.path.join(plan_dir, "strategy.json")
            with open(strategy_path, "w", encoding="utf-8") as fh:
                json.dump(strategy, fh, indent=2, ensure_ascii=False)

        logger.info("MadPlan: plan saved to %s", path)
        return slug, path

    def load_plan(self, plan_name: str, plans_dir: str) -> dict:
        """
        Load a plan from plans_dir/<plan_name>/plan.md.
        Returns a minimal plan dict with task, created_at, and subtasks (prompts only).
        Raises MadPlanError if not found or unreadable.
        """
        if not plan_name:
            raise MadPlanError("Plan name must not be empty.")
        if any(c in plan_name for c in ("/", "\\", "..")):
            raise MadPlanError(f"Invalid plan name: '{plan_name}' contains path-traversal characters.")
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
            "strategy": self._load_strategy(plan_name, plans_dir),
            "subtasks": subtasks,
        }

    def _load_strategy(self, plan_name: str, plans_dir: str) -> dict:
        """Load strategy.json for a plan; return empty-but-valid dict if absent."""
        path = os.path.join(plans_dir, plan_name, "strategy.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("MadPlan: could not load strategy.json for '%s': %s", plan_name, exc)
            return {}

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


def delete_plan(plan_name: str, plans_dir: str) -> None:
    """
    Delete a saved plan directory.

    Raises MadPlanError if the plan is not found or if plan_name contains
    path-traversal characters (/, \\, ..).
    """
    if not plan_name:
        raise MadPlanError("Plan name must not be empty.")
    if any(c in plan_name for c in ("/", "\\", "..")):
        raise MadPlanError(f"Invalid plan name: '{plan_name}' contains path-traversal characters.")
    plan_dir = os.path.join(plans_dir, plan_name)
    plan_file = os.path.join(plan_dir, "plan.md")
    if not os.path.isdir(plan_dir) or not os.path.exists(plan_file):
        raise MadPlanError(f"Plan '{plan_name}' not found.")
    shutil.rmtree(plan_dir)
    logger.info("MadPlan: deleted plan directory '%s'", plan_dir)

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
