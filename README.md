# ⬡ Cleo

![version](https://img.shields.io/badge/version-0.1.0-blue)
![python](https://img.shields.io/badge/python-3.10%2B-green)
![license](https://img.shields.io/badge/license-MIT-grey)

**Multi-agent orchestration that plans, executes, and quality-checks your tasks.**

Agents collaborate through file-backed channels, self-claim tasks, peer-review each other, and evolve when performance drops. Process-native, model-agnostic, zero infrastructure.

---

## Install

```bash
# One-liner (recommended)
curl -fsSL https://raw.githubusercontent.com/createpjf/cleo-dev/main/install.sh | bash

# Or: clone manually
git clone https://github.com/createpjf/cleo-dev.git && cd cleo-dev
bash setup.sh
```

> Only one API key needed. `setup.sh` auto-detects local Ollama if running.

---

## Quick Start

```bash
cleo                   # interactive chat
cleo run "your task"   # one-shot
cleo gateway start     # opens dashboard at http://127.0.0.1:19789
```

### What Happens

```
You: "Build a REST API for user management"
  │
  ▼
Leo → decomposes into subtasks
  │
  ▼
Jerry → implements each subtask (streaming output)
  │
  ▼
Alic → reviews quality, suggests fixes
  │
  ▼
Result → with cost estimate and agent attribution
```

The dashboard shows this in real-time: streaming output, subtask tree, agent status, cost per task.

---

## CLI

```bash
cleo --version                   # show version
cleo --json status               # machine-readable output
```

| Command | What it does |
|---------|-------------|
| `cleo` | Interactive chat mode |
| `cleo onboard` | Setup wizard (re-run anytime) |
| `cleo configure` | Re-configure (alias for onboard) |
| `cleo configure --section <name>` | Jump to a specific config section |
| `cleo run "..."` | One-shot task |
| `cleo status` | Task board (`--json` for JSON output) |
| `cleo scores` | Reputation scores (`--json`) |
| `cleo doctor` | System health check (`--repair` / `--deep` / `--export`) |
| `cleo security audit` | Security audit (`--deep` / `--fix`) |
| `cleo gateway start` | Start dashboard |
| `cleo channels list` | List channel adapters |
| `cleo channels pairing list` | View pairing codes |
| `cleo agents create <name>` | Create agent (`--template coder\|researcher\|debugger\|doc_writer`) |
| `cleo workflow list` | List available workflows |
| `cleo workflow run <name>` | Run a workflow |
| `cleo export <task_id>` | Export task results (`--format md\|json`) |
| `cleo cron list` | Scheduled jobs |
| `cleo logs` | View agent logs (`-f` follow / `--agent` / `--level`) |
| `cleo search <query>` | Full-text search across memory and docs |
| `cleo memory status` | Memory system overview |
| `cleo plugins list` | Plugin management |
| `cleo config get/set/unset` | Read/write configuration values |
| `cleo chain status` | On-chain identity status |
| `cleo completions bash\|zsh` | Generate shell completions |
| `cleo update` | Pull latest from GitHub |
| `cleo install` / `cleo uninstall` | Install / remove CLI & daemon |

### Configure Sections

`cleo configure --section <name>` lets you jump directly to a specific section:

`model` · `agents` · `skills` · `skill_deps` · `memory` · `resilience` · `compaction` · `channels` · `gateway` · `chain` · `tools` · `health`

**Chat commands** (inside interactive mode): `/status` `/scores` `/config` `/configure` `/doctor` `/workflows` `/save` `/templates` `/cancel` `/clear` `/help`

---

## Channels

Cleo supports multi-channel messaging — connect your agents to chat platforms:

| Channel | Setup |
|---------|-------|
| **Telegram** | Set `TELEGRAM_BOT_TOKEN` in `.env`, enable in config |
| **Discord** | Set `DISCORD_BOT_TOKEN` in `.env`, enable in config |
| **Slack** | Set `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` in `.env` |
| **Feishu** | Set `FEISHU_APP_ID` + `FEISHU_APP_SECRET` in `.env` |

### Pairing & Authentication

Channels support `pairing` auth mode — first-time users receive a verification code automatically. Send the code back to verify identity. No admin intervention needed.

```bash
cleo configure --section channels   # configure channel settings
cleo channels status                # check adapter status
cleo channels pairing list          # view pending/active codes
```

### File Delivery

Agents can send files (PDF, Excel, images) directly through chat channels via the `send_file` tool. Files are delivered through the gateway HTTP proxy with automatic fallback to a polling queue.

---

## Security

```bash
cleo security audit          # basic security checks
cleo security audit --deep   # include file permissions + git history scan
cleo security audit --fix    # auto-fix common issues
```

Checks include:
- `.env` file permissions (not world-readable)
- No API keys in config files
- Gateway token configured
- Channel auth mode (pairing vs open)
- `.gitignore` coverage for sensitive files
- Deep: directory permissions, git history for leaked secrets

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
| **Tools** | 30+ built-in tools across 8 groups (web, fs, memory, task, automation, messaging, media, browser) |
| **Plugins** | Drop-in extensions: `cleo plugins install <path-or-git-url>` |

---

## Agent Tools

Agents can invoke tools during task execution. 30+ built-in tools organized by group:

| Group | Tools | Highlights |
|-------|-------|------------|
| **Web** | `web_search`, `web_fetch` | Brave + Perplexity dual-provider, locale params, markdown extraction, 15-min cache |
| **Filesystem** | `read_file`, `write_file`, `edit_file`, `list_dir` | Project-scoped, safe find-and-replace edits |
| **Memory** | `memory_search`, `memory_save`, `kb_search`, `kb_write` | Episodic memory + shared Zettelkasten KB |
| **Task** | `task_create`, `task_status` | Sub-task creation and status queries |
| **Automation** | `exec`, `cron`, `process` | Approval-gated shell, scheduled jobs |
| **Media** | `screenshot`, `notify`, `tts`, `transcribe`, `generate_doc` | Desktop capture, TTS, speech-to-text, document generation (PDF/Excel/Word) |
| **Messaging** | `send_file`, `send_mail` | File delivery via chat channels, inter-agent mailbox |
| **Browser** | `browser_open`, `browser_screenshot`, `browser_click`, `browser_type` | Headless Chromium for JS-rendered pages |
| **Skills** | `check_skill_deps`, `install_skill_cli`, `search_skills`, `install_remote_skill` | Skill dependency management and registry |

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
cleo workflow run code_review --input "Review auth module"
cleo workflow run bug_fix --input "Login fails on empty password"
cleo workflow run documentation --input "Write API docs for /users endpoint"
cleo workflow run brainstorm --input "Ways to improve onboarding UX"
cleo workflow run research_report --input "Compare React vs Vue in 2025"
```

Create your own: drop a YAML file in `workflows/`. See existing templates for the format.

---

## Dashboard

`cleo gateway start` opens the web dashboard at `http://127.0.0.1:19789`:

- **Overview** — Real-time streaming output, subtask tree, agent status chips
- **Agents** — Cards with model, skills, reputation sparkline; click for detail popup (Overview + Edit tabs)
- **Skills** — Full CRUD for skill documents
- **Tools** — Built-in tools with availability status
- **Usage** — Token counts, cost breakdown by agent and model
- **Logs** — Per-agent logs with level filter
- **Health** — Diagnostic checks (same as `cleo doctor`)

---

## Plugins

Extend Cleo with drop-in plugins:

```bash
cleo plugins list               # list installed plugins
cleo plugins install ./my-plugin   # install from local path
cleo plugins install https://github.com/user/plugin.git  # from git
cleo plugins remove <name>      # remove a plugin
cleo plugins enable/disable <name>  # toggle without removing
cleo plugins doctor             # check plugin health
```

Plugins can add skills, tools, and CLI commands. See `plugins/hello-world/` for the template.

---

## Configuration

All config lives in `config/agents.yaml` (auto-generated by `cleo onboard`):

```yaml
llm:
  provider: flock

agents:
  - id: leo
    role: "Strategic planner — decomposes tasks"
    model: minimax-m2.5
    skills: [_base, planning]
  - id: jerry
    role: "Implementation agent — writes solutions"
    model: minimax-m2.5
    skills: [_base, coding]
    tools:
      profile: coding
  - id: alic
    role: "Advisor — reviews quality"
    model: minimax-m2.5
    skills: [_base, review]
```

Each agent can have its own provider, API key, model, fallback chain, skills, and tool profile.

### Environment Variables

Key settings in `.env`:

| Variable | Description |
|----------|-------------|
| `FLOCK_API_KEY` | FLock API key |
| `OPENAI_API_KEY` | OpenAI API key (also used for embeddings) |
| `MINIMAX_API_KEY` | MiniMax API key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `DISCORD_BOT_TOKEN` | Discord bot token |
| `CLEO_GATEWAY_PORT` | Gateway port (default: 19789) |
| `CLEO_GATEWAY_TOKEN` | Gateway bearer token |
| `BRAVE_SEARCH_API_KEY` | Brave Search API key |
| `PERPLEXITY_API_KEY` | Perplexity API key |

---

## FAQ / Troubleshooting

**"API key not set"** — Run `cleo onboard` or edit `.env`.

**"Cannot reach LLM"** — Check your internet connection and Base URL. Run `cleo doctor` for diagnostics.

**"Port 19789 in use"** — Another gateway is running. Use `cleo gateway stop` first, or set `CLEO_GATEWAY_PORT` in `.env`.

**Tasks stuck in "claimed"** — Agent may have crashed. Tasks auto-recover after 5 min timeout, or use `/cancel` to manually cancel.

**How to use Ollama (free, local)?** — Install Ollama, pull a model (`ollama pull llama3`), then run `cleo onboard`. It auto-detects Ollama.

**How to add a new agent?** — `cleo agents create my_agent --template coder`

**Telegram bot not responding?** — Run `cleo doctor` to check channel status. Ensure `TELEGRAM_BOT_TOKEN` is set and the channel is enabled. New users are auto-verified via pairing codes.

**File sending fails?** — Ensure `cleo gateway start` is running. Send a message from the chat channel first to establish a session, then retry.

**Missing Python packages?** — `cleo onboard` auto-detects missing core dependencies and offers to install them.

---

## API Endpoints

Gateway runs on port **19789** (`CLEO_GATEWAY_PORT`). Auth: `Authorization: Bearer <token>`.

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
| `GET` | `/v1/channels` | Channel adapter status |
| `GET` | `/health` | Gateway health check |

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
 │  Leo  │ ──────► │ Jerry  │ ──────►│ Alic   │    Agents
 └───┬───┘         └───┬────┘        └───┬────┘
     │                  │                  │
     ▼                  ▼                  ▼
 ┌─────────────────────────────────────────────┐
 │  TaskBoard  ·  ContextBus  ·  Mailbox       │  File-backed
 │  (file-lock)   (shared KV)   (P2P JSONL)    │  coordination
 └──────────────────┬──────────────────────────┘
                    │
 ┌──────────────────▼──────────────────────────┐
 │  Tools (30+)  ·  Cron Scheduler             │
 │  Memory (episodic + KB)  ·  Skills          │  Capabilities
 │  Plugins  ·  Browser  ·  TTS/STT            │
 └──────────────────┬──────────────────────────┘
                    │
 ┌──────────────────▼──────────────────────────┐
 │  Reputation Scorer  ·  Peer Review          │
 │  Evolution Engine (Path A / B / C)          │  Reputation
 └──────────────────┬──────────────────────────┘
                    │
 ┌──────────────────▼──────────────────────────┐
 │  Channels (Telegram/Discord/Slack/Feishu)   │
 │  Gateway (HTTP API + SSE + Dashboard)       │  Delivery
 └──────────────────┬──────────────────────────┘
                    │
 ┌──────────────────▼──────────────────────────┐
 │  ERC-8004 Identity  ·  Lit PKP              │
 │  Gnosis Safe  ·  X.402 Payment              │  On-chain
 └─────────────────────────────────────────────┘
```

### Project Structure

```
cleo-dev/
├── main.py                     # CLI entry point + argparse tree
├── install.sh                  # Remote one-liner installer
├── setup.sh                    # Local dev setup
├── config/agents.yaml          # Team definition
├── core/
│   ├── orchestrator.py         # Process launcher + persistent pool
│   ├── agent.py                # Task execution loop + tool loop
│   ├── task_board.py           # File-locked task lifecycle
│   ├── tools.py                # 30+ built-in agent tools
│   ├── gateway.py              # HTTP API + SSE + dashboard
│   ├── workflow.py             # Declarative workflow engine
│   ├── onboard.py              # Setup wizard + dependency checker
│   ├── cron.py                 # Scheduled job engine
│   ├── doctor.py               # Health checks + diagnostics
│   ├── config_manager.py       # Config CRUD operations
│   ├── plugin_sdk.py           # Plugin system SDK
│   ├── completions.py          # Shell completion generator
│   └── i18n.py                 # Internationalization
├── cli/
│   ├── __init__.py             # Lazy-load command dispatcher
│   ├── chat.py                 # Interactive chat mode
│   ├── channels_cmd.py         # Channel management
│   ├── security_cmd.py         # Security audit
│   ├── doctor_cmd.py           # Doctor diagnostics
│   ├── plugins_cmd.py          # Plugin management
│   ├── logs_cmd.py             # Log viewer
│   └── ...                     # Other CLI modules
├── adapters/
│   ├── llm/                    # FLock · OpenAI · MiniMax · Ollama
│   ├── memory/                 # Hybrid · ChromaDB · Episodic · KB
│   ├── channels/               # Telegram · Discord · Slack · Feishu
│   ├── voice/                  # TTS · Speech-to-text
│   ├── browser/                # Headless Chromium automation
│   └── chain/                  # ERC-8004 · Lit PKP · Gnosis
├── reputation/                 # Scoring + evolution engine
├── skills/                     # Hot-reload markdown skills
├── workflows/                  # YAML workflow templates
├── plugins/                    # Drop-in plugin extensions
├── scripts/                    # Dev scripts (LOC check, etc.)
└── docs/                       # Per-agent cognition profiles
```

</details>

---

## Requirements

- Python 3.10+
- One LLM API key (FLock, OpenAI, MiniMax, or local Ollama)

**Core deps:** `httpx` `pyyaml` `filelock` `rich` `questionary`
**Optional:** `chromadb` (vector memory) · `python-telegram-bot` (Telegram) · `discord.py` (Discord) · `web3` (on-chain features)

`cleo onboard` auto-checks core dependencies and offers to install missing packages.

---

## License

MIT
