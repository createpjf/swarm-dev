# Cleo — Product Logic V0.01

## 1. Agent System

### 1.1 Agent Roles

Cleo operates as a team of three specialized agents, each running in its own OS process:

| Agent | ID | Role | Model | Tool Profile |
|-------|----|------|-------|-------------|
| **Leo** | `leo` | Planner / Brain | MiniMax-M2.5 | `minimal` |
| **Jerry** | `jerry` | Executor / Builder | minimax-m2.5 | `coding` |
| **Alic** | `alic` | Reviewer / Advisor | minimax-m2.5 | `minimal` |

**Leo (Planner)**
- Receives every user task directly
- Decomposes into max 3 subtasks using `TASK:` + `COMPLEXITY:` format
- Never executes commands — all execution is delegated to Jerry
- Performs closeout synthesis: combines all subtask results + Alic's feedback into one polished answer
- Has two phases: Phase 1 (Decomposition) and Phase 2 (Closeout)

**Jerry (Executor)**
- Carries out atomic subtasks assigned by Leo
- Returns raw results (code, data, analysis) — not user-facing answers
- Has access to the `coding` tool profile (filesystem, exec, browser, etc.)
- Must not plan or decompose — that is Leo's job
- Delivers production-ready output: no placeholders, no stubs

**Alic (Reviewer/Advisor)**
- Scores subtask outputs 1-10 using HLE-based dimensions
- Is an advisor, not a gatekeeper — never blocks tasks from completing
- Leo reads scores and suggestions during final synthesis
- Responds with structured JSON: `{"score": <1-10>, "comment": "...", "suggestions": ["optional"]}`
- Omits suggestions if score >= 8; max 3 suggestions

### 1.2 Agent Capabilities & Restrictions

```
┌────────────────────────────────────────────────────────────┐
│                     Capability Matrix                       │
├──────────────┬───────────┬───────────┬────────────────────┤
│  Capability  │    Leo    │   Jerry   │       Alic         │
├──────────────┼───────────┼───────────┼────────────────────┤
│ Decompose    │    ✅     │    ❌     │        ❌          │
│ Execute      │    ❌     │    ✅     │        ❌          │
│ Review       │    ❌     │    ❌     │        ✅          │
│ Synthesize   │    ✅     │    ❌     │        ❌          │
│ Tool use     │  Minimal  │   Full    │     Minimal        │
│ Memory       │    ✅     │    ✅     │        ✅          │
│ Block tasks  │    ❌     │    ❌     │        ❌          │
└──────────────┴───────────┴───────────┴────────────────────┘
```

### 1.3 Agent Lifecycle (Per Tick)

Each agent runs an event loop (`_agent_loop`) with three priority-ordered stages:

```
┌─────────────────────────────────────────────────────┐
│                  Agent Loop (per tick)                │
│                                                       │
│  1. Mailbox Scan (highest priority)                  │
│     ├─ "shutdown" → exit                             │
│     └─ "critique_request" → run review → check       │
│        planner closeouts                              │
│                                                       │
│  2. Critique Revision                                │
│     └─ If own task has suggestions → fix (max 1      │
│        round) → resubmit or auto-complete            │
│                                                       │
│  3. Regular Task Claim                               │
│     ├─ board.claim_next(agent_id, reputation, role)  │
│     ├─ Planner path: run → extract subtasks →        │
│     │   register mapping → wait for closeout          │
│     └─ Executor path: run → submit for review →      │
│         route to advisor (complexity-based)            │
│                                                       │
│  Idle: progressive backoff (1s → 5s)                 │
│  Background: stale task recovery (every 30s)         │
└─────────────────────────────────────────────────────┘
```

### 1.4 Agent Execution Pipeline (BaseAgent.run)

When an agent claims a task, it executes a 10-step pipeline:

1. **Load skills** — Hot-reload markdown skill files (shared → team → per-agent → overrides)
2. **Load docs** — Read reference documents from `docs/` directory
3. **Build context** — Read ContextBus snapshot for cross-agent state
4. **Recall memory** — Progressive episodic + knowledge base loading (L0/L1/L2)
5. **Build system prompt** — Role + skills + docs + context + memory + tools
6. **Prepare messages** — System prompt + short-term conversation history
7. **LLM call** — Send to ResilientLLM (with failover and circuit breaker)
8. **Parse tool calls** — Extract structured tool invocations from output
9. **Tool loop** — Execute tools, feed results back, repeat (with cancellation checks)
10. **Store memory** — Save episode, extract patterns, update knowledge base

