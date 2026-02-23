# USER.md — User Profile

## Basic Info

- Role: Cleo system creator and primary user
- Working language: Chinese primary, English secondary
- Technical background: Full-stack dev, Web3/blockchain, AI Agent systems

## Platform

- Hardware: Mac Mini
- OS: macOS
- Primary channel: Telegram (enabled)
- Other channels: Discord / Feishu / Slack (configured but disabled)

## Current Work

- Cleo Multi-Agent System development and operations
- ERC-8004 on-chain Agent identity system
- OpenClaw spec adaptation and integration
- Multi-channel access (Telegram / Discord / Feishu)

## Preferences

- Response style: Concise and direct, no pleasantries
- Format: Prefer Markdown structured output
- Error handling: State the problem and solution directly, no hedging
- Task granularity: Deliver complete solutions at once, don't ask for step-by-step confirmation
- Code style: Minimal, functional, comments explain "why" not "what"
- File delivery: Use `send_file` for documents/reports/plans — never paste long content as plain text
- Language: Chinese for communication; keep technical terms, variable names, logs, and code in English

## Common Command Patterns

- "gateway restart" → Restart Cleo Gateway service
- "install skill" → Install a new skill to the specified agent
- "help me search / 帮我搜/查" → Web search or knowledge base retrieval
- Direct task description → Auto-decompose and execute
- "make a XX plan/report" → Generate file + send_file delivery
- "read aloud / 念" → TTS voice synthesis
- "install XX skill" → search_skills + install_remote_skill
- "check health / status" → healthcheck skill
