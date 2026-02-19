# ⬡ Swarm

**Multi-agent orchestration framework with reputation-driven evolution.**

Agents collaborate through file-backed channels, self-claim tasks, peer-review each other, and automatically evolve when performance drops. Process-native, model-agnostic, zero external infrastructure.

---

## Features

- **Multi-process isolation** — each agent runs in its own OS process with its own asyncio loop
- **File-backed coordination** — TaskBoard, ContextBus, Mailbox — no Redis, no RabbitMQ
- **5-dimension reputation** — EMA scoring with automatic evolution when performance drops
- **3-layer episodic memory** — L0 index → L1 summary → L2 full (OpenViking-inspired)
- **Shared knowledge base** — Zettelkasten atomic notes across the team
- **Hot-reload skills** — edit `skills/*.md`, changes apply on next task cycle
- **Resilient LLM** — exponential backoff, circuit breaker, fallback model chains
- **On-chain identity** — ERC-8004 reputation registry on Base Sepolia (optional)
- **Web dashboard** — real-time orchestration pipeline + ChatGPT-style result panel
- **Declarative workflows** — YAML-defined multi-step pipelines with dependency graphs

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/createpjf/swarm-dev.git
cd swarm-dev

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env — add your LLM API keys (FLock, OpenAI, etc.)

# 4. Run
python main.py                # interactive chat mode
python main.py run "Build a REST API for user management"  # one-shot

# 5. Dashboard
python main.py gateway        # starts at http://127.0.0.1:19789
```

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    Orchestrator                       │
│    (spawns each agent as an independent OS process)   │
└────┬──────────────────┬──────────────────┬───────────┘
     │                  │                  │
 ┌───▼───┐         ┌───▼────┐        ┌───▼────┐
 │Planner│ ──────► │Executor│ ──────►│Reviewer│    Agents
 └───┬───┘         └───┬────┘        └───┬────┘
     │                  │                  │
     ▼                  ▼                  ▼
 ┌─────────────────────────────────────────────┐
 │  TaskBoard  ·  ContextBus  ·  Mailbox       │  File-backed
 │  (file-lock)   (shared KV)   (P2P JSONL)    │  coordination
 └──────────────────┬──────────────────────────┘
                    │
 ┌──────────────────▼──────────────────────────┐
 │  Reputation Scorer  ·  Peer Review          │
 │  Evolution Engine (Path A / B / C)          │  Reputation
 └──────────────────┬──────────────────────────┘
                    │
 ┌──────────────────▼──────────────────────────┐
 │  ERC-8004 Identity  ·  Lit PKP              │
 │  Gnosis Safe  ·  X.402 Payment              │  On-chain
 └─────────────────────────────────────────────┘
```

### Coordination Channels

| Channel | Purpose | Backing |
|---------|---------|---------|
| **TaskBoard** | Task lifecycle (create → claim → review → complete) | `.task_board.json` + file lock |
| **ContextBus** | Shared KV store (agent outputs → system prompts) | `.context_bus.json` + file lock |
| **Mailbox** | P2P messages (shutdown, peer requests) | `.mailboxes/{id}.jsonl` |
| **Heartbeat** | Agent liveness detection | `.heartbeats/{id}.json` |

---

## Reputation & Evolution

Five-dimension scoring with exponential moving average (α=0.3):

| Dimension | Weight | What it measures |
|-----------|--------|-----------------|
| Task Completion | 25% | Success rate |
| Output Quality | 30% | Peer review scores |
| Improvement Rate | 25% | Recovery trend over time |
| Consistency | 10% | Variance penalty |
| Review Accuracy | 10% | Reviewer calibration |

**Threshold states:** `≥80` healthy · `60–79` watch · `40–59` warning · `<40` evolve

When an agent's composite score drops below 40, the **Evolution Engine** activates:

| Path | Trigger | Action | Approval |
|------|---------|--------|----------|
| **A — Prompt Upgrade** | Score < 40 | Append constraints to `skills/agent_overrides/{id}.md` | Automatic |
| **B — Model Swap** | Path A insufficient | Switch to stronger model in fallback chain | Leader confirm |
| **C — Role Restructure** | Fundamental role mismatch | Restructure agent responsibilities | 60% team vote |

---

## Memory System

```
┌─────────────────────────────────────────────────────┐
│  Short-term: conversation window (volatile, N turns) │
├─────────────────────────────────────────────────────┤
│  Long-term: Hybrid BM25 + ChromaDB (RRF fusion)     │
├─────────────────────────────────────────────────────┤
│  Episodic: 3-layer progressive loading               │
│    L0 (~100 tok) index + tags                        │
│    L1 (~500 tok) summary + decisions                 │
│    L2 (full)     complete input/output               │
├─────────────────────────────────────────────────────┤
│  Knowledge Base: shared Zettelkasten (atomic notes)  │
│    + MOC navigation  + cross-agent insights feed     │
└─────────────────────────────────────────────────────┘
```

