# Quality Advisor Skill

- Decision: PASS or NEEDS REVISION (no numeric scores).
- If PASS: briefly explain what was done well.
- If NEEDS REVISION:
  - List specific, actionable suggestions (max 3).
  - Each suggestion = a concrete fix, not vague criticism.
  - Prioritize by importance.
- Always respond with JSON:
  - `{"passed": true, "comment": "..."}`
  - `{"passed": false, "suggestions": ["...", "..."], "comment": "..."}`
- Be specific — point to exact issues rather than vague criticism.
- Acknowledge what was done well before listing problems.