---

## 2. Task Lifecycle

### 2.1 TaskBoard State Machine

```
                    ┌────────────┐
                    │  PENDING   │◀─── create()
                    └─────┬──────┘
                          │ claim_next()
                    ┌─────▼──────┐
                    │  CLAIMED   │
                    └─────┬──────┘
                          │ submit_for_review()
                    ┌─────▼──────┐
             ┌──────│  REVIEW    │──────┐
             │      └────────────┘      │
             │ (timeout)      add_critique()
             │                          │
    ┌────────▼───┐              ┌───────▼────┐
    │ COMPLETED  │              │ COMPLETED  │
    │(auto-      │              │ (with      │
    │ complete)  │              │  critique) │
    └────────────┘              └────────────┘

    Special transitions:
    ─ cancel()  → CANCELLED (from any non-terminal)
    ─ pause()   → PAUSED (from PENDING/CLAIMED)
    ─ resume()  → PENDING (from PAUSED)
    ─ retry()   → PENDING (from FAILED/CANCELLED)
    ─ fail()    → FAILED (on exception)
    ─ recover_stale_tasks() → PENDING (stale CLAIMED after 180s)
                            → COMPLETED (stale REVIEW after 300s)
```

### 2.2 Self-Claiming Model

Tasks are not assigned — agents autonomously claim them:

1. Agent calls `board.claim_next(agent_id, reputation, agent_role)`
2. TaskBoard acquires file lock
3. Iterates pending tasks, checking:
   - `min_reputation` threshold
   - Dependency resolution (`blocked_by` all completed)
   - Agent claim restrictions (Alic can only claim review tasks)
   - Role matching (planner tasks → Leo, implement tasks → Jerry)
4. First qualifying task is atomically claimed (status → CLAIMED)
5. File lock released

**Role routing map:**

| required_role | Eligible Agents |
|--------------|----------------|
| `planner` / `plan` | leo, planner |
| `implement` / `execute` / `code` | jerry, executor, coder, developer, builder |
| `review` / `critique` | alic, reviewer, auditor |

Strict roles (planner, review) have no fallback — only mapped agents qualify.

### 2.3 Subtask Extraction

When Leo completes its planning phase:

1. Output is parsed for lines starting with `TASK:` and optional `COMPLEXITY:`
2. Each `TASK:` line becomes a new subtask with:
   - `required_role` inferred from keywords (review/plan/implement)
   - `complexity` inferred or from Leo's explicit tag (simple/normal/complex)
   - `parent_id` linking to the original user task
3. Subtasks are created as immediately PENDING
4. Parent→subtask mapping is registered for closeout tracking
5. WakeupBus signals all agents to check for new tasks immediately

**Complexity routing:**

| Complexity | Review? | Path |
|-----------|---------|------|
| `simple` | No | Auto-complete after execution |
| `normal` | Yes | Send to Alic for critique, then complete |
| `complex` | Yes | Send to Alic for critique (higher scrutiny) |

### 2.4 Planner Closeout

After all subtasks complete:

1. `_check_planner_closeouts()` detects all subtask IDs are in `completed` status
2. Collects all executor results with attribution
3. Collects all reviewer critiques (scores, comments, suggestions)
4. Builds a synthesis prompt with:
   - Original user request
   - All subtask results
   - All reviewer feedback
   - File generation warnings (if applicable)
5. Leo's LLM generates the final polished answer
6. Mini tool-loop: if Leo invokes tools during synthesis (max 3 rounds), they execute
7. Parent task updated with final answer, marked COMPLETED

### 2.5 Emergency Stop

Users can cancel tasks through:

- **Command**: `/cancel` in any channel
- **Natural language**: "取消", "停止", "stop", "cancel", "abort", "やめて", "стоп"

When triggered:
1. `board.cancel_all()` cancels all non-terminal tasks
2. Agent tool loops check `board.is_cancelled()` each iteration
3. Currently running agents abort their tool loop and move to next tick

---

## 3. Context Bus

### 3.1 Four-Layer Architecture