Each agent maintains its own episodic memory in `memory/agents/{id}/`. The shared knowledge base lives in `memory/shared/` and is accessible to all agents.

---

## Dashboard

The web dashboard (`http://127.0.0.1:19789`) provides:

**Top Panel — Orchestration:**
- Real-time agent pipeline visualization (Planner → Executor → Reviewer)
- Data packet animation showing task flow between agents
- Compact dispatch log with agent activity, token usage, review scores

**Bottom Panel — ChatBox:**
- ChatGPT-style conversation interface
- Submit tasks, view final results
- File/image upload support
- Brave Search integration for web-augmented tasks

**Other panels:** Agents (inline editor) · Skills (full CRUD) · Chain (ERC-8004 status) · Usage (token stats) · Logs (per-agent, level filter) · Health (diagnostic checks)

---

## Pluggable Adapters

```
adapters/
├── llm/                        # Language model providers
│   ├── flock.py                # FLock API (Minimax, DeepSeek, Qwen)
│   ├── openai.py               # OpenAI / Azure / compatible
│   ├── ollama.py               # Local Ollama
│   └── resilience.py           # Retry + circuit breaker + fallback chains
│
├── memory/                     # Memory backends
│   ├── hybrid.py               # BM25 + ChromaDB (reciprocal rank fusion)
│   ├── chroma.py               # ChromaDB vector store
│   ├── episodic.py             # 3-layer episodic memory
│   ├── knowledge_base.py       # Shared Zettelkasten KB
│   ├── extractor.py            # Case/pattern extraction from episodes
│   └── mock.py                 # In-memory (testing)
│
└── chain/                      # Blockchain integrations
    ├── erc8004.py              # ERC-8004 Identity & Reputation Registry
    ├── chain_manager.py        # Unified chain interface
    ├── lit_pkp.py              # Lit Protocol PKP
    ├── gnosis_safe.py          # Multi-sig treasury
    └── x402_client.py          # HTTP 402 payment protocol
```

---

## Configuration

All team config lives in **`config/agents.yaml`**:

```yaml
llm:
  provider: flock               # Global default (overrideable per-agent)

agents:
  - id: planner
    role: "Strategic planner — decomposes tasks into subtasks"
    model: minimax-m2.1
    fallback_models: [deepseek-v3.2, qwen3-235b-thinking]
    skills: [_base, planning]
    llm:
      api_key_env: FLOCK_API_KEY
      base_url_env: PLANNER_BASE_URL

  - id: executor
    role: "Implementation agent — writes code and solutions"
    model: minimax-m2.1
    fallback_models: [deepseek-v3.2, qwen3-235b-thinking]
    skills: [_base, coding]
    llm:
      api_key_env: EXECUTOR_API_KEY
      base_url_env: EXECUTOR_BASE_URL

  - id: reviewer
    role: "Peer reviewer — evaluates outputs and scores quality"
    model: deepseek-v3.2
    fallback_models: [minimax-m2.1, qwen3-235b-thinking]
    skills: [_base, review]
    llm:
      api_key_env: REVIEWER_API_KEY
      base_url_env: REVIEWER_BASE_URL
```

Each agent can have its own LLM provider, API key, model, fallback chain, skills, memory settings, and autonomy level.

---

## CLI Commands

```bash
# Interactive mode
python main.py                          # chat loop with rich TUI
python main.py --setup                  # run setup wizard first

# One-shot
python main.py run "Build a REST API"   # execute and exit
python main.py status                   # show task board
python main.py scores                   # reputation scores
python main.py doctor                   # system health check
python main.py gateway                  # start HTTP gateway

# Chat commands (inside interactive mode)
/status         # task board
/scores         # reputation scores
/config         # current agent team
/doctor         # health check
/clear          # clear task history
/help           # all commands

# On-chain
python main.py chain status             # ERC-8004 identity status
python main.py chain init <agent>       # register agent on-chain
python main.py chain balance            # USDC balances

# Evolution
python main.py evolve <agent> confirm   # approve model swap (Path B)
```

---

## API Endpoints

The gateway runs on port **19789** (configurable via `SWARM_GATEWAY_PORT`).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Web dashboard |
| `POST` | `/v1/task` | Submit a task |
| `GET` | `/v1/task/:id` | Get task status |
| `GET` | `/v1/status` | Full task board |
| `GET` | `/v1/scores` | Reputation scores |
| `GET` | `/v1/agents` | Team info |
| `GET` | `/v1/heartbeat` | Agent liveness |
| `GET` | `/v1/usage` | Token usage stats |
| `GET` | `/v1/usage/recent` | Recent API calls |
| `GET` | `/v1/doctor` | Health check |
| `GET` | `/v1/skills` | List skills |
| `PUT` | `/v1/skills/:name` | Create/update skill |
| `GET` | `/v1/logs/:agent_id` | Agent logs |
| `POST` | `/v1/search` | Brave web search |
| `POST` | `/v1/chain/init` | Init agent on-chain |
| `GET` | `/v1/chain/status` | Chain status |

