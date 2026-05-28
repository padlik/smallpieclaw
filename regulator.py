"""
regulator.py
------------
Regulator orchestrator for the agent's Regulator operating mode.

The Regulator decomposes a complex task into atomic sub-tasks, selects
the best LLM and parameters for each using data/models_capabilities.json,
and executes them sequentially via sub-agents.
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

class RegulatorError(Exception):
    """Base class for regulator errors."""


class RegulatorExecutionError(RegulatorError):
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
You are an expert task decomposition specialist.

## Default behavior
Execute the task as a SINGLE continuous flow unless decomposition is clearly justified. \
Do not decompose unless the task contains multiple steps with FULLY INDEPENDENT contexts.

## When to decompose
Decompose a task ONLY when it contains multiple steps where each step can be executed \
in isolation, producing a transferable artifact (file, dataset, message, report) that \
the next step consumes as input — independent of any live session state.

✅ Decompose when each step operates on portable, storable artifacts:
  Download data → Analyze / Enrich → Summarize → Send email
  Each step takes the previous step's OUTPUT (a file or dataset) as input, not its live state.

❌ Do NOT decompose when steps share live session context:
  Open website → Find items → Open form → Change value → Save
  These steps share browser state, DOM navigation, and form context. Splitting them \
  forces redundant re-entry into the same session and breaks the flow.

## Decision rule
Before splitting a step, ask: "Can step N independently process a batch of outputs \
prepared by step N-1 earlier — without re-entering the same session or tool context?"
  YES → candidate for decomposition
  NO  → keep as a single atomic task; execute the whole flow sequentially

## Rules for decomposed sub-tasks
- Sub-tasks must be ATOMIC: each has a single, well-defined output.
- Sub-tasks must be NON-OVERLAPPING: no two sub-tasks produce the same artifact.
- Decomposition must be MINIMAL: batch small steps within a single sub-task; \
  avoid spawning sub-tasks for trivial operations.
- Mark dependencies: if sub-task B requires output from sub-task A, set depends_on=["A_id"].
- Independent sub-tasks have depends_on=[].
- Sub-task IDs MUST be short verb-noun phrases in snake_case that reflect the action and \
  subject (e.g. "fetch_invoice_data", "enrich_contacts", "send_summary_email"). \
  Never use generic labels like "t1", "t2", "task_3", or numbered placeholders.

Respond with a JSON object only (no markdown fences). Schema:
{
  "subtasks": [
    {
      "id": "fetch_invoice_data",
      "name": "Fetch invoice data",
      "description": "What exactly this sub-task produces",
      "depends_on": []
    }
  ]
}
"""

_DECOMPOSE_USER_TMPL = """\
Analyze the following task. First decide whether decomposition is warranted: \
if the task is a sequential or session-bound workflow that cannot be meaningfully split \
into independent steps with transferable outputs, return a single sub-task that covers \
the full task. Otherwise decompose into minimal atomic sub-tasks.

Task:
{task}"""

_MODEL_SELECT_SYSTEM = """\
You are an expert AI model selector. Your goal is to match a task to the most capable \
and cost-effective model from the CONFIGURED models list.

IMPORTANT: You MUST select from the "Configured Models" list below. These are the only \
models available for execution. The "Capabilities Reference" section provides additional \
context for models that have documented capabilities — use it to make better decisions, \
but you cannot select a model that is not in the configured list.

## Step 1 — Review available models
Read the configured models list. For each, check if capabilities data is available.

## Step 2 — Analyze the task
Identify the core requirements: reasoning depth, domain knowledge, instruction-following \
precision, output format, required context length, latency, and cost sensitivity.

## Step 3 — Select one model
Choose exactly one model from the configured list. Use its "model" field as the model_name. \
If capabilities data is available, ground your selection in specific evidence. \
If no capabilities data exists, select based on model name, provider, and general knowledge.

## Step 4 — Set parameters
Specify `temperature`, `top_p`, and `max_tokens` appropriate for the task type \
(e.g. lower temperature for deterministic extraction, higher for creative synthesis).

## Step 5 — Write the execution prompt
Draft a complete, self-contained prompt for the chosen model. It must:
- Include all context the model needs to execute without follow-up.
- Specify the exact expected output format.
- Be tailored to the model's known strengths and limitations.

---

Configured Models (select ONLY from this list):
{configured_models_json}

---

Capabilities Reference (enrichment data for models that have documented capabilities):
{capabilities_json}

---

Respond with a JSON object only (no markdown fences). Schema:
{{
  "model_name": "exact 'model' field from the configured models list",
  "temperature": <number>,
  "top_p": <number>,
  "max_tokens": <integer>,
  "rationale": "2-4 sentences grounding the choice",
  "prompt": "complete ready-to-use execution prompt"
}}
"""

_MODEL_SELECT_USER_TMPL = """\
Sub-task: {name}

Description: {description}
{upstream_note}
Select the best model and write the execution prompt for this sub-task."""


