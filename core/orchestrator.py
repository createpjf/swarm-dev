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
import re
import signal
import sys
import time
from typing import Any

import yaml

from core.protocols import (  # shared utilities
    _strip_think, FileLock,
    CritiqueSpec, CritiqueDimensions, CritiqueItem,
    CritiqueVerdict, INTENT_KEY_PREFIX,
)

# ── Tool-block stripper ──
_TOOL_BLOCK_RE = re.compile(
    r"```tool\s*\n.*?\n```"          # ```tool ... ```
    r"|<tool_code>.*?</tool_code>"   # <tool_code>...</tool_code>
    r"|```tool\s*\n.*?</tool_code>"  # mixed: ```tool ... </tool_code>
    r"|<tool_code>.*?\n```",         # mixed: <tool_code> ... ```
    re.DOTALL)


def _strip_tool_blocks(text: str) -> str:
    """Strip remaining tool invocation blocks from final output."""
    text = _strip_think(text)
    text = _TOOL_BLOCK_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

# ── Graceful shutdown flag (per-process) ──
_shutdown_requested = False

from core.agent import AgentConfig
from core.context_bus import ContextBus
from core.task_board import TaskBoard

logger = logging.getLogger(__name__)


# ── Per-process entry point ─────────────────────────────────────────────────

def _agent_process(agent_cfg_dict: dict, agent_def: dict, config: dict,
                    wakeup=None):
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
        asyncio.run(_agent_loop(agent, bus, board, config, tracker, hb,
                                wakeup=wakeup))
    finally:
        hb.stop()  # clean up heartbeat file on exit


# ── Helper: resolve agent model from config ──────────────────────────────────

_agent_model_cache: dict[str, str] = {}

def _get_agent_model(agent_id: str) -> str:
    """Look up the model name for a given agent from agents.yaml."""
    if agent_id in _agent_model_cache:
        return _agent_model_cache[agent_id]
    try:
        with open("config/agents.yaml") as f:
            cfg = yaml.safe_load(f)
        for a in cfg.get("agents", []):
            aid = a.get("id", "")
            mdl = a.get("model", "unknown")
            _agent_model_cache[aid] = mdl
        return _agent_model_cache.get(agent_id, "unknown")
    except Exception:
        return "unknown"


# ── Critique request handler (replaces review_request) ─────────────────────

