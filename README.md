# ⬡ Swarm

![version](https://img.shields.io/badge/version-0.1.0-blue)
![python](https://img.shields.io/badge/python-3.10%2B-green)
![license](https://img.shields.io/badge/license-MIT-grey)

**Multi-agent orchestration that plans, executes, and quality-checks your tasks.**

Agents collaborate through file-backed channels, self-claim tasks, peer-review each other, and evolve when performance drops. Process-native, model-agnostic, zero infrastructure.

<!-- demo gif placeholder: record a 30s walkthrough and replace this comment -->

---

## Install

```bash
# One-liner (recommended)
curl -fsSL https://raw.githubusercontent.com/createpjf/swarm-dev/main/install.sh | bash

# Or: clone manually
git clone https://github.com/createpjf/swarm-dev.git && cd swarm-dev
bash setup.sh
```

> Only one API key needed. `setup.sh` auto-detects local Ollama if running.

---

## Quick Start

```bash
swarm                  # interactive chat
swarm run "your task"  # one-shot
swarm gateway start    # opens dashboard at http://127.0.0.1:19789
```

### What Happens

```
You: "Build a REST API for user management"
  │
  ▼
Planner → decomposes into subtasks
  │
  ▼
Executor → implements each subtask (streaming output)
  │
  ▼
Advisor → reviews quality, suggests fixes
  │
  ▼
Result → with cost estimate and agent attribution
```

The dashboard shows this in real-time: streaming output, subtask tree, agent status, cost per task.

---

## CLI

```bash
swarm --version                  # show version
swarm --json status              # machine-readable output
```

| Command | What it does |
|---------|-------------|
| `swarm` | Interactive chat mode |
| `swarm onboard` | Setup wizard (re-run anytime) |
| `swarm run "..."` | One-shot task |
| `swarm status` | Task board (`--json` for JSON output) |
| `swarm scores` | Reputation scores (`--json`) |
| `swarm doctor` | System health check (`--json` / `--repair` / `--deep`) |
| `swarm gateway start` | Start dashboard |
| `swarm agents create <name>` | Create agent (`--template coder\|researcher\|debugger\|doc_writer`) |
| `swarm workflow list` | List available workflows |
| `swarm workflow run <name>` | Run a workflow |
| `swarm export <task_id>` | Export task results (`--format md\|json`) |
| `swarm cron list` | Scheduled jobs |
| `swarm chain status` | On-chain identity status |
| `swarm update` | Pull latest from GitHub |
| `swarm install` | Install from GitHub |
| `swarm uninstall` | Remove CLI & daemon |

**Chat commands** (inside interactive mode): `/status` `/scores` `/config` `/configure` `/doctor` `/workflows` `/save` `/templates` `/cancel` `/clear` `/help`

---

## Core Concepts

| Concept | Summary |
|---------|---------|
| **TaskBoard** | File-locked task lifecycle: create → claim → review → complete |
| **ContextBus** | Shared KV store — agent outputs feed into other agents' prompts |
| **Reputation** | 5-dimension EMA scoring (quality 30%, completion 25%, improvement 25%, consistency 10%, review accuracy 10%) |
| **Evolution** | Score < 40 triggers: Path A (prompt upgrade) → B (model swap) → C (role restructure) |
| **Skills** | Hot-reload markdown files in `skills/`. Edit → next task picks up changes |
| **Workflows** | YAML pipelines with dependency graphs, variable passing, approval gates |
| **Memory** | 3-layer episodic (L0 index → L1 summary → L2 full) + shared Zettelkasten KB |
| **Tools** | 18 built-in tools across 6 groups (web, fs, memory, task, automation, messaging) |

---

## Agent Tools

Agents can invoke tools during task execution. 18 built-in tools organized by group:

| Group | Tools | Highlights |
|-------|-------|------------|
| **Web** | `web_search`, `web_fetch` | Brave + Perplexity dual-provider, locale params, markdown extraction, 15-min cache |
| **Filesystem** | `read_file`, `write_file`, `edit_file`, `list_dir` | Project-scoped, safe find-and-replace edits |
| **Memory** | `memory_search`, `memory_save`, `kb_search`, `kb_write` | Episodic memory + shared Zettelkasten KB |
| **Task** | `task_create`, `task_status` | Sub-task creation and status queries |
| **Automation** | `exec`, `cron`, `process` | Approval-gated shell, scheduled jobs |
| **Media** | `screenshot`, `notify` | Desktop capture, macOS notifications |
| **Messaging** | `send_mail` | Inter-agent mailbox communication |

**Web search** supports `country`, `search_lang`, `ui_lang` locale parameters. Auto-detects Brave Search or Perplexity Sonar based on available API keys, with automatic fallback.

**Web fetch** supports `extract_mode: "text"` (plain text) or `"markdown"` (preserves headings, links, lists). Blocks private/internal hostnames for security.

Access control via profiles (`minimal`, `coding`, `full`) and per-agent allow/deny lists:

```yaml
agents:
  - id: researcher
    tools:
      profile: "full"
      deny: ["exec", "write_file"]
```

---

## Workflows

Built-in templates — run without setup:

```bash
swarm workflow run code_review --input "Review auth module"
swarm workflow run bug_fix --input "Login fails on empty password"
swarm workflow run documentation --input "Write API docs for /users endpoint"
swarm workflow run brainstorm --input "Ways to improve onboarding UX"
swarm workflow run research_report --input "Compare React vs Vue in 2025"
```

Create your own: drop a YAML file in `workflows/`. See existing templates for the format.

---

## Dashboard

`swarm gateway start` opens the web dashboard at `http://127.0.0.1:19789`:

- **Overview** — Real-time streaming output, subtask tree, agent status chips
- **Agents** — Cards with model, skills, reputation sparkline; click for detail popup (Overview + Edit tabs)
- **Skills** — Full CRUD for skill documents
- **Tools** — 18 built-in tools across 6 groups with availability status
- **Usage** — Token counts, cost breakdown by agent and model
- **Logs** — Per-agent logs with level filter
- **Health** — Diagnostic checks (same as `swarm doctor`)

---

## Configuration

All config lives in `config/agents.yaml` (auto-generated by `swarm onboard`):

```yaml
llm:
  provider: flock

agents:
  - id: planner
    role: "Strategic planner — decomposes tasks"
    model: minimax-m2.1
    skills: [_base, planning]
  - id: executor
    role: "Implementation agent — writes solutions"
    model: minimax-m2.1
    skills: [_base, coding]
    tools:
      profile: coding
  - id: reviewer
    role: "Advisor — reviews quality"
    model: deepseek-v3.2
    skills: [_base, review]
```

Each agent can have its own provider, API key, model, fallback chain, skills, and tool profile.

---

## FAQ / Troubleshooting

**"API key not set"** — Run `swarm onboard` or edit `.env` line 7.

**"Cannot reach LLM"** — Check your internet connection and Base URL. Run `swarm doctor` for diagnostics.

**"Port 19789 in use"** — Another gateway is running. Use `swarm gateway stop` first, or set `SWARM_GATEWAY_PORT` in `.env`.

**Tasks stuck in "claimed"** — Agent may have crashed. Tasks auto-recover after 5 min timeout, or use `/cancel` to manually cancel.

**How to use Ollama (free, local)?** — Install Ollama, pull a model (`ollama pull llama3`), then run `swarm onboard`. It auto-detects Ollama.

**How to add a new agent?** — `swarm agents create my_agent --template coder`

---

## API Endpoints

Gateway runs on port **19789** (`SWARM_GATEWAY_PORT`). Auth: `Authorization: Bearer <token>`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Web dashboard |
| `POST` | `/v1/task` | Submit a task |
| `GET` | `/v1/task/:id` | Task status |
| `GET` | `/v1/status` | Full task board |
| `GET` | `/v1/scores` | Reputation scores |
| `GET` | `/v1/scores/history` | Score trend data |
| `GET` | `/v1/agents` | Team info + current task + recent logs |
| `POST` | `/v1/agents` | Create a new agent |
| `PUT` | `/v1/agents/:id` | Update agent config |
| `DELETE` | `/v1/agents/:id` | Delete an agent |
| `GET` | `/v1/heartbeat` | Agent liveness |
| `GET` | `/v1/usage` | Token usage stats |
| `GET` | `/v1/doctor` | Health check |
| `GET` | `/v1/skills` | List skills |
| `PUT` | `/v1/skills/:name` | Create/update skill |
| `GET` | `/v1/tools` | List available tools |
| `POST` | `/v1/search` | Web search |

---

<details>
<summary><strong>Architecture</strong></summary>

```
┌──────────────────────────────────────────────────────┐
│                    Orchestrator                       │
│    (spawns each agent as an independent OS process)   │
└────┬──────────────────┬──────────────────┬───────────┘
     │                  │                  │
 ┌───▼───┐         ┌───▼────┐        ┌───▼────┐
 │Planner│ ──────► │Executor│ ──────►│Advisor │    Agents
 └───┬───┘         └───┬────┘        └───┬────┘
     │                  │                  │
     ▼                  ▼                  ▼
 ┌─────────────────────────────────────────────┐
 │  TaskBoard  ·  ContextBus  ·  Mailbox       │  File-backed
 │  (file-lock)   (shared KV)   (P2P JSONL)    │  coordination
 └──────────────────┬──────────────────────────┘
                    │
 ┌──────────────────▼──────────────────────────┐
 │  Tools (18 built-in)  ·  Cron Scheduler     │
 │  Memory (episodic + KB)  ·  Skills          │  Capabilities
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

### Project Structure

```
swarm-dev/
├── main.py                     # CLI entry point
├── install.sh                  # Remote one-liner installer
├── setup.sh                    # Local dev setup
├── config/agents.yaml          # Team definition
├── core/
│   ├── orchestrator.py         # Process launcher
│   ├── agent.py                # Task execution loop + tool loop
│   ├── task_board.py           # File-locked task lifecycle
│   ├── tools.py                # 18 built-in agent tools
│   ├── gateway.py              # HTTP API + dashboard
│   ├── workflow.py             # Declarative workflow engine
│   ├── onboard.py              # Setup wizard
│   ├── cron.py                 # Scheduled job engine
│   └── doctor.py               # Health checks
├── adapters/
│   ├── llm/                    # FLock · OpenAI · Ollama
│   ├── memory/                 # Hybrid · ChromaDB · Episodic · KB
│   └── chain/                  # ERC-8004 · Lit PKP · Gnosis
├── reputation/                 # Scoring + evolution engine
├── skills/                     # Hot-reload markdown skills
├── workflows/                  # YAML workflow templates
└── docs/                       # Per-agent cognition profiles
```

</details>

---

## Requirements

- Python 3.10+
- One LLM API key (FLock, OpenAI, or local Ollama)

**Core deps:** `httpx` `pyyaml` `filelock` `rich` `questionary`
**Optional:** `chromadb` (vector memory) · `web3` (on-chain features)

---

## License

MIT
