"""
core/tools.py
Built-in tool registry — OpenClaw-inspired agent tool system.

Architecture:
  - Tools are callable functions with JSON-schema parameters
  - Agent system prompts include tool descriptions
  - Agents invoke tools via structured JSON blocks in their output
  - Tool results are fed back to the agent as context

Tool categories (18 tools across 7 groups):
  - Web:        web_search (Brave + Perplexity), web_fetch (text + markdown)
  - Filesystem: read_file, write_file, edit_file, list_dir
  - Memory:     memory_search, memory_save, kb_search, kb_write
  - Task:       task_create, task_status
  - Automation: exec, cron, process
  - Media:      screenshot, notify
  - Messaging:  send_mail

Access control:
  - Tool profiles: "minimal", "coding", "full"
  - Per-agent allow/deny lists in agents.yaml
  - Deny always wins over allow

Usage in agents.yaml:
  agents:
    - id: researcher
      tools:
        profile: "full"            # or "coding", "minimal"
        allow: ["web_search"]      # additional allowlist
        deny: ["exec", "write_file"]  # explicit denylist
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL SCHEMA
# ══════════════════════════════════════════════════════════════════════════════

class Tool:
    """A single tool that agents can invoke."""

    def __init__(self, name: str, description: str,
                 parameters: dict[str, dict],
                 handler: Callable[..., dict],
                 group: str = "",
                 requires_env: list[str] | None = None):
        self.name = name
        self.description = description
        self.parameters = parameters        # {param_name: {type, description, required?}}
        self.handler = handler
        self.group = group                  # e.g. "web", "automation", "media", "fs"
        self.requires_env = requires_env or []

    def is_available(self) -> bool:
        """Check if required env vars are set."""
        for env in self.requires_env:
            if not os.environ.get(env):
                return False
        return True

    def to_prompt(self) -> str:
        """Generate tool description for system prompt injection."""
        params_desc = []
        for pname, pinfo in self.parameters.items():
            req = " (required)" if pinfo.get("required") else ""
            params_desc.append(
                f"    - {pname}: {pinfo.get('type', 'string')} — "
                f"{pinfo.get('description', '')}{req}")
        params_str = "\n".join(params_desc) if params_desc else "    (no parameters)"
        return (f"### {self.name}\n"
                f"{self.description}\n"
                f"  Parameters:\n{params_str}\n")

    def to_schema(self) -> dict:
        """Generate JSON schema for function-calling LLMs."""
        properties = {}
        required = []
        for pname, pinfo in self.parameters.items():
            properties[pname] = {
                "type": pinfo.get("type", "string"),
                "description": pinfo.get("description", ""),
            }
            if pinfo.get("required"):
                required.append(pname)
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    def execute(self, **kwargs) -> dict:
        """Execute the tool handler."""
        try:
            return self.handler(**kwargs)
        except Exception as e:
            logger.error("Tool %s error: %s", self.name, e)
            return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL PROFILES (access control)
# ══════════════════════════════════════════════════════════════════════════════

TOOL_PROFILES = {
    "minimal": {"web_search", "web_fetch", "memory_search", "kb_search"},
    "coding": {"web_search", "web_fetch", "exec", "read_file", "write_file",
               "edit_file", "list_dir", "process", "memory_search", "memory_save",
               "kb_search", "kb_write", "task_create", "task_status"},
    "full": None,  # None = all tools allowed
}

# Tool groups for bulk allow/deny
TOOL_GROUPS = {
    "group:web": ["web_search", "web_fetch"],
    "group:automation": ["exec", "cron", "process"],
    "group:media": ["screenshot", "notify"],
    "group:fs": ["read_file", "write_file", "edit_file", "list_dir"],
    "group:memory": ["memory_search", "memory_save", "kb_search", "kb_write"],
    "group:task": ["task_create", "task_status"],
    "group:messaging": ["send_mail"],
}


# ══════════════════════════════════════════════════════════════════════════════
#  BUILT-IN TOOL HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

# ── Web tool cache (15-minute TTL) ──
_web_cache: dict[str, tuple[float, dict]] = {}
_WEB_CACHE_TTL = 900  # 15 minutes


def _cache_get(key: str) -> dict | None:
    """Get cached result if still fresh."""
    if key in _web_cache:
        ts, result = _web_cache[key]
        if time.time() - ts < _WEB_CACHE_TTL:
            result["_cached"] = True
            return result
        del _web_cache[key]
    return None


def _cache_set(key: str, result: dict) -> dict:
    """Cache a result and evict stale entries (max 100)."""
    now = time.time()
    # Evict stale
    stale = [k for k, (ts, _) in _web_cache.items() if now - ts >= _WEB_CACHE_TTL]
    for k in stale:
        del _web_cache[k]
    # Evict oldest if over limit
    if len(_web_cache) >= 100:
        oldest_key = min(_web_cache, key=lambda k: _web_cache[k][0])
        del _web_cache[oldest_key]
    _web_cache[key] = (now, result)
    return result


def _is_private_hostname(hostname: str) -> bool:
    """Block requests to private/internal hostnames."""
    import socket
    blocked = {"localhost", "127.0.0.1", "0.0.0.0", "::1",
               "metadata.google.internal", "169.254.169.254"}
    if hostname.lower() in blocked:
        return True
    # Block private IP ranges
    try:
        ip = socket.gethostbyname(hostname)
        parts = ip.split(".")
        if len(parts) == 4:
            a, b = int(parts[0]), int(parts[1])
            if a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
                return True
            if ip.startswith("127.") or ip.startswith("0."):
                return True
    except (socket.gaierror, ValueError):
        pass
    return False


def _handle_web_search(query: str, count: int = 5, freshness: str = "",
                       country: str = "", search_lang: str = "",
                       ui_lang: str = "", provider: str = "", **_) -> dict:
    """Search the web. Supports Brave Search API and Perplexity Sonar.

    Provider auto-detection:
      - If BRAVE_API_KEY is set → Brave Search (default)
      - If PERPLEXITY_API_KEY is set → Perplexity Sonar (fallback or explicit)
      - Use provider="perplexity" to force Perplexity
    """
    # Check cache
    cache_key = f"search:{query}:{count}:{freshness}:{country}:{search_lang}:{provider}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    brave_key = os.environ.get("BRAVE_API_KEY", "")
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "")
    use_perplexity = (provider == "perplexity" and pplx_key) or (not brave_key and pplx_key)

    if use_perplexity:
        return _cache_set(cache_key,
                          _search_perplexity(query, int(count), pplx_key))

    if not brave_key:
        return {"ok": False, "error": "No search API configured. Set BRAVE_API_KEY "
                "(https://brave.com/search/api/) or PERPLEXITY_API_KEY."}

    params: dict[str, Any] = {"q": query, "count": min(int(count), 20)}
    if freshness:
        params["freshness"] = freshness
    if country:
        params["country"] = country        # e.g. "US", "CN", "JP"
    if search_lang:
        params["search_lang"] = search_lang  # e.g. "en", "zh", "ja"
    if ui_lang:
        params["ui_lang"] = ui_lang          # e.g. "en-US", "zh-CN"

    url = ("https://api.search.brave.com/res/v1/web/search?"
           + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": brave_key,
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            data = json.loads(raw)

        results = []
        for item in (data.get("web", {}).get("results", []))[:int(count)]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        result = {"ok": True, "query": query, "results": results,
                  "total": len(results), "provider": "brave"}
        return _cache_set(cache_key, result)
    except Exception as e:
        # Auto-fallback to Perplexity if Brave fails and key is available
        if pplx_key and provider != "brave":
            logger.warning("Brave search failed, falling back to Perplexity: %s", e)
            return _cache_set(cache_key,
                              _search_perplexity(query, int(count), pplx_key))
        return {"ok": False, "error": f"Search failed: {e}"}


def _search_perplexity(query: str, count: int, api_key: str) -> dict:
    """Search using Perplexity Sonar API (chat-based search)."""
    payload = json.dumps({
        "model": "sonar",
        "messages": [{"role": "user", "content": query}],
        "max_tokens": 1024,
    }).encode()

    req = urllib.request.Request(
        "https://api.perplexity.ai/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        citations = data.get("citations", [])

        results = []
        for i, cite in enumerate(citations[:count]):
            results.append({
                "title": cite if isinstance(cite, str) else cite.get("title", f"Source {i+1}"),
                "url": cite if isinstance(cite, str) else cite.get("url", ""),
                "snippet": "",
            })
        # If no structured citations, return the answer text as a single result
        if not results:
            results.append({
                "title": "Perplexity Answer",
                "url": "",
                "snippet": content[:500],
            })

        return {"ok": True, "query": query, "results": results,
                "total": len(results), "provider": "perplexity",
                "answer": content[:2000]}
    except Exception as e:
        return {"ok": False, "error": f"Perplexity search failed: {e}"}


def _handle_web_fetch(url: str, max_chars: int = 8000,
                      extract_mode: str = "text",
                      timeout: int = 15, **_) -> dict:
    """Fetch URL content and extract readable content.

    extract_mode:
      - "text" (default): Plain text extraction — removes all HTML tags
      - "markdown": Convert HTML to simplified Markdown (headings, links, lists)
    """
    # Validate URL
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return {"ok": False, "error": "Invalid URL — must include scheme (https://)"}

    # Block private hostnames
    hostname = parsed.hostname or ""
    if _is_private_hostname(hostname):
        return {"ok": False, "error": f"Blocked: private/internal hostname '{hostname}'"}

    # Check cache
    cache_key = f"fetch:{url}:{extract_mode}:{max_chars}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "SwarmBot/1.0 (https://github.com/createpjf/swarm-dev)",
            "Accept-Encoding": "gzip, deflate",
            "Accept": "text/html,application/xhtml+xml,text/plain,*/*",
        })
        # Follow up to 5 redirects (urllib default), but cap response size
        with urllib.request.urlopen(req, timeout=int(timeout)) as resp:
            content_type = resp.headers.get("Content-Type", "")
            encoding = resp.headers.get("Content-Encoding", "")
            # Cap at 1MB to prevent memory issues
            raw = resp.read(1_000_000)
            final_url = resp.url  # capture redirect target

        # Decompress if gzipped
        if encoding == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            import zlib
            raw = zlib.decompress(raw)

        # Try to detect charset from content-type
        charset = "utf-8"
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=")[-1].split(";")[0].strip()
        text = raw.decode(charset, errors="ignore")

        if "html" in content_type.lower():
            if extract_mode == "markdown":
                text = _html_to_markdown(text)
            else:
                text = _html_to_text(text)

        text = text[:int(max_chars)]
        result = {"ok": True, "url": final_url or url, "content": text,
                  "chars": len(text), "extract_mode": extract_mode}
        if final_url and final_url != url:
            result["redirected_from"] = url
        return _cache_set(cache_key, result)
    except Exception as e:
        return {"ok": False, "error": f"Fetch failed: {e}"}


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text — strips all tags."""
    # Remove script/style/nav/footer
    for tag in ("script", "style", "nav", "footer", "header", "noscript"):
        html = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', '', html,
                       flags=re.DOTALL | re.IGNORECASE)
    # Remove comments
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    # Remove tags
    html = re.sub(r'<[^>]+>', ' ', html)
    # Decode HTML entities
    html = html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    # Collapse whitespace
    html = re.sub(r'\s+', ' ', html).strip()
    return html