async def _handle_critique_request(agent, board: TaskBoard, mail: dict, sched):
    """
    V0.02 Advisor mode: structured 5-dimension scoring via CritiqueSpec.
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

    # V0.02: Look up original user intent for context (IntentAnchor)
    intent_context = ""
    task_obj_intent = board.get(task_id)
    if task_obj_intent and task_obj_intent.parent_id:
        try:
            from core.context_bus import ContextBus
            bus_path = os.path.join(os.path.dirname(board.path), ".context_bus.json")
            _bus = ContextBus(bus_path)
            parent_intent = _bus.get("system",
                                     f"{INTENT_KEY_PREFIX}{task_obj_intent.parent_id}")
            if parent_intent:
                intent_val = (parent_intent.get("value", parent_intent)
                              if isinstance(parent_intent, dict)
                              else parent_intent)
                intent_context = f"## Original User Intent\n{intent_val}\n\n"
        except Exception:
            pass

    # V0.02 CritiqueSpec prompt (5-dimension structured scoring)
    critique_prompt = (
        f"Score this subtask output using 5 dimensions (1-10 each).\n\n"
        f"{intent_context}"
        f"## Subtask\n{description}\n\n"
        f"## Output\n{result}\n\n"
        f"IMPORTANT: This is a SUBTASK result (raw data/code), NOT a final user-facing answer.\n"
        f"The planner will synthesize all subtask results into the final response.\n"
        f"Judge each dimension independently.\n\n"
        f"Respond with JSON:\n"
        f'{{"dimensions": {{"accuracy": <1-10>, "completeness": <1-10>, '
        f'"technical": <1-10>, "calibration": <1-10>, "efficiency": <1-10>}}, '
        f'"verdict": "LGTM" or "NEEDS_WORK", '
        f'"items": [{{"dimension": "...", "issue": "...", "suggestion": "..."}}], '
        f'"confidence": <0.0-1.0>}}\n\n'
        f"Rules:\n"
        f"- Weights: accuracy 30%, completeness 20%, technical 20%, calibration 20%, efficiency 10%\n"
        f"- If ALL scores >= 8: verdict MUST be LGTM, items MUST be empty []\n"
        f"- Max 3 items. Only for dimensions scoring < 8.\n"
        f"- If any score < 5: verdict MUST be NEEDS_WORK with item for that dimension.\n"
    )
    messages = [
        {"role": "system", "content": agent.cfg.role},
        {"role": "user",   "content": critique_prompt},
    ]

    critique: CritiqueSpec | None = None
    score = 7
    suggestions: list[str] = []
    comment = ""

    try:
        raw = await agent.llm.chat(messages, agent.cfg.model)
        # Extract JSON object from response (handles markdown wrapping, prose preamble, etc.)
        json_str = raw
        start = json_str.find("{")
        end = json_str.rfind("}") + 1
        if start >= 0 and end > start:
            json_str = json_str[start:end]

        parsed = json.loads(json_str)

        # Detect V0.02 format (has "dimensions") vs V0.01 format (has "score")
        if "dimensions" in parsed:
            critique = CritiqueSpec.from_json(json_str)
            critique.task_id = task_id
            critique.reviewer_id = agent.cfg.agent_id
            critique.timestamp = time.time()
            critique.auto_simplify()
            score = int(critique.composite_score)
            suggestions = [item.suggestion for item in critique.items if item.suggestion]
            comment = f"5D Score: {critique.composite_score:.1f} [{critique.verdict}]"
        else:
            # V0.01 fallback: {"score": N, "suggestions": [...], "comment": "..."}
            score = parsed.get("score", 7)
            suggestions = parsed.get("suggestions", [])
            comment = parsed.get("comment", "")
            critique = CritiqueSpec.from_legacy_score(score, comment, suggestions)
            critique.task_id = task_id
            critique.reviewer_id = agent.cfg.agent_id
            critique.timestamp = time.time()

    except Exception as e:
        logger.error("[%s] critique LLM call failed: %s", agent.cfg.agent_id, e)
        score, suggestions, comment = 7, [], f"Critique failed: {e}"
        critique = CritiqueSpec.from_legacy_score(score, comment)
        critique.task_id = task_id
        critique.reviewer_id = agent.cfg.agent_id
        critique.timestamp = time.time()

    passed = True  # Reviewer NEVER blocks — always pass

    # Store critique + CritiqueSpec atomically (single write to avoid race)
    board.add_critique(task_id, agent.cfg.agent_id, passed, suggestions, comment,
                       score=score,
                       critique_spec_json=critique.to_json() if critique else None)

    await sched.on_critique(agent.cfg.agent_id, passed, score=score)

    task_obj = board.get(task_id)
    if task_obj and task_obj.agent_id:
        await sched.on_critique_result(
            task_obj.agent_id,
            passed_first_time=True,
            had_revision=False,
            critique_score=score,
        )

    # V0.02: Append to critique_log for TextGrad Pipeline
    if critique:
        _eval_agent = task_obj.agent_id if (task_obj and task_obj.agent_id) else ""
        _append_critique_log(critique, evaluated_agent_id=_eval_agent)

    # ── Persist critique to Alic's episodic memory ──────────────────────
    try:
        from adapters.memory.episodic import make_episode
        if hasattr(agent, "episodic") and agent.episodic:
            task_obj_ep = board.get(task_id)
            evaluated_agent_id = (task_obj_ep.agent_id
                                  if task_obj_ep else "unknown")
            evaluated_model = _get_agent_model(evaluated_agent_id)

            episode = make_episode(
                agent_id=agent.cfg.agent_id,
                task_id=f"critique_{task_id}",
                task_description=(
                    f"Review [{evaluated_agent_id}] task: "
                    f"{description[:200]}"),
                result=json.dumps({
                    "score": score,
                    "critique_spec": critique.to_json() if critique else None,
                    "suggestions": suggestions,
                    "comment": comment,
                    "evaluated_agent": evaluated_agent_id,
                    "evaluated_task_id": task_id,
                }, ensure_ascii=False),
                score=score,
                tags=["critique", evaluated_agent_id],
                context={
                    "evaluated_agent": evaluated_agent_id,
                    "evaluated_model": evaluated_model,
                    "reviewer_model": agent.cfg.model,
                    "original_description": description[:500],
                    "original_result_preview": result[:500],
                },
                outcome="success",
                model=agent.cfg.model,
            )
            agent.episodic.save_episode(episode)

            icon = "⭐" if score >= 8 else "⚠️" if score >= 5 else "❌"
            verdict_str = critique.verdict if critique else "N/A"
            agent.episodic.append_daily_log(
                f"{icon} **Review** [{evaluated_agent_id}] "
                f"(model: {evaluated_model})\n"
                f"**Composite:** {score}/10 [{verdict_str}]\n"
                f"**Task:** {description[:100]}\n"
                f"**Comment:** {comment[:200]}"
            )
    except Exception as e:
        logger.debug("[%s] critique episode save failed: %s",
                     agent.cfg.agent_id, e)

    logger.info("[%s] scored task %s: %d/10 [%s]%s",
                agent.cfg.agent_id, task_id, score,
                critique.verdict if critique else "N/A",
                f" ({len(suggestions)} items)" if suggestions else "")


# ── V0.02: Critique log for TextGrad Pipeline ────────────────────────────

CRITIQUE_LOG_FILE = os.path.join("memory", "critique_log.jsonl")


def _append_critique_log(critique, evaluated_agent_id: str = "") -> None:
    """Append CritiqueSpec to the critique log for TextGrad consumption."""
    try:
        os.makedirs(os.path.dirname(CRITIQUE_LOG_FILE), exist_ok=True)
        # Build entry with evaluated agent_id (TextGrad groups by this)
        entry = json.loads(critique.to_json())
        entry["agent_id"] = evaluated_agent_id
        with open(CRITIQUE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("critique_log append failed: %s", e)


# Legacy handler: forward old review_request to critique handler
async def _handle_review_request(agent, board: TaskBoard, mail: dict, sched):
    """Backward-compatible wrapper: treat review_request as critique_request."""
    await _handle_critique_request(agent, board, mail, sched)


# ── Subtask extraction (Phase 5) ──────────────────────────────────────────


def _repair_json_quotes(raw: str):
    """Fix LLM JSON with unescaped inner quotes by iteratively escaping at
    error positions.

    LLMs often produce:  {"objective": "标题: "内容""}
    where the inner quotes aren't escaped.  json.loads() fails because
    the inner ``"`` terminates the string prematurely.

    Strategy: catch JSONDecodeError, find the offending quote just before
    the error position, escape it, retry.  Repeat up to 20 times.
    Returns the parsed dict on success, or *None* on failure.
    """
    import json as _json
    s = raw
    for _ in range(20):
        try:
            return _json.loads(s)
        except _json.JSONDecodeError as e:
            if e.pos is None or e.pos <= 0:
                return None
            # Walk backward from error position to find the unescaped quote
            p = min(e.pos, len(s) - 1)
            while p >= 0 and s[p] != '"':
                p -= 1
            if p <= 0 or (p > 0 and s[p - 1] == '\\'):
                return None
            # Escape this inner quote and retry
            s = s[:p] + '\\' + s[p:]
    return None


def _extract_subtask_specs(planner_output: str, parent_task_id: str,
                           parent_description: str) -> list:
    """Parse planner output for SubTaskSpec JSON blocks or legacy TASK: lines.

    V0.02 format (preferred):
        ```subtask
        {"objective": "...", "tool_hint": ["web"], ...}
        ```

    V0.01 fallback:
        TASK: <description>
        COMPLEXITY: simple|normal|complex

    Returns list of SubTaskSpec objects.
    """
    from core.protocols import SubTaskSpec

    specs: list[SubTaskSpec] = []

    # --- Phase 1: Try V0.02 ```subtask JSON blocks ---
    # V0.03: Multi-pattern extraction (most specific → most lenient)
    # to handle common LLM output variations that the rigid V0.02 regex missed.
    import re
    _subtask_patterns = [
        re.compile(r'```subtask\s*\n(.*?)\n\s*```', re.DOTALL),      # exact
        re.compile(r'```\s*subtask\s*\n(.*?)\n\s*```', re.DOTALL),   # space before subtask
        re.compile(r'```subtask\s*([\{].*?[\}])\s*```', re.DOTALL),  # no newline required
    ]
    for pat in _subtask_patterns:
        for match in pat.finditer(planner_output):
            raw_json = match.group(1).strip()
            try:
                spec = SubTaskSpec.from_json(raw_json)
                specs.append(spec)
            except Exception as e:
                logger.warning("Failed to parse subtask spec JSON: %s — %s",
                               raw_json[:100], e)
                # Try to repair LLM's malformed JSON (unescaped inner quotes)
                repaired = _repair_json_quotes(raw_json)
                if repaired and isinstance(repaired, dict) and "objective" in repaired:
                    try:
                        spec = SubTaskSpec(**{
                            k: v for k, v in repaired.items()
                            if k in SubTaskSpec.__dataclass_fields__})
                        specs.append(spec)
                        logger.info(
                            "Repaired malformed subtask JSON — objective: %s",
                            spec.objective[:80])
                    except Exception as e2:
                        logger.warning("JSON repair also failed: %s", e2)
        if specs:
            break  # Found specs with this pattern, stop trying others

    if specs:
        logger.info("Parsed %d SubTaskSpec blocks from planner output (V0.02)",
                     len(specs))
        return specs

    # --- Phase 1.5: Detect bare SubTaskSpec JSON objects (no fences) ---
    # V0.03: If Leo outputs {"objective": "..."} blocks without ```subtask
    # fences, detect and parse them as a fallback.
    _bare_json_pattern = re.compile(
        r'\{[^{}]*"objective"\s*:\s*"[^"]+?"[^{}]*\}')
    for match in _bare_json_pattern.finditer(planner_output):
        raw_json = match.group(0).strip()
        try:
            spec = SubTaskSpec.from_json(raw_json)
            specs.append(spec)
        except Exception as e:
            logger.debug("Bare JSON object parse failed: %s — %s",
                         raw_json[:100], e)

    if specs:
        logger.info("Parsed %d SubTaskSpec blocks from bare JSON (Phase 1.5 fallback)",
                     len(specs))
        return specs

    # --- Phase 2: Fallback to V0.01 TASK:/COMPLEXITY: lines ---
    lines = planner_output.strip().split("\n")
    pending_description: str | None = None

    for line in lines:
        stripped = line.strip()
        for prefix in ("- ", "* ", "• "):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                break

        # Check for COMPLEXITY: line (follows a TASK: line)
        if stripped.upper().startswith("COMPLEXITY:") and pending_description:
            complexity = stripped[11:].strip().lower()
            if complexity == "simple":
                complexity = "normal"
            if complexity not in ("normal", "complex"):
                complexity = _infer_complexity(pending_description)
            specs.append(SubTaskSpec.from_legacy_task(
                pending_description, complexity))
            pending_description = None
            continue

        # If we had a pending TASK without COMPLEXITY, create it now
        if pending_description:
            complexity = _infer_complexity(pending_description)
            specs.append(SubTaskSpec.from_legacy_task(
                pending_description, complexity))
            pending_description = None

        if stripped.upper().startswith("TASK:"):
            description = stripped[5:].strip()
            if description:
                pending_description = description

    # Flush last pending task
    if pending_description:
        complexity = _infer_complexity(pending_description)
        specs.append(SubTaskSpec.from_legacy_task(
            pending_description, complexity))

    if specs:
        logger.info("Parsed %d subtask specs from TASK: lines (V0.01 fallback)",
                     len(specs))
    else:
        # V0.03: Log extraction failure for debugging
        logger.warning(
            "[subtask_extract] 0 specs from planner output (%d chars). "
            "First 500 chars: %s", len(planner_output),
            planner_output[:500])

    return specs


def _create_subtasks_from_specs(board: TaskBoard, specs: list,
                                 parent_task_id: str,
                                 parent_description: str) -> list[str]:
    """Create TaskBoard tasks from SubTaskSpec list.

    Returns list of created subtask IDs.
    """
    subtask_ids: list[str] = []

    for spec in specs:
        # Inject parent_intent (IntentAnchor — Improvement 5)
        if not spec.parent_intent:
            spec.parent_intent = parent_description

        description = spec.to_task_description()
        role = _infer_role(spec.objective)
        complexity = spec.complexity

        new_task = board.create(
            description,
            blocked_by=[],
            required_role=role,
            parent_id=parent_task_id,
        )
        # Set complexity and spec on the task
        with board.lock:
            data = board._read()
            t = data.get(new_task.task_id)
            if t:
                t["complexity"] = complexity
                t["spec"] = spec.to_json()
                board._write(data)
        subtask_ids.append(new_task.task_id)
        logger.info("Created subtask %s [role=%s, complexity=%s, tools=%s]: %s",
                     new_task.task_id, role or "any", complexity,
                     ",".join(spec.tool_hint) or "all",
                     spec.objective[:60])

    if subtask_ids:
        logger.info("Planner extracted %d subtasks from task %s",
                     len(subtask_ids), parent_task_id)

    return subtask_ids


# Keep legacy function as alias for backward compatibility
def _extract_and_create_subtasks(board: TaskBoard, planner_output: str,
                                  parent_task_id: str) -> list[str]:
    """Legacy wrapper: parse + create in one step (V0.01 compatibility)."""
    specs = _extract_subtask_specs(planner_output, parent_task_id, "")
    return _create_subtasks_from_specs(board, specs, parent_task_id, "")


def _infer_complexity(description: str) -> str:
    """Infer task complexity from description keywords.

    Conservative approach: most tasks go through review (Alic).
    Only mark 'simple' for truly trivial operations like listing or printing.
    Leo can explicitly set complexity via COMPLEXITY: tag in subtask descriptions.
    """
    desc_lower = description.lower()

    # 1) Check for explicit COMPLEXITY: tag from Leo's decomposition
    import re
    explicit = re.search(r'complexity:\s*(simple|normal|complex)', desc_lower)
    if explicit:
        return explicit.group(1)

    # 2) Complex: tasks requiring analysis/judgment
    if any(kw in desc_lower for kw in [
        "review", "audit", "verify", "analyze", "evaluate", "compare",
        "research", "investigate", "design", "architect", "plan",
    ]):
        return "complex"

    # 3) Simple: only for extremely trivial read-only operations
    #    (narrowed from previous overly-broad list)
    if any(kw in desc_lower for kw in [
        "print hello", "echo ", "list directory",
    ]):
        return "simple"

    # 4) Default: normal → goes through Alic review
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
        "synthesize", "summary", "综合", "总结",
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


def _has_pending_closeouts() -> bool:
    """Return True if any planner close-outs are registered but not yet synthesized.

    Prevents agents from exiting while a planner is waiting for subtask
    results to be collected and synthesized into a final answer.
    """
    try:
        lock = FileLock(_SUBTASK_MAP_LOCK)
        with lock:
            with open(_SUBTASK_MAP_FILE, "r") as f:
                mapping = json.load(f)
        return bool(mapping)
    except (FileNotFoundError, json.JSONDecodeError, Exception):
        return False


# ── Agent loop ─────────────────────────────────────────────────────────────

async def _agent_loop(agent, bus: ContextBus, board: TaskBoard,
                       config: dict, tracker=None, heartbeat=None,
                       wakeup=None):
    """Core event loop for every agent process (Leo, Jerry, Alic).

    Runs as the main coroutine inside each ``multiprocessing.Process``
    spawned by ``Orchestrator._launch_all()``.  The loop is a state
    machine with three priority-ordered claim stages per tick:

    **State Machine (per tick, high → low priority):**

    1. **Mailbox scan** — read P2P messages from teammates.
       • ``shutdown`` → exit.
       • ``critique_request`` / ``review_request`` → advisor handles the
         review, then checks whether any planner close-outs unblocked.
    2. **Critique revision** — if the agent's own task was sent back
       with suggestions, pick it up for revision (max 1 round).
    3. **Regular task claim** — ``board.claim_next()`` using the agent's
       role (planner / executor / advisor) and reputation score.
       • **Planner path:** run task → extract subtasks → register
         parent-child mapping → wait for close-out.
       • **Executor path:** run task → submit result → route to
         advisor (complexity-based: simple tasks skip review).

    **Idle / shutdown behaviour:**
    - Each tick with no claimable task increments ``idle_count``.
    - If other tasks are still active (pending/claimed/review/blocked),
      the counter grows at half rate so the agent waits for subtask
      completions.
    - ``max_idle_cycles`` (default 30, overridden to ~300 by the
      persistent pool in ``ChannelManager``) controls exit threshold.

    **Background duties (every 30 s):**
    - ``board.recover_stale_tasks()`` — reclaim tasks stuck in
      ``claimed`` state beyond their heartbeat timeout.

    **Reputation integration:**
    - ``ReputationScheduler.on_task_complete()`` / ``on_error()`` feed
      the 5-dim EMA scorer after each task.
    - Score influences task claim priority via ``board.claim_next()``.

    Args:
        agent:     Fully initialised ``BaseAgent`` (skills, memory, LLM loaded).
        bus:       Shared ``ContextBus`` for cross-agent state snapshots.
        board:     File-locked ``TaskBoard`` (single source of truth for tasks).
        config:    Merged ``agents.yaml`` config dict.
        tracker:   Optional ``UsageTracker`` for per-call cost accounting.
        heartbeat: Optional ``Heartbeat`` writer for gateway status display.
    """
    from reputation.scheduler import ReputationScheduler
    sched = ReputationScheduler(board)

    idle_count = 0
    max_idle   = config.get("max_idle_cycles", 30)
    _last_recovery_check = 0.0
    _last_work_time: float = 0.0  # for 1.5s status-light delay

    # V0.02: MemoryConsolidator (background, non-blocking)
    _consolidator = None
    try:
        from adapters.memory.consolidator import MemoryConsolidator
        from adapters.memory.episodic import EpisodicMemory
        from adapters.memory.knowledge_base import KnowledgeBase
        _ep_mem = EpisodicMemory(agent.cfg.agent_id)
        _kb = KnowledgeBase()
        _consolidator = MemoryConsolidator(_ep_mem, _kb)
    except Exception as e:
        logger.debug("[%s] consolidator init skipped: %s",
                     agent.cfg.agent_id, e)

    # V0.02: TextGrad Pipeline (background, non-blocking)
    _textgrad = None
    try:
        from reputation.textgrad import TextGradPipeline
        _textgrad = TextGradPipeline()
    except Exception as e:
        logger.debug("[%s] textgrad init skipped: %s",
                     agent.cfg.agent_id, e)

    while True:
        # --- check shutdown signal ---
        if _shutdown_requested:
            logger.info("[%s] shutdown signal received, exiting gracefully",
                        agent.cfg.agent_id)
            return

        # --- heartbeat (1.5s delay after task before going idle) ---
        if heartbeat:
            now = time.time()
            if _last_work_time and now - _last_work_time < 1.5:
                heartbeat.beat("working", progress="wrapping up...")
            else:
                heartbeat.beat("idle")

        # --- periodic stale task recovery (every 30s) ---
        now = time.time()
        if now - _last_recovery_check > 30:
            _last_recovery_check = now
            recovered = board.recover_stale_tasks()
            if recovered:
                logger.info("[%s] recovered %d stale tasks",
                            agent.cfg.agent_id, len(recovered))

        # --- V0.02: periodic memory consolidation (daily, non-blocking) ---
        if _consolidator and _consolidator.should_run(interval_seconds=86400):
            try:
                import asyncio as _aio
                _cons_stats = await _aio.to_thread(_consolidator.run)
                if _cons_stats.get("compressed", 0) > 0:
                    logger.info("[%s] memory consolidation: %s",
                                agent.cfg.agent_id, _cons_stats)
            except Exception as e:
                logger.debug("[%s] memory consolidation failed: %s",
                             agent.cfg.agent_id, e)

        # --- V0.02: TextGrad pipeline (every 60s, lightweight) ---
        if _textgrad and _textgrad.should_run(interval_seconds=60):
            try:
                import asyncio as _aio
                _tg_stats = await _aio.to_thread(_textgrad.run)
                if _tg_stats.get("agents_patched", 0) > 0:
                    logger.info("[%s] textgrad pipeline: %s",
                                agent.cfg.agent_id, _tg_stats)
            except Exception as e:
                logger.debug("[%s] textgrad pipeline failed: %s",
                             agent.cfg.agent_id, e)

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
                # After critique completes subtask → check if planner closeout is ready
                await _check_planner_closeouts(agent, bus, board, config)

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
            # Check lightweight task signals before full board scan
            signals = board.consume_task_signals()
            if signals:
                # New tasks created — immediately retry claim instead of sleeping
                logger.debug("[%s] task signal detected (%d new), retrying claim",
                             agent.cfg.agent_id, len(signals))
                continue

            # If there are still tasks in-progress (claimed/review/pending),
            # keep waiting — other agents might produce subtasks for us
            active = _has_active_tasks(board)
            pending_closeouts = _has_pending_closeouts()
            if active or pending_closeouts:
                idle_count = min(idle_count + 1, max_idle // 2)
                # Never exit while work is happening or closeouts pending
            else:
                idle_count += 1

            if idle_count >= max_idle and not active and not pending_closeouts:
                logger.info("[%s] idle limit reached (no active tasks, no pending closeouts), exiting",
                            agent.cfg.agent_id)
                return
            # Progressive backoff: idle longer → check less often (1s → 5s max)
            # WakeupBus: block on event instead of blind sleep — instant wakeup
            # when another agent creates subtasks.
            backoff = min(1.0 + idle_count * 0.5, 5.0)
            if wakeup:
                await wakeup.async_wait(agent.cfg.agent_id, timeout=backoff)
            else:
                await asyncio.sleep(backoff)
            continue

        idle_count = 0
        logger.info("[%s] claimed task %s", agent.cfg.agent_id, task.task_id)
        agent.log_transcript("task_claimed", task.task_id,
                             task.description[:200])

        # --- heartbeat: working ---
        if heartbeat:
            heartbeat.beat("working", task.task_id, progress="preparing...")

        try:
            if heartbeat:
                heartbeat.beat("working", task.task_id,
                               progress="loading skills & context...")

            # V0.02: Extract tool_hints from SubTaskSpec for ToolScope
            _tool_hints = None
            with board.lock:
                data = board._read()
                t = data.get(task.task_id)
                if t and t.get("spec"):
                    try:
                        from core.protocols import SubTaskSpec as _STS
                        _spec = _STS.from_json(t["spec"])
                        _tool_hints = _spec.tool_hint or None
                    except Exception:
                        pass

            result = await agent.run(task, bus, tool_hints=_tool_hints)

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

            _planner_ids = {"leo", "planner"}
            _aid = agent.cfg.agent_id.lower()
            # NOTE: Do NOT scan agent.cfg.role text for keywords — the role
            # field contains the full system prompt which may mention planner
            # keywords in negation (e.g. "you MUST NOT decompose").  Jerry's
            # role includes "decompose" in a prohibition, causing false-positive
            # planner detection and infinite fallback subtask loops.
            is_planner = _aid in _planner_ids

            # Planner: route decision, then extract subtasks or direct-answer
            if is_planner:
                # V0.02 TaskRouter: check if Leo declared a route
                _route_decision = None
                try:
                    from core.task_router import (
                        parse_route_from_output, classify_task)
                    from core.protocols import RouteDecision
                    _route_decision = parse_route_from_output(result)
                    if _route_decision is None:
                        # Leo didn't declare ROUTE: — use heuristic
                        _route_decision = classify_task(task.description)
                        logger.debug(
                            "[%s] task_router heuristic → %s for: %s",
                            agent.cfg.agent_id, _route_decision.name,
                            task.description[:80])
                    else:
                        logger.info(
                            "[%s] Leo declared ROUTE: %s",
                            agent.cfg.agent_id, _route_decision.name)
                except Exception as e:
                    logger.debug("[%s] task_router failed, defaulting MAS: %s",
                                 agent.cfg.agent_id, e)

                # DIRECT_ANSWER path: Leo already answered, skip Jerry+Alic
                if (_route_decision is not None
                        and _route_decision.name == "DIRECT_ANSWER"):
                    board.submit_for_review(task.task_id, result)
                    board.complete(task.task_id)
                    logger.info(
                        "[%s] DIRECT_ANSWER route — task %s completed by planner",
                        agent.cfg.agent_id, task.task_id)
                    await sched.on_task_complete(
                        agent.cfg.agent_id, task, result)
                    _extract_and_store_memories(agent, task, result)
                    _last_work_time = time.time()
                    await _check_planner_closeouts(
                        agent, bus, board, config)
                    continue

                # MAS_PIPELINE path: extract subtasks normally
                specs = _extract_subtask_specs(
                    result, task.task_id, task.description)
                subtask_ids = _create_subtasks_from_specs(
                    board, specs, task.task_id, task.description) if specs else []
                if subtask_ids:
                    # Store subtask mapping for close-out tracking
                    _register_subtasks(bus, task.task_id, subtask_ids)
                    # V0.02: Publish IntentAnchor to ContextBus L0 (TASK layer)
                    try:
                        from core.protocols import INTENT_KEY_PREFIX
                        from core.context_bus import LAYER_TASK
                        bus.publish("system",
                                    f"{INTENT_KEY_PREFIX}{task.task_id}",
                                    task.description,
                                    layer=LAYER_TASK)
                    except Exception as e:
                        logger.debug("[%s] intent anchor publish failed: %s",
                                     agent.cfg.agent_id, e)
                    # Keep planner result but mark as review (waiting for close-out)
                    board.submit_for_review(task.task_id, result)
                    logger.info("[%s] planner created %d subtasks for task %s, waiting for close-out",
                                agent.cfg.agent_id, len(subtask_ids), task.task_id)
                    # Wake executors instantly so they can claim subtasks
                    if wakeup:
                        wakeup.wake_all()
                else:
                    # No TASK: lines found — fallback delegation
                    stripped_result = result.strip()
                    # Guard: prevent recursive fallback chains.  If this
                    # task is ALREADY a fallback, don't create yet another
                    # one — just auto-complete to break the cascade.
                    _is_already_fallback = "planner fallback delegation" in (task.description or "")
                    if (len(stripped_result) > 20
                            and task.description.strip()
                            and not _is_already_fallback):
                        # Leo produced content but forgot TASK: format.
                        # Wrap the original request as a single implement subtask
                        # so Jerry still gets the work.
                        logger.warning(
                            "[%s] planner output has content but no TASK: lines "
                            "— auto-delegating to executor: %s",
                            agent.cfg.agent_id, task.task_id)
                        fallback_desc = (
                            f"Execute the following task (planner fallback delegation):\n"
                            f"Original request: {task.description[:500]}\n"
                            f"Reference plan: {stripped_result[:1500]}"
                        )
                        fallback_task = board.create(
                            fallback_desc,
                            blocked_by=[],
                            required_role="implement",
                            parent_id=task.task_id,
                        )
                        # Set complexity to normal (goes through Alic review)
                        with board.lock:
                            data = board._read()
                            t = data.get(fallback_task.task_id)
                            if t:
                                t["complexity"] = "normal"
                                board._write(data)
                        _register_subtasks(
                            bus, task.task_id, [fallback_task.task_id])
                        board.submit_for_review(task.task_id, result)
                        logger.info(
                            "[%s] auto-created fallback subtask %s for task %s",
                            agent.cfg.agent_id, fallback_task.task_id,
                            task.task_id)
                        # Wake executors for fallback subtask
                        if wakeup:
                            wakeup.wake_all()
                    else:
                        # Truly empty, trivially short, or already a
                        # fallback (recursion guard) — auto-complete.
                        if _is_already_fallback:
                            logger.warning(
                                "[%s] skipping recursive fallback for task %s "
                                "— auto-completing to break cascade",
                                agent.cfg.agent_id, task.task_id)
                        board.submit_for_review(task.task_id, result)
                        board.complete(task.task_id)
                        logger.info(
                            "[%s] planner auto-completed task %s (no subtasks)",
                            agent.cfg.agent_id, task.task_id)
                await sched.on_task_complete(agent.cfg.agent_id, task, result)
                _extract_and_store_memories(agent, task, result)
                _last_work_time = time.time()

                # Check if any pending close-outs are ready
                await _check_planner_closeouts(agent, bus, board, config)
                continue

            # Executor: complexity-based routing after task completion
            # V0.03: Capture complexity BEFORE submit to avoid TOCTOU race
            # (submit_for_review changes status; a concurrent read could see stale data)
            task_data = board.get(task.task_id)
            task_complexity = task_data.complexity if task_data else "normal"
            board.submit_for_review(task.task_id, result)

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
            _last_work_time = time.time()
            agent.log_transcript("task_completed", task.task_id,
                                 result[:200])

            # Check if planner close-outs are now possible
            await _check_planner_closeouts(agent, bus, board, config)

        except Exception as exc:
            logger.exception("[%s] task %s failed: %s",
                             agent.cfg.agent_id, task.task_id, exc)
            board.fail(task.task_id, str(exc))
            await sched.on_error(agent.cfg.agent_id, task.task_id, str(exc))
            # Store failure in episodic memory for error pattern learning
            error_type = type(exc).__name__
            agent._store_to_memory(task, f"FAILED: {exc}",
                                   outcome="failure", error_type=error_type)
            agent.log_transcript("task_failed", task.task_id,
                                 str(exc)[:200],
                                 metadata={"error_type": error_type})
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
                       subtask_ids: list[str]) -> None:
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
    If so, synthesize a final answer and complete the parent task.

    Uses FileLock around the entire read-check-synthesize cycle to prevent
    race conditions where multiple agents trigger close-out simultaneously.
    """
    lock = FileLock(_SUBTASK_MAP_LOCK)
    with lock:
        try:
            with open(_SUBTASK_MAP_FILE, "r") as f:
                mapping = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

    if not mapping:
        return

    # Re-read board under the board's own lock for consistency
    with board.lock:
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
        all_done = True
        has_in_review = False
        for sid in subtask_ids:
            st = data.get(sid, {}).get("status", "")
            if st == "completed":
                continue
            elif st in ("review", "critique"):
                has_in_review = True
                all_done = False
                break
            else:
                all_done = False
                break

        if not all_done:
            if has_in_review:
                logger.debug("Parent %s: subtask(s) still in review, deferring close-out",
                             parent_id)
            continue

        # V0.03: Double-check inside board lock to prevent race where two agents
        # both detect "all subtasks complete" and both enter synthesis.
        # Mark parent as "synthesizing" atomically to claim exclusive synthesis.
        with board.lock:
            fresh_data = board._read()
            fresh_parent = fresh_data.get(parent_id)
            if not fresh_parent or fresh_parent.get("status") in (
                    "completed", "synthesizing"):
                completed_ids.add(parent_id)
                continue  # Already handled by another agent
            fresh_all_done = all(
                fresh_data.get(sid, {}).get("status") == "completed"
                for sid in subtask_ids)
            if not fresh_all_done:
                continue  # Subtask status changed since initial check
            fresh_parent["status"] = "synthesizing"
            board._write(fresh_data)

        # All subtasks done — synthesize final answer with reviewer feedback
        logger.info("All %d subtasks completed for parent %s, synthesizing close-out",
                     len(subtask_ids), parent_id)
        results_text, critique_text = board.collect_results_with_critiques(
            parent_id, subtask_ids=subtask_ids)
        parent_desc = fresh_parent.get("description", "")

        # ── Check if file generation tasks actually produced files ──
        file_gen_warning = ""
        _file_keywords = ("文件", "文档", "file", "document",
                          "pdf", "docx", "excel", "word", "generate_doc")
        if any(kw in parent_desc.lower() for kw in _file_keywords):
            import re as _re
            _fp_re = _re.compile(
                r'/tmp/doc_\w+\.\w+|"path"\s*:\s*"([^"]+)"')
            if not _fp_re.search(results_text):
                file_gen_warning = (
                    "\n⚠️ WARNING: No file path found in subtask results. "
                    "The file may not have been generated successfully. "
                    "Do NOT tell the user the file was sent. Report the issue honestly.\n")

        # V0.02: Read IntentAnchor for closeout synthesis
        _intent_text = ""
        try:
            from core.protocols import INTENT_KEY_PREFIX
            _intent_raw = bus.get("system", f"{INTENT_KEY_PREFIX}{parent_id}")
            if _intent_raw:
                _iv = (_intent_raw.get("value", _intent_raw)
                       if isinstance(_intent_raw, dict) else _intent_raw)
                if _iv and _iv != parent_desc:
                    _intent_text = f"## Original User Intent (anchored)\n{_iv}\n\n"
        except Exception:
            pass

        close_prompt = (
            f"You are synthesizing the FINAL answer for the user.\n\n"
            f"{_intent_text}"
            f"## Original User Request\n{parent_desc}\n\n"
            f"## Subtask Results (from executor)\n{results_text}\n\n"
            f"{file_gen_warning}"
            f"## Reviewer Feedback (scores & suggestions)\n{critique_text}\n\n"
            f"## Instructions\n"
            f"1. Synthesize ALL subtask results into ONE coherent, polished response.\n"
            f"2. Consider reviewer suggestions — incorporate valid improvements.\n"
            f"3. Remove all internal task IDs, agent references, and metadata.\n"
            f"4. Your response must DIRECTLY answer the user's original question.\n"
            f"5. Respond in the user's language (default: Chinese).\n"
            f"6. If subtask results contain file paths (e.g. /tmp/doc_*.pdf), files are auto-delivered by the system — "
            f"just confirm the file was sent. If no file path or an error is reported, tell the user honestly.\n"
            f"7. **FORBIDDEN**: Do not say 'system limitation' or 'cannot send via Telegram directly'. "
            f"If the file was not generated, say 'file generation encountered an issue' and suggest retrying.\n"
            f"8. **STRICTLY FORBIDDEN**: Do not include TASK:, COMPLEXITY: lines in your reply. "
            f"These are internal directives, not user-visible content.\n"
        )
        try:
            # Build full system prompt for planner (with tools, skills, soul)
            # so the synthesizing LLM has access to exec and other tools.
            planner_def = None
            for a in config.get("agents", []):
                _cid = a.get("id", "").lower()
                _crole = (a.get("role", "") or "").lower()
                if (_cid in ("leo", "planner")
                        or "planner" in _crole
                        or "orchestrat" in _crole
                        or "brain" in _crole
                        or "decompos" in _crole):
                    planner_def = a
                    break
            planner_role = (planner_def or {}).get("role", "") or agent.cfg.role
            planner_model = (planner_def or {}).get("model", "") or agent.cfg.model

            # Inject tools section (same as BaseAgent.run does)
            tools_section = ""
            tools_schemas = None
            planner_tools_cfg = (planner_def or {}).get("tools", {})
            if planner_tools_cfg:
                try:
                    from core.tools import build_tools_prompt, build_tools_schemas
                    tools_prompt = build_tools_prompt({"tools": planner_tools_cfg})
                    if tools_prompt:
                        tools_section = f"\n\n{tools_prompt}"
                    # Build native function-calling schemas so LLM can invoke
                    # tools like send_file during closeout synthesis
                    tools_profile = planner_tools_cfg.get("profile", "minimal")
                    if tools_profile in ("coding", "full") or planner_tools_cfg.get("allow"):
                        tools_schemas = build_tools_schemas(
                            {"tools": planner_tools_cfg})
                except Exception as e:
                    logger.warning("closeout tools prompt build failed: %s", e)

            system_prompt = (
                f"You are leo.\n\n"
                f"## Role\n{planner_role}\n\n"
                f"## IMPORTANT\n"
                f"You are in Phase 2 (Closeout Synthesis). "
                f"Synthesize all subtask results into one polished answer. "
                f"File delivery is automatic ONLY when the executor's result "
                f"contains a valid file path. Check the subtask results before "
                f"claiming a file was sent. Do NOT generate TASK: lines.\n"
                f"{tools_section}\n"
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": close_prompt},
            ]
            final_answer = await agent.llm.chat(
                messages, planner_model, tools=tools_schemas)
            final_answer = _strip_think(final_answer)

            # Mini tool-loop: if planner invokes tools during closeout,
            # execute them and feed results back (max 3 rounds).
            if planner_tools_cfg:
                try:
                    from core.tools import parse_tool_calls, execute_tool_calls
                    for _round in range(3):
                        calls = parse_tool_calls(final_answer)
                        if not calls:
                            break
                        logger.info("closeout tool round %d: %s",
                                    _round + 1, [c["tool"] for c in calls])
                        tool_results = execute_tool_calls(
                            calls, {"tools": planner_tools_cfg})
                        feedback_parts = []
                        for tr in tool_results:
                            status = "✓" if tr["result"].get("ok") else "✗"
                            rj = json.dumps(tr["result"], indent=2,
                                            ensure_ascii=False, default=str)
                            feedback_parts.append(
                                f"### Tool Result: {tr['tool']} [{status}]\n"
                                f"```json\n{rj}\n```")
                        messages.append({"role": "assistant",
                                         "content": final_answer})
                        messages.append({"role": "user", "content":
                            "## Tool Execution Results\n\n"
                            + "\n\n".join(feedback_parts)
                            + "\n\nBased on the tool results above, write your "
                            "FINAL polished answer for the user in Chinese. "
                            "Do NOT invoke more tools. Just synthesize."})
                        final_answer = await agent.llm.chat(
                            messages, planner_model, tools=tools_schemas)
                        final_answer = _strip_think(final_answer)
                except Exception as tool_err:
                    logger.warning("closeout tool loop error: %s", tool_err)

            # Strip any remaining tool call blocks from the final answer
            final_answer = _strip_tool_blocks(final_answer)

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

