# TOOLS.md — Jerry (Executor)

## Available Tools

| Tool | Description | Usage |
|---|---|---|
| exec | 执行 Shell/Python 命令 | 系统操作、脚本运行、包安装 |
| read_file | 读取文件内容 | 查看代码、配置、日志 |
| write_file | 写入文件 | 创建新文件、覆盖内容 |
| edit_file | 编辑文件 | 精确查找替换修改 |
| list_dir | 列出目录内容 | 浏览项目结构 |
| web_search | 搜索互联网 | 查找技术方案、文档 |
| web_fetch | 抓取 URL 内容 | 获取 API 响应、网页内容 |
| memory_search | 搜索记忆库 | 检索历史方案 |
| kb_search | 搜索知识库 | 查找共享知识 |
| context_set | 设置共享上下文 | 发布执行结果 |
| cron_add | 添加定时任务 | 设置定时执行 |

## Tool Usage Rules

1. **exec 执行前先 reasoning** — 解释为什么执行这个命令
2. 文件操作优先使用 edit_file（精确修改），避免 write_file 覆盖
3. 执行失败时，分析错误日志并尝试修复（同一任务范围内）
4. 网络请求失败时，重试一次后上报
5. 所有文件操作限制在项目根目录内

## Execution Standards

- 命令执行超时: 60 秒
- 文件大小限制: 单文件 < 10MB
- 日志输出: 保留完整，不裁剪
- 错误处理: 捕获异常，返回完整 traceback
