"""
core/orchestrator.py
Launches one OS process per agent (multiprocessing).
Each process runs its own asyncio event loop.
Coordination via:
  - ContextBus  (shared file-backed KV)
  - TaskBoard   (file-locked self-claim)
  - Mailbox     (per-agent JSONL inbox)
Signal handling: SIGTERM/SIGINT trigger graceful shutdown of all children.
"""

from __future__ import annotations
import asyncio
import json
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
from typing import Any

import yaml

# ── Graceful shutdown flag (per-process) ──
_shutdown_requested = False

try:
    from filelock import FileLock
except ImportError:
    class FileLock:  # type: ignore
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

from core.agent import AgentConfig
from core.context_bus import ContextBus
from core.task_board import TaskBoard
from core.skill_loader import SkillLoader

logger = logging.getLogger(__name__)


# ── Per-process entry point ─────────────────────────────────────────────────

def _agent_process(agent_cfg_dict: dict, agent_def: dict, config: dict):
    """
    Runs in a child process.
    Imports adapters here to avoid pickling issues.
    Child output is redirected to .logs/ to keep the terminal clean.
    Registers signal handlers for graceful shutdown.
    """
    import asyncio, logging, os, sys

    global _shutdown_requested

    # ── Signal handlers for graceful shutdown ──
    def _handle_signal(signum, frame):
        global _shutdown_requested
        _shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # ── Silence child process output ──
    # Redirect stdout/stderr to log files so urllib3/chromadb/etc.
    # warnings don't pollute the user's terminal.
    agent_id = agent_cfg_dict.get("agent_id", "unknown")
    os.makedirs(".logs", exist_ok=True)
    log_path = os.path.join(".logs", f"{agent_id}.log")
    try:
        log_file = open(log_path, "w")
        sys.stdout = log_file
        sys.stderr = log_file
    except OSError:
        pass  # If we can't redirect, proceed anyway

    logging.basicConfig(
        level=logging.INFO,
        format=f"[%(asctime)s][%(name)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],  # goes to log file
        force=True,
    )

    # Load .env in child process so per-agent env vars are available
    from core.env_loader import load_dotenv
    load_dotenv()

    # Build adapters — LLM is per-agent, memory is per-agent isolated
    llm     = _build_llm_for_agent(agent_def, config)
    memory  = _build_memory(config, agent_id=agent_id)
    chain   = _build_chain(config)
    episodic, kb = _build_episodic_memory(config, agent_id)

    from core.agent import AgentConfig, BaseAgent
    from core.skill_loader import SkillLoader
    from core.usage_tracker import UsageTracker

    cfg     = AgentConfig(**agent_cfg_dict)
    agent   = BaseAgent(cfg, llm, memory, SkillLoader(), chain,
                        episodic=episodic, kb=kb)
    tracker = UsageTracker()

    bus   = ContextBus()
    board = TaskBoard()

    from core.heartbeat import Heartbeat
    hb = Heartbeat(agent_id)

    try:
        asyncio.run(_agent_loop(agent, bus, board, config, tracker, hb))
    finally:
        hb.stop()  # clean up heartbeat file on exit


# ── Critique request handler (replaces review_request) ─────────────────────