def _build_llm_for_agent(agent_def: dict, config: dict) -> "ResilientLLM":
    """
    Build a Resilient LLM adapter for a specific agent.

    Two modes:
      1. Provider Router (cross-provider failover) — if provider_router.enabled
         Routes requests across multiple providers (MiniMax → OpenAI → Ollama)
      2. Single Provider + ResilientLLM (default) — model failover within one provider

    Per-agent llm: block overrides global llm: config.
    """
    # ── Mode 1: Provider Router (cross-provider failover) ──
    try:
        from core.provider_router import get_router
        router = get_router()
        if router and router.provider_names:
            logger.info("[orchestrator] Using provider router for agent %s",
                        agent_def.get("id", "?"))
            return router
    except ImportError:
        pass

    # ── Mode 2: Single provider + ResilientLLM (default) ──
    agent_llm    = agent_def.get("llm", {})
    global_llm   = config.get("llm", {})
    provider     = agent_llm.get("provider") or global_llm.get("provider", "flock")
    api_key_env  = agent_llm.get("api_key_env", "")
    base_url_env = agent_llm.get("base_url_env", "")

    api_key  = os.getenv(api_key_env) if api_key_env else None
    base_url = os.getenv(base_url_env) if base_url_env else None

    # Multi-key rotation support: read api_keys array from agent or global config
    api_keys_cfg = agent_llm.get("api_keys") or global_llm.get("api_keys", [])
    all_keys = []
    for kc in api_keys_cfg:
        env_name = kc.get("env", "") if isinstance(kc, dict) else str(kc)
        k = os.getenv(env_name) if env_name else None
        if k:
            all_keys.append(k)
    # If primary key exists and not already in multi-key list, prepend it
    if api_key and api_key not in all_keys:
        all_keys.insert(0, api_key)
    # If we only got keys from api_keys config but no primary, use first as primary
    if not api_key and all_keys:
        api_key = all_keys[0]

    # Build base adapter
    if provider == "flock":
        from adapters.llm.flock import FLockAdapter
        base = FLockAdapter(api_key=api_key, base_url=base_url)
    elif provider == "openai":
        from adapters.llm.openai import OpenAIAdapter
        base = OpenAIAdapter(api_key=api_key, base_url=base_url)
    elif provider == "minimax":
        from adapters.llm.minimax import MinimaxAdapter
        base = MinimaxAdapter(api_key=api_key, base_url=base_url)
    elif provider == "ollama":
        from adapters.llm.ollama import OllamaAdapter
        base = OllamaAdapter(api_key=api_key, base_url=base_url)
    else:
        # Treat unknown providers as OpenAI-compatible (anthropic, deepseek, custom, etc.)
        from adapters.llm.openai import OpenAIAdapter
        base = OpenAIAdapter(api_key=api_key, base_url=base_url)

    # Wrap with resilience layer (retry + circuit breaker + model failover)
    from adapters.llm.resilience import ResilientLLM, CredentialRotator

    fallback_models = agent_def.get("fallback_models", [])
    resilience_cfg  = config.get("resilience", {})

    # Build credential rotator if multiple keys available
    rotator = None
    if len(all_keys) > 1:
        rotator = CredentialRotator(all_keys)
        logger.info("[orchestrator] Credential rotation enabled: %d keys for %s",
                    len(all_keys), agent_def.get("id", "?"))

    return ResilientLLM(
        adapter=base,
        fallback_models=fallback_models,
        max_retries=resilience_cfg.get("max_retries", 3),
        base_delay=resilience_cfg.get("base_delay", 1.0),
        max_delay=resilience_cfg.get("max_delay", 30.0),
        jitter=resilience_cfg.get("jitter", 0.5),
        cb_threshold=resilience_cfg.get("circuit_breaker_threshold", 3),
        cb_cooldown=resilience_cfg.get("circuit_breaker_cooldown", 120.0),
        credential_rotator=rotator,
    )

