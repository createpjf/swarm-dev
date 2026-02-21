# HEARTBEAT.md — Jerry

## Background Checks

Every task start, Jerry should verify:

### Execution Environment
- Shell available and responsive
- Python runtime accessible
- Working directory is project root
- Disk space > 100MB free

### Tool Status
- exec tool: test with simple command (echo ok)
- File system: workspace/ directory writable
- Network: outbound HTTP connectivity (if web tools needed)

### Active Task Health
- Current task timeout countdown
- Command execution duration tracking
- File operation success/failure rate

### Skill Loading
- skills/ directory modification time
- Per-agent skill files loaded
- Hot-reload triggers detected