async def _handle_critique_request(agent, board: TaskBoard, mail: dict, sched):
    """
    Advisor mode: score subtask output 1-10 with optional suggestions.
    Reviewer is an ADVISOR, not a gatekeeper — tasks are NEVER blocked.
    The planner reads scores/suggestions during final synthesis.
    """
    try:
        payload = json.loads(mail["content"])
        task_id     = payload["task_id"]
        description = payload["description"]
        result      = payload["result"]
    except (KeyError, json.JSONDecodeError) as e:
        logger.error("[%s] bad critique_request: %s", agent.cfg.agent_id, e)
        return

    critique_prompt = (
        f"Score this subtask output on a scale of 1-10.\n\n"
        f"## Subtask\n{description}\n\n"
        f"## Output\n{result}\n\n"
        f"IMPORTANT: This is a SUBTASK result (raw data/code), NOT a final user-facing answer.\n"
        f"The planner will synthesize all subtask results into the final response.\n"
        f"Judge ONLY: correctness for this specific subtask, completeness, clarity.\n\n"
        f"Respond with JSON:\n"
        f'{{"score": <1-10>, "suggestions": ["optional improvement 1"], "comment": "brief assessment"}}\n'
        f"Omit suggestions if score >= 7. Max 3 suggestions if needed."
    )
    messages = [
        {"role": "system", "content": agent.cfg.role},
        {"role": "user",   "content": critique_prompt},
    ]

    try:
        raw = await agent.llm.chat(messages, agent.cfg.model)
        # Extract JSON from response (may have markdown wrapping)
        json_str = raw.strip()
        if "```" in json_str:
            start = json_str.find("{")
            end = json_str.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = json_str[start:end]
        critique_data = json.loads(json_str)
        score       = critique_data.get("score", 7)
        suggestions = critique_data.get("suggestions", [])
        comment     = critique_data.get("comment", "")
        passed      = True  # Reviewer NEVER blocks — always pass
    except Exception as e:
        logger.error("[%s] critique LLM call failed: %s", agent.cfg.agent_id, e)
        # On failure, pass the task through with default score
        passed, score, suggestions, comment = True, 7, [], f"Critique failed: {e}"

    board.add_critique(task_id, agent.cfg.agent_id, passed, suggestions, comment,
                       score=score)
    await sched.on_critique(agent.cfg.agent_id, passed, score=score)

    task_obj = board.get(task_id)
    if task_obj and task_obj.agent_id:
        await sched.on_critique_result(
            task_obj.agent_id,
            passed_first_time=True,   # Always passes now
            had_revision=False,
        )

    logger.info("[%s] scored task %s: %d/10%s",
                agent.cfg.agent_id, task_id, score,
                f" ({len(suggestions)} suggestions)" if suggestions else "")


# Legacy handler: forward old review_request to critique handler
async def _handle_review_request(agent, board: TaskBoard, mail: dict, sched):
    """Backward-compatible wrapper: treat review_request as critique_request."""
    await _handle_critique_request(agent, board, mail, sched)


# ── Subtask extraction (Phase 5) ──────────────────────────────────────────

def _extract_and_create_subtasks(board: TaskBoard, planner_output: str,
                                  parent_task_id: str) -> list[str]:
    """
    Parse planner output for lines starting with 'TASK:' and optional 'COMPLEXITY:'.
    Creates subtasks as immediately PENDING (no blocked_by) so agents
    can claim them right away after planner finishes.
    Returns list of created subtask IDs.
    """
    lines = planner_output.strip().split("\n")
    subtask_ids: list[str] = []
    pending_description: str | None = None
    pending_role: str | None = None

    def _create_subtask(desc: str, role: str, complexity: str):
        new_task = board.create(
            desc,
            blocked_by=[],
            required_role=role,
            parent_id=parent_task_id,
        )
        # Set complexity on the task
        with board.lock:
            data = board._read()
            t = data.get(new_task.task_id)
            if t:
                t["complexity"] = complexity
                board._write(data)
        subtask_ids.append(new_task.task_id)
        logger.info("Created subtask %s [role=%s, complexity=%s]: %s",
                     new_task.task_id, role or "any", complexity,
                     desc[:60])

    for line in lines:
        stripped = line.strip()
        for prefix in ("- ", "* ", "• "):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                break

        # Check for COMPLEXITY: line (follows a TASK: line)
        if stripped.upper().startswith("COMPLEXITY:") and pending_description:
            complexity = stripped[11:].strip().lower()
            if complexity not in ("simple", "normal", "complex"):
                complexity = _infer_complexity(pending_description)
            _create_subtask(pending_description, pending_role, complexity)
            pending_description = None
            pending_role = None
            continue

        # If we had a pending TASK without COMPLEXITY, create it now
        if pending_description:
            complexity = _infer_complexity(pending_description)
            _create_subtask(pending_description, pending_role, complexity)
            pending_description = None
            pending_role = None

        if stripped.upper().startswith("TASK:"):
            description = stripped[5:].strip()
            if description:
                pending_description = description
                pending_role = _infer_role(description)

    # Flush last pending task
    if pending_description:
        complexity = _infer_complexity(pending_description)
        _create_subtask(pending_description, pending_role, complexity)

    if subtask_ids:
        logger.info("Planner extracted %d subtasks from task %s",
                     len(subtask_ids), parent_task_id)

    return subtask_ids


