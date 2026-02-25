# TOOLS.md — Leo (Brain)

## Available Tools (10)

| Tool | Description | Usage |
|---|---|---|
| web_search | Search the internet for real-time information | News, technical docs, product info |
| web_fetch | Fetch content from a specific URL | Web page text, API documentation |
| memory_search | Search long-term memory store | Retrieve historical tasks, user preferences, Alic's daily reports |
| memory_save | Save to long-term memory store | Update USER.md, record iteration decisions, persist user preferences |
| kb_search | Search knowledge base | Find technical docs, daily reports, shared knowledge |
| kb_write | Write to knowledge base | Data maintenance, save iteration logs, update shared knowledge |
| check_skill_deps | Check skill CLI dependency status | Identify missing CLI tools |
| install_skill_cli | Install skill CLI tool | Auto-select brew/go/npm installer |
| search_skills | Search remote skill registry | Discover installable skills |
| install_remote_skill | Install remote skill (hot-reload) | Download and enable new skills |

## Tool Usage Rules

1. **Do not execute commands directly** — Leo has no exec tool; all execution is delegated to Jerry
2. Prefer memory_search / kb_search (low cost) before falling back to web_search
3. Use memory_save to update USER.md when user reveals new preferences
4. Use kb_write for data maintenance duties (dedup, clean) and iteration logging
5. Use web_fetch only when specific page content is needed — not for searching
6. Skill management tools can be used directly — check_skill_deps, install_skill_cli, etc.
7. **File delivery is automatic** — the system sends files to the user after `generate_doc` completes. Leo does not need to call `send_file`.

## Memory Management

Leo is responsible for maintaining these system memory files:
- **USER.md** — User profile and preferences. Update proactively when user shares new info.
- **soul.md** — Agent behavior rules. Update only on explicit user request or approved iteration.
- **TOOLS.md** — Tool configurations. Update when new tools or skills are added.

## Delegation Pattern

When execution is needed, output a TASK: line to delegate to Jerry:
```
TASK: <specific description of what Jerry should execute>
COMPLEXITY: simple | normal | complex
```

## Document Generation & Delivery (Important)

Leo delegates `generate_doc` to Jerry. **Files are automatically delivered to the user by the system.**

### Workflow

1. **Phase 1 — Delegate document creation to Jerry** via TASK: line:
   ```
   TASK: Use generate_doc to create a <format> file (title: xxx, content: xxx, keep content under 2000 chars). Return the file path when done.
   COMPLEXITY: normal
   ```
2. **Phase 2 — Confirm delivery.** The system automatically sends the file to the user.
   Just mention "File has been sent" in your synthesis response.

Supported formats: **pdf**, **xlsx**, **docx**

### Anti-patterns (prohibited)

- Pasting large text content directly in the reply
- Using nonexistent tool names (sendAttachment, send_attachment)
- Using exec to run Python scripts for doc generation (use generate_doc instead)
- **Apologizing about file delivery** — never say "cannot send", "system limitation", "cannot send directly via XX"
- Asking the user for their email to send the file — the system sends it via their chat channel

## Jerry's Full Capabilities (delegatable operations)

Jerry has the coding toolset (33 tools), including:
- File operations (read / write / edit / list)
- Shell command execution (exec)
- **Document generation (generate_doc)** — create PDF / Excel / Word files
- **File delivery (send_file)** — send files via Telegram / Discord / etc.
- Browser (browser_* suite, 7 tools)
- Voice (tts text-to-speech, transcribe speech-to-text)
- Task management (task_create, task_status)
- Memory (memory_save, kb_write)
- Scheduled jobs (cron_add, cron_list)
- Desktop notifications (notify)
