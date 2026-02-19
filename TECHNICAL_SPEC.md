# Agent Stack — Technical Specification

> Decentralised multi-agent coordination with reputation, peer review, and autonomous evolution.
> Architecture inspired by **Claude Agent Teams** patterns, implemented in plain Python — model-agnostic, process-native.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Multi-Process Execution](#3-multi-process-execution)
4. [Agent Lifecycle](#4-agent-lifecycle)
5. [Context Bus & Mailbox](#5-context-bus--mailbox)
6. [Task Board & Self-Claim](#6-task-board--self-claim)
7. [Reputation System](#7-reputation-system)
8. [Peer Review](#8-peer-review)
9. [Evolution Engine](#9-evolution-engine)
10. [Adapters](#10-adapters)
11. [Configuration Reference](#11-configuration-reference)
12. [Quickstart](#12-quickstart)
13. [File Layout](#13-file-layout)

---

## 1. Overview

Agent Stack is a local multi-process agent framework. Each agent runs in its own OS process with its own asyncio event loop. LLM inference is always delegated to an external API (FLock, OpenAI, Ollama). Agents coordinate through three shared file-backed channels — no message broker, no central server.

### What it borrows from Claude Agent Teams

| Claude Agent Teams pattern | Agent Stack equivalent |
|---|---|
| File-lock self-claim | `TaskBoard.claim_next()` — agents race for tasks atomically |
| `TaskCompleted` hook | `board.complete()` rejects completion if peer review score < 60 |
| `TeammateIdle` hook | `max_idle_cycles` — agent exits gracefully after N idle polls |
| P2P teammate messaging | Per-agent `.mailboxes/<id>.jsonl` inbox |
| Graceful shutdown request | `orchestrator.shutdown_agent()` writes shutdown mail |

### What it adds

- **Multi-dimensional reputation** — 5-axis weighted score, updated per task via EMA
- **Anti-gaming peer review** — mutual inflation detection + consistency tracking
- **Evolution Engine** — three escalating paths (prompt / model / role)
- **ERC-8004 integration** — on-chain reputation registry (optional)
- **Model-agnostic** — swap FLock → OpenAI → Ollama in one config line

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────┐
│  Main Process (Orchestrator)                        │
│  • reads config/agents.yaml                         │
│  • creates tasks on TaskBoard                       │
│  • spawns one child process per agent               │
│  • waits for all children to finish                 │
└──────────────┬──────────────────────────────────────┘
               │  multiprocessing.Process × N
    ┌──────────▼──────────┐   ┌─────────────────────┐
    │  Agent Process A    │   │  Agent Process B    │
    │  asyncio event loop │   │  asyncio event loop │
    │  ─────────────────  │   │  ─────────────────  │
    │  BaseAgent          │   │  BaseAgent          │
    │  AgentMemory        │   │  AgentMemory        │
    │  SkillLoader        │   │  SkillLoader        │
    │  ReputationScheduler│   │  ReputationScheduler│
    └──────────┬──────────┘   └──────────┬──────────┘
               │                         │
    ┌──────────▼─────────────────────────▼──────────┐
    │  Shared File Layer (all processes read/write)  │
    │  ┌──────────────┐  ┌──────────┐  ┌─────────┐  │
    │  │ContextBus    │  │TaskBoard │  │Mailboxes│  │
    │  │.context_     │  │.task_    │  │.mailboxes│ │
    │  │ bus.json     │  │board.json│  │/<id>.   │  │
    │  │(filelock)    │  │(filelock)│  │jsonl    │  │
    │  └──────────────┘  └──────────┘  └─────────┘  │
    └────────────────────────────────────────────────┘
               │ external HTTP
    ┌──────────▼──────────────────────────────────────┐
    │  LLM API (FLock / OpenAI / Ollama)              │
    └─────────────────────────────────────────────────┘
```

### Module map

```
agent-stack/
├── core/
│   ├── agent.py            BaseAgent — run, review, send_mail, read_mail
│   ├── agent_memory.py     Three-layer memory (short / long / working)
│   ├── context_bus.py      Shared file KV store
│   ├── task_board.py       File-locked task lifecycle + self-claim
│   ├── skill_loader.py     Hot-reload markdown skills
│   └── orchestrator.py     Process launcher + shutdown
├── reputation/
│   ├── scorer.py           5-dimension EMA scoring
│   ├── peer_review.py      Weighted reviews + anti-gaming
│   ├── scheduler.py        Event hooks + cron analysis
│   └── evolution.py        Evolution Engine (Path A/B/C)
├── adapters/
│   ├── llm/                flock.py  openai.py  ollama.py
│   ├── memory/             chroma.py  mock.py
│   └── chain/              erc8004.py  mock.py
├── skills/                 Markdown skill documents
├── config/agents.yaml      Team configuration
└── main.py                 CLI entry point
```

---

## 3. Multi-Process Execution

Each agent is an independent OS process. There is no shared Python memory — coordination happens exclusively through the file layer.

```python
# orchestrator.py — one Process per agent
p = multiprocessing.Process(
    target=_agent_process,   # runs in child
    args=(cfg_dict, config),
)
p.start()
```

Inside the child process:

```python
async def _agent_loop(agent, bus, board, config):
    while True:
        # 1. check mailbox (P2P messages, shutdown requests)
        mails = agent.read_mail()

        # 2. self-claim next available task
        task = board.claim_next(agent.cfg.agent_id, reputation_score)

        if task is None:
            idle_count += 1
            if idle_count >= max_idle:
                return          # TeammateIdle pattern — graceful exit
            await asyncio.sleep(1)
            continue

        # 3. execute task (external LLM call)
        result = await agent.run(task, bus)

        # 4. submit for peer review
        board.submit_for_review(task.task_id, result)

        # 5. notify reviewer agents via mailbox
        agent.send_mail(reviewer_id, review_request, msg_type="review_request")

        # 6. update reputation (EMA scoring)
        await scheduler.on_task_complete(agent_id, task, result)

        # 7. TaskCompleted hook — may reject if review score < 60
        completed = board.complete(task.task_id)
        if "review_failed" in completed.evolution_flags:
            board.create(task.description + "\n\n[REWORK]")   # re-queue
```

### Why not asyncio concurrency within one process?

| | Single-process asyncio | Multi-process |
|---|---|---|
| CPU-bound work (scoring, pattern analysis) | Blocks event loop | True parallel |
| Memory isolation | Shared — bugs leak across agents | Each process independent |
| Crash isolation | One error can kill all agents | Only one process dies |
| Context window budget | One large context | Each agent owns its own |
| Scales to many agents | Limited by GIL | OS scheduler handles it |

---

## 4. Agent Lifecycle

```
CREATED ──▶ IDLE (polling) ──▶ CLAIMED ──▶ WORKING ──▶ REVIEW_PENDING
                                                              │
                          ◀── REWORK ◀── review score < 60   │
                                                              ▼
                                                         COMPLETED
                                                         (reputation updated)
```

Key state transitions:

| Trigger | Effect |
|---|---|
| `board.claim_next()` succeeds | Task status → `claimed`, `claimed_at` set |
| `agent.run()` completes | `board.submit_for_review()` → status `review` |
| Peer review recorded | `board.add_review()`, reviewer reputation updated |
| `board.complete()` — avg score ≥ 60 | Status → `completed`, `completed_at` set |
| `board.complete()` — avg score < 60 | `evolution_flags` += `review_failed`, sent back |
| Error during `agent.run()` | `board.fail()`, `scheduler.on_error()` |

---

## 5. Context Bus & Mailbox

### Context Bus

Shared read/write KV store. Every agent process reads it at the start of each task. The current snapshot is injected into the agent's system prompt.

```python
# publish a result
bus.publish("executor", "last_result", result)

# read another agent's output
planner_plan = bus.get("planner", "last_result")

# snapshot injected into system prompt
context_str = bus.snapshot()
# → {"planner:last_result": "...", "executor:last_result": "...", ...}
```

Key format: `"{agent_id}:{key}"`. File-locked on every write.

### Mailbox (P2P, no Lead needed)

Directly inspired by Claude Agent Teams' teammate-to-teammate messaging — no orchestrator involvement needed.

```python
# reviewer sends feedback directly to executor
reviewer_agent.send_mail(
    to_agent_id="executor",
    content="Your API design is missing error codes. See review.",
    msg_type="message",
)

# executor drains its inbox at the start of each task cycle
mails = executor_agent.read_mail()
```

Each mailbox is a JSONL file at `.mailboxes/<agent_id>.jsonl`. Drained (cleared) after reading.

**Shutdown flow (Agent Teams pattern):**

```python
# orchestrator sends graceful shutdown
orchestrator.shutdown_agent("executor")
# writes: {"from": "orchestrator", "type": "shutdown", ...}

# agent loop checks mail first — exits cleanly
if mail.get("type") == "shutdown":
    return
```

---

## 6. Task Board & Self-Claim

### Self-claim (Agent Teams pattern)

Multiple agent processes simultaneously call `claim_next()`. File lock ensures exactly one agent gets each task.

```python
def claim_next(self, agent_id: str, agent_reputation: int) -> Optional[Task]:
    with self.lock:                          # filelock — atomic
        for task in pending_tasks:
            if task.min_reputation > agent_reputation:
                continue                    # reputation gate
            if any blockers not completed:
                continue                    # dependency check
            # mark claimed — only one process can reach here
            task.status    = "claimed"
            task.agent_id  = agent_id
            task.claimed_at = now()
            self._write(data)
            return task
    return None
```

### Dependency graph

```python
# task B cannot start until task A is complete
task_a = board.create("Write the database schema")
task_b = board.create("Implement API endpoints",
                      blocked_by=[task_a.task_id])
```

### TaskCompleted hook

The `board.complete()` method acts as Claude Agent Teams' `TaskCompleted` hook — it can reject completion and send the task back for rework.

```python
def complete(self, task_id: str) -> Task:
    avg = average_review_score(task)
    if avg < 60:
        task.status = "claimed"          # back to the agent
        task.evolution_flags += ["review_failed"]
        return task                      # hook rejected completion
    task.status = "completed"
    return task
```

---

## 7. Reputation System

### Five dimensions

| Dimension | Weight | Updated by |
|---|---|---|
| `task_completion` | 25% | Scheduler on every task |
| `output_quality` | 30% | Peer review aggregate |
| `improvement_rate` | 25% | Scheduler (rework recovery signal) |
| `consistency` | 10% | Variance across similar tasks |
| `review_accuracy` | 10% | Reviewer deviation from consensus |

### Exponential Moving Average

Each dimension is updated independently with α = 0.3:

```
new_score[dim] = 0.3 × signal + 0.7 × current_score[dim]
composite      = Σ(new_score[dim] × weight[dim])
```

Recent events count for 30%, history for 70%. A single bad task won't erase a strong track record.

### Threshold states

| Score | State | Action |
|---|---|---|
| ≥ 80 | `healthy` | Normal operation |
| 60–79 | `watch` | Monitoring frequency doubles |
| 40–59 | `warning` | Evolution Engine notified |
| < 40 | `evolve` | Evolution Engine triggered |

```python
# check in real time
status = scorer.threshold_status("executor")
trend  = scorer.trend("executor")   # "improving" | "declining" | "stable"
```

---

## 8. Peer Review

### Weight calculation

```python
def compute_weight(reviewer_id, target_id) -> float:
    weight = reviewer_reputation / 100          # base: reviewer quality

    # Anti-gaming 1: mutual inflation
    if avg_given > 85 and avg_received > 85:
        weight *= 0.5

    # Anti-gaming 2: consistency tracking
    deviation = reviewer_deviation_from_consensus(reviewer_id)
    weight *= max(0.3, 1.0 - deviation)

    return weight
```

### Three anti-gaming mechanisms

**1. Mutual inflation detection**
If agent A consistently gives B high scores, and B consistently gives A high scores, both their review weights are halved for each other.

**2. Reviewer reputation weighting**
A reviewer with score 90 has 3× more impact than one with score 30.

**3. Consistency tracking**
Reviewers who systematically deviate from consensus (outliers in either direction) have their weight reduced: `weight *= max(0.3, 1.0 − deviation)`.

### TaskCompleted integration

```python
# board.complete() reads aggregated peer review score
avg = peer_review.aggregate(task_id)   # weighted average
if avg < 60:
    # hook rejects — task goes back for rework
    task.evolution_flags.append("review_failed")
```

---

## 9. Evolution Engine

Triggered automatically when an agent's reputation falls below 40 (`evolve` state). Follows least-invasive-first ordering.

### Path A — Prompt Upgrade (automated)

No human confirmation required. The Evolution Engine appends new constraints to the agent's skill override file (`skills/agent_overrides/<agent_id>.md`). SkillLoader hot-reloads it on the next task — no restart needed.

```
Triggered when: score < 40 AND pattern = "inconsistent_output" OR "high_failure_rate"
Effect: new constraints written to skill override
Reputation & memory: fully preserved
Identity (wallet): unchanged
```

### Path B — Model Swap (leader confirmation)

When prompt upgrades fail to improve scores across multiple cycles, the agent's underlying model is replaced. The pending swap is written to `memory/pending_swaps/<agent_id>.json`. A human (or designated Lead agent) must confirm.

```bash
python main.py evolve executor confirm
# → shows: executor: flock/qwen3-30b-a3b → flock/qwen3-235b-thinking
# → [y/N] prompt
```

On confirmation:
- `config/agents.yaml` updated with new model
- Agent NFT identity (wallet address) unchanged
- Long-term memory and accumulated reputation preserved

### Path C — Role Restructure (team vote)

If an agent is fundamentally mismatched with its role, the Evolution Engine writes a vote request to `memory/pending_votes/<agent_id>.json`. Other agents and the human operator vote. Threshold: 60% majority.

### Evolution decision tree

```
score < 40?
  └─▶ diagnose(last 50 tasks)
        ├─▶ inconsistent_output OR high_failure_rate?
        │     └─▶ PATH A: prompt upgrade (auto)
        ├─▶ not_improving + multiple error patterns?
        │     └─▶ PATH B: model swap (confirm required)
        └─▶ fundamentally wrong role?
              └─▶ PATH C: role restructure (team vote)
```

---

## 10. Adapters

All adapters implement a minimal protocol — swap them in `config/agents.yaml`.

### LLM Adapters

```python
class LLMAdapter(Protocol):
    async def chat(self, messages: list[dict], model: str) -> str: ...
```

| Adapter | Provider | Key env vars |
|---|---|---|
| `flock.py` | FLock API | `FLOCK_API_KEY`, `FLOCK_BASE_URL` |
| `openai.py` | OpenAI / compatible | `OPENAI_API_KEY`, `OPENAI_BASE_URL` |
| `ollama.py` | Local Ollama | `OLLAMA_URL` (default: localhost:11434) |

### Memory Adapters

```python
class MemoryAdapter(Protocol):
    def add(self, collection: str, document: str, metadata: dict): ...
    def query(self, collection: str, query: str, n_results: int) -> dict: ...
```

| Adapter | Backend | Notes |
|---|---|---|
| `chroma.py` | ChromaDB | Persisted to `memory/chroma/` |
| `mock.py` | In-process dict | No persistence, no deps — for tests |

### Chain Adapters

```python
class ChainAdapter(Protocol):
    def register_agent(self, agent_id: str, metadata: dict) -> str: ...
    def submit_reputation(self, agent_id: str, score: int, signals: dict) -> str: ...
```

| Adapter | Notes |
|---|---|
| `erc8004.py` | Writes to ERC-8004 Reputation Registry via web3.py |
| `mock.py` | Logs to `memory/chain_mock.jsonl` — no blockchain needed |

---

## 11. Configuration Reference

```yaml
# config/agents.yaml

llm:
  provider: flock          # flock | openai | ollama

memory:
  backend: chroma          # chroma | mock

chain:
  enabled: false           # true → ERC-8004 writes

reputation:
  peer_review_agents:      # agent IDs that review task outputs
    - reviewer
  evolution:
    prompt_auto_apply: true
    model_swap_require_confirm: true
    role_vote_threshold: 0.6

max_idle_cycles: 30        # agent exits after N idle polls

agents:
  - id: planner
    role: "Strategic planner. ..."
    model: flock/qwen3-30b-a3b
    skills:
      - planning
      - _base
    memory:
      short_term_turns: 20   # conversation window size
      long_term: true        # enable ChromaDB vector store
      recall_top_k: 3        # episodes to inject per task
    autonomy_level: 1        # 0=max oversight  3=fully autonomous
    wallet: PLANNER_WALLET_KEY   # env var name (not the key value)
```

### Autonomy levels

| Level | Behaviour |
|---|---|
| 0 | Human approves every action (peer review, model swaps) |
| 1 | Human approves model swaps and role restructures |
| 2 | Human approves only role restructures |
| 3 | Fully autonomous — all evolution paths auto-applied |

---

## 12. Quickstart

### Level 0 — Local, mock chain, Ollama

```bash
# 1. install deps (minimal)
pip install httpx pyyaml filelock

# 2. pull a model locally
ollama pull qwen2.5:7b

# 3. set provider to ollama in config/agents.yaml
#    llm: {provider: ollama}
#    chain: {enabled: false}
#    memory: {backend: mock}

# 4. run
python main.py run "Explain the difference between TCP and UDP"
python main.py status
python main.py scores
```

### Level 1 — FLock API + ChromaDB

```bash
pip install httpx pyyaml filelock chromadb

export FLOCK_API_KEY=your_key
export FLOCK_BASE_URL=https://api.flock.io/v1

# set llm: {provider: flock} and memory: {backend: chroma} in agents.yaml
python main.py run "Design a federated learning protocol for mobile devices"
```

### Level 2 — FLock + ERC-8004 on-chain reputation

```bash
pip install httpx pyyaml filelock chromadb web3

export FLOCK_API_KEY=your_key
export RPC_URL=https://your-rpc-endpoint
export REGISTRY_ADDRESS=0x...
export CHAIN_PRIVATE_KEY=0x...

# set chain: {enabled: true} in agents.yaml
python main.py run "Audit the smart contract for reentrancy vulnerabilities"
```

### Evolution workflow

```bash
# agent drops below threshold → Path B pending
python main.py scores
# executor  38.2  declining  evolve 🔄

# confirm model swap interactively
python main.py evolve executor confirm
# Pending model swap for executor:
#   New model : flock/qwen3-235b-thinking
#   Reason    : Agent is not responding to feedback.
# Confirm? [y/N] y
# ✅ Model swap applied for executor.
```

---

## 13. File Layout

```
agent-stack/
├── main.py                     CLI entry point
├── requirements.txt
├── config/
│   └── agents.yaml             Team configuration
├── core/
│   ├── agent.py                BaseAgent
│   ├── agent_memory.py         Short / long / working memory
│   ├── context_bus.py          Shared KV store (filelock)
│   ├── task_board.py           Task lifecycle + self-claim
│   ├── skill_loader.py         Hot-reload markdown skills
│   └── orchestrator.py         Process launcher
├── reputation/
│   ├── scorer.py               EMA scoring
│   ├── peer_review.py          Anti-gaming peer review
│   ├── scheduler.py            Event hooks
│   └── evolution.py            Evolution Engine (Path A/B/C)
├── adapters/
│   ├── llm/
│   │   ├── flock.py
│   │   ├── openai.py
│   │   └── ollama.py
│   ├── memory/
│   │   ├── chroma.py
│   │   └── mock.py
│   └── chain/
│       ├── erc8004.py
│       └── mock.py
├── skills/
│   ├── _base.md                Core operating principles
│   ├── planning.md
│   ├── coding.md
│   ├── review.md
│   └── agent_overrides/        Evolution Engine writes here
├── memory/                     Runtime data (auto-created)
│   ├── reputation_cache.json
│   ├── score_log.jsonl
│   ├── peer_reviews.jsonl
│   ├── evolution_log.jsonl
│   ├── pending_swaps/
│   ├── pending_votes/
│   └── chroma/
└── .mailboxes/                 Per-agent JSONL inboxes (auto-created)
```

---

## Design Decisions

**Why file-backed IPC instead of Redis?**
Zero external dependencies for Level 0. File locks are sufficient for teams of 2–10 agents. Switch to Redis by replacing `ContextBus._read/_write` — the interface stays identical.

**Why not use Claude Agent Teams directly?**
Agent Teams locks you to Anthropic's API, has no reputation system, no evolution engine, and no on-chain integration. The coordination patterns (self-claim, hooks, P2P mailbox) are excellent and are directly reproduced here — but as a provider-agnostic foundation that FLock and ERC-8004 can plug into.

**Why EMA instead of a raw average?**
Raw averages treat a task from 6 months ago equally to yesterday's. EMA with α=0.3 weights recent behaviour at 30% per update — agents that improve recover their score quickly, and reputation reflects current capability rather than history.

**Why skill documents instead of hardcoded prompts?**
Hot-reloading markdown lets the Evolution Engine patch an agent's behaviour (Path A) without restarting the process. It also keeps prompts version-controlled and human-readable.