def _infer_complexity(description: str) -> str:
    """Infer task complexity from description keywords."""
    desc_lower = description.lower()
    if any(kw in desc_lower for kw in [
        "review", "audit", "verify", "analyze", "evaluate", "compare",
    ]):
        return "complex"
    if any(kw in desc_lower for kw in [
        "list", "show", "get", "fetch", "read", "display", "print",
    ]):
        return "simple"
    return "normal"


# ── Role inference (Phase 6) ──────────────────────────────────────────────

def _infer_role(description: str) -> str:
    """
    Infer the required agent role from subtask description keywords.
    Returns a keyword that will be matched against agent role strings.
    Priority: review > planner > implement (most specific wins).
    Default: "implement" — subtasks from planner should go to executor.
    """
    desc_lower = description.lower()

    # Review keywords have highest priority (to avoid "review code" → implement)
    if any(kw in desc_lower for kw in [
        "review", "evaluate", "audit", "verify",
    ]):
        return "review"  # matches reviewer's "Peer reviewer"

    # Planner keywords
    if any(kw in desc_lower for kw in [
        "plan", "decompose", "architect", "outline",
    ]):
        return "planner"  # matches planner's "Strategic planner"

    # Default: everything else goes to executor
    # (subtasks from planner are meant to be executed, not planned again)
    return "implement"


# ── Helper: check if any tasks are still active ───────────────────────────

def _has_active_tasks(board: TaskBoard) -> bool:
    """Return True if any tasks are still pending, claimed, in review/critique, or paused."""
    data = board._read()
    active_states = {"pending", "claimed", "review", "critique", "blocked", "paused"}
    return any(t.get("status") in active_states for t in data.values())


# ── Agent loop ─────────────────────────────────────────────────────────────

