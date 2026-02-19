# Review Skill

- Evaluate task outputs on three dimensions: correctness, clarity, and completeness.
- Score from 0 to 100:
  - 90-100: Excellent — ready to ship
  - 70-89: Good — minor issues only
  - 50-69: Acceptable — needs some rework
  - 30-49: Poor — significant issues
  - 0-29: Failing — fundamental problems
- Always respond with JSON: `{"score": <int>, "comment": "<str>"}`
- Be specific in your comment — point to exact issues rather than vague criticism.
- Acknowledge what was done well before listing problems.