def _build_memory(config: dict, agent_id: str = ""):
    """
    Build memory adapter with per-agent isolation and pluggable embeddings.
    Each agent gets its own persist directory: memory/agents/{agent_id}/chroma/
    Falls back to shared memory/chroma/ if agent_id is empty.

    Embedding provider is configured via config.memory.embedding:
        embedding:
          provider: openai | flock | local | chromadb_default
          model: text-embedding-3-small
          api_key_env: OPENAI_API_KEY
    """
    backend = config.get("memory", {}).get("backend", "chroma")
    if agent_id:
        persist_dir = os.path.join("memory", "agents", agent_id, "chroma")
    else:
        persist_dir = "memory/chroma"

    # Build embedding function from config (if configured)
    embedding_fn = None
    try:
        from adapters.memory.embedding import get_embedding_provider
        provider = get_embedding_provider(config)
        embedding_fn = provider.as_chromadb_function()
        if embedding_fn is not None:
            logger.info("[memory] Using embedding provider: %s (dim=%d)",
                        provider.name, provider.dimensions)
    except Exception as e:
        logger.debug("[memory] Embedding provider init skipped: %s", e)

    if backend == "hybrid":
        from adapters.memory.hybrid import HybridAdapter
        return HybridAdapter(persist_dir=persist_dir, embedding_fn=embedding_fn)
    elif backend == "chroma":
        from adapters.memory.chroma import ChromaAdapter
        return ChromaAdapter(persist_dir=persist_dir, embedding_fn=embedding_fn)
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

