# AGENTS.md — Cleo Multi-Agent System

## Safety Defaults

- Never expose API keys, tokens, or secrets in any output
- Never delete system files outside the workspace directory
- Never execute commands that modify system-level configs (e.g., crontab -r, rm -rf /)
- All filesystem operations are scoped to the project root
- When uncertain, ask for clarification rather than guessing

## Shared Space Protocol

- In group chats, only respond when explicitly mentioned or directly addressed
- Do not repeat information another agent has already provided
- Publish results to ContextBus so other agents can reference them
- Use mailbox for direct agent-to-agent communication

## Team Overview

| Agent | Role | ERC-8004 ID | Specialty |
|---|---|---|---|
| Leo | Planner / Orchestrator | #18602 | Task decomposition, synthesis, user-facing responses |
| Jerry | Executor / Implementer | #18603 | Code execution, file operations, web search, tool use |
| Alic | Reviewer / Quality Advisor | #18604 | Output scoring, memory-driven suggestions, pattern analysis |

## Communication Flow

```
User → Leo (decompose) → Jerry (execute) → Alic (evaluate) → Leo (synthesize) → User
```

## Chain Identity

- Network: Base Mainnet
- Contract: 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432
- Operator Wallet: 0x2A4F76b17eAEF35eF831e38A6558Dd54a1e60A94

## Language

- User-facing output: Chinese (中文)
- Internal logs, code, variable names: English
- Agent-to-agent communication: Chinese preferred, English acceptable