| Layer | Name | TTL | Purpose |
|-------|------|-----|---------|
| L0 | TASK | Until task completes | Task-specific working state |
| L1 | SESSION | 3,600s (1 hour) | Session-level context |
| L2 | SHORT | 86,400s (1 day) | Cross-task short-term memory |
| L3 | LONG | Permanent | Persistent agent state |

### 3.2 Key Format

All keys are namespaced: `{agent_id}:{key}`

Each entry stores:
- `value` — the actual content
- `layer` — context layer (0-3)
- `ttl` — time-to-live in seconds (or None for permanent)
- `ts` — timestamp of last write
- `provenance` — optional metadata about origin (agent, task_id, source)

### 3.3 Snapshot Injection

At the start of each task, the agent reads a ContextBus snapshot. Expired entries (past TTL) are automatically pruned. The snapshot is injected into the system prompt so the agent has cross-agent awareness.

---

## 4. Memory System

### 4.1 Hybrid Search Architecture

```
Query ──┬──▶ BM25 (SQLite FTS5)    ──▶ Ranked results
        │       unicode61 tokenizer
        │       Chinese stop-words
        │
        └──▶ ChromaDB (Vector)      ──▶ Ranked results
                cosine similarity
                            │                    │
                            └──────┬─────────────┘
                                   ▼
                           RRF Fusion
                        score = 1/(k + rank)
                           k = 60
                                   │
                                   ▼
                          Merged top-K results
```

**BM25 (Full-Text Search)**
- SQLite FTS5 with `unicode61` tokenizer (built-in CJK support)
- Chinese stop-word filtering (~120 common function words: "的", "了", "在", "是", etc.)
- Exact keyword matching, especially for code and technical terms

**ChromaDB (Vector Search)**
- Persistent vector store with cosine similarity
- Embedding providers: OpenAI (`text-embedding-3-small`), Flock, Local, ChromaDB default
- Semantic matching for conceptual queries

**Reciprocal Rank Fusion (RRF)**
- Formula: `score = 1 / (k + rank)` where k = 60
- Merges BM25 and vector results into a single ranked list
- Ensures both exact matches and semantic matches surface

### 4.2 Episodic Memory

Per-agent memory that persists across sessions:

**Episode Types:**
1. **Task episodes** — Complete task execution records (description, result, score, outcome)
2. **Solution cases** — Problem → solution pairs extracted from successful tasks
3. **Behavioral patterns** — Recurring patterns detected across multiple tasks
4. **Daily logs** — Append-only journal of activity summaries
5. **Error patterns** — Categorized failure modes for future avoidance
6. **Insights** — Cross-domain observations

**Progressive Loading (L0/L1/L2):**

| Level | Token Budget | Content | Purpose |
|-------|-------------|---------|---------|
| L0 | 100 tokens | Recent episode titles + scores | Quick context orientation |
| L1 | 500 tokens | Relevant cases + pattern summaries | Task-relevant knowledge |
| L2 | Full budget | Detailed episodes + daily log excerpts | Deep context when needed |

The progressive loader fills each level sequentially. If L0 is sufficient for the current task, L1 and L2 are skipped — preventing context window overflow.

### 4.3 Knowledge Base

Shared Zettelkasten-style repository:

- **Atomic notes** — Each note captures one idea, one insight, one lesson
- **Cross-agent** — All agents read from and write to the same KB
- **Tagged** — Notes are tagged for retrieval (agent_id, domain, task_type)
- **Insights** — Agents publish cross-domain observations after task completion
- **Recall budget** — Configurable per-agent (default: 800 tokens)

### 4.4 Memory Extraction Pipeline

After every task completion:

```
Task Result ──▶ extract_cases()    ──▶ Episodic Memory (cases)
            ──▶ extract_patterns() ──▶ Episodic Memory (patterns)
            ──▶ extract_insight()  ──▶ Knowledge Base (atomic notes)
            ──▶ QMD.index()        ──▶ FTS5 Search Index
            ──▶ generate_memory_md() ──▶ MEMORY.md (agent reference)
            ──▶ DocUpdater.check() ──▶ Error pattern detection
```

---

## 5. Tool System

### 5.1 Tool Registry

36+ tools organized into 9 categories:

