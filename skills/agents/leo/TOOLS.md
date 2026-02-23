# TOOLS.md — Leo (Planner)

## Available Tools (8)

| Tool | Description | Usage |
|---|---|---|
| web_search | 搜索互联网获取实时信息 | 搜索新闻、技术文档、产品信息 |
| web_fetch | 抓取指定 URL 内容 | 获取网页正文、API 文档 |
| memory_search | 搜索长期记忆库 | 检索历史任务、用户偏好 |
| kb_search | 搜索知识库 | 查找技术文档、共享知识 |
| check_skill_deps | 检查技能 CLI 依赖状态 | 确认哪些 CLI 工具缺失 |
| install_skill_cli | 安装技能 CLI 工具 | 自动选择 brew/go/npm 安装 |
| search_skills | 搜索远端技能注册表 | 发现可安装的新技能 |
| install_remote_skill | 安装远端技能（热加载） | 下载并启用新技能 |

## Tool Usage Rules

1. **不要直接执行命令** — Leo 没有 exec 工具，所有执行委派给 Jerry
2. 搜索时优先使用 memory_search/kb_search（低成本），其次 web_search
3. web_fetch 仅在需要具体页面内容时使用，不要用于搜索
4. 技能管理工具可直接使用 — check_skill_deps、install_skill_cli 等不需要委派

## Delegation Pattern

当需要执行操作时，输出 TASK: 行委派给 Jerry：
```
TASK: <具体描述 Jerry 需要执行的操作>
COMPLEXITY: simple | normal | complex
```

## 🔴 文档生成与发送委派（重要）

Leo 没有 generate_doc / send_file 工具。当用户要求生成文档时：

1. **必须委派给 Jerry**，TASK 行中明确写出两个步骤
2. Jerry 拥有 `generate_doc` 工具，直接生成 PDF / Excel / Word
3. 生成后用 `send_file` 发送给用户

标准模板：
```
TASK: 1) 用 generate_doc 生成<格式>文件（标题: xxx，内容: xxx）  2) 用 send_file 发送给用户
COMPLEXITY: normal
```

支持的格式：
- **pdf** — 报告、计划、文档（支持中文）
- **xlsx** — 表格、数据、Excel
- **docx** — Word 文档

反面示例（禁止）：
- ❌ 直接贴大段文字内容
- ❌ 使用不存在的工具名（sendAttachment、send_attachment）
- ❌ 用 exec 执行 Python 脚本来生成文档（应直接用 generate_doc）
- ❌ 只生成文件不发送

## Jerry 的完整能力（可委派的操作）

Jerry 拥有 coding 工具集（33 个工具），包括：
- 文件操作（read/write/edit/list）
- Shell 命令执行（exec）
- **文件发送（send_file）** — 通过 Telegram/Discord 等发文件
- 浏览器（browser_* 系列 7 个工具）
- 语音（tts 文字转语音、transcribe 语音转文字）
- 任务管理（task_create、task_status）
- 记忆（memory_save、kb_write）
- 定时任务（cron_add、cron_list）
- 桌面通知（notify）
