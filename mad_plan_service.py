"""
mad_plan_service.py
-------------------
Service layer for MadPlan workflows. Owns the plan→save→execute lifecycle
and keeps the Telegram interface thin (UI-only).

All methods are synchronous (designed to run in executor threads).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from mad_plan import (
    MadPlanOrchestrator,
    build_agent_capabilities_summary,
    load_models_capabilities,
)

logger = logging.getLogger(__name__)


@dataclass
class PlanResult:
    """Result of a plan or revise operation."""

    plan: dict
    plan_name: str
    saved_path: str
    plan_id: str
    html: str


@dataclass
class ExecutionResult:
    """Result of plan execution."""

    total: int
    success: bool
    failure: dict | None = None
    run_ts: str = ""
    run_dir: str = ""


class MadPlanService:
    """Stateless service for MadPlan operations.

    Encapsulates orchestrator calls, model discovery, capability summary,
    and plan persistence. Designed to be called from an executor thread.
    """

    def __init__(
        self,
        plans_dir: str,
        llm_client,
        sub_agent_factory: Callable,
        tool_registry=None,
        skill_registry=None,
        mcp_manager=None,
        data_dir: str = "",
    ):
        self._plans_dir = plans_dir
        self._llm_client = llm_client
        self._sub_agent_factory = sub_agent_factory
        self._tool_registry = tool_registry
        self._skill_registry = skill_registry
        self._mcp_manager = mcp_manager
        self._data_dir = data_dir or os.path.join(os.getcwd(), "data")

    def _get_capabilities(self) -> str:
        return build_agent_capabilities_summary(
            tool_registry=self._tool_registry,
            skill_registry=self._skill_registry,
            mcp_manager=self._mcp_manager,
        )

    def _get_configured_models(self) -> list:
        if self._llm_client and hasattr(self._llm_client, "list_models"):
            return self._llm_client.list_models()
        return []

    def _get_caps_data(self) -> dict:
        return load_models_capabilities(self._data_dir)

    def create_plan(self, task_text: str, name_override: str = "") -> PlanResult:
        """Create a new plan from a task description. Synchronous."""
        orchestrator = MadPlanOrchestrator()
        capabilities = self._get_capabilities()
        configured_models = self._get_configured_models()
        caps_data = self._get_caps_data()

        plan = orchestrator.plan_task(
            task_text,
            self._llm_client,
            caps_data,
            configured_models=configured_models,
            agent_capabilities=capabilities,
        )

        if name_override:
            plan["task"] = f"{name_override} - {plan.get('task', '')}"

        plan_name, saved_path = orchestrator.save_plan(plan, self._plans_dir)
        plan_id = uuid.uuid4().hex[:8]
        plan_html = orchestrator.format_plan_html(plan)

        return PlanResult(
            plan=plan,
            plan_name=plan_name,
            saved_path=saved_path,
            plan_id=plan_id,
            html=plan_html,
        )

    def revise_plan(
        self, original_plan: dict, feedback: str, plan_name: str = ""
    ) -> PlanResult:
        """Revise an existing plan with user feedback. Synchronous."""
        orchestrator = MadPlanOrchestrator()
        capabilities = self._get_capabilities()
        configured_models = self._get_configured_models()
        caps_data = self._get_caps_data()

        revised_plan = orchestrator.revise_plan(
            original_plan,
            feedback,
            self._llm_client,
            caps_data,
            configured_models=configured_models,
            agent_capabilities=capabilities,
        )

        # Save using original plan_name as target_slug (overwrites same directory)
        if plan_name:
            _, saved_path = orchestrator.save_plan(
                revised_plan, self._plans_dir, target_slug=plan_name
            )
        else:
            plan_name, saved_path = orchestrator.save_plan(revised_plan, self._plans_dir)

        plan_id = uuid.uuid4().hex[:8]
        plan_html = orchestrator.format_plan_html(revised_plan)

        return PlanResult(
            plan=revised_plan,
            plan_name=plan_name,
            saved_path=saved_path,
            plan_id=plan_id,
            html=plan_html,
        )

    def execute_plan(
        self,
        plan: dict,
        plan_name: str,
        *,
        traced: bool = False,
        skip_completed: set | None = None,
        resume_from_dir: str = "",
        cancel_event: Optional[threading.Event] = None,
        notify_fn: Optional[Callable[[str], None]] = None,
    ) -> ExecutionResult:
        """Execute a plan via sub-agents. Synchronous (blocking)."""
        orchestrator = MadPlanOrchestrator()

        # Create run directory
        run_ts = time.strftime("%Y%m%d_%H%M%S")
        run_dir = ""
        if plan_name:
            run_dir = os.path.join(self._plans_dir, plan_name, "runs", run_ts)
            os.makedirs(run_dir, exist_ok=True)

        output, failure = orchestrator.execute_plan(
            plan,
            sub_agent_factory=self._sub_agent_factory,
            notify_fn=notify_fn,
            run_dir=run_dir,
            skip_completed=skip_completed,
            resume_from_dir=resume_from_dir if skip_completed else "",
            cancel_event=cancel_event,
        )

        # Write trace.json when tracing is enabled
        if traced and run_dir:
            trace_data = {
                "plan_name": plan_name,
                "run_ts": run_ts,
                "traced": True,
                "success": failure is None,
                "subtasks": [
                    {
                        "id": entry["id"],
                        "name": entry["name"],
                        "traces": entry.get("traces", []),
                    }
                    for entry in output
                ],
            }
            if failure:
                trace_data["failure"] = failure
            trace_path = os.path.join(run_dir, "trace.json")
            try:
                with open(trace_path, "w") as f:
                    json.dump(trace_data, f, indent=2, ensure_ascii=False)
            except OSError:
                pass

        return ExecutionResult(
            total=len(output),
            success=failure is None,
            failure=failure,
            run_ts=run_ts,
            run_dir=run_dir,
        )
