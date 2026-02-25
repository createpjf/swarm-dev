---
name: pdf
description: Create PDF files and send them to users via Telegram/channel using send_file tool.
tags: [pdf, file, telegram, channel]
---

# PDF Creation and Delivery

## Creating a PDF

Use Python to generate PDF files. Recommended built-in libraries:

### Method 1: Using exec tool + reportlab (preferred)

```tool
{"tool": "exec", "params": {"cmd": "pip install reportlab -q && python3 -c \"\nfrom reportlab.lib.pagesizes import A4\nfrom reportlab.pdfgen import canvas\nc = canvas.Canvas('/tmp/output.pdf', pagesize=A4)\nc.drawString(72, 750, 'Hello World')\nc.save()\nprint('PDF created: /tmp/output.pdf')\n\""}}
```

### Method 2: Using exec tool + fpdf2 (lightweight, supports Chinese)

```tool
{"tool": "exec", "params": {"cmd": "pip install fpdf2 -q && python3 << 'PYEOF'\nfrom fpdf import FPDF\npdf = FPDF()\npdf.add_page()\npdf.set_font('Helvetica', size=16)\npdf.cell(text='Title Here', new_x='LMARGIN', new_y='NEXT')\npdf.set_font('Helvetica', size=12)\npdf.multi_cell(w=0, text='Body content here...')\npdf.output('/tmp/output.pdf')\nprint('PDF created: /tmp/output.pdf')\nPYEOF"}}
```

### Method 3: Using write_file + Markdown to PDF (if md2pdf is installed)

```tool
{"tool": "exec", "params": {"cmd": "pip install md2pdf -q && python3 -c \"from md2pdf.core import md2pdf; md2pdf('/tmp/output.pdf', md_content='# Title\\n\\nContent here')\""}}
```

## Sending Files to Users

After creating a file, **you must use the `send_file` tool** to send the file to the user's chat channel (Telegram/Discord, etc.):

```tool
{"tool": "send_file", "params": {"file_path": "/tmp/output.pdf", "caption": "Here is the PDF you requested"}}
```

### Important Rules

1. **Create the file first, then send it**: Make sure the file path exists and is valid
2. **Use absolute paths**: Always use `/tmp/` or other absolute paths
3. **send_file is the only way to send files**: Do not try to send files by running Python code via exec
4. **File size limit**: Telegram maximum is 50MB
5. **Caption limit**: Telegram captions have a maximum length of 1024 characters

## Complete Workflow Example

When a user asks to create and send a PDF, follow these steps:

**Step 1** — Create the PDF file:
```tool
{"tool": "exec", "params": {"cmd": "python3 << 'EOF'\nfrom fpdf import FPDF\npdf = FPDF()\npdf.add_page()\npdf.set_font('Helvetica', 'B', 20)\npdf.cell(text='Report Title', new_x='LMARGIN', new_y='NEXT')\npdf.ln(10)\npdf.set_font('Helvetica', '', 12)\npdf.multi_cell(w=0, text='Report content goes here...')\npdf.output('/tmp/report.pdf')\nprint('OK')\nEOF"}}
```

**Step 2** — Send to the user's chat:
```tool
{"tool": "send_file", "params": {"file_path": "/tmp/report.pdf", "caption": "Your report has been generated"}}
```

## Notes

- If send_file returns the error "No active channel session", it means the task was not initiated from Telegram/a channel; instead, inform the user of the file path
- Chinese content requires registering a Chinese font (fpdf2 supports the `add_font` method to load TTF fonts)
- Consider pagination when generating large PDFs