| # | Category | Tools | Description |
|---|----------|-------|-------------|
| 1 | **Web** | `web_search`, `web_fetch` | Search via Brave/Perplexity/Kimi; fetch pages as markdown |
| 2 | **Filesystem** | `read_file`, `write_file`, `edit_file`, `list_dir` | File I/O within workspace boundary |
| 3 | **Memory** | `memory_search`, `memory_save`, `kb_search`, `kb_write` | Long-term memory operations |
| 4 | **Task** | `task_create`, `task_status`, `spawn_subagent` | Task management and subagent spawning |
| 5 | **Automation** | `exec`, `cron`, `process` | Command execution, job scheduling |
| 6 | **Skill** | `check_skill_deps`, `install_skill_cli`, `search_skills`, `install_remote_skill` | Skill management |
| 7 | **Browser** | `browser_navigate`, `browser_click`, `browser_fill`, `browser_get_text`, `browser_screenshot`, `browser_evaluate`, `browser_page_info` | Full browser automation |
| 8 | **Media** | `screenshot`, `notify`, `analyze_image` | Screen capture, notifications, vision |
| 9 | **Messaging** | `send_mail`, `send_file`, `message` | Communication tools |

### 5.2 Tool Profiles

Three access levels configured per-agent in `agents.yaml`:

| Profile | Included Tools | Use Case |
|---------|---------------|----------|
| `minimal` | web_search, web_fetch, memory_search, memory_save, task_status, notify, send_mail | Read-only agents (Leo, Alic) |
| `coding` | All `minimal` + filesystem + exec + browser + media + cron + skill + subagent | Executor agents (Jerry) |
| `full` | All tools | Unrestricted access |

Additional fine-grained control:
- `allow: ["tool_name"]` — Add specific tools beyond the profile
- `deny: ["tool_name"]` — Block specific tools (deny always wins)

### 5.3 Tool Execution Flow

```
Agent LLM Output ──▶ parse_tool_calls() ──▶ List of {tool, params}
                                                      │
                                              ┌───────▼────────┐
                                              │ execute_tool_   │
                                              │ calls()         │
                                              │                 │
                                              │ For each call:  │
                                              │ 1. Validate     │
                                              │ 2. Execute      │
                                              │ 3. Collect      │
                                              │    result       │
                                              └───────┬────────┘
                                                      │
                                              Tool Results ──▶ Feed back to LLM
                                              (repeat until no more tool calls
                                               or cancellation detected)
```

### 5.4 Web Search Providers

| Provider | API | Strength |
|----------|-----|----------|
| **Brave** | Brave Search API | General web search, good for English content |
| **Perplexity** | Perplexity API | AI-enhanced search with summarization |
| **Kimi/Moonshot** | Moonshot API (`moonshot-v1-128k`) | Chinese-optimized search via `$web_search` tool |

Provider selection is automatic based on query language or explicit `provider` parameter.

---

## 6. Skill System

### 6.1 Skill Format

Skills are markdown files with YAML frontmatter:

```yaml
---
name: weather
description: Check weather for any location
triggers: ["weather", "forecast", "temperature"]
requires_cli: ["curl"]
---
# Weather Skill

## Instructions
Use the `exec` tool to call the weather API...
```

### 6.2 Load Order

Skills are loaded hierarchically with later entries overriding earlier ones:

```
1. skills/shared/       ← shared across all agents
2. skills/team/         ← team-level defaults
3. skills/{agent_id}/   ← per-agent specialization
4. skills/overrides/    ← manual overrides (highest priority)
```

### 6.3 Hot-Reload

The SkillLoader watches skill directories for changes. New or modified skills are picked up on the next task execution — no restart required. Skills are injected into the agent's system prompt during the build phase.

### 6.4 Remote Skill Registry

Cleo connects to a remote skill registry for discovering and installing community skills:
- Registry URL configured in `agents.yaml` (`skill_registry.url`)
- `search_skills` tool queries the registry
- `install_remote_skill` downloads and installs a skill locally
- Skills can declare CLI dependencies via `requires_cli` — auto-checked at load time

---

## 7. Reputation & Evolution

### 7.1 Reputation Scoring

Five dimensions tracked with Exponential Moving Average (EMA):

| Dimension | Weight | Description |
|-----------|--------|-------------|
| `task_completion` | 25% | Binary: did the agent complete the task? |
| `output_quality` | 30% | Alic's critique score (1-10) normalized |
| `improvement_rate` | 25% | Trend: quality delta over recent tasks |
| `consistency` | 10% | Variance of quality scores (lower = better) |
| `review_accuracy` | 10% | For Alic: correlation between scores and outcomes |