async def _agent_loop(agent, bus: ContextBus, board: TaskBoard,
                       config: dict, tracker=None, heartbeat=None):
    """
    Agent Teams self-claim loop.
    After finishing a task, immediately tries to claim the next one.
    Sends periodic heartbeats for gateway status monitoring.
    Checks for shutdown signals and recovers stale tasks.
    """
    from reputation.scheduler import ReputationScheduler
    sched = ReputationScheduler(board)

    idle_count = 0
    max_idle   = config.get("max_idle_cycles", 30)
    _last_recovery_check = 0.0

    while True:
        # --- check shutdown signal ---
        if _shutdown_requested:
            logger.info("[%s] shutdown signal received, exiting gracefully",
                        agent.cfg.agent_id)
            return

        # --- heartbeat ---
        if heartbeat:
            heartbeat.beat("idle")

        # --- periodic stale task recovery (every 30s) ---
        now = time.time()
        if now - _last_recovery_check > 30:
            _last_recovery_check = now
            recovered = board.recover_stale_tasks()
            if recovered:
                logger.info("[%s] recovered %d stale tasks",
                            agent.cfg.agent_id, len(recovered))

        # --- check mail first (P2P messages from teammates) ---
        mails = agent.read_mail()
        for mail in mails:
            if mail.get("type") == "shutdown":
                logger.info("[%s] shutdown requested by %s",
                            agent.cfg.agent_id, mail.get("from"))
                return

            # Handle critique/review requests from other agents
            elif mail.get("type") in ("critique_request", "review_request"):
                await _handle_critique_request(agent, board, mail, sched)

        # --- check for CRITIQUE tasks to fix (executor picks up own revisions) ---
        critique_task = board.claim_critique(agent.cfg.agent_id)
        if critique_task:
            logger.info("[%s] claimed critique revision for task %s",
                        agent.cfg.agent_id, critique_task.task_id)
            if heartbeat:
                heartbeat.beat("working", critique_task.task_id,
                               progress="revising based on feedback...")
            try:
                suggestions = (critique_task.critique or {}).get("suggestions", [])
                fix_prompt = (
                    f"You previously submitted this result:\n{critique_task.result}\n\n"
                    f"The advisor gave these revision suggestions:\n"
                    + "\n".join(f"- {s}" for s in suggestions)
                    + "\n\nPlease fix only the parts that need changing based on these suggestions."
                )
                from core.context_bus import ContextBus as _CB
                fix_result = await agent.run_with_prompt(fix_prompt, _CB())

                # After revision: if already at max critique rounds, force complete
                if critique_task.critique_round >= 1:
                    board.submit_for_review(critique_task.task_id, fix_result)
                    board.complete(critique_task.task_id)
                    logger.info("[%s] revision done (max rounds), auto-completed task %s",
                                agent.cfg.agent_id, critique_task.task_id)
                else:
                    # Resubmit for another critique round
                    board.submit_for_review(critique_task.task_id, fix_result)
                    reviewers = config.get("reputation", {}).get(
                        "peer_review_agents", [])
                    for r_id in reviewers:
                        if r_id != agent.cfg.agent_id:
                            agent.send_mail(r_id,
                                            _json_critique_request(critique_task, fix_result),
                                            msg_type="critique_request")

                await sched.on_critique_result(
                    agent.cfg.agent_id,
                    passed_first_time=False,
                    had_revision=True,
                )
            except Exception as exc:
                logger.exception("[%s] critique revision failed: %s",
                                 agent.cfg.agent_id, exc)
                board.complete(critique_task.task_id)  # force complete on error
            continue

        # --- try to claim a task (Phase 6: pass role for routing) ---
        rep   = sched.get_score(agent.cfg.agent_id)
        task  = board.claim_next(agent.cfg.agent_id, int(rep),
                                  agent_role=agent.cfg.role)

        if task is None:
            # If there are still tasks in-progress (claimed/review/pending),
            # keep waiting — other agents might produce subtasks for us
            active = _has_active_tasks(board)
            if active:
                idle_count = min(idle_count + 1, max_idle // 2)
                # Never exit while work is happening — only slow the poll
            else:
                idle_count += 1

            if idle_count >= max_idle and not active:
                logger.info("[%s] idle limit reached (no active tasks), exiting",
                            agent.cfg.agent_id)
                return
            await asyncio.sleep(1)
            continue

        idle_count = 0
        logger.info("[%s] claimed task %s", agent.cfg.agent_id, task.task_id)

        # --- heartbeat: working ---
        if heartbeat:
            heartbeat.beat("working", task.task_id, progress="preparing...")

        try:
            if heartbeat:
                heartbeat.beat("working", task.task_id,
                               progress="loading skills & context...")
            result = await agent.run(task, bus)

            if heartbeat:
                heartbeat.beat("working", task.task_id,
                               progress="processing result...")

            # Track usage from ResilientLLM
            if tracker and hasattr(agent.llm, 'usage_log') and agent.llm.usage_log:
                last_usage = agent.llm.usage_log[-1]
                try:
                    call_cost = tracker.record(
                        agent_id=agent.cfg.agent_id,
                        model=last_usage.model or agent.cfg.model,
                        prompt_tokens=last_usage.prompt_tokens,
                        completion_tokens=last_usage.completion_tokens,
                        latency_ms=last_usage.latency_ms,
                        success=last_usage.success,
                        retries=last_usage.retries,
                        failover=last_usage.failover_used,
                    )
                    # Write cost to task board for dashboard display
                    board.set_cost(task.task_id, call_cost)
                except Exception as budget_err:
                    from core.usage_tracker import BudgetExceeded
                    if isinstance(budget_err, BudgetExceeded):
                        logger.warning("[%s] %s — saving result and exiting",
                                       agent.cfg.agent_id, budget_err)
                        board.submit_for_review(task.task_id, result)
                        # Let the reviewer complete the task normally;
                        # auto-complete only if no reviewers available
                        reviewers = config.get("reputation", {}).get(
                            "peer_review_agents", [])
                        if not any(r != agent.cfg.agent_id for r in reviewers):
                            board.complete(task.task_id)
                        return  # exit agent loop gracefully
                    raise

            is_planner = "planner" in agent.cfg.agent_id.lower()

            # Planner: extract subtasks and enter waiting state for close-out
            if is_planner:
                subtask_ids = _extract_and_create_subtasks(
                    board, result, task.task_id)
                if subtask_ids:
                    # Store subtask mapping for close-out tracking
                    _register_subtasks(bus, task.task_id, subtask_ids)
                    # Keep planner result but mark as review (waiting for close-out)
                    board.submit_for_review(task.task_id, result)
                    logger.info("[%s] planner created %d subtasks for task %s, waiting for close-out",
                                agent.cfg.agent_id, task.task_id, len(subtask_ids))
                else:
                    # No subtasks extracted — auto-complete
                    board.submit_for_review(task.task_id, result)
                    board.complete(task.task_id)
                    logger.info("[%s] planner auto-completed task %s (no subtasks)",
                                agent.cfg.agent_id, task.task_id)
                await sched.on_task_complete(agent.cfg.agent_id, task, result)
                _extract_and_store_memories(agent, task, result)

                # Check if any pending close-outs are ready
                await _check_planner_closeouts(agent, bus, board, config)
                continue

            # Executor: complexity-based routing after task completion
            board.submit_for_review(task.task_id, result)

            # Get task complexity
            task_data = board.get(task.task_id)
            task_complexity = "normal"
            if task_data:
                # Read complexity from task dict
                raw_data = board._read()
                raw_t = raw_data.get(task.task_id, {})
                task_complexity = raw_t.get("complexity", "normal")

            if task_complexity == "simple":
                # Simple tasks: skip review, auto-complete
                board.complete(task.task_id)
                logger.info("[%s] simple task %s auto-completed (skip review)",
                            agent.cfg.agent_id, task.task_id)
            else:
                # Normal/complex tasks: send critique request to advisor
                reviewers = config.get("reputation", {}).get(
                    "peer_review_agents", [])
                critique_sent = False
                for r_id in reviewers:
                    if r_id != agent.cfg.agent_id:
                        agent.send_mail(r_id,
                                        _json_critique_request(task, result),
                                        msg_type="critique_request")
                        critique_sent = True

                if not critique_sent:
                    logger.warning(
                        "[%s] no advisors available, auto-completing task %s",
                        agent.cfg.agent_id, task.task_id)
                    board.complete(task.task_id)

            # --- score & evolve ---
            await sched.on_task_complete(agent.cfg.agent_id, task, result)
            _extract_and_store_memories(agent, task, result)

            # Check if planner close-outs are now possible
            await _check_planner_closeouts(agent, bus, board, config)

        except Exception as exc:
            logger.exception("[%s] task %s failed: %s",
                             agent.cfg.agent_id, task.task_id, exc)
            board.fail(task.task_id, str(exc))
            await sched.on_error(agent.cfg.agent_id, task.task_id, str(exc))
            # Track failed usage
            if tracker:
                tracker.record(
                    agent_id=agent.cfg.agent_id,
                    model=agent.cfg.model,
                    success=False,
                )


def _json_critique_request(task, result: str) -> str:
    """Build JSON payload for a critique/review request."""
    return json.dumps({
        "task_id":     task.task_id,
        "description": task.description,
        "result":      result,
    }, ensure_ascii=False)


# Legacy alias
_json_review_request = _json_critique_request


# ── Planner close-out helpers ──────────────────────────────────────────────

# File-based subtask registry for planner close-out tracking
_SUBTASK_MAP_FILE = ".planner_subtasks.json"
_SUBTASK_MAP_LOCK = ".planner_subtasks.lock"


def _register_subtasks(bus: "ContextBus", parent_task_id: str,
                       subtask_ids: list[str]):
    """Register parent→subtask mapping for planner close-out."""
    lock = FileLock(_SUBTASK_MAP_LOCK)
    with lock:
        try:
            with open(_SUBTASK_MAP_FILE, "r") as f:
                mapping = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            mapping = {}
        mapping[parent_task_id] = subtask_ids
        with open(_SUBTASK_MAP_FILE, "w") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)