def _extract_and_store_memories(agent, task, result: str) -> None:
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

    # FTS5 incremental indexing — index task result for future search
    try:
        from core.search import QMD
        qmd = QMD()
        qmd.index(
            title=task.description[:120],
            content=result[:2000],
            collection="memory",
            agent_id=agent_id,
            source_type="episode",
        )
        qmd.close()
    except Exception:
        pass  # search index is optional enhancement

    # Refresh MEMORY.md from episodic data
    if agent.episodic:
        try:
            agent.episodic.generate_memory_md()
        except Exception:
            pass  # MEMORY.md generation is non-critical

    # Auto-update docs (error pattern detection + lesson consolidation)
    try:
        from core.doc_updater import DocUpdater
        updater = DocUpdater(agent.cfg.agent_id)
        updater.check_and_update()
    except Exception:
        pass  # doc update is non-critical

    # ── Memo Protocol auto-upload hook (V0.03) ───────────────────────────
    try:
        from adapters.memo.config import MemoConfig
        import yaml as _yaml
        _cfg_path = "config/agents.yaml"
        if os.path.exists(_cfg_path):
            with open(_cfg_path) as _f:
                _raw_cfg = _yaml.safe_load(_f) or {}
        else:
            _raw_cfg = {}
        _memo_cfg = MemoConfig.from_yaml(_raw_cfg)
        if _memo_cfg.enabled and _memo_cfg.auto_upload_enabled:
            import asyncio
            from adapters.memo.hooks import post_task_memo_hook
            _outcome = getattr(task, "outcome", None) or "success"
            _score = getattr(task, "score", None)
            asyncio.create_task(
                post_task_memo_hook(
                    agent_id=agent.cfg.agent_id,
                    task_id=task.task_id,
                    outcome=_outcome,
                    score=_score,
                    config=_memo_cfg,
                ))
    except ImportError:
        pass  # memo module not installed, silently skip
    except Exception:
        pass  # memo hook failure never affects core pipeline