Auth: `Authorization: Bearer <SWARM_GATEWAY_TOKEN>`

---

## Project Structure

```
swarm-dev/
├── main.py                     # CLI entry point (interactive + one-shot)
├── swarm                       # Alternative CLI entry point
├── config/
│   ├── agents.yaml             # Team definition (roles, models, skills)
│   └── chain_contracts.json    # ERC-8004 contract addresses
├── core/
│   ├── orchestrator.py         # Process launcher — spawns N agents
│   ├── agent.py                # BaseAgent — task execution loop
│   ├── task_board.py           # File-locked task lifecycle
│   ├── context_bus.py          # Shared KV store
│   ├── gateway.py              # HTTP API server
│   ├── dashboard.html          # Web dashboard (single-file)
│   ├── skill_loader.py         # Hot-reload markdown skills
│   ├── workflow.py             # Declarative workflow engine
│   ├── heartbeat.py            # Agent liveness detection
│   ├── compaction.py           # Context window compression
│   ├── usage_tracker.py        # Token usage & cost tracking
│   ├── onboard.py              # Interactive setup wizard
│   ├── doctor.py               # System health checks
│   └── ...
├── adapters/
│   ├── llm/                    # FLock · OpenAI · Ollama · Resilience
│   ├── memory/                 # Hybrid · ChromaDB · Episodic · KB
│   └── chain/                  # ERC-8004 · Lit PKP · Gnosis · X.402
├── reputation/
│   ├── scorer.py               # 5-dimension EMA scoring
│   ├── peer_review.py          # Weighted aggregation + anti-gaming
│   ├── evolution.py            # Evolution Engine (Path A/B/C)
│   └── scheduler.py            # Event hooks & reputation updates
├── skills/                     # Markdown skill documents (hot-reload)
│   ├── _base.md                # Core operating principles
│   ├── planning.md             # Planner role skills
│   ├── coding.md               # Executor role skills
│   ├── review.md               # Reviewer role skills
│   └── agent_overrides/        # Per-agent evolved skills
├── workflows/                  # YAML workflow definitions
│   ├── code_review.yaml
│   └── research_report.yaml
├── scripts/                    # Deployment utilities
│   ├── deploy_erc8004.py       # Deploy ERC-8004 contracts
│   └── mint_naga_pkp.py        # Mint Lit Protocol PKP
├── docs/                       # Per-agent cognition profiles
│   ├── _shared/                # Team reference documents
│   ├── planner/cognition.md
│   ├── executor/cognition.md
│   └── reviewer/cognition.md
└── TECHNICAL_SPEC.md           # Full architecture specification
```

**Runtime directories** (auto-created, gitignored):
```
memory/                         # Reputation cache, episodic memory, KB
.task_board.json                # Active task state
.context_bus.json               # Shared KV store
.heartbeats/                    # Agent liveness files
.mailboxes/                     # P2P message inboxes
.logs/                          # Agent process logs
```

---

## Workflows

Define multi-step pipelines in YAML:

```yaml
# workflows/code_review.yaml
name: Code Review Pipeline
steps:
  - id: plan
    role: plan
    description: "Analyze requirements and create implementation plan"
  - id: implement
    role: implement
    description: "Implement based on {{plan.result}}"
    depends_on: [plan]
  - id: review
    role: review
    description: "Review implementation from {{implement.result}}"
    depends_on: [implement]
```

Features: dependency graphs, variable interpolation, approval gates, fan-out/fan-in.

---

## On-Chain Integration

Optional ERC-8004 reputation on Base Sepolia:

```bash
# Deploy contracts
python scripts/deploy_erc8004.py

# Register agents
python main.py chain init planner
python main.py chain init executor
python main.py chain init reviewer

# Check status
python main.py chain status
python main.py chain balance
```

Each agent gets an on-chain identity. Reputation scores sync periodically (max 10 writes/hour, min 5-point delta).

**Stack:** ERC-8004 Identity Registry · Lit Protocol PKP · Gnosis Safe Multi-sig · X.402 Payment Protocol

---

## Requirements

- Python 3.10+
- At least one LLM API key (FLock, OpenAI, or local Ollama)

**Core dependencies:** `httpx`, `pyyaml`, `filelock`, `rich`, `questionary`

**Optional:** `chromadb` (vector memory), `web3` + `eth-account` (on-chain features)

---

## License

MIT
