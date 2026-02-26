# Cleo — Technical Architecture V0.01

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User Interfaces                             │
│   Telegram │ Discord │ Slack │ Feishu │ Gateway API (HTTP)          │
└──────┬─────┴────┬────┴───┬──┴────┬───┴──────────┬──────────────────┘
       │          │        │       │               │
       ▼          ▼        ▼       ▼               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      ChannelManager                                 │
│   - Persistent orchestrator pool                                    │
│   - Sequential task processing per session                         │
│   - Native file delivery (photo/document/voice)                    │
│   - Abort detection (multilingual stop phrases)                    │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Orchestrator                                  │
│   - Reads agents.yaml                                              │
│   - Spawns one OS process per agent (multiprocessing.Process)      │
│   - Signal handling (SIGTERM/SIGINT → graceful shutdown)           │
│   - WakeupBus for zero-delay subtask dispatch                      │
└──────┬──────────────────┬──────────────────┬───────────────────────┘
       │                  │                  │
       ▼                  ▼                  ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Leo Process │  │ Jerry Process│  │ Alic Process │
│  (Planner)   │  │ (Executor)   │  │ (Reviewer)   │
│              │  │              │  │              │
│  _agent_loop │  │  _agent_loop │  │  _agent_loop │
│  BaseAgent   │  │  BaseAgent   │  │  BaseAgent   │
│  ResilientLLM│  │  ResilientLLM│  │  ResilientLLM│
│  HybridMemory│  │  HybridMemory│  │  HybridMemory│
│  EpisodicMem │  │  EpisodicMem │  │  EpisodicMem │
│  KnowledgeBase│ │  KnowledgeBase│ │  KnowledgeBase│
│  SkillLoader │  │  SkillLoader │  │  SkillLoader │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                  │
       ▼                 ▼                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    File-Backed Coordination                         │