async def _check_planner_closeouts(agent, bus, board: TaskBoard, config: dict):
    """Check if any planner parent tasks have all subtasks completed.
    If so, synthesize a final answer and complete the parent task."""
    lock = FileLock(_SUBTASK_MAP_LOCK)
    with lock:
        try:
            with open(_SUBTASK_MAP_FILE, "r") as f:
                mapping = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

    if not mapping:
        return

    data = board._read()
    completed_ids = set()
    for parent_id, subtask_ids in list(mapping.items()):
        parent = data.get(parent_id)
        if not parent:
            completed_ids.add(parent_id)
            continue
        # Already completed?
        if parent.get("status") == "completed":
            completed_ids.add(parent_id)
            continue

        # Check if ALL subtasks are completed
        all_done = all(
            data.get(sid, {}).get("status") == "completed"
            for sid in subtask_ids
        )
        if not all_done:
            continue

        # All subtasks done — synthesize final answer with reviewer feedback
        logger.info("All %d subtasks completed for parent %s, synthesizing close-out",
                     len(subtask_ids), parent_id)
        results_text, critique_text = board.collect_results_with_critiques(
            parent_id, subtask_ids=subtask_ids)
        parent_desc = parent.get("description", "")

        close_prompt = (
            f"You are synthesizing the FINAL answer for the user.\n\n"
            f"## Original User Request\n{parent_desc}\n\n"
            f"## Subtask Results (from executor)\n{results_text}\n\n"
            f"## Reviewer Feedback (scores & suggestions)\n{critique_text}\n\n"
            f"## Instructions\n"
            f"1. Synthesize ALL subtask results into ONE coherent, polished response.\n"
            f"2. Consider reviewer suggestions — incorporate valid improvements.\n"
            f"3. Remove all internal task IDs, agent references, and metadata.\n"
            f"4. Your response must DIRECTLY answer the user's original question.\n"
            f"5. 用中文回复用户 (respond in Chinese).\n"
        )
        try:
            messages = [
                {"role": "system", "content": agent.cfg.role},
                {"role": "user",   "content": close_prompt},
            ]
            final_answer = await agent.llm.chat(messages, agent.cfg.model)

            # Update parent task with synthesized result and complete
            with board.lock:
                data = board._read()
                t = data.get(parent_id)
                if t:
                    t["result"] = final_answer
                    t["status"] = "completed"
                    t["completed_at"] = time.time()
                    board._write(data)
            logger.info("Planner close-out completed for task %s", parent_id)
        except Exception as e:
            logger.error("Planner close-out failed for %s: %s", parent_id, e)
            # On failure, just complete with collected results
            with board.lock:
                data = board._read()
                t = data.get(parent_id)
                if t:
                    t["result"] = results_text
                    t["status"] = "completed"
                    t["completed_at"] = time.time()
                    board._write(data)

        completed_ids.add(parent_id)

    # Clean up completed entries from mapping
    if completed_ids:
        with lock:
            try:
                with open(_SUBTASK_MAP_FILE, "r") as f:
                    mapping = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                mapping = {}
            for pid in completed_ids:
                mapping.pop(pid, None)
            with open(_SUBTASK_MAP_FILE, "w") as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)


