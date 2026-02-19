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
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

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
    cognition_file:         str  = ""     # path to cognition.md (optional)


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
        os.makedirs(MAILBOX_DIR, exist_ok=True)
        # Load cognition profile if configured
        self._load_cognition()

    def _load_cognition(self):
        """Load the agent's cognition profile from docs/{agent_id}/cognition.md."""
        paths_to_try = [
            self.cfg.cognition_file,
            os.path.join("docs", self.cfg.agent_id, "cognition.md"),
            os.path.join("docs", "shared", "cognition.md"),
        ]
        for p in paths_to_try:
            if p and os.path.exists(p):
                try:
                    with open(p) as f:
                        self._cognition = f.read().strip()
                    logger.info("[%s] loaded cognition from %s",
                                self.cfg.agent_id, p)
                    return
                except OSError:
                    continue

    async def run(self, task: "Task", bus: "ContextBus") -> str:
        """
        Execute a task with full memory pipeline:
        1. Load skill documents from disk (hot-reload)
        2. Load reference documents (per-agent + shared)
        3. Get shared context snapshot from all agents
        4. Long-term memory recall (episodic + knowledge base)
        5. Build system prompt: role + cognition + skills + docs + memory + context
        6. Call LLM adapter
        7. Store episode + publish to context bus
        """
        # 1. Skills
        skills_text = self.skill_loader.load(self.cfg.skills, self.cfg.agent_id)

        # 2. Reference documents (per-agent + shared)
        docs_text = self.skill_loader.load_docs(self.cfg.agent_id)

        # 3. Context bus snapshot
        context_snap = json.dumps(bus.snapshot(), indent=2, ensure_ascii=False)

        # 4. Long-term memory recall (NEW — activates dormant memory)
        memory_section = ""
        if self.cfg.long_term:
            memory_section = self._recall_long_term(task.description)

        # 5. System prompt with all layers
        docs_section = f"\n\n## Reference Documents\n{docs_text}" if docs_text else ""
        cognition_section = (f"\n\n## Cognitive Profile\n{self._cognition}"
                             if self._cognition else "")
        memory_block = f"\n\n{memory_section}" if memory_section else ""

        system_prompt = (
            f"You are {self.cfg.agent_id}.\n\n"
            f"## Role\n{self.cfg.role}"
            f"{cognition_section}\n\n"
            f"## Skills\n{skills_text}"
            f"{docs_section}"
            f"{memory_block}\n\n"
            f"## Shared Context\n{context_snap}\n"
        )

        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # Add short-term memory (last N turns)
        for turn in self._short_term[-(self.cfg.short_term_turns * 2):]:
            messages.append(turn)

        # Current task
        messages.append({"role": "user", "content": task.description})

        # 5a. Context compaction (if history is too long)
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
            except Exception as e:
                logger.warning("[%s] compaction failed, using full history: %s",
                               self.cfg.agent_id, e)

        # 6. Call LLM (streaming if available, with partial result updates)
        result = await self._call_llm_streaming(messages, task)

        # Update short-term memory
        self._short_term.append({"role": "user", "content": task.description})
        self._short_term.append({"role": "assistant", "content": result})
        # Trim to configured window
        max_entries = self.cfg.short_term_turns * 2
        if len(self._short_term) > max_entries:
            self._short_term = self._short_term[-max_entries:]

        # 7a. Store to long-term memory (episodic + vector)
        self._store_to_memory(task, result)

        # 7b. Publish to context bus
        bus.publish(self.cfg.agent_id, "last_result", result)

        logger.info("[%s] task completed, result length=%d",
                    self.cfg.agent_id, len(result))
        return result

    async def _call_llm_streaming(self, messages: list[dict], task: "Task") -> str:
        """
        Call LLM with streaming if available, writing partial results to task board.
        Falls back to non-streaming chat() if chat_stream() is not available.
        """
        # Try streaming first
        if hasattr(self.llm, "chat_stream"):
            try:
                from core.task_board import TaskBoard
                board = TaskBoard()
                chunks: list[str] = []
                update_interval = 0
                async for chunk in self.llm.chat_stream(messages, self.cfg.model):
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

        # Fallback: non-streaming
        return await self.llm.chat(messages, self.cfg.model)

    def _recall_long_term(self, query: str) -> str:
        """
        Recall from all long-term memory layers.
        Returns formatted text for system prompt injection.

        Progressive loading (OpenViking L0→L1→L2):
        - Episodic: recent episodes, cases, patterns
        - Knowledge base: shared notes, cross-agent insights
        - Vector: hybrid BM25+ChromaDB results
        """
        parts = []

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

        return "\n".join(parts)

    def _store_to_memory(self, task: "Task", result: str):
        """
        Store completed task to long-term memory layers.
        Non-blocking, failure-tolerant.
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
                )
                self.episodic.save_episode(episode)
                # Append to daily log
                self.episodic.append_daily_log(
                    f"**Task:** {task.description[:100]}\n"
                    f"**Result:** {result[:200]}..."
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
        Read and drain mailbox (file-locked).
        Returns list of message dicts. Clears the mailbox file after reading.
        """
        path = os.path.join(MAILBOX_DIR, f"{self.cfg.agent_id}.jsonl")
        lock = FileLock(path + ".lock")

        with lock:
            if not os.path.exists(path):
                return []

            messages = []
            try:
                with open(path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                messages.append(json.loads(line))
                            except json.JSONDecodeError:
                                logger.warning("[%s] corrupt mailbox line: %s",
                                               self.cfg.agent_id, line[:80])
            except Exception as e:
                logger.error("[%s] failed to read mailbox: %s",
                             self.cfg.agent_id, e)
                return []

            # Drain: truncate file
            with open(path, "w") as f:
                pass

        return messages

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