**Composite score:** Weighted sum, range 0-100.

### 7.2 Anti-Gaming Mechanisms

| Mechanism | Detects | Action |
|-----------|---------|--------|
| Mutual inflation | Two agents consistently give each other high scores | Reduce review weight |
| Consensus deviation | Reviewer scores diverge significantly from peer consensus | Flag for investigation |
| Extreme bias | Reviewer always gives 10/10 or 1/10 | Reduce `review_accuracy` dimension |

### 7.3 Evolution Engine

Three paths, triggered automatically when reputation patterns indicate need:

| Path | Trigger | Action | Approval |
|------|---------|--------|----------|
| **A: Prompt Upgrade** | Quality below threshold + specific error patterns | Generate improved system prompt | Auto-applied |
| **B: Model Swap** | Persistent quality issues not resolved by prompt upgrade | Switch to a different LLM model | Requires confirmation |
| **C: Role Restructure** | Fundamental capability mismatch | Reassign agent roles | Team vote (60% threshold) |

---

## 8. Cron & Automation

### 8.1 Job Types

| Type | Example | Description |
|------|---------|-------------|
| `once` | `{"type": "once", "at": "2024-03-15T10:00:00"}` | Run once at specified time |
| `interval` | `{"type": "interval", "seconds": 3600}` | Run every N seconds |
| `cron` | `{"type": "cron", "expr": "0 9 * * 1"}` | Standard cron expression |

### 8.2 Concurrency Control

- `_running_jobs` dictionary tracks active job count per job ID
- `max_concurrent_runs` (default: 1) prevents overlapping executions
- If a job is still running when the next trigger fires, the new run is skipped with a warning

### 8.3 Watchdog Timeout

- Each job execution is wrapped in `asyncio.wait_for(task, timeout)`
- Default timeout: 300 seconds (5 minutes)
- On timeout: task is cancelled, error logged, job slot released
- Prevents stuck jobs from permanently consuming resources

---

## 9. Subagent Spawning

### 9.1 Spawn Mechanism

Agents can dynamically create child agents via the `spawn_subagent` tool:

```json
{
  "tool": "spawn_subagent",
  "params": {
    "role": "Research assistant for financial data",
    "task": "Find Q4 2025 revenue for the top 5 tech companies",
    "timeout": 300
  }
}
```

### 9.2 Safety Limits

| Limit | Value | Purpose |
|-------|-------|---------|
| `MAX_DEPTH` | 3 | Prevents infinite recursion (agent spawns agent spawns agent...) |
| `MAX_CHILDREN` | 5 | Limits resource consumption per parent |
| `timeout` | 300s (default) | Auto-kills stuck subagents |

### 9.3 Lifecycle

1. Parent agent calls `spawn_subagent` with role, task, and timeout
2. SubagentRegistry validates depth and children limits
3. New agent is created with the specified role and task
4. Agent executes in the parent's process (async)
5. On completion: result returned to parent via mailbox
6. On timeout: status set to "failed", parent notified
7. `check_timeouts()` runs periodically to clean up stale subagents

---

## 10. Session Management

### 10.1 Session Store

Sessions provide per-user conversation isolation:

- **Key format**: `{channel}:{chat_id}` (normalized: lowercase, stripped)
- **Storage**: JSONL files per session in `.sessions/` directory
- **Expiry**: 24 hours (configurable)
- **History limit**: 50 messages FIFO (oldest messages dropped)

### 10.2 Group Chat Isolation

In group chats (Telegram groups, Discord servers):
- Each user gets their own session within the group
- Session key: `{channel}:{group_id}:{user_id}`
- Prevents cross-user context contamination

### 10.3 Session Lifecycle

```
User message ──▶ SessionStore.get_or_create(key)
                         │
                   ┌─────▼─────┐
                   │  Session   │
                   │  - history │  ◀── append new message
                   │  - metadata│
                   │  - expiry  │
                   └─────┬─────┘
                         │
              Orchestrator processes task
                         │
                   ┌─────▼─────┐
                   │  Response  │──▶ append to session history
                   └───────────┘
                         │
              After 24h: session expires and is cleaned up
```

---

*Cleo V0.01 — Product Logic*
*Last updated: 2026-02-24*
