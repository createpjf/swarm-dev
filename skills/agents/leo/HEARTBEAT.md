# HEARTBEAT.md — Leo

## Background Checks

Every session start, Leo should verify:

### Task Pipeline
- Pending tasks count: if > 5, warn user about queue depth
- Last successful response time: if > 10min ago, check system health
- Failed task rate in last hour: if > 50%, escalate

### Memory Health
- MEMORY.md file size: if > 50KB, trigger compaction
- Short-term memory entries: if > 40, trim oldest
- Episodic memory disk usage: monitor growth

### Team Status
- Jerry last active: if > 5min for active task, investigate
- Alic last evaluation: verify evaluation pipeline active
- ContextBus stale entries: count entries > 1hr old

### Session Continuity
- Current session message count
- Session age (time since first message)
- User engagement pattern (response frequency)
