# TOOLS.md — Alic (Reviewer)

## Available Tools (8)

| Tool | Description | Usage |
|---|---|---|
| web_search | Search the internet for real-time information | Find evaluation standards, best practices |
| web_fetch | Fetch content from a specific URL | Technical documentation, code standards |
| memory_search | Search long-term memory store | Retrieve historical scoring patterns, user preferences |
| kb_search | Search knowledge base | Find scoring reference standards, shared knowledge |
| check_skill_deps | Check skill CLI dependency status | Identify missing CLI tools |
| install_skill_cli | Install skill CLI tool | Auto-select brew/go/npm installer |
| search_skills | Search remote skill registry | Discover installable skills |
| install_remote_skill | Install remote skill (hot-reload) | Download and enable new skills |

## Tool Usage Rules

1. **Read-only evaluation** — Alic does not perform modifying operations; no exec, write_file, etc.
2. Prefer memory_search for historical scoring patterns to ensure evaluation consistency
3. Use web_search / web_fetch when external standards need to be consulted
4. Skill management tools can be used directly — check_skill_deps, install_skill_cli, etc.

## Evaluation Workflow

1. Read Jerry's execution results
2. Retrieve historical scores for similar tasks from memory store
3. Score along 5 HLE dimensions
4. Output JSON evaluation block
5. Leo reads the evaluation during synthesis phase
