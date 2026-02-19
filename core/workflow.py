"""
core/workflow.py
Lobster-style declarative workflow engine.

Defines multi-step agent pipelines in YAML:
  - Sequential steps
  - Parallel fan-out
  - Conditional routing
  - Approval gates (human-in-the-loop)
  - Variable passing between steps

Example workflow (workflows/code_review.yaml):
  name: Code Review Pipeline
  steps:
    - id: plan
      agent: planner
      prompt: "Analyze the codebase and create a review plan for: {{task}}"
    - id: implement
      agent: executor
      prompt: "{{plan.result}}"
      depends_on: [plan]
    - id: review
      agent: reviewer
      prompt: "Review: {{implement.result}}"
      depends_on: [implement]
      approval_gate: true
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

WORKFLOW_DIR = "workflows"


class StepStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    WAITING   = "waiting"    # waiting for approval gate
    SKIPPED   = "skipped"


@dataclass
class WorkflowStep:
    id:             str
    agent:          str
    prompt:         str
    depends_on:     list[str] = field(default_factory=list)
    approval_gate:  bool = False
    condition:      Optional[str] = None    # e.g. "plan.result contains 'complex'"
    timeout:        int = 300               # seconds
    retry_count:    int = 0
    max_retries:    int = 1

    # Runtime state
    status:         StepStatus = StepStatus.PENDING
    result:         Optional[str] = None
    error:          Optional[str] = None
    started_at:     Optional[float] = None
    completed_at:   Optional[float] = None


@dataclass
class Workflow:
    name:        str
    description: str = ""
    steps:       list[WorkflowStep] = field(default_factory=list)
    variables:   dict[str, str] = field(default_factory=dict)

    # Runtime
    status:      str = "pending"    # pending/running/completed/failed
    started_at:  Optional[float] = None
    completed_at: Optional[float] = None


# ── Workflow Loader ──────────────────────────────────────────────────────────

def load_workflow(path: str) -> Workflow:
    """Load a workflow definition from YAML file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    steps = []
    for s in raw.get("steps", []):
        steps.append(WorkflowStep(
            id=s["id"],
            agent=s["agent"],
            prompt=s["prompt"],
            depends_on=s.get("depends_on", []),
            approval_gate=s.get("approval_gate", False),
            condition=s.get("condition"),
            timeout=s.get("timeout", 300),
            max_retries=s.get("max_retries", 1),
        ))

    return Workflow(
        name=raw.get("name", "Unnamed"),
        description=raw.get("description", ""),
        steps=steps,
        variables=raw.get("variables", {}),
    )


def list_workflows() -> list[dict]:
    """List available workflow definitions."""
    os.makedirs(WORKFLOW_DIR, exist_ok=True)
    results = []
    for fname in sorted(os.listdir(WORKFLOW_DIR)):
        if fname.endswith((".yaml", ".yml")):
            path = os.path.join(WORKFLOW_DIR, fname)
            try:
                with open(path) as f:
                    raw = yaml.safe_load(f)
                results.append({
                    "file": fname,
                    "name": raw.get("name", fname),
                    "description": raw.get("description", ""),
                    "steps": len(raw.get("steps", [])),
                })
            except Exception as e:
                logger.warning("Failed to load workflow %s: %s", fname, e)
    return results


# ── Template rendering ───────────────────────────────────────────────────────

def _render_template(template: str, context: dict) -> str:
    """
    Simple {{variable}} template rendering.
    Supports:
      {{task}}              — top-level variable
      {{step_id.result}}    — step result reference
    """
    def replacer(match):
        key = match.group(1).strip()
        if "." in key:
            parts = key.split(".", 1)
            obj = context.get(parts[0], {})
            if isinstance(obj, dict):
                return str(obj.get(parts[1], f"<{key}>"))
            return str(obj)
        return str(context.get(key, f"<{key}>"))

    return re.sub(r"\{\{(.+?)\}\}", replacer, template)


# ── Condition evaluator ──────────────────────────────────────────────────────

def _evaluate_condition(condition: str, context: dict) -> bool:
    """
    Evaluate simple conditions:
      "step.result contains 'keyword'"
      "step.result length > 100"
      "step.status == 'completed'"
    """
    if not condition:
        return True

    try:
        # "X contains Y"
        if " contains " in condition:
            parts = condition.split(" contains ", 1)
            left = _render_template("{{" + parts[0].strip() + "}}", context)
            right = parts[1].strip().strip("'\"")
            return right.lower() in left.lower()

        # "X == Y"
        if " == " in condition:
            parts = condition.split(" == ", 1)
            left = _render_template("{{" + parts[0].strip() + "}}", context)
            right = parts[1].strip().strip("'\"")
            return left.strip() == right.strip()

        # Default: truthy check
        val = _render_template("{{" + condition + "}}", context)
        return bool(val and val != f"<{condition}>")

    except Exception as e:
        logger.warning("Condition eval failed: %s — %s", condition, e)
        return True


# ── Workflow Engine ──────────────────────────────────────────────────────────

