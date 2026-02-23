# BOOT.md — System Startup Checklist

## Pre-flight Checks

On system startup, verify the following before accepting tasks:

### 1. Environment
- [ ] Python 3.10+ available
- [ ] Required packages installed (filelock, pyyaml, httpx)
- [ ] .env file loaded with API keys

### 2. LLM Connectivity
- [ ] Primary LLM API reachable (MiniMax — models: MiniMax-M2.5, MiniMax-M2.1)
- [ ] Embedding API reachable (OpenAI — model: text-embedding-3-small)
- [ ] Fallback models configured per agent

### 3. Memory System
- [ ] `memory/` directory writable
- [ ] `memory/agents/{id}/` directories exist for all agents
- [ ] MEMORY.md files loadable (non-corrupt)
- [ ] Episodic memory backend initialized (ChromaDB / hybrid)
- [ ] Knowledge base accessible

### 4. Agent Files
- [ ] `skills/agents/{id}/soul.md` exists for leo, jerry, alic
- [ ] `skills/agents/{id}/TOOLS.md` exists and matches tool profile (minimal: 8 tools, coding: 33 tools)
- [ ] `skills/` directory readable for hot-reload

### 5. Skill Dependencies
- [ ] Core skill CLIs installed (verifiable via `check_skill_deps`)
- [ ] Missing deps logged as warnings (non-blocking)

### 6. Communication Layer
- [ ] `.mailboxes/` directory writable
- [ ] ContextBus file accessible (`.context_bus.json`)
- [ ] TaskBoard file accessible (`.task_board.json`)

### 7. Channel Adapters (if enabled)
- [ ] Bot tokens configured in environment
- [ ] Session store writable (`memory/channel_sessions.json`)

## Post-boot Actions

1. Load all agent soul.md + TOOLS.md profiles
2. Restore short-term memory from disk
3. Initialize ContextBus with WORKSPACE layer
4. Start channel adapters (if configured)
5. Log startup status to daily log
