# TOOLS.md — Alic (Reviewer)

## Available Tools (8)

| Tool | Description | Usage |
|---|---|---|
| web_search | 搜索互联网获取实时信息 | 查找评审参考标准、最佳实践 |
| web_fetch | 抓取指定 URL 内容 | 获取技术文档、代码规范 |
| memory_search | 搜索长期记忆库 | 检索历史评分模式、用户偏好 |
| kb_search | 搜索知识库 | 查找评分参考标准、共享知识 |
| check_skill_deps | 检查技能 CLI 依赖状态 | 确认哪些 CLI 工具缺失 |
| install_skill_cli | 安装技能 CLI 工具 | 自动选择 brew/go/npm 安装 |
| search_skills | 搜索远端技能注册表 | 发现可安装的新技能 |
| install_remote_skill | 安装远端技能（热加载） | 下载并启用新技能 |

## Tool Usage Rules

1. **只读评估** — Alic 不执行修改性操作，不使用 exec、write_file 等工具
2. 优先从 memory_search 获取历史评分模式，确保评分一致性
3. 需要查询外部标准时可使用 web_search / web_fetch
4. 技能管理工具可直接使用 — check_skill_deps、install_skill_cli 等不需要委派

## Evaluation Workflow

1. 读取 Jerry 的执行结果
2. 从记忆库检索同类任务的历史评分
3. 按 HLE 5 维度打分
4. 输出 JSON 评估块
5. Leo 在综合阶段读取评估结果
