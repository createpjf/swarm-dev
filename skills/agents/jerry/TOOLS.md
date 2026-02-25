# TOOLS.md — Jerry (Executor)

## Available Tools (34)

> **V0.02 ToolScope**: The system dynamically loads a tool subset (9-14 tools) based on the SubTaskSpec's `tool_hint`. You may only receive a portion of the tools listed below. Base tools (Memory + Messaging) are always available.

### 🌐 Web
| Tool | Description | Usage |
|---|---|---|
| web_search | Search the internet (Brave / Perplexity dual-engine) | Technical solutions, docs, news |
| web_fetch | Fetch URL content (text/Markdown) | API responses, web page text |

### ⚙️ Execution
| Tool | Description | Usage |
|---|---|---|
| exec | Execute Shell/Python commands (approval-gated) | System ops, scripts, package installs |
| process | List system processes | Check running services |
| cron_list | List scheduled tasks | View existing cron jobs |
| cron_add | Create scheduled task | Reminders, periodic execution, webhooks |

### 📁 Filesystem
| Tool | Description | Usage |
|---|---|---|
| read_file | Read file content | Code, config, logs |
| write_file | Write to file | Create new files, generate documents |
| edit_file | Find-and-replace editing | Precise modifications to existing files |
| list_dir | List directory contents | Browse project structure |
| **generate_doc** | **Generate document files (PDF/Excel/Word)** | **Reports, plans, spreadsheets, documents** |

### 🧠 Memory
| Tool | Description | Usage |
|---|---|---|
| memory_search | Search episodic memory | Historical solutions, user preferences |
| memory_save | Save problem→solution to memory | Valuable technical solutions |
| kb_search | Search shared knowledge base | Technical docs, shared knowledge |
| kb_write | Write to shared knowledge base (Zettelkasten) | Save reusable insights |

### 🎙️ Media
| Tool | Description | Usage |
|---|---|---|
| notify | Send desktop notification (macOS) | Task completion, reminders |
| transcribe | Speech-to-text (Whisper API) | mp3/m4a/wav transcription |
| tts | Text-to-speech (multi-engine) | Generate audio files |
| list_voices | List available TTS voices | Voice selection |

### 📋 Task Management
| Tool | Description | Usage |
|---|---|---|
| task_create | Create task on task board | New to-do items |
| task_status | Check task status | Monitor task progress |

### 💬 Messaging
| Tool | Description | Usage |
|---|---|---|
| send_mail | Send message to another agent | Cross-agent communication |
| **send_file** | **Send file to user (Telegram/Discord/Feishu/Slack)** | **Documents, PDFs, images** |

### 🌍 Browser (Headless)
| Tool | Description | Usage |
|---|---|---|
| browser_navigate | Open URL (headless browser) | Pages requiring JS rendering |
| browser_click | Click page element | Interactive operations |
| browser_fill | Fill form field | Auto-fill forms |
| browser_get_text | Get page text | Extract rendered content |
| browser_screenshot | Page screenshot | Visual capture |
| browser_evaluate | Execute page JS | Advanced scraping |
| browser_page_info | Get page info (URL/title) | Confirm navigation state |

### 🤖 A2A Delegation (Phase 5)
| Tool | Description | Usage |
|---|---|---|
| a2a_delegate | Delegate subtask to external A2A agent | Chart generation, specialized analysis, image gen |

**a2a_delegate parameters**:
- `agent_url`: Target Agent URL or `"auto"` to auto-match via Registry (required)
- `message`: Task description to send to the external Agent; English recommended (required)
- `files`: Attached file paths (comma-separated; only verified Agents can send files)
- `required_skills`: Required capability tags (comma-separated; used with auto matching)
- `timeout`: Maximum wait in seconds (default: 120)

**Return fields**: `result.text`, `result.files`, `result.status` (completed/failed/timeout), `result.agent_name`, `result.trust_level`, `result.warnings`

### 🔧 Skill Management
| Tool | Description | Usage |
|---|---|---|
| check_skill_deps | Check skill CLI dependency status | View missing CLI tools |
| install_skill_cli | Install skill CLI (auto-select package manager) | brew/go/npm/uv install |
| search_skills | Search remote skill registry | Discover new capabilities |
| install_remote_skill | Install remote skill (hot-reload) | Download and enable new skills |

---

## Tool Usage Rules

1. **Reason before exec** — Explain why you're running each command
2. **Prefer edit_file** for modifications (precise edits); avoid write_file overwriting entire files
3. **On execution failure** — Analyze error logs and attempt fix within the same task scope
4. **On network failure** — Retry once, then report
5. **File ops restricted to project root and /tmp/** — Write generated temp files (e.g., user-requested docs) to /tmp/
6. **🔴 Document generation & delivery** — When the task originates from a chat channel and requires document generation:
   - Use `generate_doc` to create the file (supports pdf/xlsx/docx), e.g.:
     `{"tool": "generate_doc", "params": {"format": "pdf", "content": "...", "title": "Title"}}`
   - Then use `send_file` to deliver the generated file to the user
   - **Prefer generate_doc** (built-in PDF/Excel/Word support, no exec needed)
   - **Do not** paste long document content directly in the response
7. **Memory persistence** — Save valuable problem→solution pairs with `memory_save`; reusable knowledge with `kb_write`
8. **Browser** — Only use `browser_*` for JS-rendered pages; use `web_fetch` for regular pages
9. **TTS** — Use `tts` when user requests reading/voice output; use `list_voices` first to confirm available voices

## Execution Standards

- Command timeout: 60 seconds
- File size limit: single file < 10MB
- Log output: Preserve in full, do not truncate
- Error handling: Catch exceptions, return full traceback
