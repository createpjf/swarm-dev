"""
core/tools.py
Built-in tool registry — OpenClaw-inspired agent tool system.

Architecture:
  - Tools are callable functions with JSON-schema parameters
  - Agent system prompts include tool descriptions
  - Agents invoke tools via structured JSON blocks in their output
  - Tool results are fed back to the agent as context

Tool categories:
  - Web:        web_search, web_fetch
  - Automation: exec, cron, process
  - Media:      screenshot, notify
  - Filesystem: read_file, write_file, list_dir

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

def _handle_web_search(query: str, count: int = 5,
                       freshness: str = "", **_) -> dict:
    """Search the web using Brave Search API."""
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "BRAVE_API_KEY not configured. "
                "Get one at https://brave.com/search/api/"}

    params = {"q": query, "count": min(int(count), 10)}
    if freshness:
        params["freshness"] = freshness

    url = ("https://api.search.brave.com/res/v1/web/search?"
           + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
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
        return {"ok": True, "query": query, "results": results,
                "total": len(results)}
    except Exception as e:
        return {"ok": False, "error": f"Search failed: {e}"}


def _handle_web_fetch(url: str, max_chars: int = 8000, **_) -> dict:
    """Fetch URL content and extract readable text."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "SwarmBot/1.0 (https://github.com/createpjf/swarm-dev)",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(500_000)  # max 500KB

        text = raw.decode("utf-8", errors="ignore")

        # Simple HTML → text extraction
        if "html" in content_type.lower():
            # Remove script/style tags
            text = re.sub(r'<script[^>]*>.*?</script>', '', text,
                          flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text,
                          flags=re.DOTALL | re.IGNORECASE)
            # Remove HTML tags
            text = re.sub(r'<[^>]+>', ' ', text)
            # Collapse whitespace
            text = re.sub(r'\s+', ' ', text).strip()

        text = text[:int(max_chars)]
        return {"ok": True, "url": url, "content": text,
                "chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": f"Fetch failed: {e}"}


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
         "Search the web using Brave Search API.",
         {"query": {"type": "string", "description": "Search query", "required": True},
          "count": {"type": "integer", "description": "Number of results (1-10)", "required": False},
          "freshness": {"type": "string", "description": "Time filter: pd/pw/pm/py", "required": False}},
         _handle_web_search, group="web",
         requires_env=["BRAVE_API_KEY"]),

    Tool("web_fetch",
         "Fetch a URL and extract readable text content.",
         {"url": {"type": "string", "description": "URL to fetch", "required": True},
          "max_chars": {"type": "integer", "description": "Max chars to return (default 8000)", "required": False}},
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
