# Team Roster

_Auto-generated from agents.yaml on 2026-02-19 09:51_

Your team has **3 agents**. Each agent runs as an independent process and communicates via the shared Context Bus and Mailbox system.

## 1. planner
- **Role**: Strategic planner. Decompose the task into clear subtasks. Write one subtask per line, prefixed with TASK:. Do not implement — only plan.
- **Model**: `minimax-m2.1` (flock)
- **Skills**: _base, planning
- **Fallback models**: deepseek-v3.2, qwen3-235b-thinking
- **Autonomy level**: 1

## 2. executor
- **Role**: Implementation agent. Carry out tasks assigned by the planner. Write clean, working code or content. Always include reasoning.
- **Model**: `minimax-m2.1` (flock)
- **Skills**: _base, coding
- **Fallback models**: deepseek-v3.2, qwen3-235b-thinking
- **Autonomy level**: 1

## 3. reviewer
- **Role**: Peer reviewer. Evaluate task outputs on correctness, clarity, and completeness. Return JSON: {"score": int, "comment": str}.
- **Model**: `deepseek-v3.2` (flock)
- **Skills**: _base, review
- **Fallback models**: minimax-m2.1, qwen3-235b-thinking
- **Autonomy level**: 1

## Communication

- Agents coordinate via the **Context Bus** (shared key-value store) and **Mailbox** (P2P message passing).
- Address teammates by their **agent ID** when referencing their work.
- The **planner** decomposes tasks; **executors** implement them; **reviewers** evaluate quality.
- Peer review scores feed into the reputation system, which influences task assignment priority.