def _html_to_markdown(html: str) -> str:
    """Convert HTML to simplified Markdown — preserves structure."""
    # Remove script/style/nav/footer/noscript
    for tag in ("script", "style", "nav", "footer", "noscript"):
        html = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', '', html,
                       flags=re.DOTALL | re.IGNORECASE)
    # Remove comments
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)

    # ── 1. Inline elements first (before block elements) ──

    # Convert bold/strong (use [\s>] boundary to avoid matching <body> etc.)
    html = re.sub(r'<(?:b|strong)(?:\s[^>]*)?>(.+?)</(?:b|strong)>', r'**\1**', html,
                   flags=re.DOTALL | re.IGNORECASE)
    # Convert italic/em (use [\s>] boundary to avoid matching <iframe> etc.)
    html = re.sub(r'<(?:i|em)(?:\s[^>]*)?>(.+?)</(?:i|em)>', r'*\1*', html,
                   flags=re.DOTALL | re.IGNORECASE)
    # Convert inline code
    html = re.sub(r'<code[^>]*>(.*?)</code>', r'`\1`', html,
                   flags=re.DOTALL | re.IGNORECASE)
    # Convert links: <a href="url">text</a> → [text](url)
    html = re.sub(
        r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        r'[\2](\1)', html, flags=re.DOTALL | re.IGNORECASE)
    # Convert images: <img src="url" alt="text"> → ![text](url)
    html = re.sub(r'<img[^>]*src="([^"]*)"[^>]*alt="([^"]*)"[^>]*/?>',
                   r'![\2](\1)', html, flags=re.IGNORECASE)
    html = re.sub(r'<img[^>]*src="([^"]*)"[^>]*/?>',
                   r'![image](\1)', html, flags=re.IGNORECASE)

    # ── 2. Block elements ──

    # Convert pre/code blocks (before heading/paragraph stripping)
    html = re.sub(r'<pre[^>]*>(.*?)</pre>', r'\n```\n\1\n```\n', html,
                   flags=re.DOTALL | re.IGNORECASE)

    # Convert headings
    for level in range(1, 7):
        prefix = "#" * level
        html = re.sub(
            rf'<h{level}[^>]*>(.*?)</h{level}>',
            rf'\n\n{prefix} \1\n\n', html,
            flags=re.DOTALL | re.IGNORECASE)

    # Convert list items
    html = re.sub(r'<li[^>]*>(.*?)</li>', r'\n- \1', html,
                   flags=re.DOTALL | re.IGNORECASE)

    # Convert paragraphs and line breaks
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<p[^>]*>(.*?)</p>', r'\n\n\1\n\n', html,
                   flags=re.DOTALL | re.IGNORECASE)

    # ── 3. Clean up ──

    # Remove remaining tags
    html = re.sub(r'<[^>]+>', '', html)
    # Decode HTML entities
    html = html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    # Collapse excessive newlines
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html.strip()


