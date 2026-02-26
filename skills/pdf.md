---
name: pdf
description: Create document files (PDF, Word, Excel, PowerPoint, CSV, HTML, TXT, Markdown) and send them to users via channel.
tags: [pdf, file, telegram, channel, docx, xlsx, pptx, csv, html]
---

# Document Generation and Delivery

## Using `generate_doc` tool

The `generate_doc` tool supports **8 output formats**:

| Format | Aliases | Input | Library |
|--------|---------|-------|---------|
| `pdf` | — | Markdown text | fpdf2 (CJK supported) |
| `docx` | `word` | Markdown text | python-docx |
| `xlsx` | `excel` | JSON array / markdown table | openpyxl |
| `pptx` | `powerpoint`, `ppt` | Markdown (## = new slide) | python-pptx |
| `csv` | — | JSON / markdown table / TSV | stdlib |
| `txt` | `text` | Any (markdown stripped) | stdlib |
| `md` | `markdown` | Any (pass-through) | stdlib |
| `html` | `htm` | Markdown text | stdlib |

## Quick Examples

### PDF (with Chinese support)

```tool
{"tool": "generate_doc", "params": {"format": "pdf", "content": "# 报告标题\n\n这是正文内容。\n\n| 项目 | 状态 |\n|------|------|\n| A | 完成 |\n| B | 进行中 |", "title": "月度报告"}}
```

### PowerPoint Presentation

```tool
{"tool": "generate_doc", "params": {"format": "pptx", "content": "## 第一页\n\n- 要点一\n- 要点二\n\n## 第二页\n\n| 数据 | 值 |\n|------|----|\n| X | 100 |", "title": "项目汇报"}}
```

### Excel Spreadsheet

```tool
{"tool": "generate_doc", "params": {"format": "xlsx", "content": "[{\"name\": \"张三\", \"age\": 25}, {\"name\": \"李四\", \"age\": 30}]", "title": "人员表"}}
```

### CSV from Markdown Table

```tool
{"tool": "generate_doc", "params": {"format": "csv", "content": "| 姓名 | 年龄 |\n|------|------|\n| 张三 | 25 |\n| 李四 | 30 |"}}
```

### HTML Document

```tool
{"tool": "generate_doc", "params": {"format": "html", "content": "# 标题\n\n正文内容，支持**粗体**和*斜体*。\n\n## 表格\n\n| A | B |\n|---|---|\n| 1 | 2 |", "title": "文档"}}
```

## Sending Files to Users

After generating, **use `send_file`** to deliver:

```tool
{"tool": "send_file", "params": {"file_path": "/tmp/doc_xxx.pdf", "caption": "Your document"}}
```

## Important Rules

1. **Create then send**: generate_doc first, then send_file
2. **Use absolute paths**: Always use `/tmp/` or absolute paths
3. **Content limit**: Keep content under 2000 chars to avoid MiniMax truncation
4. **Auto-send**: If a channel session is active, generate_doc auto-sends
5. **PDF fallback**: If PDF fails (font issues), auto-retries as DOCX
6. **CJK support**: PDF/DOCX/PPTX/HTML all support Chinese/Japanese/Korean
7. **File size limit**: Telegram max is 50MB, caption max is 1024 chars

## PPTX Slide Structure

For PowerPoint, use `## Heading` to start each new slide:

```markdown
## Slide 1 Title
- Bullet point A
- Bullet point B

## Slide 2 Title
1. Numbered item
2. Another item

## Data Slide
| Column A | Column B |
|----------|----------|
| Data 1   | Data 2   |
```

This creates 3 slides plus a title slide.