# ── Adapter factories ───────────────────────────────────────────────────────

def _build_llm_for_agent(agent_def: dict, config: dict):
    """
    Build a Resilient LLM adapter for a specific agent.
    Per-agent llm: block overrides global llm: config.
    Wraps base adapter with retry, circuit breaker, and model failover.
    """
    agent_llm    = agent_def.get("llm", {})
    provider     = agent_llm.get("provider") or config.get("llm", {}).get("provider", "flock")
    api_key_env  = agent_llm.get("api_key_env", "")
    base_url_env = agent_llm.get("base_url_env", "")

    api_key  = os.getenv(api_key_env) if api_key_env else None
    base_url = os.getenv(base_url_env) if base_url_env else None

    # Build base adapter
    if provider == "flock":
        from adapters.llm.flock import FLockAdapter
        base = FLockAdapter(api_key=api_key, base_url=base_url)
    elif provider == "openai":
        from adapters.llm.openai import OpenAIAdapter
        base = OpenAIAdapter(api_key=api_key, base_url=base_url)
    elif provider == "ollama":
        from adapters.llm.ollama import OllamaAdapter
        base = OllamaAdapter(api_key=api_key, base_url=base_url)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")

    # Wrap with resilience layer (retry + circuit breaker + model failover)
    from adapters.llm.resilience import ResilientLLM

    fallback_models = agent_def.get("fallback_models", [])
    resilience_cfg  = config.get("resilience", {})

    return ResilientLLM(
        adapter=base,
        fallback_models=fallback_models,
        max_retries=resilience_cfg.get("max_retries", 3),
        base_delay=resilience_cfg.get("base_delay", 1.0),
        max_delay=resilience_cfg.get("max_delay", 30.0),
        jitter=resilience_cfg.get("jitter", 0.5),
        cb_threshold=resilience_cfg.get("circuit_breaker_threshold", 3),
        cb_cooldown=resilience_cfg.get("circuit_breaker_cooldown", 120.0),
    )