# ---------------------------------------------------------------------------
# RegulatorOrchestrator
# ---------------------------------------------------------------------------

class RegulatorOrchestrator:
    """
    Stateless orchestrator. Instantiate fresh per call.
    """

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def plan_task(
        self,
        task: str,
        llm_client,
        models_capabilities: list[dict],
        configured_models: Optional[list[dict]] = None,
        images: Optional[list[str]] = None,
    ) -> dict:
        """
        Decompose a task and select models for each sub-task.

        Args:
            task: the task description text
            llm_client: LLM client for planning calls
            models_capabilities: capabilities reference data
            configured_models: list from llm_client.list_models() — selection is constrained to these
            images: optional list of image descriptions/references

        Returns enriched plan dict:
        {
          "task": str,
          "created_at": ISO-8601 timestamp,
          "subtasks": [
            {
              "id": str,
              "name": str,
              "description": str,
              "model_name": str,
              "params": {"temperature": float, "top_p": float, "max_tokens": int},
              "prompt": str,
              "rationale": str,
              "depends_on": [str],
            },
            ...
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
        logger.info("Regulator: decomposing task (%d chars)", len(task))
        raw_decomp = llm_client.chat(
            [{"role": "user", "content": _DECOMPOSE_USER_TMPL.format(task=decompose_task)}],
            system=_DECOMPOSE_SYSTEM,
            json_mode=True,
        )
        subtasks = self._parse_decomposition(raw_decomp)

        # Step 2: Model selection per sub-task
        # Build configured models summary for the prompt (include aliases for mapping)
        configured_models = configured_models or []
        cfg_summary = []
        for m in configured_models:
            entry = {"model": m.get("model", ""), "name": m.get("name", ""),
                     "provider": m.get("provider", ""), "vision": m.get("vision", False)}
            if m.get("aliases"):
                entry["aliases"] = m["aliases"]
            cfg_summary.append(entry)
        configured_models_json = json.dumps(cfg_summary, indent=2)
        capabilities_json = json.dumps(models_capabilities, indent=2)
        model_system = _MODEL_SELECT_SYSTEM.format(
            configured_models_json=configured_models_json,
            capabilities_json=capabilities_json,
        )

        # Valid model IDs for post-selection validation
        valid_model_ids = {m.get("model") for m in configured_models if m.get("model")}

        enriched = []
        for st in subtasks:
            logger.info("Regulator: selecting model for sub-task '%s'", st["id"])
            upstream_note = ""
            if st.get("depends_on"):
                upstream_note = (
                    f"\nNote: this sub-task depends on the outputs of: "
                    f"{', '.join(st['depends_on'])}. "
                    f"Upstream results will be injected at execution time.\n"
                )
            user_msg = _MODEL_SELECT_USER_TMPL.format(
                name=st["name"],
                description=st["description"],
                upstream_note=upstream_note,
            )
            raw_sel = llm_client.chat(
                [{"role": "user", "content": user_msg}],
                system=model_system,
                json_mode=True,
            )
            sel = self._parse_model_selection(raw_sel)

            # Validate selected model is in configured set
            selected_model = sel.get("model_name", "")
            if valid_model_ids and selected_model not in valid_model_ids:
                logger.warning(
                    "Regulator: LLM selected '%s' which is not configured. "
                    "Falling back to first configured model.", selected_model,
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
            raise RegulatorError(
                f"Decomposition returned no sub-tasks. Raw: {raw[:300]}"
            )
        result = []
        for i, st in enumerate(subtasks):
            if not isinstance(st, dict):
                continue
            name = str(st.get("name", f"subtask_{i + 1}"))
            # Fallback: slugify the name if id is missing or looks generic (t1, task_2, etc.)
            raw_id = str(st.get("id", ""))
            if not raw_id or re.match(r"^t\d+$|^task[_\s]?\d+$", raw_id, re.IGNORECASE):
                slug = re.sub(r"[^\w]+", "_", name.lower()).strip("_")[:40] or f"subtask_{i + 1}"
                raw_id = slug
            result.append({
                "id": raw_id,
                "name": name,
                "description": str(st.get("description", "")),
                "depends_on": [str(d) for d in st.get("depends_on", [])],
            })
        if not result:
            raise RegulatorError("Decomposition returned empty sub-task list.")
        return result

    def _parse_model_selection(self, raw: str) -> dict:
        data = _extract_json(raw)
        return {
            "model_name": str(data.get("model_name", "")),
            "temperature": _to_float(data.get("temperature")),
            "top_p": _to_float(data.get("top_p")),
            "max_tokens": _to_int(data.get("max_tokens")),
            "rationale": str(data.get("rationale", "")),
            "prompt": str(data.get("prompt", "")),
        }

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_plan_html(self, plan: dict) -> str:
        """Render plan as Telegram HTML."""
        task_preview = html.escape(plan["task"][:200])
        created = plan.get("created_at", "")[:19].replace("T", " ")
        subtasks = plan.get("subtasks", [])

        lines = [
            "🧠 <b>Regulator Plan</b>",
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
    # Saving
    # ------------------------------------------------------------------

    def save_plan(self, plan: dict, plans_dir: str) -> str:
        """
        Save plan as Markdown to plans_dir/<task_slug>.md.
        The filename is derived from the task text (first ~5 words, slugified).
        Returns the absolute file path.
        """
        os.makedirs(plans_dir, exist_ok=True)

        # Build a concise filename from the first few words of the task
        task_text = plan.get("task", "plan")
        created_at = plan.get("created_at", datetime.now(timezone.utc).isoformat())
        words = re.findall(r"[a-zA-Z0-9]+", task_text)[:5]
        slug = "_".join(w.lower() for w in words) if words else "plan"
        # Avoid overwriting: append timestamp suffix if file already exists
        base = os.path.join(plans_dir, f"{slug}.md")
        if os.path.exists(base):
            ts_suffix = re.sub(r"[^\d]", "", created_at[:19])
            base = os.path.join(plans_dir, f"{slug}_{ts_suffix}.md")
        path = base

        lines = [
            f"# Plan: {plan['task'][:80]}",
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

        logger.info("Regulator: plan saved to %s", path)
        return path

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

        Upstream results are injected into downstream sub-task prompts.
        On failure, returns partial results and failure info instead of raising.

        Returns:
            (completed_results, failure_info)
            - completed_results: list of {id, name, result} for successfully completed sub-tasks
            - failure_info: None if all succeeded, or {subtask_id, model_name, error, remaining}
        """
        results: dict[str, str] = {}
        output: list[dict] = []
        subtasks = plan.get("subtasks", [])

        for i, st in enumerate(subtasks):
            st_id = st["id"]
            model_name = st.get("model_name", "")
            params = st.get("params", {})
            prompt = st.get("prompt", st.get("description", ""))

            # Inject upstream context (with warning for missing deps)
            upstream_parts = []
            for dep_id in st.get("depends_on", []):
                if dep_id in results:
                    upstream_parts.append(
                        f"## Result of sub-task {dep_id}\n{results[dep_id]}"
                    )
                else:
                    logger.warning(
                        "Regulator: sub-task '%s' depends on '%s' which has no result",
                        st_id, dep_id,
                    )
                    upstream_parts.append(
                        f"## Result of sub-task {dep_id}\n"
                        f"[WARNING: dependency '{dep_id}' has no result — "
                        f"it may have been skipped or not yet executed]"
                    )
            if upstream_parts:
                prompt = "\n\n".join(upstream_parts) + "\n\n---\n\n" + prompt

            if notify_fn:
                notify_fn(f"▶ Sub-task {st_id}: {st['name']} [{model_name}]")

            logger.info(
                "Regulator: executing sub-task '%s' with model '%s'", st_id, model_name
            )

            try:
                runner = sub_agent_factory(
                    model=model_name or None,
                    label=f"reg-{st_id}",
                    temperature=params.get("temperature"),
                    top_p=params.get("top_p"),
                    max_tokens=params.get("max_tokens"),
                )
                result = runner.run(prompt)
            except Exception as exc:
                logger.error(
                    "Regulator: sub-task '%s' failed: %s", st_id, exc
                )
                remaining = [s["id"] for s in subtasks[i + 1:]]
                return output, {
                    "subtask_id": st_id,
                    "model_name": model_name,
                    "error": str(exc),
                    "remaining": remaining,
                }

            results[st_id] = result
            output.append({"id": st_id, "name": st["name"], "result": result})
            logger.info("Regulator: sub-task '%s' completed", st_id)

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
        logger.warning("Regulator: could not load models_capabilities.json: %s", exc)
        return []


def validate_models_for_regulator(
    configured_models: list[dict],
    capabilities: list[dict],
) -> dict:
    """
    Cross-reference configured models against capabilities data using exact matching.

    Matching priority:
    1. Exact match on model field
    2. Exact match on any alias

    Args:
        configured_models: list from llm_client.list_models() — each has 'name', 'model', 'provider', optional 'aliases'
        capabilities: list from load_models_capabilities() — each has 'model_name', 'specifications', etc.

    Returns:
        {
            "available": [{"model": ..., "name": ..., "capabilities": <cap_entry or None>}, ...],
            "with_capabilities": [...],  # subset with capabilities data
            "missing_capabilities": [...],  # subset without capabilities data
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

        # Exact match: model field first, then aliases
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


def _extract_json(raw: str) -> dict:
    """Extract and parse the first JSON object from a string."""
    raw = raw.strip()
    # Strip markdown fences
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
        raise RegulatorError(
            f"Could not parse JSON from LLM response: {raw[:300]}"
        )


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
