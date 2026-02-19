# Planning Skill

- Decompose the task into clear, actionable subtasks.
- Write one subtask per line, prefixed with `TASK:`.
- For each task, add `COMPLEXITY: simple|normal|complex` on the next line.
- Order subtasks by dependency — earlier tasks should not depend on later ones.
- Each subtask should be self-contained enough for another agent to execute independently.
- Do not implement — only plan.
- Aim for 3-7 subtasks per task. Split further only if a subtask is still too complex.

## Closing Out Tasks

When all subtasks are completed, synthesize a final answer:
- Combine outputs, resolve contradictions.
- Present as one unified user-facing response.
- Remove internal task references.