def _build_memory(config: dict, agent_id: str = ""):
    """
    Build memory adapter with per-agent isolation.
    Each agent gets its own persist directory: memory/agents/{agent_id}/chroma/
    Falls back to shared memory/chroma/ if agent_id is empty.
    """
    backend = config.get("memory", {}).get("backend", "chroma")
    if agent_id:
        persist_dir = os.path.join("memory", "agents", agent_id, "chroma")
    else:
        persist_dir = "memory/chroma"

    if backend == "hybrid":
        from adapters.memory.hybrid import HybridAdapter
        return HybridAdapter(persist_dir=persist_dir)
    elif backend == "chroma":
        from adapters.memory.chroma import ChromaAdapter
        return ChromaAdapter(persist_dir=persist_dir)
    elif backend == "mock":
        from adapters.memory.mock import MockMemory
        return MockMemory()
    else:
        raise ValueError(f"Unknown memory backend: {backend}")

def _build_chain(config: dict):
    if not config.get("chain", {}).get("enabled", False):
        from adapters.chain.mock import MockChain
        return MockChain()
    try:
        from adapters.chain.chain_manager import ChainManager
        return ChainManager(config)
    except Exception as e:
        logger.warning("ChainManager init failed (%s), falling back to MockChain", e)
        from adapters.chain.mock import MockChain
        return MockChain()


def _build_episodic_memory(config: dict, agent_id: str):
    """
    Build episodic memory (per-agent) and knowledge base (shared).
    Returns (EpisodicMemory | None, KnowledgeBase | None).
    """
    mem_cfg = config.get("memory", {})
    if not mem_cfg.get("long_term", True):
        return None, None

    episodic = None
    kb = None

    try:
        from adapters.memory.episodic import EpisodicMemory
        episodic = EpisodicMemory(agent_id=agent_id)
        logger.info("[%s] episodic memory initialized", agent_id)
    except Exception as e:
        logger.warning("[%s] episodic memory init failed: %s", agent_id, e)

    try:
        from adapters.memory.knowledge_base import KnowledgeBase
        kb = KnowledgeBase()
        logger.info("[%s] knowledge base connected", agent_id)
    except Exception as e:
        logger.warning("[%s] knowledge base init failed: %s", agent_id, e)

    return episodic, kb


# ── Post-task memory extraction ────────────────────────────────────────────

def _extract_and_store_memories(agent, task, result: str):
    """
    Extract reusable knowledge from a completed task and store
    in episodic memory + shared knowledge base.

    Called after task completion, non-blocking.
    """
    try:
        from adapters.memory.extractor import (
            extract_cases, extract_patterns, extract_insight)

        agent_id = agent.cfg.agent_id

        # Extract and store cases
        if agent.episodic:
            cases = extract_cases(task.description, result, agent_id)
            for case in cases:
                agent.episodic.save_case(
                    problem=case["problem"],
                    solution=case["solution"],
                    tags=case.get("tags", []),
                    source_task_id=task.task_id,
                )

            patterns = extract_patterns(task.description, result, agent_id)
            for pat in patterns:
                agent.episodic.save_pattern(
                    pattern=pat["pattern"],
                    evidence=pat["evidence"],
                    tags=pat.get("tags", []),
                )

        # Extract and publish cross-agent insight
        if agent.kb:
            insight = extract_insight(task.description, result, agent_id)
            if insight:
                agent.kb.add_insight(agent_id, insight)
                logger.debug("[%s] published insight to KB", agent_id)

    except Exception as e:
        logger.debug("[%s] memory extraction failed (non-critical): %s",
                     agent.cfg.agent_id, e)


# ── Orchestrator ────────────────────────────────────────────────────────────