│                                                                     │
│   .task_board.json  ← TaskBoard (file-locked)                      │
│   .context_bus.json ← ContextBus (4-layer KV)                     │
│   .mailboxes/*.jsonl ← Inter-agent P2P messages                   │
│   .planner_subtasks.json ← Parent→subtask mapping                  │
│   .task_signals/    ← Lightweight new-task notifications           │
│   .heartbeats/      ← Agent liveness tracking                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Process Model

### 2.1 One Process Per Agent

Each agent runs in its own OS process via Python's `multiprocessing.Process`:

```python
for agent_def in config["agents"]:
    p = mp.Process(
        target=_agent_process,
        args=(cfg_dict, agent_def, config, wakeup),
        name=agent_def["id"],
        daemon=False,
    )
    p.start()
```

**Benefits:**
- True parallelism (no GIL contention)
- Process isolation (one agent crash doesn't kill others)
- Independent log files (`.logs/{agent_id}.log`)
- Clean signal handling per process

### 2.2 File-Backed Coordination

All inter-process coordination uses JSON files with `filelock`:

| File | Purpose | Lock |
|------|---------|------|
| `.task_board.json` | Task lifecycle state | `.task_board.lock` |
| `.context_bus.json` | Cross-agent context sharing | `.context_bus.lock` |
| `.mailboxes/{id}.jsonl` | P2P messages (JSONL append) | `.mailboxes/{id}.jsonl.lock` |
| `.planner_subtasks.json` | Parent→subtask mapping for closeout | `.planner_subtasks.lock` |
| `.task_signals/` | Lightweight task creation notifications | No lock (write-once files) |
| `.heartbeats/{id}.json` | Agent liveness tracking | No lock (single writer) |

**Design rationale:**
- No external database required (SQLite, Redis, etc.)
- Crash-resilient: files persist across restarts
- Debuggable: plain JSON, human-readable
- Atomic operations via `filelock`

### 2.3 WakeupBus

Event-driven agent wakeup mechanism for zero-delay subtask dispatch:

```python
class WakeupBus:
    def register(agent_id)       # Register agent for wakeup events
    def wake_all()               # Signal all agents (after subtask creation)
    async def async_wait(id, timeout)  # Block until wakeup or timeout
```

When Leo creates subtasks, `wakeup.wake_all()` immediately signals Jerry and Alic instead of waiting for the next poll cycle. This reduces subtask pickup latency from seconds to milliseconds.

### 2.4 Heartbeat System

Each agent process writes a heartbeat file (`.heartbeats/{id}.json`) periodically:

```json
{
  "agent_id": "jerry",
  "status": "working",
  "task_id": "abc123",
  "progress": "executing tool: web_search",
  "ts": 1708876543.21
}
```

The Gateway API reads heartbeat files to display agent status in real-time. Stale heartbeats (agent crash) trigger task recovery.

---

## 3. LLM Adapter Layer

### 3.1 Provider Architecture

```
┌──────────────────────────────────────────────────────┐
│                   ResilientLLM                        │
│   - Retry with exponential backoff + jitter          │
│   - Model failover (primary → fallback chain)        │
│   - Circuit breaker (CLOSED → OPEN → HALF_OPEN)     │
│   - Credential rotation (multi-key)                  │
│   - Usage logging (tokens, latency, cost)            │
└────────────────────────┬─────────────────────────────┘
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
      ┌──────────┐ ┌──────────┐ ┌──────────┐
      │ MiniMax  │ │ OpenAI   │ │ Ollama   │
      │ Adapter  │ │ Adapter  │ │ Adapter  │
      └──────────┘ └──────────┘ └──────────┘
```

### 3.2 Supported Providers

| Provider | Adapter | Notes |
|----------|---------|-------|
| **MiniMax** | `MinimaxAdapter` | Primary provider. Streaming support. Trace-ID tracking. Truncated JSON repair |
| **OpenAI** | `OpenAIAdapter` | OpenAI-compatible API. Also supports Anthropic, DeepSeek, custom endpoints |
| **Ollama** | `OllamaAdapter` | Local model inference. No API key required |
| **Flock** | `FLockAdapter` | FLock network inference |

Unknown providers default to OpenAI-compatible adapter.

### 3.3 ResilientLLM

Two-stage failover mechanism:

**Stage 1: Retry**
```
Request → Failure → Wait (base_delay * 2^attempt + jitter) → Retry
                     └─ Max retries: 3
                     └─ Base delay: 1.0s
                     └─ Max delay: 30.0s
                     └─ Jitter: ±0.5s
```

**Stage 2: Model Failover**
```
Primary model fails → Try fallback_models[0] → Try fallback_models[1] → ...
```

Fallback models are configured per-agent in `agents.yaml`:
```yaml
- id: jerry
  model: minimax-m2.5
  fallback_models:
    - MiniMax-M2.5
    - MiniMax-M2.1
```

### 3.4 Circuit Breaker

Three-state protection against cascading failures:

| State | Behavior | Transition |
|-------|----------|------------|
| **CLOSED** | Normal operation. Track consecutive failures | → OPEN after 3 consecutive failures |
| **OPEN** | All requests fail immediately (fast-fail) | → HALF_OPEN after 120s cooldown |
| **HALF_OPEN** | Allow one probe request | → CLOSED on success, → OPEN on failure |

### 3.5 Credential Rotation

For providers with rate limits, multiple API keys can be configured:

```yaml
llm:
  api_keys:
    - env: MINIMAX_KEY_1
    - env: MINIMAX_KEY_2
    - env: MINIMAX_KEY_3
```

`CredentialRotator` cycles through keys on rate-limit errors, distributing load across accounts.

### 3.6 MiniMax Trace-ID

For MiniMax API calls, the `Trace-Id` header is extracted from responses:
- Logged at DEBUG level for successful calls
- Included in error messages for support ticket filing
- Format: `resp.headers.get("Trace-Id")`

### 3.7 Usage Tracking

Every LLM call is logged with:
- Model name, provider
- Prompt tokens, completion tokens
- Latency (ms)
- Success/failure status
- Retry count, failover used
- Estimated cost (USD)

The `UsageTracker` supports daily/monthly budgets with `BudgetExceeded` exceptions.

---

## 4. Memory Infrastructure

### 4.1 Per-Agent Isolation

Each agent gets its own memory directory:
```
memory/
├── agents/
│   ├── leo/
│   │   └── chroma/        ← Leo's ChromaDB vectors
│   ├── jerry/
│   │   └── chroma/        ← Jerry's ChromaDB vectors
│   └── alic/
│       └── chroma/        ← Alic's ChromaDB vectors
├── episodic/
│   ├── leo/               ← Leo's episodes, cases, patterns
│   ├── jerry/
│   └── alic/
└── knowledge_base/        ← Shared KB (all agents)
```

### 4.2 ChromaDB Configuration

- **Persistence**: Disk-backed (survives restarts)
- **Distance metric**: Cosine similarity
- **Collection**: One per agent
- **Max results**: Configurable `recall_top_k` (default: 3)

### 4.3 Embedding Providers

Pluggable embedding system via `adapters/memory/embedding.py`:

| Provider | Model | Dimensions | Config |
|----------|-------|------------|--------|
| OpenAI | `text-embedding-3-small` | 1536 | `api_key_env: OPENAI_API_KEY` |
| Flock | Network-provided | Varies | `api_key_env: FLOCK_API_KEY` |
| Local | Sentence-transformers | Varies | No API key |
| ChromaDB default | Built-in | 384 | No config needed |

Configuration in `agents.yaml`:
```yaml
memory:
  embedding:
    provider: openai
    model: text-embedding-3-small
    api_key_env: OPENAI_API_KEY
```

### 4.4 BM25 Full-Text Search

SQLite FTS5 index with:
- **Tokenizer**: `unicode61` (built-in CJK character support)
- **Chinese stop-words**: ~120 common function words filtered during tokenization
  - Examples: "的", "了", "在", "是", "我", "有", "和", "就", "不", etc.
- **Index target**: Task descriptions, results, episode content
- **Collection-scoped**: Queries can filter by `collection` parameter

### 4.5 RRF Fusion Formula

```
For each document d:
  score_bm25  = 1 / (k + rank_in_bm25_results)
  score_vector = 1 / (k + rank_in_vector_results)
  score_final = score_bm25 + score_vector

  where k = 60 (constant)
```

Documents appearing in only one result set get their score from that set alone. This balances exact keyword matches with semantic similarity.

---

## 5. Security Model

### 5.1 Command Execution Security

Three-layer defense in `core/exec_tool.py`:

**Layer 1: Hard Deny List (never allowed)**
```python
HARD_DENY = {"rm -rf /", "mkfs", "dd if=/dev/zero", ":(){ :|:& };:", ...}
```
Patterns that are always blocked regardless of other settings.

**Layer 2: Allowlist**
```python
ALLOWLIST = {"ls", "cat", "grep", "python3", "node", "git", ...}
```
Only commands in the allowlist can execute. Unknown commands are rejected.

**Layer 3: Denylist (regex patterns)**
```python
DENY_LIST = [
    r"rm\s+-rf\s+/",
    r"sudo\s+",
    r"chmod\s+777",
    ...
]
```
Even allowed commands are checked against deny patterns.

**Execution flow:**
```
Command → Hard Deny check → Allowlist check → Denylist regex check → Execute
                ↓                 ↓                    ↓
            BLOCKED           BLOCKED              BLOCKED
```

### 5.2 Rate Limiting

Token bucket algorithm for command execution:

```python
class RateLimiter:
    bucket_size = 10       # Max burst
    refill_rate = 1/sec    # Sustained rate
    tokens = bucket_size   # Current tokens

    def allow():
        refill()
        if tokens >= 1:
            tokens -= 1
            return True
        return False
```

### 5.3 Path Validation

All file operations validate paths:

```python
def _is_allowed_path(abs_path: str) -> bool:
    cwd = os.path.abspath(".")
    real_path = os.path.realpath(abs_path)  # resolve symlinks
    sys_tmp = os.path.realpath(tempfile.gettempdir())
    slash_tmp = os.path.realpath("/tmp")
    return (real_path.startswith(cwd)
            or real_path.startswith(sys_tmp + os.sep)
            or real_path.startswith(slash_tmp + os.sep))
```

- `os.path.realpath()` resolves symlinks (prevents symlink attacks)
- Allowed paths: project directory + system temp directory + `/tmp`
- Blocks traversal attacks (`../../etc/passwd`)

### 5.4 User Authentication

Pairing-code mechanism:

1. User sends `/start` to the bot
2. Bot generates a random pairing code
3. User enters the code in the gateway or CLI
4. On match: user is authenticated, `allowed_users` list updated
5. Subsequent messages are checked against the allowed list

### 5.5 Config Value Redaction

The `redact_config()` function in `core/gateway.py`:

```python
SENSITIVE_KEYS = re.compile(r'token|key|secret|password|credential', re.I)

def redact_config(d: dict) -> dict:
    result = {}
    for k, v in d.items():
        if SENSITIVE_KEYS.search(k):
            result[k] = "***"
        elif isinstance(v, dict):
            result[k] = redact_config(v)
        else:
            result[k] = v
    return result
```

Applied to `/v1/status` and `/v1/config` API responses. Prevents API keys, tokens, and secrets from being exposed.

### 5.6 Audit Logging

Sensitive tool invocations are logged to `.logs/tool_audit.log`:

```json
{
  "ts": "2026-02-24T10:30:00Z",
  "tool": "exec",
  "agent": "jerry",
  "command": "python3 analyze.py",
  "exit_code": 0
}
```

---

## 6. Channel Architecture

### 6.1 Telegram Adapter

**Polling model:**
- Long-polling via `getUpdates` API
- Per-bot offset tracking
- Configurable `mention_required` for group chats

**Supported commands:**
| Command | Action |
|---------|--------|
| `/start` | Initiate pairing or welcome message |
| `/status` | Show system status (agents, tasks, memory) |
| `/cancel` | Cancel all active tasks |

**Native file delivery:**
- Photos: `sendPhoto` API
- Documents: `sendDocument` API (PDF, DOCX, etc.)
- Voice: `sendVoice` API
- Auto-detection by file extension

### 6.2 ChannelManager

Central coordinator for all channel adapters:

```python
class ChannelManager:
    def __init__(self):
        self.orchestrator_pool = {}  # Persistent orchestrators per session
        self._processing_lock = {}   # Sequential processing per session

    async def handle_message(self, channel, chat_id, text, user_id):
        session_key = normalize_key(channel, chat_id)

        # 1. Abort detection
        if is_abort_phrase(text):
            cancel_all_tasks(session_key)
            return "Tasks cancelled."

        # 2. Get or create orchestrator for this session
        orch = self.get_orchestrator(session_key)

        # 3. Sequential processing (one task at a time per session)
        async with self._processing_lock[session_key]:
            result = await orch.run(text)

        # 4. Deliver response with file attachments
        await deliver(channel, chat_id, result)
```

### 6.3 Session Key Normalization

```python
def normalize_key(channel: str, chat_id: str) -> str:
    return f"{channel}:{chat_id}".lower().strip()
```

Prevents duplicate sessions from case variations or whitespace.

### 6.4 Abort Detection

Multilingual stop phrase detection:

```python
ABORT_PHRASES = {
    "stop", "cancel", "abort",      # English
    "取消", "停止", "中止",           # Chinese
    "やめて", "中止して",             # Japanese
    "стоп", "отмена",               # Russian
}
```

Checked before task processing. On match: `board.cancel_all()` + immediate response.

---

## 7. Gateway API

### 7.1 HTTP Server

- **Port**: 19789 (configurable)
- **Framework**: `aiohttp` async HTTP server
- **Authentication**: Bearer token (`GATEWAY_TOKEN` env var)

### 7.2 Endpoint Reference

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/chat` | Submit a task and get response (SSE streaming) |
| GET | `/v1/status` | System status (agents, tasks, memory, uptime) |
| GET | `/v1/tasks` | List all tasks with status |
| GET | `/v1/tasks/{id}` | Get specific task details |
| POST | `/v1/tasks/{id}/cancel` | Cancel a task |
| POST | `/v1/tasks/{id}/pause` | Pause a task |
| POST | `/v1/tasks/{id}/resume` | Resume a paused task |
| POST | `/v1/tasks/{id}/retry` | Retry a failed task |
| POST | `/v1/tasks/clear` | Clear task board |
| GET | `/v1/memory/search` | Search long-term memory |
| POST | `/v1/memory/save` | Save to long-term memory |
| GET | `/v1/memory/episodic/{agent_id}` | Query episodic memory |
| GET | `/v1/memory/kb` | Query knowledge base |
| GET | `/v1/config` | Current configuration (redacted) |
| GET | `/v1/agents` | List agents with status |
| GET | `/v1/agents/{id}/heartbeat` | Agent heartbeat status |
| GET | `/v1/reputation/{agent_id}` | Agent reputation scores |
| POST | `/v1/cron` | Create/manage cron jobs |
| GET | `/v1/cron` | List scheduled jobs |
| GET | `/v1/usage` | LLM usage statistics |
| GET | `/v1/skills` | List loaded skills |
| POST | `/v1/skills/install` | Install a remote skill |

### 7.3 SSE Streaming

The `/v1/chat` endpoint supports Server-Sent Events for real-time streaming:

```
POST /v1/chat
Content-Type: application/json
Authorization: Bearer <token>

{"message": "Search for the latest AI news"}
```

Response (SSE):
```
event: status
data: {"phase": "planning", "agent": "leo"}

event: partial
data: {"text": "Searching for AI news..."}

event: status
data: {"phase": "executing", "agent": "jerry", "tool": "web_search"}

event: partial
data: {"text": "Found 5 relevant articles..."}

event: complete
data: {"result": "Here are the latest AI news...", "task_id": "abc123"}
```

---

## 8. Deployment

### 8.1 System Requirements

| Component | Requirement |
|-----------|------------|
| **OS** | macOS (primary), Linux |
| **Python** | 3.11+ |
| **Dependencies** | See `requirements.txt` |
| **Memory** | 2GB+ RAM (ChromaDB vectors) |
| **Storage** | 1GB+ (memory persistence, logs) |

### 8.2 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LEO_API_KEY` | Yes | LLM API key for Leo |
| `JERRY_API_KEY` | Yes | LLM API key for Jerry |
| `ALIC_API_KEY` | Yes | LLM API key for Alic |
| `LEO_BASE_URL` | No | Custom LLM endpoint for Leo |
| `JERRY_BASE_URL` | No | Custom LLM endpoint for Jerry |
| `ALIC_BASE_URL` | No | Custom LLM endpoint for Alic |
| `TELEGRAM_BOT_TOKEN` | For Telegram | Telegram bot token |
| `DISCORD_BOT_TOKEN` | For Discord | Discord bot token |
| `SLACK_BOT_TOKEN` | For Slack | Slack bot token |
| `SLACK_APP_TOKEN` | For Slack | Slack app-level token |
| `FEISHU_APP_ID` | For Feishu | Feishu app ID |
| `FEISHU_APP_SECRET` | For Feishu | Feishu app secret |
| `OPENAI_API_KEY` | For embeddings | OpenAI API key (embeddings) |
| `BRAVE_API_KEY` | For web search | Brave Search API key |
| `PERPLEXITY_API_KEY` | For web search | Perplexity API key |
| `MOONSHOT_API_KEY` | For Kimi search | Moonshot API key |
| `GATEWAY_TOKEN` | For API | Gateway bearer token |
| `BASE_RPC_URL` | For chain | Blockchain RPC endpoint |
| `CHAIN_PRIVATE_KEY` | For chain | Blockchain operator key |

### 8.3 Directory Structure

```
cleo-dev/
├── config/
│   └── agents.yaml            # Agent definitions, LLM config, channels
├── core/
│   ├── orchestrator.py        # Multi-process agent launcher
│   ├── agent.py               # BaseAgent with 10-step pipeline
│   ├── task_board.py          # File-locked task lifecycle
│   ├── context_bus.py         # 4-layer shared KV store
│   ├── tools.py               # 36+ tool registry
│   ├── exec_tool.py           # Secure command execution
│   ├── gateway.py             # HTTP API server
│   ├── cron.py                # Job scheduler
│   ├── subagent.py            # Dynamic subagent spawning
│   ├── skill_loader.py        # Hot-reload skill system
│   ├── wakeup.py              # Event-driven agent wakeup
│   ├── heartbeat.py           # Agent liveness tracking
│   ├── usage_tracker.py       # LLM cost accounting
│   └── compaction.py          # Context window management
├── adapters/
│   ├── llm/
│   │   ├── minimax.py         # MiniMax adapter (primary)
│   │   ├── openai.py          # OpenAI-compatible adapter
│   │   ├── ollama.py          # Local model adapter
│   │   ├── flock.py           # FLock network adapter
│   │   └── resilience.py      # ResilientLLM wrapper
│   ├── memory/
│   │   ├── hybrid.py          # BM25 + ChromaDB + RRF
│   │   ├── chroma.py          # ChromaDB adapter
│   │   ├── episodic.py        # Per-agent episodic memory
│   │   ├── knowledge_base.py  # Shared Zettelkasten KB
│   │   └── embedding.py       # Pluggable embedding providers
│   ├── channels/
│   │   ├── manager.py         # ChannelManager coordinator
│   │   ├── telegram.py        # Telegram long-polling adapter
│   │   ├── discord.py         # Discord adapter
│   │   ├── slack.py           # Slack adapter
│   │   ├── feishu.py          # Feishu adapter
│   │   └── session.py         # Per-user session store
│   └── chain/
│       ├── chain_manager.py   # Blockchain integration
│       └── mock.py            # Mock chain for testing
├── reputation/
│   ├── scorer.py              # 5-dimension EMA scoring
│   ├── peer_review.py         # Anti-gaming mechanisms
│   ├── evolution.py           # 3-path evolution engine
│   └── scheduler.py           # Reputation event scheduler
├── skills/
│   ├── shared/                # Skills for all agents
│   ├── team/                  # Team-level skills
│   ├── leo/                   # Leo-specific skills
│   ├── jerry/                 # Jerry-specific skills
│   └── alic/                  # Alic-specific skills
├── workspace/                 # Shared agent workspace
├── memory/                    # Persistent memory storage
├── tests/                     # 274+ tests
├── .logs/                     # Agent logs + audit log
├── .sessions/                 # User session data
├── .mailboxes/                # Inter-agent messages
├── .heartbeats/               # Agent liveness data
├── .env                       # Environment variables
└── requirements.txt           # Python dependencies
```

### 8.4 Process Management

**Starting Cleo:**
```bash
# Start with Telegram channel
python3 -m adapters.channels.telegram

# Start gateway API only
python3 -m core.gateway

# Start with CLI (direct task)
python3 main.py "Your task here"
```

**Graceful shutdown:**
1. SIGTERM/SIGINT received by main process
2. Orchestrator sends `shutdown` via mailbox to each agent
3. Agents complete current task (or abort if in tool loop)
4. 5-second grace period
5. Remaining processes receive SIGTERM
6. Final 3-second wait, then forced exit

**Log files:**
```
.logs/
├── leo.log           # Leo's process output
├── jerry.log         # Jerry's process output
├── alic.log          # Alic's process output
└── tool_audit.log    # Sensitive tool invocations
```

---

## 9. Configuration Reference

### 9.1 agents.yaml Schema

```yaml
# LLM provider (global default)
llm:
  provider: minimax          # minimax | openai | ollama | flock

# Memory backend
memory:
  backend: hybrid            # hybrid | chroma | mock
  long_term: true
  embedding:
    provider: openai         # openai | flock | local | chromadb_default
    model: text-embedding-3-small
    api_key_env: OPENAI_API_KEY
  episodic:
    enabled: true
    recall_budget_tokens: 1500
  knowledge_base:
    enabled: true
    recall_budget_tokens: 800

# Channel configuration
channels:
  telegram:
    enabled: true
    auth_mode: pairing
    bot_token_env: TELEGRAM_BOT_TOKEN
    mention_required: true
  discord:
    auth_mode: pairing
    bot_token_env: DISCORD_BOT_TOKEN
  slack:
    enabled: false
    bot_token_env: SLACK_BOT_TOKEN
    app_token_env: SLACK_APP_TOKEN
  feishu:
    enabled: false
    app_id_env: FEISHU_APP_ID
    app_secret_env: FEISHU_APP_SECRET

# Reputation system
reputation:
  peer_review_agents: [alic]
  evolution:
    prompt_auto_apply: true
    model_swap_require_confirm: true
    role_vote_threshold: 0.6

# Resilience settings
resilience:
  base_delay: 1.0
  max_delay: 30.0
  jitter: 0.5
  circuit_breaker_threshold: 3
  circuit_breaker_cooldown: 120

# Context compaction
compaction:
  enabled: true
  max_context_tokens: 30000
  summary_target_tokens: 2000
  keep_recent_turns: 4

# Workspace
workspace:
  path: workspace
  shared: true

# Idle behavior
max_idle_cycles: 30

# Skill registry
skill_registry:
  url: https://raw.githubusercontent.com/.../registry.json
  auto_update: false

# Agent definitions
agents:
  - id: leo
    role: "Planner / Brain ..."
    model: MiniMax-M2.5
    fallback_models: [MiniMax-M2.1]
    skills: [_base, planning, review]
    tools:
      profile: minimal
    memory:
      short_term_turns: 6
      long_term: true
      recall_top_k: 3
      episodic_recall_budget: 1500
      kb_recall_budget: 800
    autonomy_level: 1
    llm:
      provider: minimax
      api_key_env: LEO_API_KEY
      base_url_env: LEO_BASE_URL

  - id: jerry
    role: "Executor / Builder ..."
    model: minimax-m2.5
    fallback_models: [MiniMax-M2.5, MiniMax-M2.1]
    skills: [_base, coding, copywriting]
    tools:
      profile: coding
    memory:
      short_term_turns: 20
      long_term: true
      recall_top_k: 3
      episodic_recall_budget: 2000
      kb_recall_budget: 800
    autonomy_level: 1
    llm:
      provider: minimax
      api_key_env: JERRY_API_KEY

  - id: alic
    role: "Reviewer / Advisor ..."
    model: minimax-m2.5
    fallback_models: [MiniMax-M2.5, MiniMax-M2.1]
    skills: [_base, review, copywriting]
    tools:
      profile: minimal
    memory:
      short_term_turns: 20
      long_term: true
      recall_top_k: 3
      episodic_recall_budget: 1000
      kb_recall_budget: 1000
    autonomy_level: 1
    llm:
      provider: minimax
      api_key_env: ALIC_API_KEY
```

---

## 10. Data Flow Diagrams

### 10.1 Task Processing Flow

```
User Message
     │
     ▼
ChannelManager.handle_message()
     │
     ├─ Abort check → cancel_all() if stop phrase detected
     │
     ▼
SessionStore.get_or_create(key)
     │
     ▼
Orchestrator.submit(task, role="planner")
     │
     ▼
TaskBoard.create() → PENDING
     │
     ▼
Leo._agent_loop() → claim_next() → CLAIMED
     │
     ▼
Leo.BaseAgent.run(task) → LLM decomposition
     │
     ▼
_extract_and_create_subtasks()
     │
     ├─ Subtask A → PENDING (role=implement)
     ├─ Subtask B → PENDING (role=implement)
     └─ Subtask C → PENDING (role=implement)
     │
     ▼
WakeupBus.wake_all()
     │
     ▼
Jerry._agent_loop() → claim_next() → Subtask A CLAIMED
     │
     ▼
Jerry.BaseAgent.run(subtask_a) → Tool execution
     │
     ▼
TaskBoard.submit_for_review() → REVIEW
     │
     ▼
Jerry.send_mail("alic", critique_request)
     │
     ▼
Alic._agent_loop() → read_mail() → critique_request
     │
     ▼
Alic → LLM scoring → add_critique() → COMPLETED
     │
     ▼
_check_planner_closeouts()
     │ (repeat for all subtasks)
     ▼
All subtasks COMPLETED → Leo closeout synthesis
     │
     ▼
Final answer → TaskBoard.complete(parent) → COMPLETED
     │
     ▼
ChannelManager → deliver to user (text + files)
     │
     ▼
Memory extraction (episodes, cases, patterns, KB)
```

### 10.2 Memory Recall Flow

```
Agent starts task
     │
     ▼
Build system prompt
     │
     ├─ L0: Episodic summary (100 tokens)
     │      └─ Recent episode titles + scores
     │
     ├─ L1: Knowledge base recall (500 tokens)
     │      └─ Relevant atomic notes + cases
     │
     ├─ L2: Full episodic recall (remaining budget)
     │      └─ Detailed episodes + daily logs
     │
     └─ Hybrid search: memory.search(query, top_k)
            ├─ BM25 (FTS5) → ranked results
            ├─ ChromaDB (vector) → ranked results
            └─ RRF fusion → merged top-K
```

---

*Cleo V0.01 -- Technical Architecture*
*Last updated: 2026-02-24*
