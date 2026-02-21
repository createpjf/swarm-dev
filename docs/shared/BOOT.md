# BOOT.md — System Startup Checklist

## Pre-flight Checks

On system startup, verify the following before accepting tasks:

### 1. Environment
- [ ] Python 3.10+ available
- [ ] Required packages installed (filelock, pyyaml, httpx)
- [ ] .env file loaded with API keys

### 2. LLM Connectivity
- [ ] Primary LLM API reachable (DeepSeek / OpenAI)
- [ ] Fallback model configured

### 3. Memory System
- [ ] `memory/` directory writable
- [ ] `memory/agents/{id}/` directories exist for all agents
- [ ] MEMORY.md files loadable (non-corrupt)

### 4. Agent Files
- [ ] `skills/agents/{id}/soul.md` exists for leo, jerry, alic
- [ ] `skills/` directory readable for hot-reload

### 5. Communication Layer
- [ ] `.mailboxes/` directory writable
- [ ] ContextBus file accessible
- [ ] TaskBoard file accessible

### 6. Channel Adapters (if enabled)
- [ ] Bot tokens configured in environment
- [ ] Session store writable (`memory/channel_sessions.json`)

## Post-boot Actions

1. Load all agent soul.md profiles
2. Restore short-term memory from disk
3. Initialize ContextBus with WORKSPACE layer
4. Start channel adapters (if configured)
5. Log startup status to daily log