class Orchestrator:
    """
    Reads agents.yaml, spins up one OS process per agent,
    submits the initial task, then waits for all processes to finish.
    Handles SIGTERM/SIGINT for graceful shutdown of all children.
    """

    def __init__(self, config_path: str = "config/agents.yaml"):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)
        self.bus    = ContextBus()
        self.board  = TaskBoard()
        self.procs: list[mp.Process] = []
        self._shutting_down = False

        # Auto-generate team skill on every launch
        try:
            from core.team_skill import generate_team_skill
            generate_team_skill(config_path=config_path)
        except Exception as e:
            logger.warning("Failed to generate team skill: %s", e)

    def submit(self, task_description: str,
               blocked_by: list[str] | None = None,
               required_role: str | None = None) -> str:
        """Create a task on the board. Returns task_id."""
        task = self.board.create(task_description, blocked_by=blocked_by,
                                  required_role=required_role)
        return task.task_id

    def run(self, task_description: str) -> None:
        """
        Submit task, launch all agent processes, wait for completion.
        Agents self-claim tasks from the board.
        Initial task always goes to planner first for decomposition.
        """
        self.submit(task_description, required_role="planner")
        self._launch_all()
        self._wait()

    def _launch_all(self):
        for agent_def in self.config["agents"]:
            compact_cfg = self.config.get("compaction", {})
            cfg_dict = {
                "agent_id":       agent_def["id"],
                "role":           agent_def["role"],
                "model":          agent_def["model"],
                "skills":         agent_def.get("skills", ["_base"]),
                "wallet_key":     os.getenv(
                    agent_def.get("wallet", ""), ""),
                "short_term_turns": agent_def.get("memory", {})
                                     .get("short_term_turns", 20),
                "long_term":      agent_def.get("memory", {})
                                     .get("long_term", True),
                "recall_top_k":   agent_def.get("memory", {})
                                     .get("recall_top_k", 3),
                "autonomy_level": agent_def.get("autonomy_level", 1),
                # Compaction config
                "compaction_enabled":    compact_cfg.get("enabled", True),
                "max_context_tokens":    compact_cfg.get("max_context_tokens", 8000),
                "summary_target_tokens": compact_cfg.get("summary_target_tokens", 1500),
                "keep_recent_turns":     compact_cfg.get("keep_recent_turns", 4),
                # Episodic + KB memory config
                "episodic_recall_budget": agent_def.get("memory", {})
                                          .get("episodic_recall_budget", 1500),
                "kb_recall_budget":       agent_def.get("memory", {})
                                          .get("kb_recall_budget", 800),
                # Tool configuration (OpenClaw-inspired)
                "tools_config":           agent_def.get("tools", {}),
            }
            p = mp.Process(
                target=_agent_process,
                args=(cfg_dict, agent_def, self.config),
                name=agent_def["id"],
                daemon=False,
            )
            p.start()
            self.procs.append(p)
            logger.info("launched process for agent '%s' (pid=%d)",
                        agent_def["id"], p.pid)

    def _wait(self):
        for p in self.procs:
            p.join()
        logger.info("all agent processes finished")

    def shutdown(self):
        """Gracefully shut down all agent processes."""
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Orchestrator shutting down — sending SIGTERM to %d processes",
                     len(self.procs))
        # Send shutdown via mailbox first (clean exit)
        for p in self.procs:
            if p.is_alive():
                self.shutdown_agent(p.name)
        # Give agents 5s to exit cleanly, then SIGTERM
        deadline = time.time() + 5
        while time.time() < deadline:
            if not any(p.is_alive() for p in self.procs):
                break
            time.sleep(0.5)
        # Force SIGTERM on remaining
        for p in self.procs:
            if p.is_alive():
                try:
                    os.kill(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        # Final wait
        for p in self.procs:
            p.join(timeout=3)

    def shutdown_agent(self, agent_id: str):
        """Send shutdown message via mailbox (Agent Teams pattern)."""
        # Phase 7: file-locked mailbox write
        path = f".mailboxes/{agent_id}.jsonl"
        lock = FileLock(path + ".lock")
        msg  = json.dumps({"from": "orchestrator", "type": "shutdown",
                            "content": "shutdown requested", "ts": time.time()})
        with lock:
            os.makedirs(".mailboxes", exist_ok=True)
            with open(path, "a") as f:
                f.write(msg + "\n")