def _handle_exec(command: str, timeout: int = 120, **_) -> dict:
    """Execute a shell command with approval gating."""
    from core.exec_tool import execute
    return execute(command=command, agent_id="tool", timeout=int(timeout))


def _handle_cron_list(**_) -> dict:
    """List all scheduled cron jobs."""
    from core.cron import list_jobs
    jobs = list_jobs()
    return {"ok": True, "jobs": jobs, "total": len(jobs)}


def _handle_cron_add(name: str, action: str, payload: str,
                     schedule_type: str, schedule: str, **_) -> dict:
    """Create a new scheduled job."""
    from core.cron import add_job
    job = add_job(name, action, payload, schedule_type, schedule)
    return {"ok": True, "job": job}


def _handle_process_list(**_) -> dict:
    """List running background processes."""
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().split("\n")
        return {"ok": True, "processes": lines[:50],
                "total": len(lines) - 1}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_screenshot(**_) -> dict:
    """Take a screenshot of the current desktop (macOS)."""
    import platform
    if platform.system() != "Darwin":
        return {"ok": False, "error": "Screenshots only supported on macOS"}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join("memory", f"screenshot_{ts}.png")
    os.makedirs("memory", exist_ok=True)

    try:
        result = subprocess.run(
            ["screencapture", "-x", path],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and os.path.exists(path):
            size = os.path.getsize(path)
            return {"ok": True, "path": path, "size": size}
        return {"ok": False, "error": result.stderr or "Failed to capture"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_notify(title: str, message: str = "", **_) -> dict:
    """Send a macOS notification."""
    import platform
    if platform.system() != "Darwin":
        return {"ok": False, "error": "Notifications only supported on macOS"}

    script = (f'display notification "{message}" '
              f'with title "{title}" sound name "Glass"')
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10)
        return {"ok": result.returncode == 0,
                "error": result.stderr if result.returncode else None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_read_file(path: str, max_lines: int = 200, **_) -> dict:
    """Read a file from the project directory."""
    # Safety: prevent reading outside project
    abs_path = os.path.abspath(path)
    cwd = os.path.abspath(".")
    if not abs_path.startswith(cwd):
        return {"ok": False, "error": "Cannot read files outside project"}

    if not os.path.exists(abs_path):
        return {"ok": False, "error": f"File not found: {path}"}

    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[:int(max_lines)]
        content = "".join(lines)
        return {"ok": True, "path": path, "content": content,
                "lines": len(lines)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_write_file(path: str, content: str, **_) -> dict:
    """Write content to a file in the project directory."""
    abs_path = os.path.abspath(path)
    cwd = os.path.abspath(".")
    if not abs_path.startswith(cwd):
        return {"ok": False, "error": "Cannot write files outside project"}

    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": path, "size": len(content)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_list_dir(path: str = ".", **_) -> dict:
    """List directory contents."""
    abs_path = os.path.abspath(path)
    cwd = os.path.abspath(".")
    if not abs_path.startswith(cwd):
        return {"ok": False, "error": "Cannot list outside project"}

    if not os.path.isdir(abs_path):
        return {"ok": False, "error": f"Not a directory: {path}"}

    try:
        entries = []
        for name in sorted(os.listdir(abs_path)):
            full = os.path.join(abs_path, name)
            entries.append({
                "name": name,
                "type": "dir" if os.path.isdir(full) else "file",
                "size": os.path.getsize(full) if os.path.isfile(full) else 0,
            })
        return {"ok": True, "path": path, "entries": entries[:100],
                "total": len(entries)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_edit_file(path: str, old_str: str, new_str: str, **_) -> dict:
    """Find-and-replace edit in a file (safe, project-scoped)."""
    abs_path = os.path.abspath(path)
    cwd = os.path.abspath(".")
    if not abs_path.startswith(cwd):
        return {"ok": False, "error": "Cannot edit files outside project"}

    if not os.path.exists(abs_path):
        return {"ok": False, "error": f"File not found: {path}"}

    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        count = content.count(old_str)
        if count == 0:
            return {"ok": False, "error": "old_str not found in file"}
        if count > 1:
            return {"ok": False, "error": f"old_str found {count} times — must be unique (include more context)"}

        new_content = content.replace(old_str, new_str, 1)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return {"ok": True, "path": path, "replacements": 1}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_memory_search(query: str, limit: int = 5, agent_id: str = "tool", **_) -> dict:
    """Search episodic memory for past cases (problem→solution pairs)."""
    try:
        from adapters.memory.episodic import EpisodicMemory
        mem = EpisodicMemory(agent_id=agent_id)
        cases = mem.search_cases(query, limit=int(limit))
        return {"ok": True, "results": cases, "total": len(cases)}
    except ImportError:
        return {"ok": False, "error": "Episodic memory module not available"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_memory_save(problem: str, solution: str,
                        tags: str = "", agent_id: str = "tool", **_) -> dict:
    """Save a problem→solution case to episodic memory."""
    try:
        from adapters.memory.episodic import EpisodicMemory
        mem = EpisodicMemory(agent_id=agent_id)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        case_id = mem.save_case(problem, solution, tags=tag_list)
        return {"ok": True, "case_id": case_id}
    except ImportError:
        return {"ok": False, "error": "Episodic memory module not available"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_kb_search(query: str, limit: int = 5, **_) -> dict:
    """Search the shared knowledge base for notes."""
    try:
        from adapters.memory.knowledge_base import KnowledgeBase
        kb = KnowledgeBase()
        notes = kb.search_notes(query, limit=int(limit))
        # Trim content for return
        results = []
        for n in notes:
            results.append({
                "topic": n.get("topic", ""),
                "slug": n.get("slug", ""),
                "content": n.get("content", "")[:500],
                "tags": n.get("tags", []),
                "author": n.get("author", ""),
            })
        return {"ok": True, "results": results, "total": len(results)}
    except ImportError:
        return {"ok": False, "error": "Knowledge base module not available"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_kb_write(topic: str, content: str, tags: str = "",
                     agent_id: str = "tool", **_) -> dict:
    """Create or update a note in the shared knowledge base."""
    try:
        from adapters.memory.knowledge_base import KnowledgeBase
        kb = KnowledgeBase()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        slug = kb.create_note(topic, content, tags=tag_list, author=agent_id)
        return {"ok": True, "slug": slug, "topic": topic}
    except ImportError:
        return {"ok": False, "error": "Knowledge base module not available"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_task_create(description: str, **_) -> dict:
    """Create a new task on the task board."""
    try:
        from core.task_board import TaskBoard
        board = TaskBoard()
        task = board.create(description)
        return {"ok": True, "task_id": task.id, "description": task.description,
                "status": task.status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_task_status(task_id: str = "", **_) -> dict:
    """Get task status from the task board. If no task_id, list recent tasks."""
    try:
        if not os.path.exists(".task_board.json"):
            return {"ok": True, "tasks": [], "total": 0}

        with open(".task_board.json") as f:
            data = json.load(f)

        if task_id:
            # Find by prefix match
            matches = {tid: t for tid, t in data.items()
                       if tid.startswith(task_id)}
            if not matches:
                return {"ok": False, "error": f"No task matching '{task_id}'"}
            return {"ok": True, "tasks": matches}

        # Return last 10 tasks
        items = list(data.items())[-10:]
        recent = {tid: {"status": t["status"],
                        "agent_id": t.get("agent_id", ""),
                        "description": t["description"][:80]}
                  for tid, t in items}
        return {"ok": True, "tasks": recent, "total": len(data)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_send_mail(to: str, content: str,
                      agent_id: str = "tool", msg_type: str = "message",
                      **_) -> dict:
    """Send a message to another agent's mailbox."""
    try:
        mailbox_dir = ".mailbox"
        os.makedirs(mailbox_dir, exist_ok=True)
        mailbox_file = os.path.join(mailbox_dir, f"{to}.jsonl")

        entry = {
            "from": agent_id,
            "type": msg_type,
            "content": content,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with open(mailbox_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

        return {"ok": True, "to": to, "from": agent_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

_BUILTIN_TOOLS: list[Tool] = [
    # ── Web tools ──
    Tool("web_search",
         "Search the web. Supports Brave Search and Perplexity Sonar with auto-fallback. "
         "Results are cached for 15 minutes.",
         {"query": {"type": "string", "description": "Search query", "required": True},
          "count": {"type": "integer", "description": "Number of results (1-20, default 5)", "required": False},
          "freshness": {"type": "string", "description": "Time filter: pd (past day), pw (past week), pm (past month), py (past year)", "required": False},
          "country": {"type": "string", "description": "Country code for results, e.g. US, CN, JP, DE", "required": False},
          "search_lang": {"type": "string", "description": "Search language, e.g. en, zh, ja", "required": False},
          "ui_lang": {"type": "string", "description": "UI language, e.g. en-US, zh-CN", "required": False},
          "provider": {"type": "string", "description": "Force search provider: brave or perplexity (auto-detected by default)", "required": False}},
         _handle_web_search, group="web"),

    Tool("web_fetch",
         "Fetch a URL and extract content. Supports text and markdown extraction modes. "
         "Blocks private/internal hostnames. Results cached for 15 minutes.",
         {"url": {"type": "string", "description": "URL to fetch (must include https://)", "required": True},
          "max_chars": {"type": "integer", "description": "Max chars to return (default 8000)", "required": False},
          "extract_mode": {"type": "string", "description": "Extraction mode: 'text' (plain text, default) or 'markdown' (preserves structure)", "required": False},
          "timeout": {"type": "integer", "description": "Request timeout in seconds (default 15)", "required": False}},
         _handle_web_fetch, group="web"),

    # ── Automation tools ──
    Tool("exec",
         "Execute a shell command (approval-gated). Only allowlisted commands are permitted.",
         {"command": {"type": "string", "description": "Shell command to run", "required": True},
          "timeout": {"type": "integer", "description": "Max seconds (default 120)", "required": False}},
         _handle_exec, group="automation"),

    Tool("cron",
         "Manage scheduled jobs. Lists existing cron jobs.",
         {},
         _handle_cron_list, group="automation"),

    Tool("process",
         "List running system processes.",
         {},
         _handle_process_list, group="automation"),

    # ── Media tools ──
    Tool("screenshot",
         "Capture a screenshot of the current desktop (macOS only).",
         {},
         _handle_screenshot, group="media"),

    Tool("notify",
         "Send a desktop notification (macOS only).",
         {"title": {"type": "string", "description": "Notification title", "required": True},
          "message": {"type": "string", "description": "Notification body", "required": False}},
         _handle_notify, group="media"),

    # ── Filesystem tools ──
    Tool("read_file",
         "Read a file from the project directory.",
         {"path": {"type": "string", "description": "File path relative to project root", "required": True},
          "max_lines": {"type": "integer", "description": "Max lines to read (default 200)", "required": False}},
         _handle_read_file, group="fs"),

    Tool("write_file",
         "Write content to a file in the project directory.",
         {"path": {"type": "string", "description": "File path relative to project root", "required": True},
          "content": {"type": "string", "description": "Content to write", "required": True}},
         _handle_write_file, group="fs"),

    Tool("list_dir",
         "List directory contents.",
         {"path": {"type": "string", "description": "Directory path (default: project root)", "required": False}},
         _handle_list_dir, group="fs"),

    Tool("edit_file",
         "Find-and-replace edit in a project file. The old_str must be unique in the file.",
         {"path": {"type": "string", "description": "File path relative to project root", "required": True},
          "old_str": {"type": "string", "description": "Exact text to find (must be unique)", "required": True},
          "new_str": {"type": "string", "description": "Replacement text", "required": True}},
         _handle_edit_file, group="fs"),

    # ── Memory tools ──
    Tool("memory_search",
         "Search episodic memory for past problem→solution cases.",
         {"query": {"type": "string", "description": "Search query", "required": True},
          "limit": {"type": "integer", "description": "Max results (default 5)", "required": False}},
         _handle_memory_search, group="memory"),

    Tool("memory_save",
         "Save a problem→solution case to episodic memory for future recall.",
         {"problem": {"type": "string", "description": "Problem description", "required": True},
          "solution": {"type": "string", "description": "Solution description", "required": True},
          "tags": {"type": "string", "description": "Comma-separated tags", "required": False}},
         _handle_memory_save, group="memory"),

    Tool("kb_search",
         "Search the shared knowledge base for notes and insights.",
         {"query": {"type": "string", "description": "Search query", "required": True},
          "limit": {"type": "integer", "description": "Max results (default 5)", "required": False}},
         _handle_kb_search, group="memory"),

    Tool("kb_write",
         "Create or update a note in the shared knowledge base (Zettelkasten).",
         {"topic": {"type": "string", "description": "Note topic/title", "required": True},
          "content": {"type": "string", "description": "Note content", "required": True},
          "tags": {"type": "string", "description": "Comma-separated tags", "required": False}},
         _handle_kb_write, group="memory"),

    # ── Task tools ──
    Tool("task_create",
         "Create a new task on the task board.",
         {"description": {"type": "string", "description": "Task description", "required": True}},
         _handle_task_create, group="task"),

    Tool("task_status",
         "Get task status. Without task_id, lists recent tasks.",
         {"task_id": {"type": "string", "description": "Task ID or prefix (optional)", "required": False}},
         _handle_task_status, group="task"),

    # ── Messaging tools ──
    Tool("send_mail",
         "Send a message to another agent's mailbox for inter-agent communication.",
         {"to": {"type": "string", "description": "Target agent ID", "required": True},
          "content": {"type": "string", "description": "Message content", "required": True},
          "msg_type": {"type": "string", "description": "Message type (default: message)", "required": False}},
         _handle_send_mail, group="messaging"),
]

# Keyed registry for fast lookup
_registry: dict[str, Tool] = {t.name: t for t in _BUILTIN_TOOLS}


def get_tool(name: str) -> Tool | None:
    """Get a tool by name."""
    return _registry.get(name)


def list_all_tools() -> list[Tool]:
    """List all registered tools."""
    return list(_BUILTIN_TOOLS)


def get_available_tools(agent_config: dict | None = None) -> list[Tool]:
    """Get tools available to an agent based on its config.

    agent_config can have:
      tools.profile: "minimal" | "coding" | "full"
      tools.allow: ["tool_name", "group:web"]
      tools.deny: ["tool_name", "group:automation"]
    """
    tools_cfg = (agent_config or {}).get("tools", {})
    profile = tools_cfg.get("profile", "full")
    allow_extra = set(tools_cfg.get("allow", []))
    deny_list = set(tools_cfg.get("deny", []))

    # Expand groups
    expanded_allow = set()
    for item in allow_extra:
        if item in TOOL_GROUPS:
            expanded_allow.update(TOOL_GROUPS[item])
        else:
            expanded_allow.add(item)

    expanded_deny = set()
    for item in deny_list:
        if item in TOOL_GROUPS:
            expanded_deny.update(TOOL_GROUPS[item])
        else:
            expanded_deny.add(item)

    # Base set from profile
    base_set = TOOL_PROFILES.get(profile)  # None = full

    available = []
    for tool in _BUILTIN_TOOLS:
        # Check deny first (deny always wins)
        if tool.name in expanded_deny:
            continue

        # Check base profile
        if base_set is not None and tool.name not in base_set:
            # Not in base profile — check if explicitly allowed
            if tool.name not in expanded_allow:
                continue

        # Check env requirements
        if not tool.is_available():
            continue

        available.append(tool)

    return available


def build_tools_prompt(agent_config: dict | None = None) -> str:
    """Build the tools section for agent system prompt."""
    tools = get_available_tools(agent_config)
    if not tools:
        return ""

    lines = [
        "## Available Tools",
        "",
        "You can invoke tools by including a JSON block in your response:",
        "```tool",
        '{"tool": "tool_name", "params": {"param1": "value1"}}',
        "```",
        "",
        "Available tools:",
        "",
    ]
    for t in tools:
        lines.append(t.to_prompt())

    return "\n".join(lines)


def build_tools_schemas(agent_config: dict | None = None) -> list[dict]:
    """Build tool schemas for function-calling LLMs."""
    tools = get_available_tools(agent_config)
    return [t.to_schema() for t in tools]


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL INVOCATION PARSER
# ══════════════════════════════════════════════════════════════════════════════

# Matches ```tool\n{...}\n``` blocks in agent output
_TOOL_BLOCK_RE = re.compile(
    r'```tool\s*\n(\{[^`]+?\})\s*\n```',
    re.DOTALL)


def parse_tool_calls(text: str) -> list[dict]:
    """Extract tool invocation blocks from agent output.

    Returns list of {"tool": "name", "params": {...}}
    """
    calls = []
    for match in _TOOL_BLOCK_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
            if "tool" in data:
                calls.append({
                    "tool": data["tool"],
                    "params": data.get("params", {}),
                    "raw": match.group(0),
                })
        except json.JSONDecodeError:
            continue
    return calls


def execute_tool_calls(calls: list[dict],
                       agent_config: dict | None = None) -> list[dict]:
    """Execute parsed tool calls and return results.

    Returns list of {"tool": "name", "result": {...}}
    """
    available = {t.name for t in get_available_tools(agent_config)}
    results = []
    for call in calls:
        name = call["tool"]
        if name not in available:
            results.append({
                "tool": name,
                "result": {"ok": False, "error": f"Tool '{name}' not available"},
            })
            continue

        tool = _registry.get(name)
        if not tool:
            results.append({
                "tool": name,
                "result": {"ok": False, "error": f"Unknown tool: {name}"},
            })
            continue

        logger.info("Executing tool: %s(%s)", name,
                     str(call.get("params", {}))[:100])
        result = tool.execute(**call.get("params", {}))
        results.append({"tool": name, "result": result})

    return results
