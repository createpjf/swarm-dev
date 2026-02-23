# TOOLS.md — Jerry (Executor)

## Available Tools (33)

### 🌐 Web
| Tool | Description | Usage |
|---|---|---|
| web_search | 搜索互联网（Brave / Perplexity 双引擎） | 技术方案、文档、新闻 |
| web_fetch | 抓取 URL 内容（文本/Markdown） | API 响应、网页正文 |

### ⚙️ Execution
| Tool | Description | Usage |
|---|---|---|
| exec | 执行 Shell/Python 命令（审批门控） | 系统操作、脚本、包安装 |
| process | 列出系统进程 | 检查运行中的服务 |
| cron_list | 列出定时任务 | 查看已有 cron |
| cron_add | 创建定时任务 | 提醒、定期执行、webhook |

### 📁 Filesystem
| Tool | Description | Usage |
|---|---|---|
| read_file | 读取文件内容 | 代码、配置、日志 |
| write_file | 写入文件 | 创建新文件、生成文档 |
| edit_file | 查找替换编辑 | 精确修改已有文件 |
| list_dir | 列出目录内容 | 浏览项目结构 |
| **generate_doc** | **生成文档文件（PDF/Excel/Word）** | **报告、计划、表格、文档** |

### 🧠 Memory
| Tool | Description | Usage |
|---|---|---|
| memory_search | 搜索事件记忆 | 历史方案、用户偏好 |
| memory_save | 保存 problem→solution 到记忆 | 有价值的技术方案 |
| kb_search | 搜索共享知识库 | 技术文档、共享知识 |
| kb_write | 写入共享知识库（Zettelkasten） | 保存可复用的洞察 |

### 🎙️ Media
| Tool | Description | Usage |
|---|---|---|
| notify | 发送桌面通知（macOS） | 任务完成、提醒 |
| transcribe | 语音转文字（Whisper API） | mp3/m4a/wav 转录 |
| tts | 文字转语音（多引擎） | 生成语音文件 |
| list_voices | 列出 TTS 语音列表 | 选择语音 |

### 📋 Task Management
| Tool | Description | Usage |
|---|---|---|
| task_create | 创建任务到任务板 | 新建待办事项 |
| task_status | 查看任务状态 | 检查任务进度 |

### 💬 Messaging
| Tool | Description | Usage |
|---|---|---|
| send_mail | 发送消息给其他 agent | 跨 agent 通信 |
| **send_file** | **发送文件给用户（Telegram/Discord/飞书/Slack）** | **文档、PDF、图片发送** |

### 🌍 Browser (Headless)
| Tool | Description | Usage |
|---|---|---|
| browser_navigate | 打开 URL（无头浏览器） | 需要 JS 渲染的页面 |
| browser_click | 点击页面元素 | 交互操作 |
| browser_fill | 填写表单字段 | 自动填表 |
| browser_get_text | 获取页面文字 | 提取渲染后内容 |
| browser_screenshot | 页面截图 | 视觉捕获 |
| browser_evaluate | 执行页面 JS | 高级抓取 |
| browser_page_info | 获取页面信息（URL/标题） | 确认导航状态 |

### 🔧 Skill Management
| Tool | Description | Usage |
|---|---|---|
| check_skill_deps | 检查技能 CLI 依赖状态 | 查看缺失的 CLI 工具 |
| install_skill_cli | 安装技能 CLI（自动选择包管理器） | brew/go/npm/uv 安装 |
| search_skills | 搜索远端技能注册表 | 发现新能力 |
| install_remote_skill | 安装远端技能（热加载） | 下载并启用新技能 |

---

## Tool Usage Rules

1. **exec 前先 reasoning** — 解释为什么执行这个命令
2. **文件编辑优先 edit_file**（精确修改），避免 write_file 覆盖整个文件
3. **执行失败** — 分析错误日志并在同一任务范围内尝试修复
4. **网络请求失败** — 重试一次后上报
5. **文件操作限制在项目根目录和 /tmp/ 内** — 生成的临时文件（如用户要求的文档）写入 /tmp/
6. **🔴 文档生成与发送** — 当任务来自聊天频道且需要生成文档时：
   - 用 `generate_doc` 生成文件（支持 pdf/xlsx/docx），如：
     `{"tool": "generate_doc", "params": {"format": "pdf", "content": "...", "title": "标题"}}`
   - 再用 `send_file` 将生成的文件发送给用户
   - **优先使用 generate_doc**（内置 PDF/Excel/Word 支持，无需 exec）
   - **不要**把长文档内容直接贴在回复中
7. **记忆保存** — 有价值的 problem→solution 用 `memory_save` 保存；可复用知识用 `kb_write`
8. **浏览器** — 仅在需要 JS 渲染的页面使用 `browser_*`，普通页面用 `web_fetch`
9. **TTS** — 用户要求朗读/语音时使用 `tts`，先用 `list_voices` 确认可用语音

## Execution Standards

- 命令执行超时: 60 秒
- 文件大小限制: 单文件 < 10MB
- 日志输出: 保留完整，不裁剪
- 错误处理: 捕获异常，返回完整 traceback
