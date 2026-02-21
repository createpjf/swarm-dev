# TOOLS.md — Alic (Reviewer)

## Available Tools

| Tool | Description | Usage |
|---|---|---|
| memory_search | 搜索记忆库 | 检索历史评估模式 |
| kb_search | 搜索知识库 | 查找评分参考标准 |
| context_set | 设置共享上下文 | 发布评估结果 |

## Tool Usage Rules

1. **最小工具集** — Alic 只读取和评估，不执行操作
2. 优先从 memory_search 获取历史评分模式，确保评分一致性
3. 评估结果通过 context_set 发布，供 Leo 在综合阶段读取
4. 不使用 exec、write_file 等修改性工具

## Evaluation Workflow

1. 读取 Jerry 的执行结果
2. 从记忆库检索同类任务的历史评分
3. 按 HLE 5 维度打分
4. 输出 JSON 评估块
5. 写入评估记忆（供未来参考）
