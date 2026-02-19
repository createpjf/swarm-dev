# Planning Skill

## Phase 1: Task Decomposition
- Decompose the task into clear, actionable subtasks.
- Write one subtask per line, prefixed with `TASK:`.
- For each task, add `COMPLEXITY: simple|normal|complex` on the next line.
- Order subtasks by dependency — earlier tasks should not depend on later ones.
- Each subtask should be self-contained enough for another agent to execute independently.
- Do not implement — only plan.
- Aim for 3-7 subtasks per task. Split further only if a subtask is still too complex.

## Phase 2: Final Synthesis (Closeout)

When all subtasks are completed, synthesize the FINAL answer for the user:
- You will receive all executor results (raw data) AND reviewer feedback (scores + suggestions).
- Combine ALL subtask outputs into ONE coherent, polished response.
- Incorporate valid reviewer suggestions to improve quality.
- Remove all internal task IDs, agent references, and metadata.
- The response must DIRECTLY answer the user's original question.
- Present as one unified, professional, user-facing response.
- This is the MOST IMPORTANT step — the quality of your synthesis determines the user experience.
