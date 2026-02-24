"""
core/agent.py
BaseAgent — run, review, send_mail, read_mail
AgentConfig — dataclass matching orchestrator's cfg_dict

Memory architecture (OpenViking-inspired):
  - Short-term: volatile conversation window (_short_term list)
  - Long-term:  episodic memory (per-agent) + knowledge base (shared)
  - Recall:     progressive L0/L1/L2 loading into system prompt
"""

from __future__ import annotations
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)

def _strip_think(text: str) -> str:
    """Strip <think>...</think> blocks from LLM output.
    If stripping leaves nothing, extract the think content as the result
    (some models wrap their entire response in <think> tags)."""
    think_contents = _THINK_RE.findall(text)
    stripped = _THINK_RE.sub("", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    if stripped:
        return stripped
    # Entire output was think blocks — use the content rather than returning empty
    if think_contents:
        combined = "\n\n".join(c.strip() for c in think_contents if c.strip())
        if combined:
            logger.info("[_strip_think] entire output was <think> — recovering %d chars",
                        len(combined))
            return re.sub(r"\n{3,}", "\n\n", combined).strip()
    return stripped

if TYPE_CHECKING:
    from core.context_bus import ContextBus
    from core.task_board import Task
    from adapters.memory.episodic import EpisodicMemory
    from adapters.memory.knowledge_base import KnowledgeBase

try:
    from filelock import FileLock
except ImportError:
    import warnings
    warnings.warn(
        "filelock package not installed. Mailbox is NOT process-safe. "
        "Install with: pip install filelock",
        RuntimeWarning, stacklevel=2,
    )

    class FileLock:  # type: ignore
        def __init__(self, path):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

logger = logging.getLogger(__name__)

MAILBOX_DIR = ".mailboxes"


@dataclass
class AgentConfig:
    agent_id:         str
    role:             str
    model:            str
    skills:           list[str]       = field(default_factory=lambda: ["_base"])
    wallet_key:       str             = ""
    short_term_turns: int             = 20
    long_term:        bool            = True
    recall_top_k:     int             = 3
    autonomy_level:   int             = 1
    # Docs directory for per-agent reference documents
    docs_dir:             str             = "docs"
    # Compaction settings
    compaction_enabled:    bool = True
    max_context_tokens:    int  = 8000
    summary_target_tokens: int  = 1500
    keep_recent_turns:     int  = 4
    # Episodic + knowledge base memory settings
    episodic_recall_budget: int  = 1500   # token budget for episodic recall
    kb_recall_budget:       int  = 800    # token budget for knowledge base recall
    cognition_file:         str  = ""     # path to cognition.md (legacy, optional)
    soul_file:              str  = ""     # path to soul.md (OpenClaw pattern, optional)
    # System prompt budget (prevents exceeding model context window)
    max_system_prompt_tokens: int = 12000  # ~48K chars; 0 = no limit
    # Tool configuration (OpenClaw-inspired)
    tools_config:           dict = field(default_factory=dict)  # {profile, allow, deny}


class BaseAgent:
    """
    Single agent instance — runs inside a child process.
    Owns an LLM adapter, memory adapter, skill loader, and chain adapter.

    Memory layers:
      - _short_term:  volatile conversation window (list of dicts)
      - memory:       hybrid BM25+ChromaDB adapter (existing)
      - episodic:     per-agent episodic memory (episodes/cases/patterns)
      - kb:           shared knowledge base (atomic notes/insights)
    """

    def __init__(self, cfg: AgentConfig, llm, memory, skill_loader, chain,
                 episodic: Optional["EpisodicMemory"] = None,
                 kb: Optional["KnowledgeBase"] = None):
        self.cfg          = cfg
        self.llm          = llm
        self.memory       = memory
        self.skill_loader = skill_loader
        self.chain        = chain
        self.episodic     = episodic
        self.kb           = kb
        self._short_term: list[dict] = []  # conversation window
        self._cognition: str = ""          # cached cognition profile
        self._soul: str = ""               # cached soul.md (OpenClaw pattern)
        self._tools_md: str = ""           # cached TOOLS.md (per-agent tool spec)
        self._user_md: str = ""            # cached USER.md (user identity)
        os.makedirs(MAILBOX_DIR, exist_ok=True)
        # Session transcript persistence
        self._transcript_dir = os.path.join("memory", "transcripts")
        os.makedirs(self._transcript_dir, exist_ok=True)
        # Load soul + cognition + tools + user profiles
        self._load_soul()
        self._load_tools_md()
        self._load_user_md()
        # Restore short-term memory from disk (survives restarts)
        self._load_short_term()

    def _load_soul(self):
        """Load agent personality — prefers soul.md (OpenClaw pattern), falls back to cognition.md.

        Search order for soul.md:
          1. Explicit soul_file path from config
          2. docs/{agent_id}/soul.md
          3. docs/shared/soul.md

        Fallback to cognition.md (legacy):
          1. Explicit cognition_file path from config
          2. docs/{agent_id}/cognition.md
          3. docs/shared/cognition.md
        """
        # Try soul.md first (OpenClaw pattern)
        soul_paths = [
            self.cfg.soul_file,
            os.path.join("skills", "agents", self.cfg.agent_id, "soul.md"),
            os.path.join("docs", self.cfg.agent_id, "soul.md"),
            os.path.join("docs", "shared", "soul.md"),
        ]
        for p in soul_paths:
            if p and os.path.exists(p):
                try:
                    with open(p) as f:
                        self._soul = f.read().strip()
                    logger.info("[%s] loaded soul from %s",
                                self.cfg.agent_id, p)
                    break
                except OSError:
                    continue

        # Fallback: cognition.md (legacy)
        cognition_paths = [
            self.cfg.cognition_file,
            os.path.join("skills", "agents", self.cfg.agent_id, "cognition.md"),
            os.path.join("docs", self.cfg.agent_id, "cognition.md"),
            os.path.join("docs", "shared", "cognition.md"),
        ]
        for p in cognition_paths:
            if p and os.path.exists(p):
                try:
                    with open(p) as f:
                        self._cognition = f.read().strip()
                    logger.info("[%s] loaded cognition from %s",
                                self.cfg.agent_id, p)
                    break
                except OSError:
                    continue

    def _load_tools_md(self):
        """Load per-agent TOOLS.md — tool usage specification (OpenClaw pattern).

        Search order:
          1. skills/agents/{agent_id}/TOOLS.md
          2. docs/{agent_id}/TOOLS.md
        """
        tools_paths = [
            os.path.join("skills", "agents", self.cfg.agent_id, "TOOLS.md"),
            os.path.join("docs", self.cfg.agent_id, "TOOLS.md"),
        ]
        for p in tools_paths:
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        self._tools_md = f.read().strip()
                    logger.info("[%s] loaded TOOLS.md from %s",
                                self.cfg.agent_id, p)
                    break
                except OSError:
                    continue

    def _load_user_md(self):
        """Load shared USER.md — user identity and preferences.

        Search order:
          1. docs/shared/USER.md
          2. docs/USER.md
        """
        user_paths = [
            os.path.join("docs", "shared", "USER.md"),
            os.path.join("docs", "USER.md"),
        ]
        for p in user_paths:
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        self._user_md = f.read().strip()
                    logger.info("[%s] loaded USER.md from %s",
                                self.cfg.agent_id, p)
                    break
                except OSError:
                    continue

    # ── Short-term memory persistence ─────────────────────────────────────

    def _short_term_path(self) -> str:
        """Path to the short-term memory JSONL file for this agent."""
        return os.path.join(
            "memory", "agents", self.cfg.agent_id, "short_term.jsonl")

    def _load_short_term(self):
        """Restore short-term conversation memory from disk."""
        path = self._short_term_path()
        if not os.path.exists(path):
            return
        try:
            entries: list[dict] = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            # Keep only the most recent entries within window
            max_entries = self.cfg.short_term_turns * 2
            self._short_term = entries[-max_entries:]
            if self._short_term:
                logger.info("[%s] restored %d short-term entries from disk",
                            self.cfg.agent_id, len(self._short_term))
        except OSError as e:
            logger.warning("[%s] failed to load short-term memory: %s",
                           self.cfg.agent_id, e)

    def _save_short_term(self):
        """Persist short-term conversation memory to disk."""
        path = self._short_term_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                for entry in self._short_term:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("[%s] failed to save short-term memory: %s",
                           self.cfg.agent_id, e)

    async def run(self, task: "Task", bus: "ContextBus") -> str:
        """
        Execute a task with full memory pipeline:
        1. Load skill documents from disk (hot-reload)
        2. Load reference documents (per-agent + shared)
        3. Get shared context snapshot from all agents
        4. Long-term memory recall (episodic + knowledge base)
        5. Build system prompt: role + cognition + skills + tools + docs + memory + context
        6. Call LLM adapter
        7. Tool execution loop (parse tool calls → execute → feed back)
        8. Store episode + publish to context bus
        """
        # 1. Skills
        skills_text = self.skill_loader.load(self.cfg.skills, self.cfg.agent_id)

        # 2. Reference documents (per-agent + shared)
        docs_text = self.skill_loader.load_docs(self.cfg.agent_id)

        # 3. Context bus snapshot (layered — filtered by agent visibility)
        context_snap = json.dumps(
            bus.snapshot_for_agent(self.cfg.agent_id),
            indent=2, ensure_ascii=False)

        # 4. Long-term memory recall (NEW — activates dormant memory)
        memory_section = ""
        if self.cfg.long_term:
            memory_section = self._recall_long_term(task.description)

        # 5. System prompt with all layers
        docs_section = f"\n\n## Reference Documents\n{docs_text}" if docs_text else ""
        # Soul.md (OpenClaw pattern) takes precedence; cognition.md is fallback
        soul_section = ""
        if self._soul:
            soul_section = f"\n\n## Soul\n{self._soul}"
        elif self._cognition:
            soul_section = f"\n\n## Cognitive Profile\n{self._cognition}"
        memory_block = f"\n\n{memory_section}" if memory_section else ""

        # 5b. Tools prompt (OpenClaw-inspired tool system)
        tools_section = ""
        tools_schemas = None
        tools_cfg = self.cfg.tools_config
        if tools_cfg:
            try:
                from core.tools import build_tools_prompt, build_tools_schemas
                tools_prompt = build_tools_prompt({"tools": tools_cfg})
                if tools_prompt:
                    tools_section = f"\n\n{tools_prompt}"
                # Native function calling for executor agents (coding/full),
                # OR for planners that have explicitly allowed tools (e.g.
                # Leo with send_file — doesn't produce content but needs to
                # deliver files to users).
                tools_profile = tools_cfg.get("profile", "minimal")
                if tools_profile in ("coding", "full") or tools_cfg.get("allow"):
                    tools_schemas = build_tools_schemas({"tools": tools_cfg})
            except Exception as e:
                logger.warning("[%s] tools prompt build failed: %s",
                               self.cfg.agent_id, e)

        # 5b-2. TOOLS.md (per-agent tool specification from OpenClaw)
        tools_md_section = ""
        if self._tools_md:
            tools_md_section = f"\n\n{self._tools_md}"

        # 5b-3. USER.md (user identity and preferences)
        user_section = ""
        if self._user_md:
            user_section = f"\n\n{self._user_md}"

        # 5c. Recent task history (cross-round context)
        history_section = ""
        try:
            from core.task_history import load_recent
            history_text = load_recent(n=3)
            if history_text:
                history_section = (
                    f"\n\n## Recent Task History\n"
                    f"Below are results from previous task rounds. "
                    f"Use this context to maintain continuity.\n\n"
                    f"{history_text}"
                )
        except Exception as e:
            logger.debug("[%s] task history load skipped: %s",
                         self.cfg.agent_id, e)

        # Workspace awareness
        workspace_section = ""
        ws_path = "workspace"
        if os.path.isdir(ws_path):
            try:
                ws_files = os.listdir(ws_path)
                # Filter out hidden files like .gitkeep
                ws_files = [f for f in ws_files if not f.startswith('.')]
                ws_files = ws_files[:20]  # limit to 20 files
                if ws_files:
                    workspace_section = (
                        f"\n\n## Shared Workspace (workspace/)\n"
                        f"Files: {', '.join(ws_files)}\n"
                        f"Use read_file/write_file with workspace/ prefix "
                        f"to collaborate with other agents.\n"
                    )
            except Exception as e:
                logger.debug("Workspace listing failed: %s", e)

        system_prompt = self._budget_system_prompt(
            role_section=self.cfg.role,
            soul_section=soul_section,
            tools_md_section=tools_md_section,
            user_section=user_section,
            skills_text=skills_text,
            tools_section=tools_section,
            docs_section=docs_section,
            memory_block=memory_block,
            history_section=history_section,
            workspace_section=workspace_section,
            context_snap=context_snap,
        )

        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # Add short-term memory (last N turns)
        for turn in self._short_term[-(self.cfg.short_term_turns * 2):]:
            messages.append(turn)

        # Current task
        messages.append({"role": "user", "content": task.description})

        # 5c. Context compaction (if history is too long)
        if self.cfg.compaction_enabled:
            try:
                from core.compaction import compact_history, needs_compaction
                if needs_compaction(messages, self.cfg.max_context_tokens):
                    messages = await compact_history(
                        messages, self.llm, self.cfg.model,
                        max_context_tokens=self.cfg.max_context_tokens,
                        summary_target_tokens=self.cfg.summary_target_tokens,
                        keep_recent_turns=self.cfg.keep_recent_turns,
                    )
                    logger.info("[%s] context compacted to %d messages",
                                self.cfg.agent_id, len(messages))
                    self.log_transcript("context_compacted", task.task_id,
                                        f"Compacted to {len(messages)} messages")
            except Exception as e:
                logger.warning("[%s] compaction failed, using full history: %s",
                               self.cfg.agent_id, e)

        # 6. Call LLM (streaming if available, with partial result updates)
        result = await self._call_llm_streaming(messages, task,
                                                 tools_schemas=tools_schemas)

        # 6b. Strip <think>...</think> blocks from model output
        result = _strip_think(result)

        # 7. Tool execution loop — parse tool calls, execute, feed back results
        if tools_cfg:
            result = await self._tool_loop(messages, task, result,
                                           tools_schemas=tools_schemas)

        # Update short-term memory
        self._short_term.append({"role": "user", "content": task.description})
        self._short_term.append({"role": "assistant", "content": result})
        # Trim to configured window
        max_entries = self.cfg.short_term_turns * 2
        if len(self._short_term) > max_entries:
            self._short_term = self._short_term[-max_entries:]
        # Persist to disk (survives process restarts)
        self._save_short_term()

        # 8a. Store to long-term memory (episodic + vector)
        self._store_to_memory(task, result)

        # 8b. Publish to context bus (short-term layer — 1 day TTL)
        from core.context_bus import LAYER_SHORT
        bus.publish(self.cfg.agent_id, "last_result", result,
                    layer=LAYER_SHORT)

        logger.info("[%s] task completed, result length=%d",
                    self.cfg.agent_id, len(result))
        return result

    async def run_with_prompt(self, prompt: str, bus: "ContextBus") -> str:
        """Ad-hoc LLM call — lighter than run(), no task lifecycle/memory store.

        Used for targeted revisions and synthesis tasks where the full
        run() pipeline (skills, memory recall, context bus) is overkill.
        Includes tools section so agent can invoke exec, etc.
        """
        system_prompt = f"You are {self.cfg.agent_id}.\n\n## Role\n{self.cfg.role}\n"
        if self._soul:
            system_prompt += f"\n## Soul\n{self._soul}\n"
        elif self._cognition:
            system_prompt += f"\n## Cognitive Profile\n{self._cognition}\n"

        # Inject tools section so agent has tool access
        tools_cfg = self.cfg.tools_config
        tools_schemas = None
        if tools_cfg:
            try:
                from core.tools import build_tools_prompt, build_tools_schemas
                tools_prompt = build_tools_prompt({"tools": tools_cfg})
                if tools_prompt:
                    system_prompt += f"\n{tools_prompt}\n"
                tools_schemas = build_tools_schemas({"tools": tools_cfg})
            except Exception as e:
                logger.error("[%s] Failed to load tools: %s",
                             self.cfg.agent_id, e)

        llm_kwargs: dict = {}
        if tools_schemas:
            llm_kwargs["tools"] = tools_schemas

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        result = await self.llm.chat(messages, self.cfg.model, **llm_kwargs)

        # Mini tool loop (max 3 rounds)
        if tools_cfg:
            try:
                from core.tools import parse_tool_calls, execute_tool_calls
                for _ in range(3):
                    calls = parse_tool_calls(result)
                    if not calls:
                        break
                    tool_results = execute_tool_calls(calls, {"tools": tools_cfg})
                    feedback = []
                    for tr in tool_results:
                        status = "✓" if tr["result"].get("ok") else "✗"
                        rj = json.dumps(tr["result"], indent=2,
                                        ensure_ascii=False, default=str)
                        feedback.append(
                            f"### Tool Result: {tr['tool']} [{status}]\n"
                            f"```json\n{rj}\n```")
                    messages.append({"role": "assistant", "content": result})
                    messages.append({"role": "user", "content":
                        "## Tool Execution Results\n\n"
                        + "\n\n".join(feedback)
                        + "\n\nContinue with your task."})
                    result = await self.llm.chat(messages, self.cfg.model,
                                                 **llm_kwargs)
            except Exception as e:
                logger.error("[%s] Tool execution loop failed: %s",
                             self.cfg.agent_id, e)

        return result

    async def _call_llm_streaming(self, messages: list[dict], task: "Task",
                                    tools_schemas: list[dict] | None = None) -> str:
        """
        Call LLM with streaming if available, writing partial results to task board.
        Falls back to non-streaming chat() if chat_stream() is not available.

        When tools_schemas is provided, passes them to the adapter for
        native function calling (MiniMax, OpenAI, etc.).
        """
        llm_kwargs: dict = {}
        if tools_schemas:
            llm_kwargs["tools"] = tools_schemas

        # Try streaming first
        if hasattr(self.llm, "chat_stream"):
            try:
                from core.task_board import TaskBoard
                board = TaskBoard()
                chunks: list[str] = []
                update_interval = 0
                async for chunk in self.llm.chat_stream(
                    messages, self.cfg.model, **llm_kwargs
                ):
                    chunks.append(chunk)
                    update_interval += 1
                    # Write partial result every 5 chunks to avoid excessive I/O
                    if update_interval >= 5:
                        board.update_partial(task.task_id, "".join(chunks))
                        update_interval = 0
                result = "".join(chunks)
                # Final partial update (will be cleared when task completes)
                if chunks:
                    board.update_partial(task.task_id, result)
                return result
            except Exception as e:
                logger.warning("[%s] streaming failed, falling back to blocking: %s",
                               self.cfg.agent_id, e)
                # Reset circuit breakers so blocking fallback has a fair chance
                if hasattr(self.llm, '_circuits'):
                    for circuit in self.llm._circuits.values():
                        circuit.failures = 0
                        circuit.is_open = False

        # Fallback: non-streaming
        return await self.llm.chat(messages, self.cfg.model, **llm_kwargs)

    async def _tool_loop(self, messages: list[dict], task: "Task",
                         initial_result: str,
                         max_rounds: int = 5,
                         tools_schemas: list[dict] | None = None) -> str:
        """
        Tool execution loop — parse tool calls from LLM output, execute them,
        feed results back to the LLM for a follow-up response.

        Stops when:
        - No tool calls found in response (agent is done)
        - Max rounds reached (prevent infinite loops)
        """
        try:
            from core.tools import parse_tool_calls, execute_tool_calls
        except ImportError:
            return initial_result

        result = initial_result
        tools_agent_cfg = {"tools": self.cfg.tools_config}

        for round_num in range(max_rounds):
            # Check for task cancellation before each round
            try:
                from core.task_board import TaskBoard
                if TaskBoard().is_cancelled(task.task_id):
                    logger.info("[%s] task %s cancelled, aborting tool loop",
                                self.cfg.agent_id, task.task_id[:8])
                    return result + "\n\n[Task cancelled by user]"
            except Exception:
                pass

            # Parse tool invocations from agent output
            calls = parse_tool_calls(result)
            if not calls:
                break  # No tool calls — agent is done

            logger.info("[%s] tool round %d: %d call(s) — %s",
                        self.cfg.agent_id, round_num + 1, len(calls),
                        [c["tool"] for c in calls])

            # Execute all tool calls
            tool_results = execute_tool_calls(calls, tools_agent_cfg)

            # Build tool results message
            results_text = []
            for tr in tool_results:
                tool_name = tr["tool"]
                tool_result = tr["result"]
                status = "✓" if tool_result.get("ok") else "✗"
                result_json = json.dumps(tool_result, indent=2,
                                         ensure_ascii=False, default=str)
                results_text.append(
                    f"### Tool Result: {tool_name} [{status}]\n"
                    f"```json\n{result_json}\n```"
                )

            tool_feedback = (
                "## Tool Execution Results\n\n"
                + "\n\n".join(results_text)
                + "\n\nContinue with your task using the tool results above. "
                "If you need more tools, invoke them. "
                "Otherwise, provide your final answer."
            )

            # Append assistant response + tool feedback to messages
            messages.append({"role": "assistant", "content": result})
            messages.append({"role": "user", "content": tool_feedback})

            # Call LLM again with tool results
            result = await self._call_llm_streaming(messages, task,
                                                     tools_schemas=tools_schemas)
            result = _strip_think(result)

        return result

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token count — ~4 chars per token for English/mixed-lang text."""
        return len(text) // 4 if text else 0

    def _budget_system_prompt(
        self,
        *,
        role_section: str,
        soul_section: str,
        tools_md_section: str,
        user_section: str,
        skills_text: str,
        tools_section: str,
        docs_section: str,
        memory_block: str,
        history_section: str,
        workspace_section: str,
        context_snap: str,
    ) -> str:
        """Assemble system prompt and trim to fit within token budget.

        Priority (highest → lowest):
          P0: role identity + soul (never trimmed)
          P1: tools_md + tools_section + user_section (rarely trimmed)
          P2: skills_text (trimmed first — can be very large)
          P3: docs_section, memory_block, history_section
          P4: workspace_section, context_snap (trimmed last)

        If max_system_prompt_tokens <= 0, no budget is applied.
        """
        budget = self.cfg.max_system_prompt_tokens
        if budget <= 0:
            # No budget — assemble as-is
            return (
                f"You are {self.cfg.agent_id}.\n\n"
                f"## Role\n{role_section}"
                f"{soul_section}"
                f"{tools_md_section}"
                f"{user_section}\n\n"
                f"## Skills\n{skills_text}"
                f"{tools_section}"
                f"{docs_section}"
                f"{memory_block}"
                f"{history_section}"
                f"{workspace_section}\n\n"
                f"## Shared Context\n{context_snap}\n"
            )

        # ── Build in priority order, track running total ──
        # P0 — identity (never trimmed)
        header = f"You are {self.cfg.agent_id}.\n\n## Role\n{role_section}{soul_section}"
        used = self._estimate_tokens(header)

        # P1 — tools + user
        p1_parts = [tools_md_section, user_section, tools_section]
        for part in p1_parts:
            used += self._estimate_tokens(part)

        # Remaining budget for P2–P4
        remaining = max(budget - used, 200)  # always keep at least 200 tokens

        # P2 — skills (biggest contributor, trim if needed)
        skills_tokens = self._estimate_tokens(skills_text)
        if skills_tokens > remaining * 0.6:
            # Trim skills to 60% of remaining budget
            max_chars = int(remaining * 0.6 * 4)
            if len(skills_text) > max_chars:
                skills_text = skills_text[:max_chars] + "\n\n[... skills truncated for context budget ...]\n"
                logger.warning(
                    "[%s] system prompt budget: skills trimmed from %d to %d tokens",
                    self.cfg.agent_id, skills_tokens, max_chars // 4)

        # Recalculate remaining after skills
        used += self._estimate_tokens(skills_text)
        remaining = max(budget - used, 100)

        # P3 — docs, memory, history (trim proportionally if over budget)
        p3_sections = [
            ("docs", docs_section),
            ("memory", memory_block),
            ("history", history_section),
        ]
        p3_total = sum(self._estimate_tokens(s) for _, s in p3_sections)
        if p3_total > remaining * 0.8:
            # Trim each proportionally to fit 80% of remaining
            max_p3_chars = int(remaining * 0.8 * 4)
            trimmed_p3 = []
            for label, section in p3_sections:
                if not section:
                    trimmed_p3.append(("", label))
                    continue
                share = max(len(section) * max_p3_chars // max(p3_total * 4, 1), 100)
                if len(section) > share:
                    section = section[:share] + f"\n[... {label} truncated ...]\n"
                    logger.warning(
                        "[%s] system prompt budget: %s trimmed to %d chars",
                        self.cfg.agent_id, label, share)
                trimmed_p3.append((section, label))
            docs_section = trimmed_p3[0][0]
            memory_block = trimmed_p3[1][0]
            history_section = trimmed_p3[2][0]

        # P4 — workspace, context (lowest priority, hard cap)
        used += sum(self._estimate_tokens(s) for s in [docs_section, memory_block, history_section])
        remaining = max(budget - used, 50)
        p4_budget_chars = remaining * 4

        if len(workspace_section) + len(context_snap) > p4_budget_chars:
            # Trim context first (it's the least critical)
            ctx_limit = max(p4_budget_chars - len(workspace_section), 200)
            if len(context_snap) > ctx_limit:
                context_snap = context_snap[:ctx_limit] + "\n[... context truncated ...]\n"
                logger.warning("[%s] system prompt budget: context_snap trimmed to %d chars",
                               self.cfg.agent_id, ctx_limit)
            if len(workspace_section) > p4_budget_chars // 2:
                workspace_section = workspace_section[:p4_budget_chars // 2] + "\n[... truncated ...]\n"

        prompt = (
            f"{header}"
            f"{tools_md_section}"
            f"{user_section}\n\n"
            f"## Skills\n{skills_text}"
            f"{tools_section}"
            f"{docs_section}"
            f"{memory_block}"
            f"{history_section}"
            f"{workspace_section}\n\n"
            f"## Shared Context\n{context_snap}\n"
        )

        final_tokens = self._estimate_tokens(prompt)
        if final_tokens > budget:
            logger.warning(
                "[%s] system prompt (%d est. tokens) still exceeds budget (%d) after trimming",
                self.cfg.agent_id, final_tokens, budget)

        return prompt

    def _recall_long_term(self, query: str) -> str:
        """Recall from all long-term memory layers for system prompt injection.

        Assembles contextual memory from five independent sources, each
        failure-tolerant (a failing source is skipped, never fatal).
        Results are concatenated and injected into the system prompt
        before the LLM call in ``BaseAgent.run()``.

        **Source priority (highest → lowest):**

        1. **Hot Memory (MEMORY.md)** — hand-curated P0/P1/P2
           crystallised knowledge per agent.  Loaded from
           ``memory/agents/{agent_id}/MEMORY.md``, truncated to 1500
           chars.  Always included when the file exists.
        2. **Episodic Memory** — recent task episodes, failure cases,
           and behavioural patterns from ``EpisodicMemory.recall()``.
           Budget-controlled via ``cfg.episodic_recall_budget`` tokens.
        3. **Knowledge Base** — shared cross-agent notes and insights
           from ``KnowledgeBase.recall()``.  Budget-controlled via
           ``cfg.kb_recall_budget`` tokens.
        4. **Vector / BM25 Hybrid** — semantic search via ChromaDB +
           BM25 reranking from ``HybridMemory.query()``.  Returns
           ``cfg.recall_top_k`` results (default 5), each truncated
           to 300 chars.
        5. **FTS5 Search (QMD)** — full-text SQLite search as an
           optional augmentation.  Returns up to 3 results with
           title + 200-char snippets.

        The total recall output is bounded by ``_budget_system_prompt()``
        which trims low-priority sections when the system prompt
        approaches the configured token budget.

        Args:
            query: The user's task description or message, used as the
                   search query across all memory layers.

        Returns:
            Concatenated markdown sections (``## Persistent Memory``,
            ``## Vector Memory Recall``, etc.) ready for system prompt
            injection.  Empty string if all sources return nothing.
        """
        parts = []

        # Hot Memory: MEMORY.md (P0/P1/P2 crystallized knowledge)
        memory_md_path = os.path.join(
            "memory", "agents", self.cfg.agent_id, "MEMORY.md")
        if os.path.exists(memory_md_path):
            try:
                with open(memory_md_path) as f:
                    md_content = f.read().strip()[:1500]
                if md_content:
                    parts.append(f"## Persistent Memory\n{md_content}")
            except OSError:
                pass

        # Episodic memory recall (per-agent)
        if self.episodic:
            try:
                ep_recall = self.episodic.recall(
                    query,
                    token_budget=self.cfg.episodic_recall_budget,
                )
                if ep_recall:
                    parts.append(ep_recall)
            except Exception as e:
                logger.debug("[%s] episodic recall failed: %s",
                             self.cfg.agent_id, e)

        # Error pattern recall: inject failure history for similar tasks
        if self.episodic:
            try:
                keywords = [w for w in query.split()[:10] if len(w) > 2]
                error_episodes = self.episodic.query_error_patterns(
                    keywords=keywords, limit=3)
                if error_episodes:
                    err_lines = ["## Past Failures (similar tasks)"]
                    for ep in error_episodes:
                        err_type = ep.get("error_type", "unknown")
                        err_lines.append(
                            f"- **{ep.get('title', '?')}** [{ep.get('outcome','?')}]"
                            f" error_type={err_type}\n"
                            f"  Preview: {(ep.get('result_preview', '') or '')[:200]}")
                    parts.append("\n".join(err_lines))
            except Exception as e:
                logger.debug("[%s] error pattern recall failed: %s",
                             self.cfg.agent_id, e)

        # Knowledge base recall (shared)
        if self.kb:
            try:
                kb_recall = self.kb.recall(
                    query, self.cfg.agent_id,
                    token_budget=self.cfg.kb_recall_budget,
                )
                if kb_recall:
                    parts.append(kb_recall)
            except Exception as e:
                logger.debug("[%s] KB recall failed: %s",
                             self.cfg.agent_id, e)

        # Vector/BM25 recall (existing hybrid memory)
        if self.memory and self.cfg.recall_top_k > 0:
            try:
                collection = f"agent_{self.cfg.agent_id}"
                result = self.memory.query(
                    collection, query,
                    n_results=self.cfg.recall_top_k,
                )
                docs = result.get("documents", [[]])[0]
                if docs:
                    section = "## Vector Memory Recall\n"
                    for doc in docs:
                        if doc:
                            section += f"- {doc[:300]}\n"
                    parts.append(section)
            except Exception as e:
                logger.debug("[%s] vector recall failed: %s",
                             self.cfg.agent_id, e)

        # FTS5 search augmentation (QMD engine)
        try:
            from core.search import QMD
            qmd = QMD()
            fts_results = qmd.search(query, collection="memory", limit=3)
            qmd.close()
            if fts_results:
                fts_section = "## FTS5 Search Results\n"
                for r in fts_results:
                    title = r.get("title", "")
                    snippet = r.get("snippet", "")[:200]
                    fts_section += f"- {title}: {snippet}\n"
                parts.append(fts_section)
        except Exception:
            pass  # FTS5 is optional enhancement

        return "\n".join(parts)

    def _store_to_memory(self, task: "Task", result: str,
                         outcome: str = "success",
                         error_type: str | None = None):
        """
        Store completed task to long-term memory layers.
        Non-blocking, failure-tolerant.

        Args:
            outcome: "success", "failure", or "partial"
            error_type: Error category for pattern learning
        """
        # Store to episodic memory
        if self.episodic:
            try:
                from adapters.memory.episodic import make_episode
                episode = make_episode(
                    agent_id=self.cfg.agent_id,
                    task_id=task.task_id,
                    task_description=task.description,
                    result=result,
                    outcome=outcome,
                    error_type=error_type,
                    model=getattr(self.cfg, "model", None),
                )
                self.episodic.save_episode(episode)
                # Append to daily log
                status_icon = "✓" if outcome == "success" else "✗"
                self.episodic.append_daily_log(
                    f"{status_icon} **Task:** {task.description[:100]}\n"
                    f"**Outcome:** {outcome}"
                    + (f" (error: {error_type})" if error_type else "") +
                    f"\n**Result:** {result[:200]}..."
                )
            except Exception as e:
                logger.debug("[%s] episodic store failed: %s",
                             self.cfg.agent_id, e)

        # Store to vector memory (existing hybrid)
        if self.memory:
            try:
                collection = f"agent_{self.cfg.agent_id}"
                self.memory.add(
                    collection,
                    f"Task: {task.description}\nResult: {result[:1000]}",
                    {"task_id": task.task_id, "agent_id": self.cfg.agent_id,
                     "ts": time.time(), "id": task.task_id},
                )
            except Exception as e:
                logger.debug("[%s] vector store failed: %s",
                             self.cfg.agent_id, e)

    # ── Mailbox ───────────────────────────────────────────────────────────────

    def read_mail(self) -> list[dict]:
        """
        Read and drain mailbox using move-then-delete for crash safety.

        Instead of read-then-truncate (which loses messages on crash),
        we: rename → parse → delete.  If the process crashes after rename
        but before delete, the .processing file survives and is recovered
        on the next call.
        """
        path = os.path.join(MAILBOX_DIR, f"{self.cfg.agent_id}.jsonl")
        processing_path = path + ".processing"
        lock = FileLock(path + ".lock")

        messages: list[dict] = []

        with lock:
            # ── Phase 1: recover any previously interrupted read ──
            if os.path.exists(processing_path):
                logger.warning("[%s] recovering unprocessed mailbox from previous crash",
                               self.cfg.agent_id)
                messages.extend(self._parse_mailbox_file(processing_path))
                try:
                    os.remove(processing_path)
                except OSError:
                    pass

            # ── Phase 2: atomically move current mailbox to .processing ──
            if os.path.exists(path):
                try:
                    os.rename(path, processing_path)
                except OSError as e:
                    logger.error("[%s] failed to rename mailbox for safe read: %s",
                                 self.cfg.agent_id, e)
                    # Fallback: read in-place (old behaviour)
                    messages.extend(self._parse_mailbox_file(path))
                    with open(path, "w") as f:
                        pass
                    return messages

                messages.extend(self._parse_mailbox_file(processing_path))

                # ── Phase 3: delete .processing (all messages now in memory) ──
                try:
                    os.remove(processing_path)
                except OSError:
                    pass

        return messages

    def _parse_mailbox_file(self, filepath: str) -> list[dict]:
        """Parse a JSONL mailbox file, skipping corrupt lines."""
        results: list[dict] = []
        try:
            with open(filepath, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            results.append(json.loads(line))
                        except json.JSONDecodeError:
                            logger.warning("[%s] corrupt mailbox line: %s",
                                           self.cfg.agent_id, line[:80])
        except Exception as e:
            logger.error("[%s] failed to read mailbox file %s: %s",
                         self.cfg.agent_id, filepath, e)
        return results

    def send_mail(self, to_agent_id: str, content: str,
                  msg_type: str = "message"):
        """
        Send a message to another agent's mailbox (file-locked append).
        """
        path = os.path.join(MAILBOX_DIR, f"{to_agent_id}.jsonl")
        lock = FileLock(path + ".lock")
        msg = json.dumps({
            "from":    self.cfg.agent_id,
            "type":    msg_type,
            "content": content,
            "ts":      time.time(),
        }, ensure_ascii=False)

        with lock:
            os.makedirs(MAILBOX_DIR, exist_ok=True)
            with open(path, "a") as f:
                f.write(msg + "\n")

        logger.debug("[%s] sent %s mail to %s",
                     self.cfg.agent_id, msg_type, to_agent_id)

    # ── Session Transcript ─────────────────────────────────────────────────

    def log_transcript(self, event: str, task_id: str = "",
                       content: str = "", metadata: dict | None = None):
        """Append a structured event to the session transcript.

        Records the full chain: user → planner → executor → reviewer.
        Stored as JSONL for easy parsing and dashboard display.

        Args:
            event: Event type ("task_received", "task_claimed", "task_completed",
                   "mail_sent", "mail_received", "critique", "closeout")
            task_id: Related task ID
            content: Event content (truncated for space)
            metadata: Optional additional data (provenance, scores, etc.)
        """
        entry = {
            "agent_id": self.cfg.agent_id,
            "event": event,
            "task_id": task_id,
            "content": content[:500] if content else "",
            "ts": time.time(),
        }
        if metadata:
            entry["metadata"] = metadata
        try:
            path = os.path.join(self._transcript_dir, "session_transcript.jsonl")
            with open(path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def read_transcript(limit: int = 50) -> list[dict]:
        """Read the most recent transcript entries."""
        path = os.path.join("memory", "transcripts", "session_transcript.jsonl")
        if not os.path.exists(path):
            return []
        entries: list[dict] = []
        try:
            with open(path) as f:
                lines = f.readlines()
            for line in lines[-limit:]:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
        return entries
