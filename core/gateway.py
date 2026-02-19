"""
core/gateway.py
Lightweight HTTP gateway — exposes Swarm Agent Stack as a local API.

Endpoints:
  GET  /                            Web Dashboard (no auth)
  GET  /health                      Health check (no auth)
  POST /v1/task                     Submit a task → returns task_id
  GET  /v1/task/:id                 Get task status & result
  GET  /v1/status                   Full task board
  GET  /v1/scores                   Reputation scores
  GET  /v1/agents                   Agent team info
  GET  /v1/usage                    Usage statistics
  GET  /v1/config                   Agent configuration (sanitized)
  GET  /v1/doctor                   System health check
  GET  /v1/skills                   List all skills with metadata
  GET  /v1/skills/team              Read team skill content
  GET  /v1/skills/:name             Read shared skill content
  GET  /v1/skills/agents/:aid/:name Read per-agent skill content
  GET  /v1/logs/:agent_id           Read agent log file
  PUT  /v1/agents/:id               Update agent config
  PUT  /v1/skills/team              Update team skill manually
  PUT  /v1/skills/:name             Create/update shared skill
  PUT  /v1/skills/agents/:aid/:name Create/update per-agent skill
  PUT  /v1/config/gateway           Gateway token management
  DELETE /v1/skills/:name           Delete shared skill
  DELETE /v1/skills/agents/:aid/:name Delete per-agent skill
  POST /v1/skills/team/regenerate   Force regenerate team skill
  GET  /v1/heartbeat               Agent heartbeat statuses
  GET  /v1/chain/status             Chain status (PKP, identity, balances)
  GET  /v1/chain/balance/:agent_id  USDC balance for agent
  GET  /v1/chain/identity/:agent_id On-chain identity info
  POST /v1/chain/init/:agent_id     Initialize agent on-chain (PKP + ERC-8004)
  POST /v1/chain/register/:agent_id Register agent on ERC-8004
  GET  /v1/memory/status             Memory system status (all agents)
  GET  /v1/memory/episodes/:agent_id Agent's episodic memory entries
  GET  /v1/memory/cases/:agent_id    Agent's extracted cases
  GET  /v1/memory/daily/:agent_id    Agent's daily learning log
  GET  /v1/memory/kb/notes           Shared knowledge base notes
  GET  /v1/memory/kb/moc             Map of Content
  GET  /v1/memory/kb/insights        Cross-agent insights feed

Default port: 19789  (configurable via SWARM_GATEWAY_PORT or config)
Auth: Bearer token  (auto-generated, configurable via SWARM_GATEWAY_TOKEN)
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PORT = 19789
_start_time: float = 0.0
_token: str = ""
_config: dict = {}

_SAFE_NAME = re.compile(r'^[a-zA-Z0-9_-]+$')


# ══════════════════════════════════════════════════════════════════════════════
#  REQUEST HANDLER
# ══════════════════════════════════════════════════════════════════════════════

class _Handler(BaseHTTPRequestHandler):
    """Minimal JSON API handler."""

    def log_message(self, fmt, *args):
        logger.debug(fmt, *args)

    # ── Auth ──
    def _check_auth(self) -> bool:
        if not _token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {_token}":
            return True
        self._json_response(401, {"error": "Unauthorized"})
        return False

    # ── Response helpers ──
    def _json_response(self, code: int, data: Any):
        body = json.dumps(data, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, code: int, content: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content)

    def _read_body(self) -> dict:
        MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length > MAX_BODY_SIZE:
            self._json_response(413, {"error": "Request body too large"})
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _validate_name(self, name: str) -> bool:
        """Validate name/id to prevent path traversal."""
        if not _SAFE_NAME.match(name):
            self._json_response(400, {"error": f"Invalid name: {name}"})
            return False
        return True

    # ── GET Routes ──
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Dashboard — public (no auth required)
        if path == "" or path == "/":
            self._serve_dashboard()
            return

        # /health is public (no auth required)
        if path == "/health":
            self._handle_health()
            return

        if not self._check_auth():
            return

        if path == "/v1/status":
            self._handle_status()
        elif path == "/v1/scores":
            self._handle_scores()
        elif path == "/v1/agents":
            self._handle_agents()
        elif path == "/v1/usage":
            self._handle_usage()
        elif path == "/v1/usage/recent":
            self._handle_usage_recent()
        elif path == "/v1/config":
            self._handle_config()
        elif path == "/v1/doctor":
            self._handle_doctor()
        elif path.startswith("/v1/task/"):
            task_id = path[len("/v1/task/"):]
            self._handle_get_task(task_id)
        # ── Skills routes (order matters: team > agents > generic) ──
        elif path == "/v1/skills":
            self._handle_list_skills()
        elif path == "/v1/skills/team":
            self._handle_get_team_skill()
        elif path.startswith("/v1/skills/agents/"):
            parts = path[len("/v1/skills/agents/"):].split("/", 1)
            if len(parts) == 2:
                self._handle_get_skill(parts[1], agent_id=parts[0])
            else:
                self._json_response(400, {"error": "Expected /v1/skills/agents/:aid/:name"})
        elif path.startswith("/v1/skills/"):
            name = path[len("/v1/skills/"):]
            self._handle_get_skill(name)
        # ── Chain routes ──
        elif path == "/v1/chain/status":
            self._handle_chain_status()
        elif path.startswith("/v1/chain/balance/"):
            agent_id = path[len("/v1/chain/balance/"):]
            self._handle_chain_balance(agent_id)
        elif path.startswith("/v1/chain/identity/"):
            agent_id = path[len("/v1/chain/identity/"):]
            self._handle_chain_identity(agent_id)
        # ── Memory routes ──
        elif path == "/v1/memory/status":
            self._handle_memory_status()
        elif path.startswith("/v1/memory/episodes/"):
            agent_id = path[len("/v1/memory/episodes/"):]
            self._handle_memory_episodes(agent_id)
        elif path.startswith("/v1/memory/cases/"):
            agent_id = path[len("/v1/memory/cases/"):]
            self._handle_memory_cases(agent_id)
        elif path.startswith("/v1/memory/daily/"):
            agent_id = path[len("/v1/memory/daily/"):]
            self._handle_memory_daily(agent_id)
        elif path == "/v1/memory/kb/notes":
            self._handle_kb_notes()
        elif path == "/v1/memory/kb/moc":
            self._handle_kb_moc()
        elif path == "/v1/memory/kb/insights":
            self._handle_kb_insights()
        # ── Heartbeat route ──
        elif path == "/v1/heartbeat":
            self._handle_heartbeat()
        # ── SSE event stream ──
        elif path == "/v1/events":
            self._handle_sse()
        # ── Budget & Alerts ──
        elif path == "/v1/budget":
            self._handle_get_budget()
        elif path == "/v1/alerts":
            self._handle_get_alerts()
        # ── Logs route ──
        elif path.startswith("/v1/logs/"):
            agent_id = path[len("/v1/logs/"):]
            query = urllib.parse.parse_qs(parsed.query)
            self._handle_get_logs(agent_id, query)
        else:
            self._json_response(404, {"error": "Not found"})

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # ── POST Routes ──
    def do_POST(self):
        if not self._check_auth():
            return

        path = self.path.rstrip("/")

        if path == "/v1/task":
            self._handle_submit_task()
        elif path == "/v1/search":
            self._handle_brave_search()
        elif path == "/v1/skills/team/regenerate":
            self._handle_regen_team_skill()
        elif path.startswith("/v1/chain/init/"):
            agent_id = path[len("/v1/chain/init/"):]
            self._handle_chain_init(agent_id)
        elif path.startswith("/v1/chain/register/"):
            agent_id = path[len("/v1/chain/register/"):]
            self._handle_chain_register(agent_id)
        # ── Task lifecycle controls ──
        elif path.startswith("/v1/task/") and path.endswith("/cancel"):
            task_id = path[len("/v1/task/"):-len("/cancel")]
            self._handle_task_cancel(task_id)
        elif path.startswith("/v1/task/") and path.endswith("/pause"):
            task_id = path[len("/v1/task/"):-len("/pause")]
            self._handle_task_pause(task_id)
        elif path.startswith("/v1/task/") and path.endswith("/resume"):
            task_id = path[len("/v1/task/"):-len("/resume")]
            self._handle_task_resume(task_id)
        elif path.startswith("/v1/task/") and path.endswith("/retry"):
            task_id = path[len("/v1/task/"):-len("/retry")]
            self._handle_task_retry(task_id)
        elif path == "/v1/tasks/cancel_all":
            self._handle_cancel_all()
        # ── Budget management ──
        elif path == "/v1/budget":
            self._handle_set_budget()
        else:
            self._json_response(404, {"error": "Not found"})

    # ── PUT Routes ──
    def do_PUT(self):
        if not self._check_auth():
            return

        path = self.path.rstrip("/")

        if path == "/v1/config/gateway":
            self._handle_update_gateway()
        elif path == "/v1/skills/team":
            self._handle_update_team_skill()
        elif path.startswith("/v1/skills/agents/"):
            parts = path[len("/v1/skills/agents/"):].split("/", 1)
            if len(parts) == 2:
                self._handle_update_skill(parts[1], agent_id=parts[0])
            else:
                self._json_response(400, {"error": "Expected /v1/skills/agents/:aid/:name"})
        elif path.startswith("/v1/skills/"):
            name = path[len("/v1/skills/"):]
            self._handle_update_skill(name)
        elif path.startswith("/v1/agents/"):
            agent_id = path[len("/v1/agents/"):]
            self._handle_update_agent(agent_id)
        else:
            self._json_response(404, {"error": "Not found"})

    # ── DELETE Routes ──
    def do_DELETE(self):
        if not self._check_auth():
            return

        path = self.path.rstrip("/")

        if path.startswith("/v1/skills/agents/"):
            parts = path[len("/v1/skills/agents/"):].split("/", 1)
            if len(parts) == 2:
                self._handle_delete_skill(parts[1], agent_id=parts[0])
            else:
                self._json_response(400, {"error": "Expected /v1/skills/agents/:aid/:name"})
        elif path.startswith("/v1/skills/"):
            name = path[len("/v1/skills/"):]
            self._handle_delete_skill(name)
        else:
            self._json_response(404, {"error": "Not found"})

    # ══════════════════════════════════════════════════════════════════════════
    #  EXISTING HANDLERS
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_health(self):
        import yaml
        agents_count = 0
        if os.path.exists("config/agents.yaml"):
            with open("config/agents.yaml") as f:
                cfg = yaml.safe_load(f) or {}
            agents_count = len(cfg.get("agents", []))

        uptime = time.time() - _start_time
        self._json_response(200, {
            "status": "ok",
            "agents": agents_count,
            "uptime_seconds": round(uptime, 1),
            "port": _config.get("port", DEFAULT_PORT),
        })

    def _handle_status(self):
        if not os.path.exists(".task_board.json"):
            self._json_response(200, {"tasks": {}})
            return
        with open(".task_board.json") as f:
            data = json.load(f)
        self._json_response(200, {"tasks": data})

    def _handle_scores(self):
        path = "memory/reputation_cache.json"
        if not os.path.exists(path):
            self._json_response(200, {"scores": {}})
            return
        with open(path) as f:
            data = json.load(f)
        self._json_response(200, {"scores": data})

    def _handle_agents(self):
        import yaml
        if not os.path.exists("config/agents.yaml"):
            self._json_response(200, {"agents": []})
            return
        with open("config/agents.yaml") as f:
            cfg = yaml.safe_load(f) or {}
        agents = []
        global_llm = cfg.get("llm", {})
        global_provider = global_llm.get("provider", "?")
        global_key_env = global_llm.get("api_key_env", "")
        global_url_env = global_llm.get("base_url_env", "")
        for a in cfg.get("agents", []):
            agent_llm = a.get("llm", {})
            key_env = agent_llm.get("api_key_env", global_key_env)
            url_env = agent_llm.get("base_url_env", global_url_env)
            # Resolve actual values for display
            raw_key = os.environ.get(key_env, "") if key_env else ""
            raw_url = os.environ.get(url_env, "") if url_env else ""
            # Mask API key: show first 6 + last 4 chars
            if raw_key and len(raw_key) > 12:
                masked_key = raw_key[:6] + "…" + raw_key[-4:]
            elif raw_key:
                masked_key = raw_key[:3] + "…"
            else:
                masked_key = ""
            agents.append({
                "id": a["id"],
                "model": a.get("model", "?"),
                "provider": agent_llm.get("provider", global_provider),
                "api_key_env": key_env,
                "base_url_env": url_env,
                "api_key_set": bool(raw_key),
                "api_key_masked": masked_key,
                "base_url": raw_url,
                "skills": a.get("skills", []),
                "fallback_models": a.get("fallback_models", []),
                "role": a.get("role", ""),
                "autonomy_level": a.get("autonomy_level", 1),
            })
        self._json_response(200, {"agents": agents, "global_key_env": global_key_env, "global_url_env": global_url_env})

    def _handle_get_task(self, task_id: str):
        if not os.path.exists(".task_board.json"):
            self._json_response(404, {"error": "No tasks"})
            return
        with open(".task_board.json") as f:
            data = json.load(f)
        task = data.get(task_id)
        if not task:
            self._json_response(404, {"error": f"Task {task_id} not found"})
            return
        self._json_response(200, {"task": task})

    # ── Brave Search ──
    def _handle_brave_search(self):
        body = self._read_body()
        query = body.get("query", "").strip()
        if not query:
            self._json_response(400, {"error": "Missing 'query'"})
            return

        api_key = os.environ.get("BRAVE_API_KEY", "")
        if not api_key:
            self._json_response(500, {"error": "BRAVE_API_KEY not configured"})
            return

        import urllib.request
        import urllib.parse
        try:
            url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({
                "q": query, "count": 8
            })
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                # Handle gzip
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                data = json.loads(raw)

            results = []
            for item in (data.get("web", {}).get("results", []))[:8]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("description", ""),
                })
            self._json_response(200, {"query": query, "results": results})
        except Exception as e:
            self._json_response(500, {"error": f"Search failed: {e}"})

    def _handle_submit_task(self):
        body = self._read_body()
        description = body.get("description", "").strip()
        if not description:
            self._json_response(400, {"error": "Missing 'description'"})
            return

        # Submit and run in a thread to avoid blocking
        from core.orchestrator import Orchestrator
        from core.task_board import TaskBoard

        board = TaskBoard()
        board.clear()
        # Clean old state
        for fp in [".context_bus.json"]:
            if os.path.exists(fp):
                os.remove(fp)
        import glob
        for fp in glob.glob(".mailboxes/*.jsonl"):
            os.remove(fp)

        orch = Orchestrator()
        task_id = orch.submit(description)

        # Run agents in background thread
        def _run():
            try:
                orch._launch_all()
                orch._wait()
            except Exception as e:
                logger.error("Task execution error: %s", e)

        t = Thread(target=_run, daemon=True)
        t.start()

        self._json_response(202, {
            "task_id": task_id,
            "status": "accepted",
            "message": "Task submitted. Poll GET /v1/task/{id} for results.",
        })

    # ── Dashboard ──
    def _serve_dashboard(self):
        """Serve the embedded web dashboard."""
        html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        if not os.path.exists(html_path):
            self._json_response(404, {"error": "dashboard.html not found"})
            return
        try:
            with open(html_path, "rb") as f:
                content = f.read()
            self._html_response(200, content)
        except Exception as e:
            self._json_response(500, {"error": f"Failed to serve dashboard: {e}"})

    # ── Usage stats ──
    def _handle_usage(self):
        """Return usage statistics from UsageTracker."""
        try:
            from core.usage_tracker import UsageTracker
            tracker = UsageTracker()
            summary = tracker.get_summary()
            self._json_response(200, summary)
        except Exception as e:
            logger.warning("Usage stats error: %s", e)
            self._json_response(200, {
                "aggregate": {},
                "by_agent": {},
                "by_model": {},
            })

    def _handle_usage_recent(self):
        """Return recent per-call usage data for token display."""
        try:
            from core.usage_tracker import UsageTracker
            tracker = UsageTracker()
            data = tracker._read()
            calls = data.get("calls", [])
            # Return last 50 calls with full detail
            recent = calls[-50:]
            self._json_response(200, {"calls": recent})
        except Exception as e:
            logger.warning("Recent usage error: %s", e)
            self._json_response(200, {"calls": []})

    # ── Config (sanitized) ──
    def _handle_config(self):
        """Return agent configuration with API keys sanitized."""
        import yaml
        if not os.path.exists("config/agents.yaml"):
            self._json_response(200, {"config": {}})
            return
        try:
            with open("config/agents.yaml") as f:
                cfg = yaml.safe_load(f) or {}

            # Sanitize — remove api_key_env values, mask any key references
            sanitized = json.loads(json.dumps(cfg, default=str))

            # Remove sensitive fields recursively
            def _sanitize(obj):
                if isinstance(obj, dict):
                    for key in list(obj.keys()):
                        if "key" in key.lower() and "api" in key.lower():
                            obj[key] = "***"
                        elif key == "api_key_env":
                            obj[key] = (obj[key] + " (set)"
                                        if os.environ.get(obj[key], "")
                                        else obj[key] + " (not set)")
                        else:
                            _sanitize(obj[key])
                elif isinstance(obj, list):
                    for item in obj:
                        _sanitize(item)

            _sanitize(sanitized)
            self._json_response(200, {"config": sanitized})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    # ── Doctor health checks ──
    def _handle_doctor(self):
        """Run system health checks and return results."""
        try:
            from core.doctor import run_doctor
            results = run_doctor()
            checks = []
            for ok, label, detail in results:
                checks.append({
                    "ok": ok,
                    "label": label,
                    "detail": detail,
                })
            all_ok = all(c["ok"] for c in checks)
            self._json_response(200, {
                "status": "healthy" if all_ok else "degraded",
                "checks": checks,
                "passed": sum(1 for c in checks if c["ok"]),
                "total": len(checks),
            })
        except Exception as e:
            self._json_response(500, {"error": f"Doctor failed: {e}"})

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW HANDLERS — Agent Management
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_update_agent(self, agent_id: str):
        """Update agent config (model, provider, role, skills, fallbacks)."""
        import yaml
        if not self._validate_name(agent_id):
            return

        body = self._read_body()
        if not body:
            self._json_response(400, {"error": "Empty body"})
            return

        if not os.path.exists("config/agents.yaml"):
            self._json_response(404, {"error": "Config not found"})
            return

        with open("config/agents.yaml") as f:
            cfg = yaml.safe_load(f) or {}

        # Find agent
        target = None
        for a in cfg.get("agents", []):
            if a.get("id") == agent_id:
                target = a
                break

        if not target:
            self._json_response(404, {"error": f"Agent '{agent_id}' not found"})
            return

        # Allowlisted fields
        updated = []
        for field in ("model", "role", "skills", "fallback_models",
                       "autonomy_level"):
            if field in body:
                target[field] = body[field]
                updated.append(field)

        # Fields that live under agent.llm.*
        if "provider" in body:
            target.setdefault("llm", {})["provider"] = body["provider"]
            updated.append("provider")
        # Direct API key / base URL values → auto-save to .env
        if "api_key" in body and body["api_key"]:
            env_name = f"{agent_id.upper()}_API_KEY"
            _save_env_var(env_name, body["api_key"])
            target.setdefault("llm", {})["api_key_env"] = env_name
            updated.append("api_key")
        if "base_url" in body and body["base_url"]:
            env_name = f"{agent_id.upper()}_BASE_URL"
            _save_env_var(env_name, body["base_url"])
            target.setdefault("llm", {})["base_url_env"] = env_name
            updated.append("base_url")

        if not updated:
            self._json_response(400, {"error": "No valid fields to update"})
            return

        # Write back
        os.makedirs("config", exist_ok=True)
        with open("config/agents.yaml", "w") as f:
            f.write("# config/agents.yaml\n\n")
            yaml.dump(cfg, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)

        # Regenerate team skill
        try:
            from core.team_skill import generate_team_skill
            generate_team_skill()
        except Exception:
            pass

        self._json_response(200, {
            "ok": True,
            "agent_id": agent_id,
            "updated": updated,
        })

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW HANDLERS — Skills Management
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_list_skills(self):
        """List all installed skills with metadata."""
        from core.skill_loader import SkillLoader
        loader = SkillLoader()
        inventory = loader.list_skills()

        # Add team skill info
        team_path = "skills/_team.md"
        inventory["team"] = {
            "exists": os.path.exists(team_path),
            "size": (os.path.getsize(team_path)
                     if os.path.exists(team_path) else 0),
        }

        self._json_response(200, inventory)

    def _handle_get_team_skill(self):
        """Read team skill content."""
        path = "skills/_team.md"
        if not os.path.exists(path):
            self._json_response(404, {"error": "Team skill not found"})
            return
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        self._json_response(200, {
            "name": "_team",
            "scope": "team",
            "content": content,
        })

    def _handle_get_skill(self, name: str, agent_id: str | None = None):
        """Read a skill file content + metadata."""
        if not self._validate_name(name):
            return
        if agent_id and not self._validate_name(agent_id):
            return

        if agent_id:
            path = os.path.join("skills", "agents", agent_id, f"{name}.md")
            scope = f"agent:{agent_id}"
        else:
            path = os.path.join("skills", f"{name}.md")
            scope = "shared"

        if not os.path.exists(path):
            self._json_response(404, {"error": f"Skill not found: {path}"})
            return

        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Parse frontmatter
        from core.skill_loader import _parse_frontmatter
        meta, body = _parse_frontmatter(content)

        self._json_response(200, {
            "name": meta.get("name", name),
            "scope": scope,
            "agent_id": agent_id,
            "content": content,
            "body": body,
            "metadata": meta,
            "file": os.path.basename(path),
            "size": len(content),
        })

    def _handle_update_skill(self, name: str, agent_id: str | None = None):
        """Create or update a skill file."""
        if not self._validate_name(name):
            return
        if agent_id and not self._validate_name(agent_id):
            return
        if name == "_team":
            self._json_response(400, {
                "error": "Use PUT /v1/skills/team for team skill"})
            return

        body = self._read_body()
        content = body.get("content", "")
        if not content:
            self._json_response(400, {"error": "Missing 'content'"})
            return

        if agent_id:
            dir_path = os.path.join("skills", "agents", agent_id)
            path = os.path.join(dir_path, f"{name}.md")
        else:
            dir_path = "skills"
            path = os.path.join(dir_path, f"{name}.md")

        os.makedirs(dir_path, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        self._json_response(200, {
            "ok": True,
            "path": path,
            "size": len(content),
        })

    def _handle_update_team_skill(self):
        """Manually update team skill content."""
        body = self._read_body()
        content = body.get("content", "")
        if not content:
            self._json_response(400, {"error": "Missing 'content'"})
            return

        os.makedirs("skills", exist_ok=True)
        with open("skills/_team.md", "w", encoding="utf-8") as f:
            f.write(content)

        self._json_response(200, {
            "ok": True,
            "path": "skills/_team.md",
            "size": len(content),
        })

    def _handle_delete_skill(self, name: str, agent_id: str | None = None):
        """Delete a skill file."""
        if not self._validate_name(name):
            return
        if agent_id and not self._validate_name(agent_id):
            return
        if name.startswith("_"):
            self._json_response(400, {
                "error": "Cannot delete system skills (starting with _)"})
            return

        if agent_id:
            path = os.path.join("skills", "agents", agent_id, f"{name}.md")
        else:
            path = os.path.join("skills", f"{name}.md")

        if not os.path.exists(path):
            self._json_response(404, {"error": f"Skill not found: {path}"})
            return

        try:
            os.remove(path)
            self._json_response(200, {"ok": True, "deleted": path})
        except OSError as e:
            self._json_response(500, {"error": str(e)})

    def _handle_regen_team_skill(self):
        """Force regenerate team skill from agents.yaml."""
        try:
            from core.team_skill import generate_team_skill
            content = generate_team_skill()
            if content:
                self._json_response(200, {
                    "ok": True,
                    "size": len(content),
                    "content": content,
                })
            else:
                self._json_response(200, {
                    "ok": False,
                    "message": "No agents found",
                })
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW HANDLERS — Gateway Config
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_update_gateway(self):
        """Gateway token management."""
        global _token, _config
        body = self._read_body()
        action = body.get("action", "get_token")

        if action == "get_token":
            masked = (_token[:10] + "***" + _token[-4:]
                      if len(_token) > 14 else "***")
            self._json_response(200, {
                "token": _token,
                "token_masked": masked,
                "port": _config.get("port", DEFAULT_PORT),
                "uptime_seconds": round(time.time() - _start_time, 1),
            })

        elif action == "regenerate_token":
            _token = generate_token()
            _config["token"] = _token
            self._json_response(200, {
                "ok": True,
                "token": _token,
                "message": "Token regenerated. Update your clients.",
            })

        else:
            self._json_response(400, {
                "error": f"Unknown action: {action}"})

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW HANDLERS — Logs
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_get_logs(self, agent_id: str, query: dict):
        """Read agent log file with optional filtering."""
        if not self._validate_name(agent_id):
            return

        log_path = os.path.join(".logs", f"{agent_id}.log")
        if not os.path.exists(log_path):
            # List available logs
            available = []
            if os.path.isdir(".logs"):
                available = [f.replace(".log", "")
                             for f in os.listdir(".logs")
                             if f.endswith(".log")]
            self._json_response(404, {
                "error": f"No log for '{agent_id}'",
                "available": available,
            })
            return

        max_lines = int(query.get("lines", ["200"])[0])
        level_filter = query.get("level", [""])[0].upper()
        search_filter = query.get("search", [""])[0].lower()

        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                all_lines = f.readlines()
        except Exception as e:
            self._json_response(500, {"error": str(e)})
            return

        # Apply filters
        filtered = []
        for line in all_lines:
            if level_filter and level_filter not in line.upper():
                continue
            if search_filter and search_filter not in line.lower():
                continue
            filtered.append(line.rstrip())

        # Return last N lines
        tail = filtered[-max_lines:]

        self._json_response(200, {
            "agent_id": agent_id,
            "total_lines": len(all_lines),
            "filtered_lines": len(filtered),
            "returned_lines": len(tail),
            "lines": tail,
        })

    # ══════════════════════════════════════════════════════════════════════════
    #  CHAIN HANDLERS
    # ══════════════════════════════════════════════════════════════════════════

    def _get_chain_manager(self):
        """Get ChainManager instance (returns None if chain disabled)."""
        try:
            import yaml
            with open("config/agents.yaml") as f:
                cfg = yaml.safe_load(f) or {}
            if not cfg.get("chain", {}).get("enabled", False):
                return None
            from adapters.chain.chain_manager import ChainManager
            return ChainManager(cfg)
        except Exception as e:
            logger.warning("Failed to create ChainManager: %s", e)
            return None

    def _handle_chain_status(self):
        mgr = self._get_chain_manager()
        if mgr is None:
            self._json_response(200, {
                "enabled": False,
                "message": "Chain is not enabled in config",
            })
            return
        try:
            status = mgr.get_status()
            self._json_response(200, status)
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_chain_balance(self, agent_id: str):
        if not self._validate_name(agent_id):
            return
        mgr = self._get_chain_manager()
        if mgr is None:
            self._json_response(200, {"agent_id": agent_id, "balance": "0.00",
                                       "chain_enabled": False})
            return
        try:
            balance = mgr.get_balance(agent_id)
            self._json_response(200, {
                "agent_id": agent_id,
                "balance_usdc": balance,
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_chain_identity(self, agent_id: str):
        if not self._validate_name(agent_id):
            return
        mgr = self._get_chain_manager()
        if mgr is None:
            self._json_response(200, {"agent_id": agent_id, "chain_enabled": False})
            return
        try:
            agent_data = mgr.state.get_agent(agent_id)
            self._json_response(200, {
                "agent_id": agent_id,
                "registered": agent_data.get("registered", False),
                "pkp_eth_address": agent_data.get("pkp_eth_address", ""),
                "erc8004_agent_id": agent_data.get("erc8004_agent_id"),
                "agent_card_cid": agent_data.get("agent_card_cid", ""),
                "created_at": agent_data.get("created_at", ""),
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_chain_init(self, agent_id: str):
        if not self._validate_name(agent_id):
            return
        mgr = self._get_chain_manager()
        if mgr is None:
            self._json_response(400, {"error": "Chain is not enabled"})
            return
        try:
            # Get agent config for capabilities
            import yaml
            with open("config/agents.yaml") as f:
                cfg = yaml.safe_load(f) or {}
            agent_cfg = None
            for a in cfg.get("agents", []):
                if a["id"] == agent_id:
                    agent_cfg = a
                    break
            result = mgr.initialize_agent(agent_id, agent_cfg)
            self._json_response(200, {
                "agent_id": agent_id,
                "status": "initialized",
                "data": result,
            })
        except Exception as e:
            logger.error("Chain init failed for %s: %s", agent_id, e)
            self._json_response(500, {"error": str(e)})

    def _handle_chain_register(self, agent_id: str):
        if not self._validate_name(agent_id):
            return
        mgr = self._get_chain_manager()
        if mgr is None:
            self._json_response(400, {"error": "Chain is not enabled"})
            return
        try:
            body = self._read_body()
            metadata = body.get("metadata", {})
            tx_hash = mgr.register_agent(agent_id, metadata)
            self._json_response(200, {
                "agent_id": agent_id,
                "tx_hash": tx_hash,
            })
        except Exception as e:
            logger.error("Chain register failed for %s: %s", agent_id, e)
            self._json_response(500, {"error": str(e)})

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW HANDLERS — Heartbeat
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_heartbeat(self):
        """Return agent heartbeat statuses."""
        try:
            from core.heartbeat import read_all_heartbeats
            agents = read_all_heartbeats()
            online = sum(1 for a in agents if a.get("online"))
            self._json_response(200, {
                "agents": agents,
                "online": online,
                "total": len(agents),
            })
        except Exception as e:
            self._json_response(200, {
                "agents": [],
                "online": 0,
                "total": 0,
                "error": str(e),
            })

    # ══════════════════════════════════════════════════════════════════════════
    #  TASK LIFECYCLE CONTROLS
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_task_cancel(self, task_id: str):
        from core.task_board import TaskBoard
        board = TaskBoard()
        ok = board.cancel(task_id)
        if ok:
            self._json_response(200, {"ok": True, "task_id": task_id,
                                       "status": "cancelled"})
        else:
            self._json_response(400, {"error": f"Cannot cancel task {task_id}"})

    def _handle_task_pause(self, task_id: str):
        from core.task_board import TaskBoard
        board = TaskBoard()
        ok = board.pause(task_id)
        if ok:
            self._json_response(200, {"ok": True, "task_id": task_id,
                                       "status": "paused"})
        else:
            self._json_response(400, {"error": f"Cannot pause task {task_id}"})

    def _handle_task_resume(self, task_id: str):
        from core.task_board import TaskBoard
        board = TaskBoard()
        ok = board.resume(task_id)
        if ok:
            self._json_response(200, {"ok": True, "task_id": task_id,
                                       "status": "pending"})
        else:
            self._json_response(400, {"error": f"Cannot resume task {task_id}"})

    def _handle_task_retry(self, task_id: str):
        from core.task_board import TaskBoard
        board = TaskBoard()
        ok = board.retry(task_id)
        if ok:
            self._json_response(200, {"ok": True, "task_id": task_id,
                                       "status": "pending"})
        else:
            self._json_response(400, {"error": f"Cannot retry task {task_id}"})

    def _handle_cancel_all(self):
        from core.task_board import TaskBoard
        board = TaskBoard()
        count = board.cancel_all()
        self._json_response(200, {"ok": True, "cancelled": count})

    # ══════════════════════════════════════════════════════════════════════════
    #  BUDGET & ALERTS
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_set_budget(self):
        body = self._read_body()
        from core.usage_tracker import UsageTracker
        budget = UsageTracker.set_budget(
            max_cost_usd=float(body.get("max_cost_usd", 0)),
            max_tokens=int(body.get("max_tokens", 0)),
            warn_at_percent=int(body.get("warn_at_percent", 80)),
            enabled=body.get("enabled", True),
        )
        self._json_response(200, {"ok": True, "budget": budget})

    def _handle_get_budget(self):
        from core.usage_tracker import UsageTracker
        budget = UsageTracker.get_budget()
        self._json_response(200, {"budget": budget})

    def _handle_get_alerts(self):
        from core.usage_tracker import UsageTracker
        alerts = UsageTracker.get_alerts()
        self._json_response(200, {"alerts": alerts, "total": len(alerts)})

    # ══════════════════════════════════════════════════════════════════════════
    #  SSE EVENT STREAM
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_sse(self):
        """Server-Sent Events stream for real-time dashboard updates.

        Pushes task board state, heartbeats, and alerts every 1.5s.
        Dashboard connects via: new EventSource('/v1/events')
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        import time as _time

        last_state_hash = ""
        try:
            while True:
                # Build current state snapshot
                snapshot = self._sse_snapshot()
                state_str = json.dumps(snapshot, ensure_ascii=False,
                                       default=str)
                # Only send if state changed (or every 5th cycle as heartbeat)
                state_hash = str(hash(state_str))
                if state_hash != last_state_hash:
                    self.wfile.write(f"event: state\ndata: {state_str}\n\n"
                                    .encode("utf-8"))
                    self.wfile.flush()
                    last_state_hash = state_hash
                else:
                    # Send keepalive comment
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()

                _time.sleep(1.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # Client disconnected

    def _sse_snapshot(self) -> dict:
        """Build a compact state snapshot for SSE push."""
        snapshot: dict = {"ts": time.time()}

        # Task board
        try:
            if os.path.exists(".task_board.json"):
                with open(".task_board.json") as f:
                    tasks = json.load(f)
                # Compact: only send essential fields
                compact_tasks = {}
                for tid, t in tasks.items():
                    compact_tasks[tid] = {
                        "s": t.get("status", "?"),
                        "a": t.get("agent_id", ""),
                        "d": (t.get("description", ""))[:60],
                        "ca": t.get("claimed_at"),
                        "co": t.get("completed_at"),
                        "rc": t.get("retry_count", 0),
                    }
                    # Include review score if available
                    scores = t.get("review_scores", [])
                    if scores:
                        avg = sum(r["score"] for r in scores) / len(scores)
                        compact_tasks[tid]["rs"] = int(avg)
                snapshot["tasks"] = compact_tasks
        except Exception:
            snapshot["tasks"] = {}

        # Agent heartbeats
        try:
            from core.heartbeat import read_all_heartbeats
            agents = read_all_heartbeats()
            snapshot["agents"] = [
                {"id": a.get("agent_id", ""), "on": a.get("online", False),
                 "st": a.get("status", "offline"), "tid": a.get("task_id")}
                for a in agents
            ]
        except Exception:
            snapshot["agents"] = []

        # Budget status (compact)
        try:
            from core.usage_tracker import UsageTracker
            budget = UsageTracker.get_budget()
            if budget.get("enabled"):
                snapshot["budget"] = {
                    "pct": budget.get("percent_used", 0),
                    "cost": round(budget.get("current_cost_usd", 0), 4),
                    "limit": budget.get("max_cost_usd", 0),
                }
        except Exception:
            pass

        return snapshot

    # ══════════════════════════════════════════════════════════════════════════
    #  MEMORY HANDLERS
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_memory_status(self):
        """Return memory system status for all agents."""
        try:
            import yaml
            with open("config/agents.yaml") as f:
                cfg = yaml.safe_load(f) or {}
            agents_data = {}
            for a in cfg.get("agents", []):
                aid = a["id"]
                try:
                    from adapters.memory.episodic import EpisodicMemory
                    ep = EpisodicMemory(aid)
                    agents_data[aid] = ep.stats()
                except Exception:
                    agents_data[aid] = {"episodes": 0, "cases": 0,
                                        "patterns": 0, "daily_logs": 0}
            # Shared KB stats
            kb_stats = {}
            try:
                from adapters.memory.knowledge_base import KnowledgeBase
                kb = KnowledgeBase()
                kb_stats = kb.stats()
            except Exception:
                kb_stats = {"notes": 0, "insights": 0}

            self._json_response(200, {
                "agents": agents_data,
                "knowledge_base": kb_stats,
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_memory_episodes(self, agent_id: str):
        """Return recent episodes for an agent."""
        if not self._validate_name(agent_id):
            return
        try:
            from adapters.memory.episodic import EpisodicMemory
            ep = EpisodicMemory(agent_id)
            episodes = ep.list_episodes(limit=30, level=1)
            self._json_response(200, {
                "agent_id": agent_id,
                "episodes": episodes,
                "total": len(episodes),
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_memory_cases(self, agent_id: str):
        """Return extracted cases for an agent."""
        if not self._validate_name(agent_id):
            return
        try:
            from adapters.memory.episodic import EpisodicMemory
            ep = EpisodicMemory(agent_id)
            cases = ep.list_cases(limit=30)
            self._json_response(200, {
                "agent_id": agent_id,
                "cases": cases,
                "total": len(cases),
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_memory_daily(self, agent_id: str):
        """Return today's daily learning log for an agent."""
        if not self._validate_name(agent_id):
            return
        try:
            from adapters.memory.episodic import EpisodicMemory
            ep = EpisodicMemory(agent_id)
            log = ep.get_daily_log()
            stats = ep.stats()
            self._json_response(200, {
                "agent_id": agent_id,
                "daily_log": log,
                "dates": stats.get("dates", []),
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_kb_notes(self):
        """Return all knowledge base notes."""
        try:
            from adapters.memory.knowledge_base import KnowledgeBase
            kb = KnowledgeBase()
            notes = kb.list_notes(limit=50)
            self._json_response(200, {
                "notes": notes,
                "total": len(notes),
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_kb_moc(self):
        """Return the Map of Content."""
        try:
            from adapters.memory.knowledge_base import KnowledgeBase
            kb = KnowledgeBase()
            moc = kb.get_moc()
            self._json_response(200, {"moc": moc})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_kb_insights(self):
        """Return recent cross-agent insights."""
        try:
            from adapters.memory.knowledge_base import KnowledgeBase
            kb = KnowledgeBase()
            insights = kb.recent_insights(limit=50)
            self._json_response(200, {
                "insights": insights,
                "total": len(insights),
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})


# ══════════════════════════════════════════════════════════════════════════════
#  .env FILE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _save_env_var(key: str, value: str, env_path: str = ".env"):
    """
    Save or update a KEY=VALUE pair in the .env file.
    Also sets it in the current process's os.environ immediately.
    """
    os.environ[key] = value

    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.split("=", 1)[0].strip()
                    if k == key:
                        lines.append(f"{key}={value}\n")
                        found = True
                        continue
                lines.append(line)

    if not found:
        lines.append(f"{key}={value}\n")

    with open(env_path, "w") as f:
        f.writelines(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  SERVER LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

def generate_token() -> str:
    """Generate a secure random token."""
    return f"swarm-{secrets.token_urlsafe(24)}"


def start_gateway(port: int = 0, token: str = "",
                   daemon: bool = True) -> HTTPServer | None:
    """
    Start the gateway HTTP server.
    Returns the server instance (or None on failure).
    """
    global _start_time, _token, _config

    port = port or int(os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT)))
    token = token or os.environ.get("SWARM_GATEWAY_TOKEN", "")

    _start_time = time.time()
    _token = token
    _config = {"port": port, "token": token}

    try:
        server = HTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        logger.error("Cannot start gateway on port %d: %s", port, e)
        return None

    if daemon:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info("Gateway started on http://127.0.0.1:%d", port)
        logger.info("Dashboard: http://127.0.0.1:%d/", port)
    else:
        logger.info("Gateway running on http://127.0.0.1:%d (foreground)",
                     port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()

    return server


def check_gateway(port: int = 0) -> tuple[bool, str]:
    """Check if the gateway is reachable. Returns (ok, message)."""
    import httpx

    port = port or int(os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT)))
    url = f"http://127.0.0.1:{port}/health"

    try:
        resp = httpx.get(url, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            return (True,
                    f"OK — {data.get('agents', 0)} agents, "
                    f"uptime {data.get('uptime_seconds', 0)}s")
        return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, f"Cannot connect to port {port}"
    except Exception as e:
        return False, str(e)


# ── CLI entry point ──
def run_gateway_cli(port: int = 0, token: str = ""):
    """Run gateway from CLI (foreground mode)."""
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] %(message)s",
                        datefmt="%H:%M:%S")

    # Load env
    from core.env_loader import load_dotenv
    load_dotenv()

    # Parse --port / --token from remaining argv (skip subcommand)
    import sys
    argv = sys.argv[1:]
    # Strip leading subcommand like "gateway"
    if argv and not argv[0].startswith("-"):
        argv = argv[1:]

    i = 0
    while i < len(argv):
        if argv[i] in ("-p", "--port") and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif argv[i] in ("-t", "--token") and i + 1 < len(argv):
            token = argv[i + 1]
            i += 2
        else:
            i += 1

    token = token or os.environ.get("SWARM_GATEWAY_TOKEN", "")
    if not token:
        token = generate_token()
        print(f"  Generated token: {token}")

    port = port or int(os.environ.get("SWARM_GATEWAY_PORT",
                                       str(DEFAULT_PORT)))
    print(f"  Dashboard: http://127.0.0.1:{port}/")
    print(f"  API Base:  http://127.0.0.1:{port}/v1")
    print()

    start_gateway(port=port, token=token, daemon=False)


# ══════════════════════════════════════════════════════════════════════════════
#  GATEWAY LIFECYCLE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def probe_gateway(port: int = 0) -> dict:
    """
    Deep probe — health check + heartbeat + task count.
    Returns a rich status dict for display.
    """
    import httpx

    port = port or int(os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT)))
    base = f"http://127.0.0.1:{port}"
    result = {
        "reachable": False,
        "port": port,
        "url": base,
    }

    # Health probe
    try:
        resp = httpx.get(f"{base}/health", timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            result["reachable"] = True
            result["agents_count"] = data.get("agents", 0)
            result["uptime_seconds"] = data.get("uptime_seconds", 0)
        else:
            result["error"] = f"HTTP {resp.status_code}"
            return result
    except httpx.ConnectError:
        result["error"] = "Cannot connect"
        return result
    except Exception as e:
        result["error"] = str(e)
        return result

    # Heartbeat probe (requires token — try without, tolerate 401)
    token = os.environ.get("SWARM_GATEWAY_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = httpx.get(f"{base}/v1/heartbeat", headers=headers, timeout=3.0)
        if resp.status_code == 200:
            hb = resp.json()
            result["agents_online"] = hb.get("online", 0)
            result["agents_total"] = hb.get("total", 0)
            result["agents"] = hb.get("agents", [])
    except Exception:
        pass

    # Task count
    try:
        resp = httpx.get(f"{base}/v1/status", headers=headers, timeout=3.0)
        if resp.status_code == 200:
            tasks = resp.json().get("tasks", {})
            result["task_count"] = len(tasks)
            result["active_tasks"] = sum(
                1 for t in tasks.values()
                if t.get("status") in ("pending", "claimed", "review")
            )
    except Exception:
        pass

    return result


def kill_port(port: int) -> bool:
    """Kill whatever process is listening on the given port. Returns True if killed."""
    import subprocess
    import signal

    try:
        # lsof to find PID
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True, text=True, timeout=5,
        )
        pids = result.stdout.strip().split()
        if not pids:
            return False
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        time.sleep(0.5)
        return True
    except FileNotFoundError:
        # lsof not available — try fuser (Linux)
        try:
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                capture_output=True, timeout=5,
            )
            time.sleep(0.5)
            return True
        except Exception:
            return False
    except Exception:
        return False
