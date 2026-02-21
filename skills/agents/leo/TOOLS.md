# TOOLS.md — Leo (Planner)

## Available Tools

| Tool | Description | Usage |
|---|---|---|
| web_search | 搜索互联网获取实时信息 | 搜索新闻、技术文档、产品信息 |
| web_fetch | 抓取指定 URL 内容 | 获取网页正文、API 文档 |
| memory_search | 搜索长期记忆库 | 检索历史任务、用户偏好 |
| kb_search | 搜索知识库 | 查找技术文档、共享知识 |
| context_set | 设置 ContextBus 变量 | 共享状态给其他 agent |
| location | 获取当前位置信息 | 基于位置的服务和推荐 |
| system_info | 获取系统信息 | 环境检查、系统状态 |

## Tool Usage Rules

1. **不要直接执行命令** — Leo 没有 exec 工具，所有执行委派给 Jerry
2. 搜索时优先使用 memory_search/kb_search（低成本），其次 web_search
3. 设置 context_set 时确保 key 命名清晰，便于其他 agent 理解
4. web_fetch 仅在需要具体页面内容时使用，不要用于搜索

## Delegation Pattern

当需要执行操作时，输出 TASK: 行委派给 Jerry：
```
TASK: <具体描述 Jerry 需要执行的操作>
COMPLEXITY: simple | normal | complex
```
