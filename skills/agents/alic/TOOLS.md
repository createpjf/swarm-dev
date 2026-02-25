# TOOLS.md — Alic (Inspector)

## Available Tools (10)

| Tool | Description | Usage |
|---|---|---|
| web_search | Search the internet for real-time information | Find evaluation standards, best practices |
| web_fetch | Fetch content from a specific URL | Technical documentation, code standards |
| memory_search | Search long-term memory store | Retrieve historical scoring patterns, daily_log entries |
| memory_save | Save to long-term memory store | Write per-task execution logs (tag: daily_log), record insights |
| kb_search | Search knowledge base | Find scoring reference standards, previous daily reports |
| kb_write | Write to knowledge base | Save daily quality reports (topic: daily_report_{date}) |
| check_skill_deps | Check skill CLI dependency status | Identify missing CLI tools |
| install_skill_cli | Install skill CLI tool | Auto-select brew/go/npm installer |
| search_skills | Search remote skill registry | Discover installable skills |
| install_remote_skill | Install remote skill (hot-reload) | Download and enable new skills |

## Tool Usage Rules

1. **Evaluation-focused** — Alic does not perform modifying operations; no exec, write_file, etc.
2. Prefer memory_search for historical scoring patterns to ensure evaluation consistency
3. Use memory_save to write per-task execution logs (tag: `daily_log`) after each review
4. Use kb_write to save daily quality reports for Leo's iteration cycle
5. Use web_search / web_fetch when external standards need to be consulted
6. Skill management tools can be used directly — check_skill_deps, install_skill_cli, etc.

## Evaluation Workflow

1. Read Jerry's execution results
2. Retrieve historical scores for similar tasks from memory store
3. Score along 5 HLE dimensions
4. Output JSON evaluation block (CritiqueSpec)
5. Write execution log entry to memory (tag: daily_log)
6. Leo reads the evaluation during synthesis phase

## Daily Report Workflow (Cron-Triggered)

1. Recall all `daily_log` entries from today via memory_search
2. Aggregate scores, identify patterns and trends
3. Generate structured Daily Quality Report
4. Save report to KB via kb_write (topic: daily_report_{date})