class WorkflowEngine:
    """
    Executes workflow definitions using the orchestrator's agent infrastructure.

    Usage:
        engine = WorkflowEngine(orchestrator)
        result = await engine.run_workflow("workflows/deploy.yaml", {"task": "..."})
    """

    def __init__(self, board, llm_factory=None):
        self.board = board
        self.llm_factory = llm_factory  # callable(agent_id) -> llm_adapter
        self._approval_callbacks: dict[str, Any] = {}

    async def run_workflow(
        self,
        workflow: Workflow,
        initial_vars: dict[str, str] | None = None,
        on_step_complete=None,
        on_approval_needed=None,
    ) -> Workflow:
        """
        Execute a workflow end-to-end.

        Args:
            workflow: Workflow definition
            initial_vars: Initial template variables (e.g. {"task": "..."})
            on_step_complete: Callback(step) after each step completes
            on_approval_needed: Callback(step) when approval gate reached
                               Must return True to proceed, False to abort

        Returns:
            Completed workflow with results
        """
        context = dict(initial_vars or {})
        context.update(workflow.variables)

        workflow.status = "running"
        workflow.started_at = time.time()

        # Topological execution
        completed_ids = set()
        step_map = {s.id: s for s in workflow.steps}

        while True:
            # Find runnable steps (dependencies met, not yet done)
            runnable = []
            failed_ids = {s.id for s in workflow.steps
                          if s.status == StepStatus.FAILED}
            for step in workflow.steps:
                if step.status in (StepStatus.COMPLETED, StepStatus.FAILED,
                                   StepStatus.SKIPPED):
                    continue
                # Skip steps whose dependencies failed
                if any(d in failed_ids for d in step.depends_on):
                    step.status = StepStatus.SKIPPED
                    step.error = "dependency failed"
                    completed_ids.add(step.id)
                    logger.info("[workflow] step %s skipped (dependency failed)",
                                step.id)
                    continue
                if all(d in completed_ids for d in step.depends_on):
                    runnable.append(step)

            if not runnable:
                break

            # Execute runnable steps (could run parallel ones concurrently)
            # For now, sequential execution for simplicity
            for step in runnable:
                # Check condition
                if step.condition and not _evaluate_condition(
                    step.condition, context
                ):
                    step.status = StepStatus.SKIPPED
                    completed_ids.add(step.id)
                    logger.info("[workflow] step %s skipped (condition false)",
                                step.id)
                    continue

                # Render prompt template
                prompt = _render_template(step.prompt, context)

                # Execute step
                step.status = StepStatus.RUNNING
                step.started_at = time.time()

                try:
                    # Create task on board and wait for result
                    task = self.board.create(
                        prompt,
                        required_role=_agent_to_role(step.agent),
                    )
                    logger.info("[workflow] step %s: created task %s for %s",
                                step.id, task.task_id, step.agent)

                    # Wait for task completion (poll board)
                    result = await self._wait_for_task(
                        task.task_id, timeout=step.timeout
                    )

                    step.result = result
                    step.completed_at = time.time()

                    # Store in context for downstream steps
                    context[step.id] = {
                        "result": result,
                        "status": "completed",
                        "agent": step.agent,
                    }

                    # Approval gate
                    if step.approval_gate:
                        step.status = StepStatus.WAITING
                        if on_approval_needed:
                            approved = on_approval_needed(step)
                            if not approved:
                                step.status = StepStatus.FAILED
                                step.error = "Approval denied"
                                workflow.status = "failed"
                                return workflow
                        # Auto-approve if no callback
                        step.status = StepStatus.COMPLETED
                    else:
                        step.status = StepStatus.COMPLETED

                    completed_ids.add(step.id)

                    if on_step_complete:
                        on_step_complete(step)

                except Exception as e:
                    step.error = str(e)
                    step.status = StepStatus.FAILED
                    step.completed_at = time.time()
                    logger.error("[workflow] step %s failed: %s", step.id, e)

                    # Retry?
                    if step.retry_count < step.max_retries:
                        step.retry_count += 1
                        step.status = StepStatus.PENDING
                        logger.info("[workflow] retrying step %s (%d/%d)",
                                    step.id, step.retry_count, step.max_retries)
                        continue
                    else:
                        completed_ids.add(step.id)

        # Determine final workflow status
        all_done = all(s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
                       for s in workflow.steps)
        workflow.status = "completed" if all_done else "failed"
        workflow.completed_at = time.time()

        return workflow

    async def _wait_for_task(self, task_id: str, timeout: int = 300) -> str:
        """Poll task board until task is completed or timeout."""
        start = time.time()
        while time.time() - start < timeout:
            task = self.board.get(task_id)
            if task is None:
                await asyncio.sleep(1)
                continue
            if task.status.value == "completed":
                return task.result or ""
            if task.status.value == "failed":
                raise RuntimeError(
                    f"Task {task_id} failed: "
                    + ", ".join(task.evolution_flags)
                )
            await asyncio.sleep(1)
        raise TimeoutError(f"Task {task_id} timed out after {timeout}s")


def _agent_to_role(agent_name: str) -> str:
    """Map agent name to required_role for task routing."""
    name = agent_name.lower()
    if "plan" in name:
        return "planner"
    if "review" in name or "audit" in name:
        return "review"
    return "implement"
