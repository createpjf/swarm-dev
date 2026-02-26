# ⬡ Cleo

![version](https://img.shields.io/badge/version-0.04-blue)
![python](https://img.shields.io/badge/python-3.11%2B-green)
![license](https://img.shields.io/badge/license-MIT-grey)

**Multi-agent system that plans, executes, and quality-checks your tasks — with on-demand agent lifecycle, hybrid memory, and structured protocols.**

Three specialized agents (Leo 🧠 planner, Jerry 🤚 executor, Alic 👁️ reviewer) collaborate via file-backed coordination, self-claim tasks, peer-review outputs, and evolve on performance drops. LazyRuntime starts agents on demand, saving ~600MB when idle.

---

## Architecture

```
User ──► Telegram / Discord / 飞书 / Slack / HTTP API / Dashboard
              │
              ▼
         Orchestrator
         ├─ TaskRouter ──► DIRECT_ANSWER (Leo only)
         │                 MAS_PIPELINE  (Leo → Jerry → Alic → Leo)
         ├─ LazyRuntime ── on-demand agent processes
         └─ TaskBoard ──── file-locked JSON state machine
              │
     ┌────────┼────────┐
     ▼        ▼        ▼
   Leo 🧠  Jerry 🤚  Alic 👁️    (independent OS processes)
     │        │        │
     ▼        ▼        ▼
   MiniMax LLM · HybridMemory · EpisodicMemory · ContextBus
```

### Agent Roles

| Agent | Role | Tools | Model |
|-------|------|-------|-------|
| **Leo** | Planner — route, decompose, synthesize | `minimal` | MiniMax-M2.5 |
| **Jerry** | Executor — code, search, build | `coding` | MiniMax-M2.5 |
| **Alic** | Reviewer — 5-dimension scoring, quality reports | `minimal` | MiniMax-M2.5 |

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/createpjf/cleo-dev/main/install.sh | bash
# or
git clone https://github.com/createpjf/cleo-dev.git && cd cleo-dev && bash setup.sh
```

## Quick Start

```bash
cleo                   # interactive chat
cleo run "your task"   # one-shot
cleo gateway start     # dashboard at http://127.0.0.1:19789
cleo doctor            # system health check
```

---

## Core Systems

### Runtime Abstraction (`core/runtime/`)

Three modes, switchable via `config/agents.yaml`:

| Mode | How it works | Status |
|------|-------------|--------|
| **`lazy`** | Only `always_on` agents start; others launch on demand when TaskBoard has matching pending tasks. Idle agents auto-stop after `idle_shutdown` seconds. | **Active** |
| `process` | One `mp.Process` per agent, all start upfront. | Stable |
| `in_process` | `asyncio.Task` per agent, single process. | Experimental |

### TaskBoard (`core/task_board.py`)

File-locked JSON (`.task_board.json`) with state machine:

```
pending → claimed → review → completed
                      ↓
                  critique → claimed (rework)
```

Role-based routing via `_ROLE_TO_AGENTS` mapping. Timeout recovery: claimed > 180s or review > 300s → auto-reset to pending.

### Structured Protocols (`core/protocols.py`)

- **SubTaskSpec** — Leo → Jerry task ticket: objective, constraints, tool_hint, complexity
- **CritiqueSpec** — Alic's 5-dimension review: accuracy (30%), completeness (20%), technical (20%), calibration (20%), efficiency (10%)
- **TaskRouter** — Heuristic classifier: signal words → `DIRECT_ANSWER` vs `MAS_PIPELINE`

### Memory System

| Layer | Module | Description |
|-------|--------|-------------|
| **Hybrid Search** | `adapters/memory/hybrid.py` | ChromaDB vectors + self-contained BM25 with RRF fusion |
| **Episodic Memory** | `adapters/memory/episodic.py` | 3-layer progressive: L0 atomic (~100 tok) → L1 overview (~500 tok) → L2 full detail |
| **Knowledge Base** | `adapters/memory/knowledge_base.py` | Shared Zettelkasten-style notes + insights |
| **Context Bus** | `core/context_bus.py` | 4-layer KV store (TASK/SESSION/SHORT/LONG) with TTL |
| **Memory Consolidation** | `adapters/memory/consolidator.py` | 3-phase pipeline: cluster old episodes (>3d) → compress → promote to KB |

### Episode Scoring

Two-stage quality scoring for every task:

1. **Baseline** — Agent self-assigns at episode creation (`success=8, partial=5, other=2`)
2. **Critique backfill** — Alic's score retroactively written to evaluated agent's episode via `update_episode_score()`

### SSE Streaming (`core/task_board.py` + `core/gateway.py`)

Real-time token streaming for chat responses:

- Per-task `.stream` files with lockless append + cursor-based reads
- `GET /v1/stream/:task_id` — Server-Sent Events endpoint
- Auto-cleanup on task complete/fail/cancel

### Dashboard (`core/dashboard.html`)

Web UI at `http://127.0.0.1:19789`:

- **Session sidebar** — multi-session conversation management
- **KB grid** — horizontal 2-column layout with scrollable cards
- **Tab badges** — live count indicators on Episodes/Cases/KB tabs
- **Episode table** — score + timestamp columns
- **Daily log** — auto-filters tool call artifacts
- **SSE streaming** — real-time token-by-token chat display

### Tools (37 tools × 10 groups)

`web` · `fs` · `memory` · `task` · `automation` · `skill` · `browser` · `media` · `messaging` · `a2a`

Access control: profiles (`minimal` / `coding` / `full`) + per-agent allow/deny lists. Audit log at `.logs/tool_audit.log`.

`generate_doc` supports 8 output formats: PDF, DOCX, XLSX, PPTX, CSV, TXT, MD, HTML.

### Channels

| Channel | Auth | Config |
|---------|------|--------|
| Telegram | Pairing code | `TELEGRAM_BOT_TOKEN` |
| Discord | Pairing code | `DISCORD_BOT_TOKEN` |
| Feishu | Pairing code | `FEISHU_APP_ID` + `FEISHU_APP_SECRET` |
| Slack | Pairing code | `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` |

### Reputation (`reputation/scorer.py`)

5-dimension EMA scoring: `new = 0.3 × signal + 0.7 × old`. Composite = weighted sum. Optional blockchain sync to ERC-8004 registry.

### Provider Router (`core/provider_router.py`)

Cross-provider LLM failover: MiniMax → OpenAI → Ollama. Strategies: `latency` / `cost` / `preference` / `round_robin`. Circuit breaker per provider.

---

## API

Gateway on port **19789** (+ WebSocket on **19790**). Auth: `Authorization: Bearer <token>`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/task` | Submit task |
| GET | `/v1/task/:id` | Task status |
| GET | `/v1/status` | Full task board |
| GET | `/v1/scores` | Reputation scores |
| GET | `/v1/agents` | Agent info |
| GET | `/v1/doctor` | Health check |
| GET | `/v1/skills` | Skill list |
| GET | `/v1/usage` | Token usage |
| GET | `/v1/memory/*` | Memory status / episodes / cases |
| GET | `/v1/chain/*` | Blockchain status / balance |
| POST | `/v1/cron` | Create scheduled job |
| GET | `/v1/stream/:id` | SSE token stream |
| GET/POST | `/v1/sessions` | Dashboard sessions |
| GET/PUT/DELETE | `/v1/sessions/:id` | Session CRUD |
| GET | `/health` | Gateway health |

30+ endpoints total — see [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full list.

---

## Configuration

All config in `config/agents.yaml`:

```yaml
runtime:
  mode: lazy
  always_on: [leo]
  idle_shutdown: 300

llm:
  provider: minimax

memory:
  backend: hybrid
  embedding:
    provider: chromadb_default
  episodic:
    enabled: true
  knowledge_base:
    enabled: true

channels:
  telegram:
    enabled: true
    auth_mode: pairing
```

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for full config reference.

---

## Project Structure

```
cleo-dev/
├── main.py                    # CLI entry
├── config/agents.yaml         # All configuration
├── core/
│   ├── orchestrator.py        # Task lifecycle engine (~1900 lines)
│   ├── agent.py               # BaseAgent + AgentConfig
│   ├── runtime/               # ProcessRuntime / LazyRuntime / InProcessRuntime
│   ├── task_board.py          # File-locked task state machine
│   ├── context_bus.py         # Layered KV store
│   ├── protocols.py           # SubTaskSpec, CritiqueSpec, ToolCategory
│   ├── task_router.py         # DIRECT_ANSWER vs MAS_PIPELINE
│   ├── tools.py               # 37 built-in tools
│   ├── gateway.py             # HTTP REST API (30+ endpoints)
│   ├── ws_gateway.py          # WebSocket 1Hz state push
│   ├── provider_router.py     # Cross-provider LLM failover
│   ├── cron.py                # Scheduled jobs
│   └── doctor.py              # Health check + auto-repair
├── adapters/
│   ├── llm/minimax.py         # MiniMax SSE streaming + truncation recovery
│   ├── memory/                # hybrid, episodic, embedding, consolidator
│   ├── channels/              # manager, telegram, discord, feishu, slack
│   └── memo/                  # Memo Protocol integration
├── reputation/
│   ├── scorer.py              # 5-dim EMA scoring
│   └── textgrad.py            # TextGrad critique → skill patches
├── skills/                    # 56+ hot-reload markdown skills
├── tests/                     # 399 tests
├── cli/memo_cmd.py            # Memo CLI
└── docs/                      # Architecture + product docs
    ├── ARCHITECTURE.md
    ├── Cleo_V0.01_Product_Logic.md
    ├── Cleo_V0.01_Product_Narrative.md
    └── Cleo_V0.01_Technical_Architecture.md
```

---

## Requirements

- Python 3.11+
- One LLM API key (MiniMax, OpenAI, or local Ollama)

**Core:** `pyyaml` `filelock` `requests` `chromadb` `websockets`
**Optional:** `python-telegram-bot` · `discord.py` · `web3` · `rich` · `python-pptx` · `slack-sdk`

---

## Docs

- **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** — Full technical architecture with code details
- **[Product Logic](docs/Cleo_V0.01_Product_Logic.md)** — Product design and decision logic
- **[Product Narrative](docs/Cleo_V0.01_Product_Narrative.md)** — Product vision and narrative
- **[Technical Architecture V0.01](docs/Cleo_V0.01_Technical_Architecture.md)** — Original technical architecture

---

## License

MIT
