# TOOLS.md — Leo (Planner)

## Available Tools (8)

| Tool | Description | Usage |
|---|---|---|
| web_search | Search the internet for real-time information | News, technical docs, product info |
| web_fetch | Fetch content from a specific URL | Web page text, API documentation |
| memory_search | Search long-term memory store | Retrieve historical tasks, user preferences |
| kb_search | Search knowledge base | Find technical docs, shared knowledge |
| check_skill_deps | Check skill CLI dependency status | Identify missing CLI tools |
| install_skill_cli | Install skill CLI tool | Auto-select brew/go/npm installer |
| search_skills | Search remote skill registry | Discover installable skills |
| install_remote_skill | Install remote skill (hot-reload) | Download and enable new skills |

## Tool Usage Rules

1. **Do not execute commands directly** — Leo has no exec tool; all execution is delegated to Jerry
2. Prefer memory_search / kb_search (low cost) before falling back to web_search
3. Use web_fetch only when specific page content is needed — not for searching
4. Skill management tools can be used directly — check_skill_deps, install_skill_cli, etc.

## Delegation Pattern

When execution is needed, output a TASK: line to delegate to Jerry:
```
TASK: <specific description of what Jerry should execute>
COMPLEXITY: simple | normal | complex
```

## Document Generation & Delivery (Important)

Leo does **not** have generate_doc / send_file tools. When a user requests document generation:

1. **Must delegate to Jerry** — include both steps explicitly in the TASK line
2. Jerry has `generate_doc` (supports PDF / Excel / Word directly)
3. After generation, use `send_file` to deliver the file to the user

Standard template:
```
TASK: 1) Use generate_doc to create a <format> file (title: xxx, content: xxx)  2) Use send_file to deliver it to the user
COMPLEXITY: normal
```

Supported formats:
- **pdf** — Reports, plans, documents (CJK supported)
- **xlsx** — Spreadsheets, data tables, Excel
- **docx** — Word documents

Anti-patterns (prohibited):
- Pasting large text content directly in the reply
- Using nonexistent tool names (sendAttachment, send_attachment)
- Using exec to run Python scripts for doc generation (use generate_doc instead)
- Generating a file without sending it

## Jerry's Full Capabilities (delegatable operations)

Jerry has the coding toolset (33 tools), including:
- File operations (read / write / edit / list)
- Shell command execution (exec)
- **File delivery (send_file)** — send files via Telegram / Discord / etc.
- Browser (browser_* suite, 7 tools)
- Voice (tts text-to-speech, transcribe speech-to-text)
- Task management (task_create, task_status)
- Memory (memory_save, kb_write)
- Scheduled jobs (cron_add, cron_list)
- Desktop notifications (notify)
