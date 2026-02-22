"""
core/tools.py
Built-in tool registry — OpenClaw-inspired agent tool system.

Architecture:
  - Tools are callable functions with JSON-schema parameters
  - Agent system prompts include tool descriptions
  - Agents invoke tools via structured JSON blocks in their output
  - Tool results are fed back to the agent as context

Tool categories (32 tools across 9 groups):
  - Web:        web_search (Brave + Perplexity), web_fetch (text + markdown)
  - Filesystem: read_file, write_file, edit_file, list_dir
  - Memory:     memory_search, memory_save, kb_search, kb_write
  - Task:       task_create, task_status
  - Automation: exec, cron, process
  - Skill:      check_skill_deps, install_skill_cli, search_skills, install_remote_skill
  - Browser:    browser_navigate, browser_click, browser_fill, browser_get_text,
                browser_screenshot, browser_evaluate, browser_page_info
  - Media:      screenshot, notify
  - Messaging:  send_mail, send_file

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
    "minimal": {"web_search", "web_fetch", "memory_search", "kb_search",
                "check_skill_deps", "install_skill_cli",
                "search_skills", "install_remote_skill"},
    "coding": {"web_search", "web_fetch", "exec", "read_file", "write_file",
               "edit_file", "list_dir", "process", "cron_list", "cron_add",
               "notify", "transcribe", "tts", "list_voices",
               "memory_search", "memory_save",
               "kb_search", "kb_write", "task_create", "task_status",
               "send_mail", "check_skill_deps", "install_skill_cli",
               "search_skills", "install_remote_skill",
               "browser_navigate", "browser_click", "browser_fill",
               "browser_get_text", "browser_screenshot",
               "browser_evaluate", "browser_page_info"},
    "full": None,  # None = all tools allowed
}

# Tool groups for bulk allow/deny
TOOL_GROUPS = {
    "group:web": ["web_search", "web_fetch"],
    "group:automation": ["exec", "cron_list", "cron_add", "process"],
    "group:media": ["screenshot", "notify", "transcribe", "tts", "list_voices"],
    "group:fs": ["read_file", "write_file", "edit_file", "list_dir"],
    "group:memory": ["memory_search", "memory_save", "kb_search", "kb_write"],
    "group:task": ["task_create", "task_status"],
    "group:skill": ["check_skill_deps", "install_skill_cli",
                    "search_skills", "install_remote_skill"],
    "group:messaging": ["send_mail"],
    "group:browser": ["browser_navigate", "browser_click", "browser_fill",
                      "browser_get_text", "browser_screenshot",
                      "browser_evaluate", "browser_page_info"],
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
            "User-Agent": "CleoBot/1.0 (https://github.com/createpjf/cleo-dev)",
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


def _handle_transcribe(file_path: str, language: str = "", **_) -> dict:
    """Transcribe audio using OpenAI Whisper API.

    Supports: mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg, oga, flac.
    Requires OPENAI_API_KEY environment variable.
    """
    import httpx

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY not set — required for Whisper transcription"}

    abs_path = os.path.abspath(file_path)
    if not os.path.isfile(abs_path):
        return {"ok": False, "error": f"File not found: {file_path}"}

    # Validate extension
    ext = os.path.splitext(abs_path)[1].lower()
    allowed = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav",
               ".webm", ".ogg", ".oga", ".flac"}
    if ext not in allowed:
        return {"ok": False,
                "error": f"Unsupported audio format '{ext}'. Supported: {', '.join(sorted(allowed))}"}

    # Check file size (Whisper limit: 25 MB)
    size = os.path.getsize(abs_path)
    if size > 25 * 1024 * 1024:
        return {"ok": False, "error": f"File too large ({size / 1024 / 1024:.1f} MB). Whisper limit is 25 MB."}

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    url = f"{base_url}/audio/transcriptions"

    try:
        with open(abs_path, "rb") as f:
            files = {"file": (os.path.basename(abs_path), f)}
            data = {"model": "whisper-1"}
            if language:
                data["language"] = language

            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                data=data,
                timeout=120,
            )

        if resp.status_code != 200:
            return {"ok": False, "error": f"Whisper API error {resp.status_code}: {resp.text[:300]}"}

        result = resp.json()
        text = result.get("text", "").strip()
        return {"ok": True, "text": text, "file": os.path.basename(abs_path),
                "size_kb": round(size / 1024, 1)}

    except httpx.TimeoutException:
        return {"ok": False, "error": "Whisper API request timed out (120s)"}
    except Exception as e:
        return {"ok": False, "error": f"Transcription failed: {e}"}


def _handle_tts(text: str, voice: str = "", speed: float = 1.0,
                provider: str = "", output_format: str = "mp3", **_) -> dict:
    """Synthesize text to speech audio file.

    Multi-provider TTS with automatic fallback:
      - OpenAI TTS (alloy/echo/nova/shimmer voices, needs OPENAI_API_KEY)
      - ElevenLabs (rachel/adam/sam voices, needs ELEVENLABS_API_KEY)
      - MiniMax (Chinese-optimized, needs MINIMAX_API_KEY + GROUP_ID)
      - Local (piper/sherpa-onnx, zero cost, needs binary installed)

    Returns path to generated audio file.
    """
    import asyncio
    try:
        from adapters.voice.tts_engine import get_tts_engine
    except ImportError:
        return {"ok": False, "error": "TTS engine not available"}

    engine = get_tts_engine()

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    lambda: asyncio.run(engine.synthesize(
                        text, voice=voice, speed=speed,
                        output_format=output_format, provider=provider))
                ).result(timeout=90)
        else:
            result = loop.run_until_complete(engine.synthesize(
                text, voice=voice, speed=speed,
                output_format=output_format, provider=provider))
    except RuntimeError:
        result = asyncio.run(engine.synthesize(
            text, voice=voice, speed=speed,
            output_format=output_format, provider=provider))

    return result


def _handle_list_voices(provider: str = "", **_) -> dict:
    """List available TTS voices across all providers."""
    try:
        from adapters.voice.tts_engine import get_tts_engine
    except ImportError:
        return {"ok": False, "error": "TTS engine not available"}

    engine = get_tts_engine()
    return {
        "ok": True,
        "providers": engine.list_providers(),
        "voices": engine.list_voices(provider=provider),
    }


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


def _handle_send_file(**kwargs) -> dict:
    """Send a file to the user via their chat channel.

    Requires the task to have originated from a channel message.
    The channel adapter will pick up the file and deliver it.
    """
    file_path = kwargs.get("file_path", "")
    caption = kwargs.get("caption", "")

    if not file_path:
        return {"ok": False, "error": "file_path parameter required"}

    if not os.path.exists(file_path):
        return {"ok": False, "error": f"File not found: {file_path}"}

    # Store file delivery request for the channel adapter to pick up
    try:
        delivery_dir = ".file_delivery"
        os.makedirs(delivery_dir, exist_ok=True)
        delivery = {
            "file_path": os.path.abspath(file_path),
            "caption": caption,
            "ts": time.time(),
        }
        delivery_file = os.path.join(delivery_dir, f"{int(time.time()*1000)}.json")
        with open(delivery_file, "w") as f:
            json.dump(delivery, f)
        return {"ok": True, "message": f"File queued for delivery: {file_path}",
                "delivery_id": os.path.basename(delivery_file)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Skill CLI install/check handlers ──

def _handle_check_skill_deps(**kwargs) -> dict:
    """Check which skill CLI dependencies are missing."""
    try:
        from core.skill_deps import (
            scan_skill_deps, get_missing_deps, get_installed_deps,
            check_prerequisites,
        )
    except ImportError:
        return {"ok": False, "error": "skill_deps module not available"}

    skill_name = kwargs.get("skill", "")

    if skill_name:
        # Check a specific skill
        all_deps = scan_skill_deps()
        match = [d for d in all_deps
                 if d["skill"] == skill_name or d["file"] == f"{skill_name}.md"]
        if not match:
            return {"ok": False, "error": f"Skill '{skill_name}' not found or has no CLI deps"}
        dep = match[0]
        return {
            "ok": True,
            "skill": dep["skill"],
            "requires": dep["requires_bins"] or dep.get("requires_any_bins", []),
            "missing": dep["missing_bins"],
            "satisfied": not dep["missing_bins"] and dep["has_any_bin"],
            "install_options": [
                {"kind": e.get("kind"), "label": e.get("label")}
                for e in dep.get("install", [])
            ],
        }

    # Check all skills
    prereqs = check_prerequisites()
    installed = get_installed_deps()
    missing = get_missing_deps()
    return {
        "ok": True,
        "package_managers": prereqs,
        "total": len(installed) + len(missing),
        "installed_count": len(installed),
        "missing_count": len(missing),
        "installed": [
            {"skill": d["skill"], "bins": d["requires_bins"]}
            for d in installed
        ],
        "missing": [
            {"skill": d["skill"],
             "needs": d["missing_bins"] or d.get("requires_any_bins", []),
             "install": [e.get("label") for e in d.get("install", [])]}
            for d in missing
        ],
    }


def _handle_install_skill_cli(**kwargs) -> dict:
    """Install CLI dependencies for a skill. Agents can self-install."""
    try:
        from core.skill_deps import (
            scan_skill_deps, pick_best_installer, install_dep,
            build_install_command,
        )
    except ImportError:
        return {"ok": False, "error": "skill_deps module not available"}

    skill_name = kwargs.get("skill", "")
    if not skill_name:
        return {"ok": False, "error": "skill parameter required"}

    all_deps = scan_skill_deps()
    match = [d for d in all_deps
             if d["skill"] == skill_name or d["file"] == f"{skill_name}.md"]
    if not match:
        return {"ok": False, "error": f"Skill '{skill_name}' not found or has no install entries"}

    dep = match[0]
    if not dep["missing_bins"] and dep["has_any_bin"]:
        return {"ok": True, "message": f"All deps for '{skill_name}' already installed",
                "bins": dep["requires_bins"]}

    install_entries = dep.get("install", [])
    if not install_entries:
        return {"ok": False, "error": f"No install entries for '{skill_name}'"}

    best = pick_best_installer(install_entries)
    if not best:
        return {"ok": False, "error": "No compatible package manager found"}

    cmd = build_install_command(best)
    logger.info("install_skill_cli: %s → %s", skill_name, cmd)

    success = install_dep(best, quiet=False)
    if not success:
        return {"ok": False, "error": f"Install failed: {cmd}",
                "command": cmd}

    # Auto-approve the installed binaries in exec_approvals so agents can use them
    installed_bins = best.get("bins", dep.get("requires_bins", []))
    _auto_approve_skill_bins(installed_bins)

    return {
        "ok": True,
        "skill": skill_name,
        "command": cmd,
        "bins": installed_bins,
        "message": f"Installed {skill_name} via: {cmd}",
    }


def _auto_approve_skill_bins(bins: list[str]):
    """Add installed skill binaries to exec_approvals.json so agents can use them."""
    try:
        from core.exec_tool import add_approval
        for b in bins:
            # Approve the binary (with any args)
            pattern = rf"^{re.escape(b)}\b"
            add_approval(pattern)
            logger.info("Auto-approved exec pattern: %s", pattern)
    except Exception as e:
        logger.warning("Could not auto-approve bins: %s", e)


# ── Remote skill registry handlers ──

def _handle_search_skills(**kwargs) -> dict:
    """Search the remote skill registry for new skills to install."""
    try:
        from core.skill_registry import get_registry
    except ImportError:
        return {"ok": False, "error": "skill_registry module not available"}

    query = kwargs.get("query", "")
    if not query:
        return {"ok": False, "error": "query parameter required"}

    registry = get_registry()
    try:
        results = registry.search(query, limit=kwargs.get("limit", 10))
    except Exception as e:
        return {"ok": False, "error": f"Search failed: {e}"}

    if not results:
        return {
            "ok": True,
            "count": 0,
            "message": f"No skills found matching '{query}'",
            "results": [],
        }

    # Format results for agent consumption
    formatted = []
    for r in results:
        entry = {
            "slug": r.get("slug", ""),
            "name": r.get("name", ""),
            "description": r.get("description", ""),
            "version": r.get("version", ""),
            "tags": r.get("tags", []),
            "installed": r.get("installed", False),
        }
        if r.get("installed"):
            entry["installed_version"] = r.get("installed_version", "")
        if r.get("requires", {}).get("bins"):
            entry["requires_cli"] = r["requires"]["bins"]
        formatted.append(entry)

    return {
        "ok": True,
        "count": len(formatted),
        "query": query,
        "results": formatted,
    }


def _handle_install_remote_skill(**kwargs) -> dict:
    """Install a skill from the remote registry."""
    try:
        from core.skill_registry import get_registry
    except ImportError:
        return {"ok": False, "error": "skill_registry module not available"}

    slug = kwargs.get("slug", "")
    if not slug:
        return {"ok": False, "error": "slug parameter required"}

    agent_id = kwargs.get("agent", "")
    add_to_all = kwargs.get("add_to_all", True)

    registry = get_registry()

    # Step 1: Install the skill
    result = registry.install(slug)
    if not result.get("ok"):
        return result

    # Step 2: Add to agent config
    if add_to_all:
        cfg_result = registry.add_to_all_agents(slug)
    elif agent_id:
        cfg_result = registry.add_to_agent(slug, agent_id)
    else:
        cfg_result = {"ok": True, "message": "Skill installed but not added to any agent config"}

    result["config"] = cfg_result.get("message", "")
    result["message"] = (
        f"{result.get('message', '')}. "
        f"{cfg_result.get('message', '')}. "
        "Skill will be active on the next task (hot-reload)."
    )

    return result


# ── Browser automation handlers ──

def _handle_browser_navigate(**kwargs) -> dict:
    """Navigate the browser to a URL."""
    try:
        from adapters.browser.playwright_adapter import handle_browser_navigate
        return handle_browser_navigate(**kwargs)
    except ImportError:
        return {"ok": False, "error": "playwright not installed. Run: pip install playwright && playwright install chromium"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_browser_click(**kwargs) -> dict:
    """Click an element in the browser."""
    try:
        from adapters.browser.playwright_adapter import handle_browser_click
        return handle_browser_click(**kwargs)
    except ImportError:
        return {"ok": False, "error": "playwright not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_browser_fill(**kwargs) -> dict:
    """Fill a form field in the browser."""
    try:
        from adapters.browser.playwright_adapter import handle_browser_fill
        return handle_browser_fill(**kwargs)
    except ImportError:
        return {"ok": False, "error": "playwright not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_browser_get_text(**kwargs) -> dict:
    """Extract text content from the page."""
    try:
        from adapters.browser.playwright_adapter import handle_browser_get_text
        return handle_browser_get_text(**kwargs)
    except ImportError:
        return {"ok": False, "error": "playwright not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_browser_screenshot(**kwargs) -> dict:
    """Take a screenshot of the page."""
    try:
        from adapters.browser.playwright_adapter import handle_browser_screenshot
        return handle_browser_screenshot(**kwargs)
    except ImportError:
        return {"ok": False, "error": "playwright not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_browser_evaluate(**kwargs) -> dict:
    """Run JavaScript in the page."""
    try:
        from adapters.browser.playwright_adapter import handle_browser_evaluate
        return handle_browser_evaluate(**kwargs)
    except ImportError:
        return {"ok": False, "error": "playwright not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_browser_page_info(**kwargs) -> dict:
    """Get current page info (URL, title)."""
    try:
        from adapters.browser.playwright_adapter import handle_browser_page_info
        return handle_browser_page_info(**kwargs)
    except ImportError:
        return {"ok": False, "error": "playwright not installed"}
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

    Tool("cron_list",
         "List all existing scheduled cron jobs.",
         {},
         _handle_cron_list, group="automation"),

    Tool("cron_add",
         "Create a new scheduled job (reminder, periodic task, webhook).",
         {"name": {"type": "string", "description": "Job name/description", "required": True},
          "action": {"type": "string", "description": "Action type: 'task' (create task), 'exec' (run command), 'webhook' (HTTP call)", "required": True},
          "payload": {"type": "string", "description": "Action payload (task description, shell command, or webhook URL)", "required": True},
          "schedule_type": {"type": "string", "description": "Schedule type: 'once' (one-shot), 'interval' (repeat), 'cron' (cron expression)", "required": True},
          "schedule": {"type": "string", "description": "Schedule value: ISO datetime for 'once', seconds for 'interval', cron expression for 'cron' (e.g. '*/5 * * * *')", "required": True}},
         _handle_cron_add, group="automation"),

    Tool("process",
         "List running system processes.",
         {},
         _handle_process_list, group="automation"),

    # ── Skill dependency tools ──
    Tool("check_skill_deps",
         "Check which skill CLI tools are installed and which are missing. "
         "Call with no params to check all, or pass a skill name to check one.",
         {"skill": {"type": "string",
                     "description": "Skill name to check (optional; omit for all)",
                     "required": False}},
         _handle_check_skill_deps, group="skill"),

    Tool("install_skill_cli",
         "Install the CLI tool required by a skill. Auto-detects the best package manager "
         "(brew/go/npm/uv) and runs the install. After install, the binary is automatically "
         "approved for exec. Use check_skill_deps first to see what's missing.",
         {"skill": {"type": "string",
                     "description": "Skill name whose CLI to install (e.g. 'apple-reminders', 'github')",
                     "required": True}},
         _handle_install_skill_cli, group="skill"),

    Tool("search_skills",
         "Search the remote skill registry for new capabilities. Use this when you "
         "need a skill that isn't currently installed. Returns matching skills with "
         "descriptions, versions, and install status.",
         {"query": {"type": "string",
                     "description": "Search query (skill name, description, or tag, e.g. 'pdf', 'browser', 'email')",
                     "required": True},
          "limit": {"type": "integer",
                     "description": "Max results to return (default 10)",
                     "required": False}},
         _handle_search_skills, group="skill"),

    Tool("install_remote_skill",
         "Download and install a skill from the remote registry. After installation, "
         "the skill is immediately available via hot-reload (no restart needed). "
         "CLI dependencies are auto-installed if possible. Use search_skills first.",
         {"slug": {"type": "string",
                    "description": "Skill slug from search_skills results (e.g. 'pdf-rotate', 'browser-control')",
                    "required": True},
          "agent": {"type": "string",
                     "description": "Agent ID to add skill to (default: add to all agents)",
                     "required": False},
          "add_to_all": {"type": "boolean",
                          "description": "Add skill to all agents (default: true)",
                          "required": False}},
         _handle_install_remote_skill, group="skill"),

    # ── Browser automation tools ──
    Tool("browser_navigate",
         "Open a URL in a headless browser. Use for web scraping, form filling, "
         "or interacting with web pages that require JavaScript rendering.",
         {"url": {"type": "string",
                   "description": "URL to navigate to (must include https://)",
                   "required": True}},
         _handle_browser_navigate, group="browser"),

    Tool("browser_click",
         "Click an element on the page by CSS selector.",
         {"selector": {"type": "string",
                        "description": "CSS selector (e.g. 'button.submit', '#login-btn')",
                        "required": True}},
         _handle_browser_click, group="browser"),

    Tool("browser_fill",
         "Fill a form input field with text.",
         {"selector": {"type": "string",
                        "description": "CSS selector for the input field",
                        "required": True},
          "value": {"type": "string",
                     "description": "Text value to enter",
                     "required": True}},
         _handle_browser_fill, group="browser"),

    Tool("browser_get_text",
         "Extract text content from the page or a specific element.",
         {"selector": {"type": "string",
                        "description": "CSS selector (default: 'body' for full page)",
                        "required": False}},
         _handle_browser_get_text, group="browser"),

    Tool("browser_screenshot",
         "Take a screenshot of the current page. Returns file path.",
         {"full_page": {"type": "boolean",
                         "description": "Capture full scrollable page (default: false)",
                         "required": False},
          "selector": {"type": "string",
                        "description": "Screenshot only this element (CSS selector)",
                        "required": False}},
         _handle_browser_screenshot, group="browser"),

    Tool("browser_evaluate",
         "Execute JavaScript code in the page context. Returns the result.",
         {"expression": {"type": "string",
                          "description": "JavaScript expression to evaluate",
                          "required": True}},
         _handle_browser_evaluate, group="browser"),

    Tool("browser_page_info",
         "Get current browser page info (URL, title).",
         {},
         _handle_browser_page_info, group="browser"),

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

    Tool("transcribe",
         "Transcribe an audio file to text using OpenAI Whisper API. "
         "Supports mp3, mp4, m4a, wav, webm, ogg, flac (max 25 MB). "
         "Requires OPENAI_API_KEY.",
         {"file_path": {"type": "string", "description": "Path to the audio file", "required": True},
          "language": {"type": "string",
                       "description": "ISO 639-1 language code hint (e.g. 'zh', 'en', 'ja'). "
                                      "Optional — Whisper auto-detects if omitted.",
                       "required": False}},
         _handle_transcribe, group="media",
         requires_env=["OPENAI_API_KEY"]),

    Tool("tts",
         "Text-to-speech: synthesize text into an audio file. "
         "Multi-provider with automatic fallback (OpenAI/ElevenLabs/MiniMax/local). "
         "Returns path to the generated audio file.",
         {"text": {"type": "string", "description": "Text to synthesize (max ~5000 chars)", "required": True},
          "voice": {"type": "string",
                    "description": "Voice ID — provider-specific. "
                    "OpenAI: alloy/echo/nova/shimmer/fable/onyx. "
                    "ElevenLabs: rachel/adam/sam/josh/bella. "
                    "MiniMax: presenter_male/presenter_female/female-shaonv.",
                    "required": False},
          "speed": {"type": "number", "description": "Speed multiplier 0.5-2.0 (default 1.0)", "required": False},
          "provider": {"type": "string", "description": "Force provider: openai/elevenlabs/minimax/local", "required": False},
          "output_format": {"type": "string", "description": "Output format: mp3/wav/ogg (default mp3)", "required": False}},
         _handle_tts, group="media"),

    Tool("list_voices",
         "List available TTS voices across all configured providers.",
         {"provider": {"type": "string", "description": "Filter by provider name (optional)", "required": False}},
         _handle_list_voices, group="media"),

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

    Tool("send_file",
         "Send a file to the user via their chat channel (Telegram/Discord/Feishu/Slack). "
         "Use this after creating a file (with write_file or exec) to deliver it to the user. "
         "Only works when the task originates from a channel message.",
         {"file_path": {"type": "string",
                        "description": "Absolute or relative path to the file to send",
                        "required": True},
          "caption": {"type": "string",
                      "description": "Optional caption/message to include with the file",
                      "required": False}},
         _handle_send_file, group="messaging"),
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
        "You can invoke tools by including a JSON block in your response.",
        "Use this EXACT format (any of these formats work):",
        "",
        "Format 1 (preferred):",
        "```tool",
        '{"tool": "tool_name", "params": {"param1": "value1"}}',
        "```",
        "",
        "Format 2:",
        "<tool_code>",
        '{"tool": "tool_name", "params": {"param1": "value1"}}',
        "</tool_code>",
        "",
        "IMPORTANT: Use ONE tool per block. The tool JSON must have a \"tool\" key and a \"params\" key.",
        "After tool execution, you will receive the results and can continue.",
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

# Fallback: matches <tool_code>\n{...}\n</tool_code> blocks (Minimax format)
_TOOL_CODE_RE = re.compile(
    r'<tool_code>\s*\n?([\s\S]+?)\n?\s*</tool_code>',
    re.DOTALL)

# Fallback: matches ```json\n{"tool":...}\n``` blocks
_JSON_BLOCK_RE = re.compile(
    r'```(?:json)?\s*\n(\{"tool"\s*:\s*[^`]+?\})\s*\n```',
    re.DOTALL)


def _try_parse_arrow_syntax(raw: str) -> dict | None:
    """Parse Minimax-style arrow syntax to JSON.

    Handles formats like:
        { tool => 'web_search', args => { --query "hello" } }
        { tool => 'web_search', params => { query: "hello" } }
    """
    try:
        # Strip outer braces and normalize
        s = raw.strip()
        if not s.startswith("{"):
            return None

        # Extract tool name: tool => 'name' or tool => "name"
        tool_match = re.search(r'''tool\s*(?:=>|:)\s*['"](\w+)['"]''', s)
        if not tool_match:
            return None
        tool_name = tool_match.group(1)

        # Extract params/args block
        params = {}
        args_match = re.search(
            r'(?:args|params)\s*(?:=>|:)\s*\{([^}]*)\}', s, re.DOTALL)
        if args_match:
            args_raw = args_match.group(1)
            # Parse --key "value" patterns (CLI-style)
            for m in re.finditer(
                    r'--(\w+)\s+["\']([^"\']*)["\']', args_raw):
                params[m.group(1)] = m.group(2)
            # Parse key: "value" or key => "value" patterns
            for m in re.finditer(
                    r'(\w+)\s*(?:=>|:)\s*["\']([^"\']*)["\']', args_raw):
                params[m.group(1)] = m.group(2)
            # Parse key: number patterns
            for m in re.finditer(
                    r'(\w+)\s*(?:=>|:)\s*(\d+)', args_raw):
                params[m.group(1)] = int(m.group(2))

        return {"tool": tool_name, "params": params}
    except Exception:
        return None


def parse_tool_calls(text: str) -> list[dict]:
    """Extract tool invocation blocks from agent output.

    Supports multiple formats for LLM compatibility:
    1. Standard: ```tool\n{"tool":"name","params":{...}}\n```
    2. Minimax: <tool_code>\n{tool=>'name',args=>{...}}\n</tool_code>
    3. JSON block: ```json\n{"tool":"name","params":{...}}\n```

    Returns list of {"tool": "name", "params": {...}}
    """
    calls = []

    # 1. Standard format (```tool ... ```)
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

    if calls:
        return calls

    # 2. <tool_code> blocks (Minimax arrow syntax)
    for match in _TOOL_CODE_RE.finditer(text):
        raw_content = match.group(1).strip()
        # Try JSON first
        try:
            data = json.loads(raw_content)
            if "tool" in data:
                calls.append({
                    "tool": data["tool"],
                    "params": data.get("params", data.get("args", {})),
                    "raw": match.group(0),
                })
                continue
        except json.JSONDecodeError:
            pass
        # Try arrow syntax
        parsed = _try_parse_arrow_syntax(raw_content)
        if parsed:
            calls.append({
                "tool": parsed["tool"],
                "params": parsed.get("params", {}),
                "raw": match.group(0),
            })

    if calls:
        return calls

    # 3. ```json blocks with tool key
    for match in _JSON_BLOCK_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
            if "tool" in data:
                calls.append({
                    "tool": data["tool"],
                    "params": data.get("params", data.get("args", {})),
                    "raw": match.group(0),
                })
        except json.JSONDecodeError:
            continue

    if not calls and any(kw in text for kw in
                         ["web_search", "web_fetch", "exec", "read_file",
                          "write_file", "memory_search"]):
        logger.warning("Tool keywords found in LLM output but no parseable "
                       "tool blocks detected. First 300 chars: %s",
                       text[:300].replace("\n", "\\n"))

    return calls


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMETER SANITIZATION — defence-in-depth for LLM-generated params
# ══════════════════════════════════════════════════════════════════════════════

# Sensitive files that tools should never read or write
_SENSITIVE_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "agents.yaml", "exec_approvals.json", "chain_contracts.json",
    ".git/config", ".netrc", ".npmrc", ".pypirc",
    "id_rsa", "id_ed25519", "authorized_keys",
}

# Sensitive path fragments (blocked anywhere in path)
_SENSITIVE_PATH_FRAGMENTS = {
    ".ssh", ".gnupg", ".aws", ".config/gcloud",
}

# Tools whose "path" param must be checked
_FS_TOOLS = {"read_file", "write_file", "edit_file", "list_dir"}

# Tools whose "url" param must be scheme-checked
_NET_TOOLS = {"web_fetch", "web_search"}


def sanitize_params(tool_name: str, params: dict,
                    tool: "Tool | None" = None) -> dict | str:
    """Validate and sanitize LLM-generated tool parameters.

    Returns sanitized params dict on success, or error string on rejection.
    Checks performed:
      1. Type coercion — cast to schema-declared types
      2. Path safety  — block sensitive files, enforce project scope
      3. URL safety   — enforce https, block private IPs (defence-in-depth)
    """
    if not isinstance(params, dict):
        return "Parameters must be a JSON object"

    # ── 1. Type coercion ──
    if tool and tool.parameters:
        for pname, pinfo in tool.parameters.items():
            if pname not in params:
                continue
            expected = pinfo.get("type", "string")
            val = params[pname]
            try:
                if expected == "integer" and not isinstance(val, int):
                    params[pname] = int(val)
                elif expected == "number" and not isinstance(val, (int, float)):
                    params[pname] = float(val)
                elif expected == "boolean" and not isinstance(val, bool):
                    params[pname] = str(val).lower() in ("true", "1", "yes")
                elif expected == "string" and not isinstance(val, str):
                    params[pname] = str(val)
            except (ValueError, TypeError):
                return f"Parameter '{pname}' must be {expected}, got {type(val).__name__}"

    # ── 2. Path safety (filesystem tools) ──
    if tool_name in _FS_TOOLS:
        raw_path = params.get("path", "")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return "Missing or empty 'path' parameter"

        # Normalise to block encoded traversal  (e.g. %2e%2e)
        decoded_path = urllib.parse.unquote(raw_path)

        # Block null bytes (can bypass os.path checks)
        if "\x00" in decoded_path:
            return "Null bytes not allowed in path"

        # Block sensitive filenames
        basename = os.path.basename(decoded_path)
        if basename.lower() in _SENSITIVE_FILENAMES:
            return f"Access to sensitive file '{basename}' is blocked"

        # Block sensitive path fragments
        norm = os.path.normpath(decoded_path).replace("\\", "/").lower()
        for frag in _SENSITIVE_PATH_FRAGMENTS:
            if frag in norm:
                return f"Path contains blocked segment '{frag}'"

        # Write-specific: block hidden dotfiles at project root
        if tool_name == "write_file" and basename.startswith("."):
            return f"Cannot write to hidden file '{basename}' (dotfiles are protected)"

        # Replace raw path with decoded version for consistency
        params["path"] = decoded_path

    # ── 3. URL safety (network tools) ──
    if tool_name in _NET_TOOLS and "url" in params:
        url = params.get("url", "")
        if not isinstance(url, str):
            return "URL must be a string"

        # Enforce https (allow http only for localhost dev, but that's
        # already blocked by _is_private_hostname)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("https", "http"):
            return f"URL scheme '{parsed.scheme}' not allowed — use https://"

        # Defence-in-depth: re-check private hostnames at param level
        # (web_fetch already checks, but belt-and-suspenders)
        hostname = parsed.hostname or ""
        if _is_private_hostname(hostname):
            return f"Blocked: private/internal hostname '{hostname}'"

    return params


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

        # ── Sanitize parameters before execution ──
        raw_params = call.get("params", {})
        sanitized = sanitize_params(name, dict(raw_params), tool)
        if isinstance(sanitized, str):
            # sanitize_params returned an error message
            logger.warning("Tool %s params rejected: %s (raw: %s)",
                           name, sanitized, str(raw_params)[:200])
            results.append({
                "tool": name,
                "result": {"ok": False, "error": f"Parameter validation: {sanitized}"},
            })
            continue

        logger.info("Executing tool: %s(%s)", name,
                     str(sanitized)[:100])
        result = tool.execute(**sanitized)
        results.append({"tool": name, "result": result})

    return results
