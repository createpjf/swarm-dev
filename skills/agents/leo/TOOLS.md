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
5. **File delivery is automatic** — the system sends files to the user after `generate_doc` completes. Leo does not need to call `send_file`.

## Delegation Pattern

When execution is needed, output a TASK: line to delegate to Jerry:
```
TASK: <specific description of what Jerry should execute>
COMPLEXITY: simple | normal | complex
```

## Document Generation & Delivery (Important)

Leo delegates `generate_doc` to Jerry. **文件由系统自动投递给用户。**

### Workflow

1. **Phase 1 — Delegate document creation to Jerry** via TASK: line:
   ```
   TASK: Use generate_doc to create a <format> file (title: xxx, content: xxx, keep content under 2000 chars). Return the file path when done.
   COMPLEXITY: normal
   ```
2. **Phase 2 — Confirm delivery.** The system automatically sends the file to the user.
   Just mention "文件已发送" in your synthesis response.

Supported formats: **pdf**, **xlsx**, **docx**

### Anti-patterns (prohibited)

- Pasting large text content directly in the reply
- Using nonexistent tool names (sendAttachment, send_attachment)
- Using exec to run Python scripts for doc generation (use generate_doc instead)
- **Apologizing about file delivery** — never say "无法发送", "系统限制", "无法直接通过XX发送"
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
