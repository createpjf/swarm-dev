# Cleo — Product Narrative V0.01

## 1. Executive Summary

Cleo is a self-evolving multi-agent AI system designed for personal productivity. It orchestrates three specialized agents — Leo (planner), Jerry (executor), and Alic (reviewer) — to decompose, execute, and quality-check tasks autonomously. Unlike single-agent assistants, Cleo maintains persistent memory across sessions, learns from every interaction through episodic recall and a shared knowledge base, and evolves its own capabilities through reputation-driven prompt upgrades, model swaps, and role restructuring.

---

## 2. Vision

**A personal AI team that thinks, acts, and improves — without being told how.**

Most AI assistants are stateless single-shot responders. They forget previous conversations, cannot coordinate multi-step workflows, and never improve from experience. Cleo challenges this paradigm by implementing a multi-agent architecture where:

- **Planning and execution are separate concerns.** Leo decomposes complex requests into atomic subtasks. Jerry executes them with real tools. This separation prevents the common failure mode where a single model tries to plan and act simultaneously.
- **Quality is built into the pipeline.** Alic reviews every non-trivial output, scoring it on accuracy, completeness, and technical quality. The planner incorporates this feedback into its final synthesis.
- **Memory persists and compounds.** Episodic memory (per-agent) and a shared knowledge base (Zettelkasten-style atomic notes) mean Cleo remembers what worked, what failed, and why — across sessions, across days, across tasks.
- **Self-evolution is automatic.** A 5-dimensional reputation system tracks each agent's performance. When patterns emerge (repeated failures, declining quality), the evolution engine triggers prompt upgrades, model swaps, or even role restructuring — no human intervention required.

---

## 3. Problem Statement

### 3.1 The Single-Agent Ceiling

Current AI assistants hit a hard ceiling when tasks require multi-step reasoning with real-world tool use:

| Problem | Impact |
|---------|--------|
| **No separation of concerns** | The same model plans, executes, and evaluates — leading to shallow plans and unreviewed outputs |
| **Stateless interactions** | Every session starts from zero. Context from yesterday's conversation is lost |
| **No quality feedback loop** | Users must manually verify outputs. There is no automated review or scoring |
| **No self-improvement** | The system never learns from its mistakes. The same errors recur indefinitely |
| **Limited tool coordination** | Single models struggle to chain multiple tools (web search → code → file write → deploy) reliably |

### 3.2 The Memory Gap

Even AI systems that persist conversation history suffer from:

- **Flat recall**: All memories are treated equally. There is no distinction between tactical knowledge ("this API endpoint requires auth") and strategic patterns ("the user prefers TypeScript over JavaScript").
- **No cross-session learning**: What was learned on Monday is unavailable on Friday.
- **No knowledge synthesis**: Raw conversation logs are not the same as distilled insights.

---

## 4. Solution: The Cleo Architecture

Cleo solves these problems through four architectural pillars:

### 4.1 Multi-Agent Orchestration

Three agents collaborate through a file-backed TaskBoard:

| Agent | Role | Responsibility |
|-------|------|---------------|
| **Leo** | Planner / Brain | Receives user requests, decomposes into subtasks, delegates to Jerry, synthesizes final answers from all results + Alic's feedback |
| **Jerry** | Executor | Carries out atomic subtasks — writes code, runs commands, searches the web, generates files. Returns raw results |
| **Alic** | Reviewer / Advisor | Scores subtask outputs on 5 dimensions (Accuracy 30%, Calibration 20%, Completeness 20%, Technical Quality 20%, Resource Usage 10%). Never blocks tasks — serves as an advisor |

The key insight: Leo never executes. Jerry never plans. Alic never blocks. This separation creates a natural quality pipeline.

### 4.2 Persistent Multi-Layer Memory

```
┌─────────────────────────────────────────┐
│  L0: Episodic Memory (per-agent)        │
│  - Task episodes (success/failure)      │
│  - Solution cases (problem → solution)  │
│  - Behavioral patterns                  │
├─────────────────────────────────────────┤
│  L1: Knowledge Base (shared)            │
│  - Zettelkasten atomic notes            │
│  - Cross-agent insights                 │
│  - Distilled learnings                  │
├─────────────────────────────────────────┤
│  L2: Hybrid Search Index                │
│  - BM25 full-text (SQLite FTS5)         │
│  - ChromaDB vector embeddings           │
│  - Reciprocal Rank Fusion (RRF)         │
└─────────────────────────────────────────┘
```

Memory is not just stored — it is progressively loaded. L0 (100 tokens) provides quick context. L1 (500 tokens) adds relevant cases. L2 (full budget) brings in detailed episodes. This prevents context window overflow while ensuring the most relevant memories are always available.