# ── Orchestrator ────────────────────────────────────────────────────────────

class Orchestrator:
    """
    Reads agents.yaml, spins up one OS process per agent,
    submits the initial task, then waits for all processes to finish.
    Handles SIGTERM/SIGINT for graceful shutdown of all children.

    Agent lifecycle is delegated to an AgentRuntime backend
    (ProcessRuntime by default — zero behaviour change from pre-v0.02).
    """

    def __init__(self, config_path: str = "config/agents.yaml"):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)
        self.bus    = ContextBus()
        self.board  = TaskBoard()
        self._shutting_down = False

        # ── AgentRuntime (Phase 1) ──
        from core.runtime import create_runtime
        self.runtime = create_runtime(self.config)

        # WakeupBus: event-driven agent wakeup (zero-delay subtask dispatch)
        # Use DualWakeupBus matching the runtime mode (process vs async)
        runtime_mode = self.config.get("runtime", {}).get("mode", "process")
        if runtime_mode == "in_process":
            from core.runtime.wakeup import DualWakeupBus
            self.wakeup = DualWakeupBus(mode="async")
        else:
            from core.wakeup import WakeupBus
            self.wakeup = WakeupBus()
        for agent_def in self.config.get("agents", []):
            self.wakeup.register(agent_def["id"])

        # Ensure shared workspace directory exists
        ws_cfg = self.config.get("workspace", {})
        ws_path = ws_cfg.get("path", "workspace")
        os.makedirs(ws_path, exist_ok=True)

        # Auto-generate team skill on every launch
        try:
            from core.team_skill import generate_team_skill
            generate_team_skill(config_path=config_path)
        except Exception as e:
            logger.warning("Failed to generate team skill: %s", e)

        # Sync skill CLI binaries into exec_approvals.json
        try:
            from core.skill_deps import sync_exec_approvals
            sync_exec_approvals()
        except Exception as e:
            logger.warning("Failed to sync exec approvals: %s", e)

    # ── backward-compat: orch.procs → runtime.procs ─────────────────────

    @property
    def procs(self) -> list:
        """Backward-compat bridge: delegates to runtime.procs.

        ChannelManager and other code access ``orch.procs`` directly.
        This property transparently routes to the runtime backend.
        """
        return self.runtime.procs

    @procs.setter
    def procs(self, value: list):
        """Backward-compat setter used by ChannelManager hot-reload."""
        self.runtime.procs = value

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
        """Launch all agents via the pluggable AgentRuntime."""
        self.runtime.start_all(self.config, self.wakeup)
        logger.info("launched %d agents via %s",
                    len(self.runtime.agent_ids()),
                    type(self.runtime).__name__)

    def _wait(self):
        """Wait for all agent work to complete.

        Supports both ProcessRuntime (all agents start upfront) and
        LazyRuntime (agents start on demand).  Polls until no active
        tasks remain and no agent processes are alive.
        """
        while True:
            alive = [p for p in self.runtime.procs if p.is_alive()]
            if alive:
                # Wait for first alive process with timeout for re-check
                alive[0].join(timeout=3)
                continue

            # No alive processes — check if tasks still need work
            data = self.board._read() or {}
            if any(t.get("status") in ("pending", "claimed", "review",
                                         "critique", "blocked", "paused",
                                         "synthesizing")
                   for t in data.values()):
                time.sleep(2)  # wait for lazy monitor to start agents
                continue

            break  # All done

        logger.info("all agent processes finished")

    def shutdown(self):
        """Gracefully shut down all agents via AgentRuntime."""
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Orchestrator shutting down via %s",
                    type(self.runtime).__name__)
        self.runtime.stop_all()

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