### 4.3 Self-Evolution Engine

```
Performance Data → Reputation Scorer → Evolution Engine
                                           │
                   ┌───────────────────────┼───────────────────────┐
                   ▼                       ▼                       ▼
              Path A                   Path B                  Path C
         Prompt Upgrade           Model Swap              Role Restructure
      (auto-applied)        (requires confirm)        (team vote: 60%)
```

The reputation system tracks 5 dimensions with Exponential Moving Average (EMA):

1. **Task Completion** (25%) — Did the agent finish the task?
2. **Output Quality** (30%) — How good was the result? (from Alic's scores)
3. **Improvement Rate** (25%) — Is quality trending up or down?
4. **Consistency** (10%) — How stable is performance across tasks?
5. **Review Accuracy** (10%) — For Alic: do review scores correlate with outcomes?

Anti-gaming mechanisms prevent score inflation: mutual inflation detection, consensus deviation analysis, and extreme bias penalties.

### 4.4 Comprehensive Tool System

36+ tools across 9 categories give agents real-world capabilities:

| Category | Tools | Purpose |
|----------|-------|---------|
| **Web** | `web_search`, `web_fetch` | Search (Brave, Perplexity, Kimi) and fetch web content |
| **Filesystem** | `read_file`, `write_file`, `edit_file`, `list_dir` | Read, write, and navigate files |
| **Memory** | `memory_search`, `memory_save`, `kb_search`, `kb_write` | Query and persist long-term knowledge |
| **Task** | `task_create`, `task_status`, `spawn_subagent` | Create subtasks, spawn child agents |
| **Automation** | `exec`, `cron`, `process` | Run commands, schedule jobs, manage processes |
| **Skill** | `check_skill_deps`, `install_skill_cli`, `search_skills` | Manage extensible skill modules |
| **Browser** | `browser_navigate`, `browser_click`, `browser_fill`, etc. | Full browser automation |
| **Media** | `screenshot`, `notify`, `analyze_image` | Screen capture, notifications, image analysis |
| **Messaging** | `send_mail`, `send_file`, `message` | Inter-agent and user communication |

---

## 5. Key Value Propositions

### 5.1 For Power Users

- **"Set it and forget it" task execution.** Describe what you need. Leo breaks it down. Jerry builds it. Alic checks it. You get the polished result.
- **Cross-session continuity.** Ask Cleo to continue yesterday's research. It remembers the context, the findings, and the open questions.
- **Multi-channel access.** Interact via Telegram, Discord, Slack, or Feishu — with native file delivery (photos, documents, voice messages).

### 5.2 For Developers

- **36+ tools out of the box.** Web search, code execution, file system access, browser automation, and more.
- **Extensible skill system.** Add custom capabilities as markdown files with YAML frontmatter. Hot-reload without restart.
- **Subagent spawning.** Dynamically create child agents for parallel workstreams (max depth: 3, max children: 5).
- **Cron scheduling.** Automate recurring tasks with once/interval/cron expressions and watchdog timeouts.

### 5.3 For Teams (Future)

- **Shared knowledge base.** Zettelkasten-style atomic notes are accessible to all agents.
- **Reputation transparency.** Every agent's performance is tracked and visible.
- **Evolution audit trail.** All prompt upgrades, model swaps, and role changes are logged.

---

## 6. Target Users & Personas

### Persona 1: The Technical Power User

- **Profile**: Software developer or technical lead who uses AI daily
- **Pain Point**: Existing assistants forget context, cannot chain tools, require constant hand-holding
- **Cleo Value**: Multi-step task automation with persistent memory and quality review
- **Key Features**: Code execution, filesystem tools, browser automation, subagent spawning

### Persona 2: The Knowledge Worker

- **Profile**: Researcher, analyst, or content creator who processes large amounts of information
- **Pain Point**: Information scattered across tools; no single system that remembers, searches, and synthesizes
- **Cleo Value**: Hybrid memory search (BM25 + vector), episodic recall, knowledge base accumulation
- **Key Features**: Web search, memory tools, file generation, cross-session continuity

### Persona 3: The Automation Enthusiast

- **Profile**: Technical user who wants to automate recurring workflows
- **Pain Point**: Building automation requires glue code, monitoring, and error handling
- **Cleo Value**: Cron scheduling, command execution, process management — with built-in error recovery
- **Key Features**: Cron jobs, exec tool, watchdog timeouts, concurrency control

---

## 7. User Journey

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   User sends │    │  Leo plans   │    │ Jerry builds │    │ Alic reviews │
│   message    │───▶│  & decomposes│───▶│ & executes   │───▶│ & scores     │
│  (Telegram)  │    │  subtasks    │    │  each one    │    │  each output │
└──────────────┘    └──────────────┘    └──────────────┘    └──────┬───────┘
                                                                   │
┌──────────────┐    ┌──────────────┐                               │
│ User receives│    │ Leo synthe-  │◀──────────────────────────────┘
│ final answer │◀───│ sizes final  │
│  + files     │    │ answer       │
└──────────────┘    └──────────────┘
```

**Step-by-step:**

1. **User sends a message** via Telegram (or Discord/Slack/Feishu)
2. **ChannelManager** creates a task on the TaskBoard with `required_role=planner`
3. **Leo** claims the task, decomposes it into subtasks with `TASK:` lines
4. **Jerry** claims each subtask, executes with real tools, returns raw results
5. **Alic** receives critique requests, scores each output (1-10)
6. **Leo** performs closeout synthesis — combines all results + Alic's feedback into one polished answer
7. **ChannelManager** delivers the final response (with any generated files) back to the user
8. **Memory system** stores the episode, extracts patterns, and updates the knowledge base

---

## 8. Competitive Positioning

| Dimension | Single-Agent Assistants (ChatGPT, Claude) | AutoGPT-style Systems | **Cleo** |
|-----------|------------------------------------------|----------------------|----------|
| **Architecture** | Single model, single turn | Single agent loop | Multi-agent with planner/executor/reviewer |
| **Memory** | Session-only (or limited) | Short-term scratchpad | Persistent episodic + knowledge base + hybrid search |
| **Quality Control** | None (user validates) | None (agent self-validates) | Dedicated reviewer agent with 5-dim scoring |
| **Self-Improvement** | None | None | Reputation-driven evolution (prompt/model/role) |
| **Tool Use** | Limited, platform-controlled | Broad but unreliable | 36+ tools with security model (allowlist/denylist) |
| **Multi-Channel** | Web/API only | CLI/Web | Telegram, Discord, Slack, Feishu with native file delivery |
| **Process Model** | Single process | Single process | One OS process per agent (true parallelism) |
| **Coordination** | N/A | In-memory | File-backed (TaskBoard, ContextBus, Mailboxes) — crash-resilient |

---

## 9. Design Principles

1. **Separation of Concerns**: Planning, execution, and review are distinct responsibilities handled by specialized agents.
2. **File-Backed Everything**: All coordination state lives in JSON files with file locks. No external database required. Crash-resilient by design.
3. **Progressive Memory Loading**: Memories are loaded in layers (L0/L1/L2) to maximize relevance while respecting context window limits.
4. **Advisor, Not Gatekeeper**: Alic scores and suggests but never blocks. Leo incorporates feedback during synthesis. This prevents deadlocks.
5. **Secure by Default**: Command execution uses allowlist + denylist. File access is workspace-bounded. Config values are redacted in API responses.
6. **Evolution Over Configuration**: Instead of manual tuning, agents self-improve through reputation tracking and automated evolution paths.
7. **Channel-Agnostic**: The core system is decoupled from delivery channels. Adding a new channel requires only an adapter — no core changes.

---

## 10. Current Status (V0.01)

| Component | Status |
|-----------|--------|
| Multi-agent orchestration (Leo/Jerry/Alic) | Production |
| TaskBoard with self-claim, cancel, pause, retry | Production |
| Context Bus (4-layer, TTL, provenance) | Production |
| Hybrid memory (BM25 + ChromaDB + RRF) | Production |
| Episodic memory (L0/L1/L2 progressive loading) | Production |
| Knowledge Base (Zettelkasten) | Production |
| 36+ tools (9 categories) | Production |
| Skill system (hot-reload, YAML frontmatter) | Production |
| Reputation scoring (5-dim EMA) | Production |
| Evolution engine (3 paths) | Production |
| Telegram channel adapter | Production |
| Discord / Slack / Feishu adapters | Beta |
| Gateway API (50+ endpoints, SSE) | Production |
| Cron scheduler with concurrency control | Production |
| Subagent spawning (depth=3, children=5) | Production |
| Emergency stop (`/cancel` + natural language) | Production |
| ResilientLLM (failover, circuit breaker, key rotation) | Production |
| Config value redaction | Production |
| MiniMax Trace-ID tracking | Production |
| Chinese stop-word filtering for BM25 | Production |
| Test suite | 274+ tests passing |

---

*Cleo V0.01 — Product Narrative*
*Last updated: 2026-02-24*
