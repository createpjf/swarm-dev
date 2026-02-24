"""
core/gateway.py
Lightweight HTTP gateway — exposes Cleo Agent Stack as a local API.

Endpoints:
  GET  /                            Web Dashboard (no auth)
  GET  /health                      Health check (no auth)
  POST /v1/task                     Submit a task → returns task_id
  GET  /v1/task/:id                 Get task status & result
  GET  /v1/status                   Full task board
  POST /v1/agents                   Create a new agent
  DELETE /v1/agents/:id             Delete an agent
  POST /v1/exec                     Execute shell command (approval-gated)
  GET  /v1/exec/approvals           List exec approval patterns
  POST /v1/exec/approve             Add command pattern to allowlist
  GET  /v1/cron                     List scheduled jobs
  POST /v1/cron                     Create a scheduled job
  DELETE /v1/cron/:id               Remove a scheduled job
  POST /v1/cron/:id/run             Manually trigger a job
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
  PUT  /v1/memory/cases/:id/:hash    Update a case (solution, tags, notes)
  PUT  /v1/memory/episodes/:id/:tid  Update an episode (notes, tags, outcome)
  POST /v1/memory/export/:agent_id   Export all memory for an agent (JSON)
  GET  /v1/channels                   Channel adapter status (Telegram/Discord/Feishu/Slack)
  PUT  /v1/channels/:name              Update channel config (enable/disable, tokens)
  GET  /v1/tools                      List built-in tools and availability

Default port: 19789  (configurable via CLEO_GATEWAY_PORT or config)
Auth: Bearer token  (auto-injected into dashboard, configurable via CLEO_GATEWAY_TOKEN)
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
_channel_manager = None  # ChannelManager instance (set by start_gateway)

# SSE task-board mtime cache (avoids json.load every 1.5s when idle)
_sse_tb_cache: dict = {}
_sse_tb_mtime: float = 0.0


# ── Sensitive field redaction ──────────────────────────────────────────────────

# Field name patterns that indicate sensitive values
_SENSITIVE_KEY_PATTERNS = re.compile(
    r'(api[_\-]?key|secret|password|token|credential|auth[_\-]?key|'
    r'private[_\-]?key|access[_\-]?key|signing[_\-]?key|'
    r'passphrase|bearer|webhook[_\-]?secret)',
    re.IGNORECASE,
)

# Env-var reference fields — show status instead of value
_ENV_REF_PATTERNS = re.compile(
    r'(api_key_env|token_env|secret_env|key_env)',
    re.IGNORECASE,
)


def redact_config(cfg: dict | list | Any) -> dict | list | Any:
    """Recursively redact sensitive fields in configuration data.

    Rules:
      - Fields matching _SENSITIVE_KEY_PATTERNS → masked as "sk-***…***"
      - Fields matching _ENV_REF_PATTERNS → show "ENV_NAME (set)" / "(not set)"
      - Nested dicts/lists are recursively processed
      - Non-string values for sensitive fields → "***"

    This is used by GET /v1/config, the dashboard config view,
    and cleo config get (--json mode) when the value is sensitive.
    """
    if isinstance(cfg, dict):
        result = {}
        for key, val in cfg.items():
            if _ENV_REF_PATTERNS.search(key):
                # Show env var name + whether it's set
                if isinstance(val, str) and val:
                    is_set = bool(os.environ.get(val, ""))
                    result[key] = f"{val} ({'set' if is_set else 'not set'})"
                else:
                    result[key] = val
            elif _SENSITIVE_KEY_PATTERNS.search(key):
                # Mask the actual value
                if isinstance(val, str) and len(val) > 8:
                    result[key] = val[:3] + "***…" + val[-3:]
                elif isinstance(val, str) and val:
                    result[key] = "***"
                else:
                    result[key] = val  # empty/null stays as-is
            else:
                result[key] = redact_config(val)
        return result
    elif isinstance(cfg, list):
        return [redact_config(item) for item in cfg]
    return cfg


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

    def _check_dashboard_auth(self) -> bool:
        """Check if dashboard request is authenticated via cookie or query param."""
        if not _token:
            return True
        # Check cookie
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("cleo_auth="):
                if part[len("cleo_auth="):] == _token:
                    return True
        # Check ?token= query param (for initial login)
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("token", [None])[0] == _token:
            return True
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
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
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

        # Login endpoint — set auth cookie and redirect to dashboard
        if path == "/login":
            parsed_url = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed_url.query)
            submitted_token = params.get("token", [None])[0]
            if submitted_token and submitted_token == _token:
                # Set cookie and redirect to dashboard
                self.send_response(302)
                self.send_header("Set-Cookie",
                                 f"cleo_auth={_token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400")
                self.send_header("Location", "/")
                self.end_headers()
            else:
                self._serve_login_page(error="Invalid token." if submitted_token else "")
            return

        # Dashboard — requires cookie/token auth
        if path == "" or path == "/":
            if self._check_dashboard_auth():
                # Set cookie if accessing via ?token= param (auto-login from onboard)
                parsed_url = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed_url.query)
                if params.get("token"):
                    self.send_response(302)
                    self.send_header("Set-Cookie",
                                     f"cleo_auth={_token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400")
                    self.send_header("Location", "/")
                    self.end_headers()
                else:
                    self._serve_dashboard()
            else:
                self._serve_login_page()
            return

        # /health is public (no auth required)
        if path == "/health":
            self._handle_health()
            return

        # Agent avatars — public (no auth required)
        if path.startswith("/avatars/"):
            self._serve_avatar(path.split("/avatars/", 1)[1])
            return

        if not self._check_auth():
            return

        if path == "/v1/status":
            self._handle_status()
        elif path == "/v1/scores":
            self._handle_scores()
        elif path.startswith("/v1/scores/history"):
            self._handle_scores_history()
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
        elif path == "/v1/tools":
            self._handle_tools()
        elif path == "/v1/models":
            self._handle_models()
        elif path == "/v1/providers":
            self._handle_providers()
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
        # ── Cleo Files routes ──
        elif path == "/v1/cleo-files":
            self._handle_list_cleo_files()
        elif path.startswith("/v1/cleo-files/"):
            rest = path[len("/v1/cleo-files/"):]
            parts = rest.split("/", 1)
            if len(parts) == 2:
                self._handle_get_cleo_file(parts[0], parts[1])
            else:
                self._json_response(400, {"error": "Expected /v1/cleo-files/:scope/:filename"})
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
        # ── Channel status ──
        elif path == "/v1/channels":
            self._handle_channels()
        # ── Cron route ──
        elif path == "/v1/cron":
            self._handle_cron_list()
        # ── Exec approvals ──
        elif path == "/v1/exec/approvals":
            self._handle_exec_approvals()
        # ── Logs route ──
        elif path.startswith("/v1/logs/"):
            agent_id = path[len("/v1/logs/"):]
            query = urllib.parse.parse_qs(parsed.query)
            self._handle_get_logs(agent_id, query)
        # ── File download (agent output files) ──
        elif path == "/v1/file":
            query = urllib.parse.parse_qs(parsed.query)
            file_path = query.get("path", [""])[0]
            self._handle_serve_file(file_path)
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
        elif path == "/v1/exec":
            self._handle_exec()
        elif path == "/v1/exec/approve":
            self._handle_exec_add_approval()
        elif path == "/v1/agents":
            self._handle_create_agent()
        elif path.startswith("/v1/agents/") and path.endswith("/avatar"):
            agent_id = path[len("/v1/agents/"):-len("/avatar")]
            self._handle_upload_avatar(agent_id)
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
        # ── Cron management ──
        elif path == "/v1/cron":
            self._handle_cron_add()
        elif path.startswith("/v1/cron/") and path.endswith("/run"):
            job_id = path[len("/v1/cron/"):-len("/run")]
            self._handle_cron_run(job_id)
        # ── Channel token test ──
        elif path.startswith("/v1/channels/") and path.endswith("/test"):
            channel_name = path[len("/v1/channels/"):-len("/test")]
            self._handle_test_channel(channel_name)
        # ── File / message send proxy (from agent subprocess) ──
        elif path == "/v1/send_file":
            self._handle_send_file_proxy()
        elif path == "/v1/send_message":
            self._handle_send_message_proxy()
        # ── Channel reload ──
        elif path == "/v1/channels/reload":
            self._handle_reload_channels()
        # ── Memory export ──
        elif path.startswith("/v1/memory/export/"):
            agent_id = path[len("/v1/memory/export/"):]
            self._handle_memory_export(agent_id)
        # ── Webhook inbound (external services: GitHub, Jira, etc.) ──
        elif path.startswith("/v1/webhook/"):
            source = path[len("/v1/webhook/"):]
            self._handle_webhook_inbound(source)
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
        elif path.startswith("/v1/cleo-files/"):
            rest = path[len("/v1/cleo-files/"):]
            parts = rest.split("/", 1)
            if len(parts) == 2:
                self._handle_update_cleo_file(parts[0], parts[1])
            else:
                self._json_response(400, {"error": "Expected /v1/cleo-files/:scope/:filename"})
        elif path.startswith("/v1/channels/"):
            channel_name = path[len("/v1/channels/"):]
            self._handle_update_channel(channel_name)
        elif path.startswith("/v1/agents/"):
            agent_id = path[len("/v1/agents/"):]
            self._handle_update_agent(agent_id)
        # ── Memory write routes ──
        elif path.startswith("/v1/memory/cases/"):
            # PUT /v1/memory/cases/:agent_id/:case_hash
            rest = path[len("/v1/memory/cases/"):]
            parts = rest.split("/", 1)
            if len(parts) == 2:
                self._handle_update_case(parts[0], parts[1])
            else:
                self._json_response(400, {"error": "Expected /v1/memory/cases/:agent_id/:hash"})
        elif path.startswith("/v1/memory/episodes/"):
            # PUT /v1/memory/episodes/:agent_id/:task_id
            rest = path[len("/v1/memory/episodes/"):]
            parts = rest.split("/", 1)
            if len(parts) == 2:
                self._handle_update_episode(parts[0], parts[1])
            else:
                self._json_response(400, {"error": "Expected /v1/memory/episodes/:agent_id/:task_id"})
        else:
            self._json_response(404, {"error": "Not found"})

    # ── DELETE Routes ──
    def do_DELETE(self):
        if not self._check_auth():
            return

        path = self.path.rstrip("/")

        if path.startswith("/v1/cron/"):
            job_id = path[len("/v1/cron/"):]
            self._handle_cron_remove(job_id)
        elif path.startswith("/v1/agents/"):
            agent_id = path[len("/v1/agents/"):]
            self._handle_delete_agent(agent_id)
        elif path.startswith("/v1/skills/agents/"):
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
        ws_info = {}
        try:
            from core.ws_gateway import _instance as _ws_instance
            if _ws_instance and _ws_instance.is_running:
                ws_info = {
                    "ws_port": _ws_instance.port,
                    "ws_clients": _ws_instance.client_count,
                }
        except ImportError:
            pass
        self._json_response(200, {
            "status": "ok",
            "agents": agents_count,
            "uptime_seconds": round(uptime, 1),
            "port": _config.get("port", DEFAULT_PORT),
            **ws_info,
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

    def _handle_scores_history(self):
        """GET /v1/scores/history?agent_id=...&limit=20"""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        agent_id = qs.get("agent_id", [None])[0]
        limit = int(qs.get("limit", [20])[0])

        try:
            from reputation.scorer import ScoreAggregator
            scorer = ScoreAggregator()
            if agent_id:
                history = scorer.get_history(agent_id, limit=limit)
                self._json_response(200, {"agent_id": agent_id, "history": history})
            else:
                # Return history for all agents
                cache_path = "memory/reputation_cache.json"
                if not os.path.exists(cache_path):
                    self._json_response(200, {"agents": {}})
                    return
                with open(cache_path) as f:
                    cache = json.load(f)
                result = {}
                for aid, entry in cache.items():
                    result[aid] = entry.get("history", [])[-limit:]
                self._json_response(200, {"agents": result})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

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
            # Current task (from task board)
            current_task = None
            try:
                if os.path.exists(".task_board.json"):
                    with open(".task_board.json") as tb:
                        tasks = json.load(tb)
                    for tid, t in tasks.items():
                        if t.get("agent_id") == a["id"] and t.get("status") in ("claimed", "critique"):
                            current_task = {
                                "id": tid[:8],
                                "description": t.get("description", "")[:80],
                                "status": t.get("status", ""),
                            }
                            break
            except Exception:
                pass

            # Recent logs (last 5 lines)
            recent_logs = []
            try:
                log_path = os.path.join(".logs", f"{a['id']}.log")
                if os.path.exists(log_path):
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as lf:
                        all_lines = lf.readlines()
                    for line in all_lines[-5:]:
                        recent_logs.append(line.rstrip()[:120])
            except Exception:
                pass

            # Agent files (soul.md, skills, etc.)
            agent_files = []
            try:
                agent_dir = os.path.join("skills", "agents", a["id"])
                if os.path.isdir(agent_dir):
                    for fn in sorted(os.listdir(agent_dir)):
                        if fn.endswith(".md"):
                            fp = os.path.join(agent_dir, fn)
                            st = os.stat(fp)
                            agent_files.append({
                                "name": fn,
                                "path": fp,
                                "size": st.st_size,
                                "mtime": st.st_mtime,
                            })
            except Exception:
                pass

            # Agent tools config
            tools_cfg = a.get("tools", {})

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
                "current_task": current_task,
                "recent_logs": recent_logs,
                "files": agent_files,
                "tools": tools_cfg,
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

        # Archive completed tasks for cross-round context before clearing
        try:
            from core.task_history import save_round
            old_data = board._read()
            if old_data:
                save_round(old_data)
        except Exception as e:
            logger.warning("Failed to archive task history: %s", e)

        board.clear(force=True)
        # NOTE: We no longer destroy .context_bus.json or .mailboxes
        # to preserve cross-round context for session continuity.
        # ContextBus TTL mechanism handles natural expiry of stale entries.

        orch = Orchestrator()
        task_id = orch.submit(description, required_role="planner")

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
    def _serve_login_page(self, error: str = ""):
        """Serve a minimal login page for dashboard authentication."""
        error_html = f'<p style="color:#ff4444;margin-bottom:16px">{error}</p>' if error else ''
        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cleo — Login</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0a0a0f; color: #e0e0e0; display: flex; align-items: center;
         justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #1a1a2e; border-radius: 12px; padding: 40px;
           max-width: 380px; width: 100%; box-shadow: 0 4px 24px rgba(0,0,0,.4); }}
  h1 {{ font-size: 24px; margin: 0 0 8px; color: #c084fc; }}
  p.sub {{ color: #888; font-size: 14px; margin: 0 0 24px; }}
  input {{ width: 100%; padding: 12px; border: 1px solid #333; border-radius: 8px;
           background: #111; color: #eee; font-size: 16px; box-sizing: border-box;
           margin-bottom: 16px; }}
  input:focus {{ border-color: #c084fc; outline: none; }}
  button {{ width: 100%; padding: 12px; border: none; border-radius: 8px;
            background: #7c3aed; color: #fff; font-size: 16px; cursor: pointer; }}
  button:hover {{ background: #6d28d9; }}
</style></head><body>
<div class="card">
  <h1>Cleo Dashboard</h1>
  <p class="sub">Enter your gateway token to continue.</p>
  {error_html}
  <form action="/login" method="GET">
    <input type="password" name="token" placeholder="Gateway token" autofocus required>
    <button type="submit">Sign in</button>
  </form>
  <p style="color:#666;font-size:12px;margin-top:16px;text-align:center">
    Find your token: <code>echo $CLEO_GATEWAY_TOKEN</code>
  </p>
</div></body></html>"""
        self._html_response(200, html.encode("utf-8"))

    def _serve_dashboard(self):
        """Serve the embedded web dashboard with auto-injected auth token."""
        html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        if not os.path.exists(html_path):
            self._json_response(404, {"error": "dashboard.html not found"})
            return
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
            # Auto-inject gateway token so the dashboard can authenticate
            # without requiring the user to manually enter it.
            if _token:
                inject_script = (
                    f'\n<script>window.__CLEO_TOKEN__="{_token}";</script>\n'
                )
                html = html.replace("</head>", inject_script + "</head>", 1)
            self._html_response(200, html.encode("utf-8"))
        except Exception as e:
            self._json_response(500, {"error": f"Failed to serve dashboard: {e}"})

    def _serve_avatar(self, filename: str):
        """Serve agent avatar images from core/avatars/."""
        import re
        if not re.match(r'^[a-zA-Z0-9_-]+\.png$', filename):
            self._json_response(404, {"error": "not found"})
            return
        avatar_path = os.path.join(os.path.dirname(__file__), "avatars", filename)
        if not os.path.exists(avatar_path):
            self._json_response(404, {"error": "avatar not found"})
            return
        try:
            with open(avatar_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self._json_response(500, {"error": "failed to serve avatar"})

    def _handle_upload_avatar(self, agent_id: str):
        """Upload a PNG avatar for an agent → core/avatars/{agent_id}.png."""
        import re as _re
        if not _re.match(r'^[a-zA-Z0-9_-]+$', agent_id):
            self._json_response(400, {"error": "Invalid agent ID"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 512_000:  # 500KB max
            self._json_response(413, {"error": "File too large (max 500KB)"})
            return
        if content_length == 0:
            self._json_response(400, {"error": "Empty body"})
            return

        raw = self.rfile.read(content_length)
        content_type = self.headers.get("Content-Type", "")

        # Extract image data from multipart or raw
        if "multipart" in content_type:
            boundary = content_type.split("boundary=")[-1].strip()
            parts = raw.split(f"--{boundary}".encode())
            png_data = None
            for part in parts:
                if b"Content-Type: image/" in part:
                    idx = part.find(b"\r\n\r\n")
                    if idx >= 0:
                        png_data = part[idx + 4:]
                        # Strip trailing boundary markers
                        if png_data.endswith(b"\r\n"):
                            png_data = png_data[:-2]
                        if png_data.endswith(b"--"):
                            png_data = png_data[:-2]
                        if png_data.endswith(b"\r\n"):
                            png_data = png_data[:-2]
                        break
            if not png_data:
                self._json_response(400, {"error": "No image found in upload"})
                return
        else:
            png_data = raw

        # Validate PNG magic bytes
        if len(png_data) < 8 or png_data[:4] != b'\x89PNG':
            self._json_response(400, {"error": "Not a valid PNG file"})
            return

        # Save to core/avatars/{agent_id}.png
        avatar_dir = os.path.join(os.path.dirname(__file__), "avatars")
        os.makedirs(avatar_dir, exist_ok=True)
        avatar_path = os.path.join(avatar_dir, f"{agent_id}.png")
        with open(avatar_path, "wb") as f:
            f.write(png_data)

        self._json_response(200, {
            "ok": True,
            "avatar": f"/avatars/{agent_id}.png",
        })

    def _handle_serve_file(self, file_path: str):
        """Serve a file produced by agents (PDF, images, data files, etc.)."""
        if not file_path:
            self._json_response(400, {"error": "Missing 'path' parameter"})
            return
        # Security: only allow /tmp/ and workspace/ paths
        real = os.path.realpath(file_path)
        allowed_prefixes = ["/tmp/", os.path.realpath("workspace") + "/"]
        if not any(real.startswith(p) for p in allowed_prefixes):
            self._json_response(403, {"error": "Access denied: only /tmp and workspace files"})
            return
        if not os.path.isfile(real):
            self._json_response(404, {"error": f"File not found: {file_path}"})
            return
        # Determine content type
        ext = os.path.splitext(real)[1].lower()
        content_types = {
            ".pdf": "application/pdf",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
            ".csv": "text/csv", ".json": "application/json",
            ".txt": "text/plain", ".md": "text/plain",
            ".html": "text/html",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        ctype = content_types.get(ext, "application/octet-stream")
        try:
            with open(real, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            fname = os.path.basename(real)
            # Inline display for images and PDFs, download for others
            if ext in (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".html"):
                self.send_header("Content-Disposition", f"inline; filename=\"{fname}\"")
            else:
                self.send_header("Content-Disposition", f"attachment; filename=\"{fname}\"")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json_response(500, {"error": f"Failed to serve file: {e}"})

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

            sanitized = json.loads(json.dumps(cfg, default=str))
            sanitized = redact_config(sanitized)
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
    #  Tools API
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_tools(self):
        """List all built-in tools with availability status."""
        try:
            from core.tools import list_all_tools, TOOL_PROFILES, TOOL_GROUPS
            tools = list_all_tools()
            tool_list = []
            for t in tools:
                tool_list.append({
                    "name": t.name,
                    "description": t.description,
                    "group": t.group,
                    "available": t.is_available(),
                    "requires_env": t.requires_env,
                    "parameters": t.parameters,
                })
            self._json_response(200, {
                "tools": tool_list,
                "profiles": {k: list(v) if v else "all"
                             for k, v in TOOL_PROFILES.items()},
                "groups": TOOL_GROUPS,
            })
        except Exception as e:
            self._json_response(500, {"error": f"Failed to list tools: {e}"})

    def _handle_models(self):
        """Fetch available models from a provider's /v1/models endpoint.
        Query params: provider, base_url, api_key (optional overrides).
        """
        import urllib.parse as up
        qs = up.parse_qs(up.urlparse(self.path).query)
        provider = (qs.get("provider", [""])[0] or "").strip()
        base_url = (qs.get("base_url", [""])[0] or "").strip()
        api_key  = (qs.get("api_key", [""])[0] or "").strip()

        # Resolve base URL from provider name
        provider_urls = {
            "flock":    "https://api.flock.io/v1",
            "openai":   "https://api.openai.com/v1",
            "minimax":  "https://api.minimax.io/v1",
            "ollama":   "http://localhost:11434/v1",
            "deepseek": "https://api.deepseek.com/v1",
        }
        if not base_url and provider:
            base_url = provider_urls.get(provider, "")
        if not base_url:
            self._json_response(400, {"error": "Missing provider or base_url"})
            return

        # Resolve API key from env
        if not api_key:
            key_env_map = {
                "flock": "FLOCK_API_KEY",
                "openai": "OPENAI_API_KEY",
                "minimax": "MINIMAX_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
            }
            env_name = key_env_map.get(provider, "FLOCK_API_KEY")
            api_key = os.environ.get(env_name, "")

        # Known model lists for providers without /v1/models endpoint
        _KNOWN_MODELS = {
            "minimax": [
                "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-M2",
                "MiniMax-M2.5-highspeed", "MiniMax-M2.1-highspeed",
            ],
            "deepseek": [
                "deepseek-chat", "deepseek-reasoner",
                "deepseek-v3.2", "deepseek-v3", "deepseek-r1",
            ],
            "anthropic": [
                "claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001",
                "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
            ],
        }

        # Try API first, fall back to known list
        models = []
        api_error = None
        from_api = False
        if api_key:
            try:
                import httpx
                url = f"{base_url.rstrip('/')}/models"
                with httpx.Client(timeout=10.0) as client:
                    resp = client.get(url, headers={
                        "Authorization": f"Bearer {api_key}",
                    })
                    resp.raise_for_status()
                    data = resp.json()
                for m in data.get("data", []):
                    models.append({
                        "id": m.get("id", ""),
                        "owned_by": m.get("owned_by", ""),
                    })
                if models:
                    from_api = True
            except Exception as e:
                api_error = str(e)

        # Fallback to known models
        if not models and provider in _KNOWN_MODELS:
            models = [{"id": m, "owned_by": provider}
                      for m in _KNOWN_MODELS[provider]]

        if models:
            models.sort(key=lambda x: x["id"])
            self._json_response(200, {
                "models": models, "provider": provider,
                "source": "api" if from_api else "known_list",
            })
        elif api_error:
            self._json_response(502, {
                "error": f"Failed to fetch models: {api_error}"})
        else:
            self._json_response(200, {
                "models": [], "provider": provider,
                "error": "No API key set and no known model list for this provider"})

    def _handle_providers(self):
        """Return provider router status for dashboard."""
        try:
            from core.provider_router import get_router
            router = get_router()
            if router:
                self._json_response(200, router.get_status())
            else:
                self._json_response(200, {
                    "enabled": False,
                    "message": "Provider router not enabled. "
                    "Add provider_router.enabled: true to agents.yaml",
                })
        except ImportError:
            self._json_response(200, {
                "enabled": False,
                "error": "provider_router module not available",
            })

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
                       "autonomy_level", "tools"):
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

    def _handle_create_agent(self):
        """Create a new agent via POST /v1/agents.

        Body: {id, role, model, provider, skills?, api_key?, base_url?}
        """
        import yaml
        body = self._read_body()
        if not body:
            self._json_response(400, {"error": "Empty body"})
            return

        agent_id = body.get("id", "").strip()
        if not agent_id:
            self._json_response(400, {"error": "Missing 'id'"})
            return
        if not self._validate_name(agent_id):
            return

        # Template support — predefined agent archetypes
        _TEMPLATES = {
            "research_agent": {
                "role": "Research specialist — web search, data analysis, report writing",
                "skills": ["_base", "research"],
                "autonomy_level": 2,
            },
            "coding_agent": {
                "role": "Software engineer — code writing, debugging, code review",
                "skills": ["_base", "coding"],
                "autonomy_level": 2,
            },
            "review_agent": {
                "role": "Quality reviewer — reviews outputs, provides critique scores",
                "skills": ["_base"],
                "autonomy_level": 1,
            },
            "assistant_agent": {
                "role": "General assistant — task planning, scheduling, communication",
                "skills": ["_base"],
                "autonomy_level": 1,
            },
        }
        template_name = body.get("template", "")
        if template_name and template_name in _TEMPLATES:
            tmpl = _TEMPLATES[template_name]
            body.setdefault("role", tmpl["role"])
            body.setdefault("skills", tmpl["skills"])
            body.setdefault("autonomy_level", tmpl["autonomy_level"])

        role = body.get("role", "general assistant").strip()
        model = body.get("model", "").strip()
        provider = body.get("provider", "flock").strip()

        if not model:
            self._json_response(400, {"error": "Missing 'model'"})
            return

        # Load existing config
        config_path = "config/agents.yaml"
        if not os.path.exists(config_path):
            self._json_response(400, {
                "error": "Config not found. Run 'cleo onboard' first."})
            return

        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}

        # Check for duplicate
        existing_ids = [a["id"] for a in cfg.get("agents", [])]
        if agent_id in existing_ids:
            self._json_response(409, {
                "error": f"Agent '{agent_id}' already exists"})
            return

        # Build agent entry (inline — avoid onboard.py dependency in gateway)
        skills = body.get("skills", ["_base"])
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(",") if s.strip()]

        # Provider → env var mapping
        _PROVIDER_ENVS = {
            "flock": ("FLOCK_API_KEY", "FLOCK_BASE_URL"),
            "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
            "ollama": ("", "OLLAMA_URL"),
            "anthropic": ("ANTHROPIC_API_KEY", ""),
            "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
        }
        key_env, url_env = _PROVIDER_ENVS.get(provider, ("", ""))

        # Tools config from request body (default: coding profile)
        tools_cfg = body.get("tools", {"profile": "coding"})

        entry = {
            "id": agent_id,
            "role": role,
            "model": model,
            "skills": skills,
            "tools": tools_cfg,
            "memory": {"short_term_turns": 20, "long_term": True,
                       "recall_top_k": 3},
            "autonomy_level": int(body.get("autonomy_level", 1)),
            "llm": {"provider": provider},
        }
        if key_env:
            entry["llm"]["api_key_env"] = key_env
        if url_env:
            entry["llm"]["base_url_env"] = url_env

        # Handle direct API key / base URL
        if body.get("api_key"):
            env_name = f"{agent_id.upper()}_API_KEY"
            _save_env_var(env_name, body["api_key"])
            entry["llm"]["api_key_env"] = env_name
        if body.get("base_url"):
            env_name = f"{agent_id.upper()}_BASE_URL"
            _save_env_var(env_name, body["base_url"])
            entry["llm"]["base_url_env"] = env_name

        cfg.setdefault("agents", []).append(entry)

        # Write config
        try:
            from core.config_manager import safe_write_yaml
            safe_write_yaml(config_path, cfg,
                            reason=f"create agent {agent_id}")
        except ImportError:
            os.makedirs("config", exist_ok=True)
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, allow_unicode=True,
                          default_flow_style=False, sort_keys=False)

        # Create supporting files
        override_dir = os.path.join("skills", "agent_overrides")
        os.makedirs(override_dir, exist_ok=True)
        override_path = os.path.join(override_dir, f"{agent_id}.md")
        if not os.path.exists(override_path):
            with open(override_path, "w") as f:
                f.write(f"# {agent_id} — Skill Overrides\n\n"
                        f"<!-- Add agent-specific instructions here -->\n")

        # Soul.md — OpenClaw-style personality file
        agent_doc_dir = os.path.join("docs", agent_id)
        os.makedirs(agent_doc_dir, exist_ok=True)
        soul_path = os.path.join(agent_doc_dir, "soul.md")
        if not os.path.exists(soul_path):
            with open(soul_path, "w") as f:
                f.write(f"# {agent_id}\n\n"
                        f"## Identity\n{role}\n\n"
                        f"## Style\n<!-- Communication approach -->\n\n"
                        f"## Values\n<!-- Priorities -->\n\n"
                        f"## Boundaries\n<!-- Limits -->\n")

        # Skills agent directory — soul.md, TOOLS.md, HEARTBEAT.md
        cap_name = agent_id.capitalize()
        skills_agent_dir = os.path.join("skills", "agents", agent_id)
        os.makedirs(skills_agent_dir, exist_ok=True)

        skills_soul = os.path.join(skills_agent_dir, "soul.md")
        if not os.path.exists(skills_soul):
            with open(skills_soul, "w") as f:
                f.write(
                    f"# Soul — {cap_name}\n\n"
                    f"## 1. Identity\n\n{role}\n\n"
                    f"## 2. Responsibilities\n\n"
                    f"<!-- Define what this agent does -->\n\n"
                    f"## 3. Standing Rules\n\n"
                    f"1. Reply to the user in Chinese\n"
                    f"2. Follow the multi-agent protocol\n")

        tool_profile = tools_cfg.get("profile", "coding") \
            if isinstance(tools_cfg, dict) else "coding"
        tools_md_path = os.path.join(skills_agent_dir, "TOOLS.md")
        if not os.path.exists(tools_md_path):
            with open(tools_md_path, "w") as f:
                f.write(
                    f"# TOOLS.md — {cap_name}\n\n"
                    f"## Tool Profile: {tool_profile}\n\n"
                    f"See global tool definitions for available tools.\n")

        hb_md_path = os.path.join(skills_agent_dir, "HEARTBEAT.md")
        if not os.path.exists(hb_md_path):
            with open(hb_md_path, "w") as f:
                f.write(
                    f"# HEARTBEAT.md — {cap_name}\n\n"
                    f"## Background Checks\n\n"
                    f"Every task start, {cap_name} should verify:\n\n"
                    f"### System Health\n"
                    f"- Tools available and responsive\n"
                    f"- Memory system accessible\n\n"
                    f"### Task Pipeline\n"
                    f"- Pending tasks count\n"
                    f"- Active session status\n")

        # Regenerate team skill
        try:
            from core.team_skill import generate_team_skill
            generate_team_skill()
        except Exception:
            pass

        # Signal hot-reload: write a marker so the persistent pool
        # detects the config change and restarts with the new agent.
        try:
            signal_path = ".agent_reload_signal"
            with open(signal_path, "w") as f:
                import json as _json
                _json.dump({
                    "action": "create",
                    "agent_id": agent_id,
                    "ts": time.time(),
                }, f)
        except OSError:
            pass

        self._json_response(201, {
            "ok": True,
            "agent_id": agent_id,
            "team": [a["id"] for a in cfg["agents"]],
            "hot_reload": True,
        })

    def _handle_delete_agent(self, agent_id: str):
        """Delete an agent via DELETE /v1/agents/:id."""
        import yaml
        if not self._validate_name(agent_id):
            return

        config_path = "config/agents.yaml"
        if not os.path.exists(config_path):
            self._json_response(404, {"error": "Config not found"})
            return

        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}

        agents = cfg.get("agents", [])
        original_count = len(agents)
        cfg["agents"] = [a for a in agents if a.get("id") != agent_id]

        if len(cfg["agents"]) == original_count:
            self._json_response(404, {
                "error": f"Agent '{agent_id}' not found"})
            return

        # Don't allow deleting the last agent
        if len(cfg["agents"]) == 0:
            self._json_response(400, {
                "error": "Cannot delete last agent. At least one required."})
            cfg["agents"] = agents  # restore
            return

        # Write config
        try:
            from core.config_manager import safe_write_yaml
            safe_write_yaml(config_path, cfg,
                            reason=f"delete agent {agent_id}")
        except ImportError:
            with open(config_path, "w") as f:
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
            "deleted": agent_id,
            "remaining": [a["id"] for a in cfg["agents"]],
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
    #  Cleo Files (AGENTS.md, USER.md, TOOLS.md, HEARTBEAT.md, MEMORY.md etc.)
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_list_cleo_files(self):
        """List all Cleo config/template files grouped by scope."""
        import yaml
        result = {"global": [], "agents": {}}

        # Global files in docs/shared/
        shared_dir = os.path.join("docs", "shared")
        if os.path.isdir(shared_dir):
            for f in sorted(os.listdir(shared_dir)):
                fp = os.path.join(shared_dir, f)
                if os.path.isfile(fp):
                    result["global"].append({
                        "name": f,
                        "scope": "global",
                        "path": fp,
                        "size": os.path.getsize(fp),
                    })

        # Per-agent files: scan skills/agents/{id}/ and memory/agents/{id}/
        config_path = os.path.join("config", "agents.yaml")
        agent_ids = []
        try:
            with open(config_path, "r") as fh:
                cfg = yaml.safe_load(fh) or {}
            agent_ids = [a["id"] for a in cfg.get("agents", []) if "id" in a]
        except Exception:
            # Fallback: scan directories
            skills_agents = os.path.join("skills", "agents")
            if os.path.isdir(skills_agents):
                agent_ids = [d for d in os.listdir(skills_agents)
                             if os.path.isdir(os.path.join(skills_agents, d))]

        for aid in sorted(set(agent_ids)):
            files = []
            seen = set()
            # Skills agent dir (soul.md, TOOLS.md, HEARTBEAT.md)
            sa_dir = os.path.join("skills", "agents", aid)
            if os.path.isdir(sa_dir):
                for f in sorted(os.listdir(sa_dir)):
                    fp = os.path.join(sa_dir, f)
                    if os.path.isfile(fp):
                        files.append({
                            "name": f,
                            "scope": f"agent-{aid}",
                            "path": fp,
                            "size": os.path.getsize(fp),
                            "agent_id": aid,
                        })
                        seen.add(f)
            # Memory agent dir (MEMORY.md, short_term.jsonl)
            ma_dir = os.path.join("memory", "agents", aid)
            if os.path.isdir(ma_dir):
                for f in sorted(os.listdir(ma_dir)):
                    fp = os.path.join(ma_dir, f)
                    if os.path.isfile(fp) and f not in seen:
                        files.append({
                            "name": f,
                            "scope": f"agent-{aid}",
                            "path": fp,
                            "size": os.path.getsize(fp),
                            "agent_id": aid,
                        })
            result["agents"][aid] = files

        self._json_response(200, result)

    def _handle_get_cleo_file(self, scope: str, filename: str):
        """Read a Cleo config file by scope and filename."""
        # Basic validation (allow dots for file extensions)
        if ".." in filename or "/" in filename or "\\" in filename:
            self._json_response(400, {"error": f"Invalid filename: {filename}"})
            return

        path = self._resolve_cleo_path(scope, filename)
        if not path:
            self._json_response(404, {"error": f"File not found: {scope}/{filename}"})
            return

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            self._json_response(200, {
                "name": filename,
                "scope": scope,
                "content": content,
                "size": len(content),
                "path": path,
            })
        except OSError as e:
            self._json_response(500, {"error": str(e)})

    def _handle_update_cleo_file(self, scope: str, filename: str):
        """Update a Cleo config file."""
        if ".." in filename or "/" in filename or "\\" in filename:
            self._json_response(400, {"error": f"Invalid filename: {filename}"})
            return

        body = self._read_body()
        content = body.get("content", "")
        if content is None:
            self._json_response(400, {"error": "Missing 'content'"})
            return

        path = self._resolve_cleo_path(scope, filename)
        if not path:
            # Try to create in the default location
            if scope == "global":
                path = os.path.join("docs", "shared", filename)
            elif scope.startswith("agent-"):
                aid = scope[len("agent-"):]
                path = os.path.join("skills", "agents", aid, filename)
            else:
                self._json_response(400, {"error": f"Unknown scope: {scope}"})
                return

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._json_response(200, {
                "ok": True,
                "path": path,
                "size": len(content),
            })
        except OSError as e:
            self._json_response(500, {"error": str(e)})

    def _resolve_cleo_path(self, scope: str, filename: str) -> str | None:
        """Resolve scope + filename to an actual file path."""
        if scope == "global":
            p = os.path.join("docs", "shared", filename)
            return p if os.path.exists(p) else None
        elif scope.startswith("agent-"):
            aid = scope[len("agent-"):]
            # Check skills dir first, then memory dir
            for base in [
                os.path.join("skills", "agents", aid),
                os.path.join("memory", "agents", aid),
            ]:
                p = os.path.join(base, filename)
                if os.path.exists(p):
                    return p
            return None
        return None

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
    #  EXEC HANDLERS
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_exec(self):
        """POST /v1/exec — execute a shell command (approval-gated).

        Body: {command, agent_id?, timeout?, cwd?, force?}
        """
        from core.exec_tool import execute
        body = self._read_body()
        if not body:
            self._json_response(400, {"error": "Empty body"})
            return
        command = body.get("command", "").strip()
        if not command:
            self._json_response(400, {"error": "Missing 'command'"})
            return
        result = execute(
            command=command,
            agent_id=body.get("agent_id", "api"),
            timeout=min(int(body.get("timeout", 300)), 600),
            cwd=body.get("cwd"),
            force=body.get("force", False),
        )
        code = 200 if result["ok"] else (403 if result.get("blocked") else 500)
        self._json_response(code, result)

    def _handle_exec_approvals(self):
        """GET /v1/exec/approvals — list exec approval patterns."""
        from core.exec_tool import list_approved_patterns
        self._json_response(200, list_approved_patterns())

    def _handle_exec_add_approval(self):
        """POST /v1/exec/approve — add a pattern to the exec allowlist.

        Body: {pattern}
        """
        from core.exec_tool import add_approval
        body = self._read_body()
        pattern = body.get("pattern", "").strip()
        if not pattern:
            self._json_response(400, {"error": "Missing 'pattern'"})
            return
        add_approval(pattern)
        self._json_response(200, {"ok": True, "pattern": pattern})

    # ══════════════════════════════════════════════════════════════════════════
    #  CHANNEL HANDLERS
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_channels(self):
        """GET /v1/channels — channel adapter status."""
        global _channel_manager
        if _channel_manager:
            statuses = _channel_manager.get_status()
        else:
            # Return known channels with config info even if manager not running
            statuses = []
            try:
                import yaml as _yaml
                with open("config/agents.yaml", "r") as _f:
                    cfg = _yaml.safe_load(_f) or {}
                ch_cfg = cfg.get("channels", {})
                known = ["telegram", "discord", "feishu", "slack"]
                _token_env_map = {
                    "telegram": ["bot_token_env"],
                    "discord": ["bot_token_env"],
                    "feishu": ["app_id_env", "app_secret_env"],
                    "slack": ["bot_token_env", "app_token_env"],
                }
                for name in known:
                    c = ch_cfg.get(name, {})
                    # Check if tokens are set in os.environ
                    env_keys = _token_env_map.get(name, [])
                    tok_ok = all(
                        bool(os.environ.get(c.get(k, ""), ""))
                        for k in env_keys
                    ) if env_keys else False
                    statuses.append({
                        "channel": name,
                        "enabled": c.get("enabled", False),
                        "running": False,
                        "token_configured": tok_ok,
                        "mention_required": c.get("mention_required", True),
                        "config": {k: v for k, v in c.items()
                                   if k != "enabled"},
                    })
            except Exception:
                pass
        self._json_response(200, {
            "channels": statuses,
            "total": len(statuses),
            "manager_running": _channel_manager is not None,
        })

    def _handle_update_channel(self, channel_name: str):
        """PUT /v1/channels/:name — update channel config (enable/disable, tokens)."""
        import yaml
        from core.config_manager import safe_write_yaml

        body = self._read_body()
        if not body:
            self._json_response(400, {"error": "Empty request body"})
            return

        valid_channels = {"telegram", "discord", "feishu", "slack"}
        if channel_name not in valid_channels:
            self._json_response(400, {
                "error": f"Unknown channel: {channel_name}",
                "valid": sorted(valid_channels),
            })
            return

        # Load current config (absolute path for reliability)
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(_root, "config", "agents.yaml")
        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f) or {}
        except Exception as e:
            self._json_response(500, {"error": f"Failed to load config: {e}"})
            return

        channels = config.setdefault("channels", {})
        ch_cfg = channels.setdefault(channel_name, {})

        # Update fields
        if "enabled" in body:
            ch_cfg["enabled"] = bool(body["enabled"])

        if "mention_required" in body:
            ch_cfg["mention_required"] = bool(body["mention_required"])

        if "allowed_channels" in body:
            ch_cfg["allowed_channels"] = body["allowed_channels"]

        if "allowed_users" in body:
            ch_cfg["allowed_users"] = body["allowed_users"]

        # Handle token values — save to .env, not YAML
        token_map = {
            "telegram": {"bot_token": "TELEGRAM_BOT_TOKEN"},
            "discord": {"bot_token": "DISCORD_BOT_TOKEN"},
            "feishu": {"app_id": "FEISHU_APP_ID", "app_secret": "FEISHU_APP_SECRET"},
            "slack": {"bot_token": "SLACK_BOT_TOKEN", "app_token": "SLACK_APP_TOKEN"},
        }

        env_keys = token_map.get(channel_name, {})
        for field_name, default_env_key in env_keys.items():
            value = body.get(field_name)
            if value:
                env_key = ch_cfg.get(f"{field_name}_env", default_env_key)
                _save_env_var(env_key, value)
                # Ensure YAML references the env var name (for adapter lookup)
                ch_cfg[f"{field_name}_env"] = env_key

        # Write updated config
        try:
            safe_write_yaml(config_path, config, f"update channel {channel_name}")
        except Exception as e:
            self._json_response(500, {"error": f"Failed to save config: {e}"})
            return

        # Auto-reload channel manager so changes take effect immediately
        reload_msg = ""
        global _channel_manager
        if _channel_manager and _channel_manager._loop:
            try:
                import asyncio
                future = asyncio.run_coroutine_threadsafe(
                    _channel_manager.reload(), _channel_manager._loop)
                future.result(timeout=15)
                reload_msg = " Channels reloaded."
            except Exception as e:
                logger.warning("Channel auto-reload failed: %s", e)
                reload_msg = " Auto-reload failed, restart gateway to apply."
        else:
            reload_msg = " Restart gateway to apply."

        self._json_response(200, {
            "ok": True,
            "channel": channel_name,
            "config": ch_cfg,
            "message": f"Channel '{channel_name}' updated.{reload_msg}",
        })

    def _handle_test_channel(self, channel_name: str):
        """POST /v1/channels/:name/test — verify channel token by calling its API."""
        import os
        valid_channels = {"telegram", "discord", "feishu", "slack"}
        if channel_name not in valid_channels:
            self._json_response(400, {"error": f"Unknown channel: {channel_name}"})
            return

        body = self._read_body()
        token = body.get("token", "")

        # If no token provided, try loading from env
        if not token:
            env_map = {
                "telegram": "TELEGRAM_BOT_TOKEN",
                "discord": "DISCORD_BOT_TOKEN",
                "slack": "SLACK_BOT_TOKEN",
                "feishu": "FEISHU_APP_ID",
            }
            token = os.environ.get(env_map.get(channel_name, ""), "")

        if not token:
            self._json_response(200, {
                "ok": False,
                "error": "No token configured",
                "channel": channel_name,
            })
            return

        # Verify token by calling the channel's API
        import urllib.request
        import urllib.error

        try:
            if channel_name == "telegram":
                url = f"https://api.telegram.org/bot{token}/getMe"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    import json as _json
                    data = _json.loads(resp.read())
                    if data.get("ok"):
                        bot_info = data.get("result", {})
                        self._json_response(200, {
                            "ok": True,
                            "channel": channel_name,
                            "bot_name": bot_info.get("first_name", ""),
                            "bot_username": bot_info.get("username", ""),
                            "message": f"Connected as @{bot_info.get('username', '?')}",
                        })
                    else:
                        self._json_response(200, {
                            "ok": False,
                            "error": data.get("description", "Unknown error"),
                            "channel": channel_name,
                        })

            elif channel_name == "discord":
                url = "https://discord.com/api/v10/users/@me"
                req = urllib.request.Request(url, headers={
                    "Authorization": f"Bot {token}",
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    import json as _json
                    data = _json.loads(resp.read())
                    self._json_response(200, {
                        "ok": True,
                        "channel": channel_name,
                        "bot_name": data.get("username", ""),
                        "message": f"Connected as {data.get('username', '?')}#{data.get('discriminator', '')}",
                    })

            elif channel_name == "slack":
                url = "https://slack.com/api/auth.test"
                req = urllib.request.Request(url, headers={
                    "Authorization": f"Bearer {token}",
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    import json as _json
                    data = _json.loads(resp.read())
                    if data.get("ok"):
                        self._json_response(200, {
                            "ok": True,
                            "channel": channel_name,
                            "bot_name": data.get("bot_id", ""),
                            "team": data.get("team", ""),
                            "message": f"Connected to {data.get('team', '?')}",
                        })
                    else:
                        self._json_response(200, {
                            "ok": False,
                            "error": data.get("error", "Unknown error"),
                            "channel": channel_name,
                        })

            elif channel_name == "feishu":
                # Feishu requires app_id + app_secret; test with tenant access token
                app_secret = body.get("app_secret", "") or os.environ.get("FEISHU_APP_SECRET", "")
                if not app_secret:
                    self._json_response(200, {
                        "ok": False,
                        "error": "App secret not configured",
                        "channel": channel_name,
                    })
                    return
                import json as _json
                payload = _json.dumps({
                    "app_id": token,
                    "app_secret": app_secret,
                }).encode()
                req = urllib.request.Request(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    data=payload,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = _json.loads(resp.read())
                    if data.get("code") == 0:
                        self._json_response(200, {
                            "ok": True,
                            "channel": channel_name,
                            "message": "Feishu app credentials verified",
                        })
                    else:
                        self._json_response(200, {
                            "ok": False,
                            "error": data.get("msg", "Unknown error"),
                            "channel": channel_name,
                        })

        except urllib.error.HTTPError as e:
            self._json_response(200, {
                "ok": False,
                "error": f"HTTP {e.code}: {e.reason}",
                "channel": channel_name,
            })
        except Exception as e:
            self._json_response(200, {
                "ok": False,
                "error": str(e),
                "channel": channel_name,
            })

    def _handle_send_file_proxy(self):
        """POST /v1/send_file — proxy file send from agent subprocess to channel manager.

        Agent processes cannot access _channel_manager directly (process isolation),
        so they POST here and the gateway (which owns the ChannelManager) relays it.
        """
        body = self._read_body()
        session_id = body.get("session_id", "")
        file_path = body.get("file_path", "")
        caption = body.get("caption", "")

        if not session_id or not file_path:
            self._json_response(400, {"error": "session_id and file_path required"})
            return
        if not os.path.isfile(file_path):
            self._json_response(400, {"error": f"File not found: {file_path}"})
            return

        global _channel_manager
        if not _channel_manager or not _channel_manager._loop:
            self._json_response(503, {"error": "Channel manager not running"})
            return

        import asyncio as _asyncio
        future = _asyncio.run_coroutine_threadsafe(
            _channel_manager.send_file(session_id, file_path, caption),
            _channel_manager._loop,
        )
        try:
            msg_id = future.result(timeout=30)
            if msg_id:
                self._json_response(200, {"ok": True, "message_id": msg_id})
            else:
                self._json_response(500, {
                    "ok": False,
                    "error": "Adapter returned empty msg_id — file may not have been delivered"})
        except Exception as e:
            logger.error("send_file proxy failed: %s", e)
            self._json_response(500, {"ok": False, "error": str(e)})

    def _handle_send_message_proxy(self):
        """POST /v1/send_message — proxy text message to channel manager.

        Agent processes cannot access _channel_manager directly (process
        isolation), so they POST here and the gateway relays to ChannelManager.
        """
        body = self._read_body()
        session_id = body.get("session_id", "")
        text = body.get("text", "")
        reply_to = body.get("reply_to", "")

        if not session_id or not text:
            self._json_response(400, {"error": "session_id and text required"})
            return

        global _channel_manager
        if not _channel_manager or not _channel_manager._loop:
            self._json_response(503, {"error": "Channel manager not running"})
            return

        import asyncio as _asyncio
        future = _asyncio.run_coroutine_threadsafe(
            _channel_manager.send_message(session_id, text, reply_to),
            _channel_manager._loop,
        )
        try:
            msg_id = future.result(timeout=30)
            if msg_id:
                self._json_response(200, {"ok": True, "message_id": msg_id})
            else:
                self._json_response(500, {
                    "ok": False,
                    "error": "Adapter returned empty msg_id — message may not have been delivered"})
        except Exception as e:
            logger.error("send_message proxy failed: %s", e)
            self._json_response(500, {"ok": False, "error": str(e)})

    def _handle_reload_channels(self):
        """POST /v1/channels/reload — hot-reload all channel adapters."""
        global _channel_manager
        if not _channel_manager or not _channel_manager._loop:
            self._json_response(503, {
                "error": "Channel manager not running. Restart gateway.",
            })
            return
        try:
            import asyncio
            future = asyncio.run_coroutine_threadsafe(
                _channel_manager.reload(), _channel_manager._loop)
            future.result(timeout=15)
            # Return updated status
            statuses = _channel_manager.get_status()
            running = [s for s in statuses if s.get("running")]
            self._json_response(200, {
                "ok": True,
                "message": f"Channels reloaded. {len(running)} adapter(s) running.",
                "channels": statuses,
            })
        except Exception as e:
            logger.exception("Channel reload failed: %s", e)
            self._json_response(500, {"error": f"Reload failed: {e}"})

    def _handle_webhook_inbound(self, source: str):
        """POST /v1/webhook/{source} — receive external webhook events.

        External services (GitHub, Jira, Notion, etc.) push events here.
        The gateway parses the payload and creates a task for the orchestrator.
        Supports HMAC-SHA256 signature verification.
        """
        # Read raw body for signature verification
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length else b""

        # Verify signature if configured
        signature = (self.headers.get("X-Hub-Signature-256", "") or
                     self.headers.get("X-Signature", ""))
        if not _verify_webhook_signature(source, raw_body, signature):
            self._json_response(401, {"error": "Invalid webhook signature"})
            return

        # Parse JSON body
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            body = {"raw": raw_body.decode("utf-8", errors="replace")[:2000]}

        # Build task description from webhook
        event_type = (self.headers.get("X-GitHub-Event", "") or
                      self.headers.get("X-Event-Type", "") or
                      body.get("event", "") or
                      "unknown")

        task_desc = (
            f"[Webhook: {source}] Event: {event_type}\n\n"
            f"Payload summary:\n"
            f"```json\n{json.dumps(body, indent=2, ensure_ascii=False, default=str)[:3000]}\n```\n\n"
            f"Process this webhook event and take appropriate action."
        )

        # Submit as task
        try:
            from core.task_board import TaskBoard
            board = TaskBoard()
            task = board.create(task_desc, required_role="planner")
            logger.info("[webhook] %s event from %s → task %s",
                        event_type, source, task.task_id)
            self._json_response(200, {
                "ok": True,
                "task_id": task.task_id,
                "source": source,
                "event": event_type,
            })
        except Exception as e:
            logger.error("Webhook task creation failed: %s", e)
            self._json_response(500, {"error": str(e)})

    # ══════════════════════════════════════════════════════════════════════════
    #  CRON HANDLERS
    # ══════════════════════════════════════════════════════════════════════════

    def _handle_cron_list(self):
        """GET /v1/cron — list all scheduled jobs."""
        from core.cron import list_jobs
        jobs = list_jobs()
        self._json_response(200, {"jobs": jobs, "total": len(jobs)})

    def _handle_cron_add(self):
        """POST /v1/cron — create a scheduled job.

        Body: {name, action, payload, schedule_type, schedule, agent_id?, enabled?}
        """
        from core.cron import add_job
        body = self._read_body()
        if not body:
            self._json_response(400, {"error": "Empty body"})
            return

        name = body.get("name", "").strip()
        action = body.get("action", "").strip()
        payload = body.get("payload", "").strip()
        schedule_type = body.get("schedule_type", "").strip()
        schedule = body.get("schedule", "").strip()

        if not all([name, action, payload, schedule_type, schedule]):
            self._json_response(400, {
                "error": "Required: name, action, payload, schedule_type, schedule"})
            return
        if action not in ("task", "exec", "webhook"):
            self._json_response(400, {
                "error": "action must be: task, exec, or webhook"})
            return
        if schedule_type not in ("once", "interval", "cron"):
            self._json_response(400, {
                "error": "schedule_type must be: once, interval, or cron"})
            return

        job = add_job(
            name=name, action=action, payload=payload,
            schedule_type=schedule_type, schedule=schedule,
            agent_id=body.get("agent_id", ""),
            enabled=body.get("enabled", True))
        self._json_response(201, {"ok": True, "job": job})

    def _handle_cron_remove(self, job_id: str):
        """DELETE /v1/cron/:id — remove a scheduled job."""
        from core.cron import remove_job
        if remove_job(job_id):
            self._json_response(200, {"ok": True, "deleted": job_id})
        else:
            self._json_response(404, {"error": f"Job '{job_id}' not found"})

    def _handle_cron_run(self, job_id: str):
        """POST /v1/cron/:id/run — manually trigger a job now."""
        from core.cron import get_job, _execute_job, _load_jobs, _save_jobs
        job = get_job(job_id)
        if not job:
            self._json_response(404, {"error": f"Job '{job_id}' not found"})
            return
        ok, msg = _execute_job(job)
        # Update run tracking
        from datetime import datetime, timezone as tz
        jobs = _load_jobs()
        for j in jobs:
            if j["id"] == job_id:
                j["last_run"] = datetime.now(tz.utc).isoformat()
                j["run_count"] = j.get("run_count", 0) + 1
                j["last_error"] = None if ok else msg
                break
        _save_jobs(jobs)
        self._json_response(200, {"ok": ok, "message": msg})

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

        # Task board (mtime-guarded to avoid redundant json.load)
        try:
            global _sse_tb_cache, _sse_tb_mtime
            tb_path = ".task_board.json"
            try:
                mtime = os.path.getmtime(tb_path)
            except OSError:
                mtime = 0
            if mtime != _sse_tb_mtime:
                if mtime > 0:
                    with open(tb_path) as f:
                        _sse_tb_cache = json.load(f)
                else:
                    _sse_tb_cache = {}
                _sse_tb_mtime = mtime
            if _sse_tb_cache:
                tasks = _sse_tb_cache
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
                    # Streaming: partial result (last 200 chars)
                    pr = t.get("partial_result", "")
                    if pr:
                        compact_tasks[tid]["pr"] = pr[-200:]
                    # Task cost from usage tracker
                    cost = t.get("cost_usd")
                    if cost is not None:
                        compact_tasks[tid]["cost"] = round(cost, 4)
                    # Parent ID for subtask tree
                    pid = t.get("parent_id")
                    if pid:
                        compact_tasks[tid]["pid"] = pid
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

    # ── Memory Write / Export Handlers ──

    def _handle_update_case(self, agent_id: str, case_hash: str):
        """Update a case file for an agent."""
        if not self._validate_name(agent_id):
            return
        if not self._validate_name(case_hash):
            return
        body = self._read_body()
        if not body:
            self._json_response(400, {"error": "Empty body"})
            return
        case_path = os.path.join("memory", "agents", agent_id,
                                 "cases", f"{case_hash}.json")
        try:
            if not os.path.exists(case_path):
                self._json_response(404, {"error": f"Case {case_hash} not found"})
                return
            with open(case_path, "r", encoding="utf-8") as f:
                case_data = json.load(f)
            # Merge updates (allow updating solution, tags, notes)
            for key in ("solution", "tags", "notes", "context"):
                if key in body:
                    case_data[key] = body[key]
            with open(case_path, "w", encoding="utf-8") as f:
                json.dump(case_data, f, ensure_ascii=False, indent=2)
            self._json_response(200, {"ok": True, "case": case_data})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_update_episode(self, agent_id: str, task_id: str):
        """Update an episode file for an agent."""
        if not self._validate_name(agent_id):
            return
        body = self._read_body()
        if not body:
            self._json_response(400, {"error": "Empty body"})
            return
        # Episodes are stored by date: memory/agents/{id}/episodes/{date}/{task_id}.json
        import glob as _glob
        pattern = os.path.join("memory", "agents", agent_id,
                               "episodes", "*", f"{task_id}.json")
        matches = _glob.glob(pattern)
        if not matches:
            self._json_response(404, {"error": f"Episode {task_id} not found"})
            return
        ep_path = matches[0]
        try:
            with open(ep_path, "r", encoding="utf-8") as f:
                ep_data = json.load(f)
            for key in ("notes", "tags", "outcome"):
                if key in body:
                    ep_data[key] = body[key]
            with open(ep_path, "w", encoding="utf-8") as f:
                json.dump(ep_data, f, ensure_ascii=False, indent=2)
            self._json_response(200, {"ok": True, "episode": ep_data})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_memory_export(self, agent_id: str):
        """Export all memory for an agent as JSON."""
        if not self._validate_name(agent_id):
            return
        base = os.path.join("memory", "agents", agent_id)
        if not os.path.isdir(base):
            self._json_response(404, {"error": f"No memory for agent {agent_id}"})
            return
        try:
            export = {"agent_id": agent_id, "exported_at": time.time()}

            # Cases
            cases_dir = os.path.join(base, "cases")
            cases = []
            if os.path.isdir(cases_dir):
                for fname in sorted(os.listdir(cases_dir)):
                    if fname.endswith(".json"):
                        with open(os.path.join(cases_dir, fname),
                                  encoding="utf-8") as f:
                            cases.append(json.load(f))
            export["cases"] = cases

            # Episodes (all dates)
            episodes_dir = os.path.join(base, "episodes")
            episodes = []
            if os.path.isdir(episodes_dir):
                for date_dir in sorted(os.listdir(episodes_dir)):
                    date_path = os.path.join(episodes_dir, date_dir)
                    if os.path.isdir(date_path):
                        for fname in sorted(os.listdir(date_path)):
                            if fname.endswith(".json"):
                                with open(os.path.join(date_path, fname),
                                          encoding="utf-8") as f:
                                    episodes.append(json.load(f))
            export["episodes"] = episodes

            # Daily logs
            daily_dir = os.path.join(base, "daily")
            daily_logs = []
            if os.path.isdir(daily_dir):
                for fname in sorted(os.listdir(daily_dir)):
                    if fname.endswith(".md"):
                        with open(os.path.join(daily_dir, fname),
                                  encoding="utf-8") as f:
                            daily_logs.append({
                                "date": fname.replace(".md", ""),
                                "content": f.read(),
                            })
            export["daily_logs"] = daily_logs

            # MEMORY.md
            mem_md = os.path.join(base, "MEMORY.md")
            if os.path.exists(mem_md):
                with open(mem_md, encoding="utf-8") as f:
                    export["memory_md"] = f.read()

            # Patterns
            patterns_dir = os.path.join(base, "patterns")
            patterns = []
            if os.path.isdir(patterns_dir):
                for fname in sorted(os.listdir(patterns_dir)):
                    if fname.endswith(".json"):
                        with open(os.path.join(patterns_dir, fname),
                                  encoding="utf-8") as f:
                            patterns.append(json.load(f))
            export["patterns"] = patterns

            # Score log (global, for alic)
            if agent_id == "alic":
                score_log = os.path.join("memory", "score_log.jsonl")
                if os.path.exists(score_log):
                    scores = []
                    with open(score_log, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    scores.append(json.loads(line))
                                except json.JSONDecodeError:
                                    continue
                    export["score_log"] = scores

            self._json_response(200, export)
        except Exception as e:
            self._json_response(500, {"error": str(e)})


# ══════════════════════════════════════════════════════════════════════════════
#  .env FILE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _save_env_var(key: str, value: str, env_path: str = ""):
    """
    Save or update a KEY=VALUE pair in the .env file.
    Also sets it in the current process's os.environ immediately.
    Uses absolute path (project root) by default for reliability.
    """
    if not env_path:
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(_root, ".env")

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
    logger.debug("Saved env var %s to %s", key, env_path)


# ══════════════════════════════════════════════════════════════════════════════
#  SERVER LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

def generate_token() -> str:
    """Generate a secure random token."""
    return f"cleo-{secrets.token_urlsafe(24)}"


_WEBHOOK_HMAC_SECRETS: dict[str, str] = {}  # source → HMAC secret


def _verify_webhook_signature(source: str, payload: bytes,
                              signature: str) -> bool:
    """Verify HMAC-SHA256 signature for webhook payloads."""
    secret = _WEBHOOK_HMAC_SECRETS.get(source)
    if not secret:
        return True  # No secret configured → accept all
    import hmac, hashlib
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    # Support "sha256=xxx" prefix (GitHub style)
    if signature.startswith("sha256="):
        signature = signature[7:]
    return hmac.compare_digest(expected, signature)


_FILE_DELIVERY_DIR = ".file_delivery"
_FILE_DELIVERY_DEAD = ".file_delivery/dead"
_FILE_DELIVERY_MAX_RETRIES = 3


def _start_file_delivery_consumer():
    """Start a background thread that polls .file_delivery/ every 5 seconds.

    Picks up queued file delivery JSONs (written by agent tools when the
    HTTP proxy is unreachable), sends them via ChannelManager, and cleans up.
    Failed deliveries are retried up to 3 times before moving to dead/.
    """
    import asyncio as _asyncio

    def _consumer_loop():
        while True:
            time.sleep(5)
            if not os.path.isdir(_FILE_DELIVERY_DIR):
                continue
            global _channel_manager
            if not _channel_manager or not _channel_manager._loop:
                continue

            try:
                files = sorted(f for f in os.listdir(_FILE_DELIVERY_DIR)
                               if f.endswith(".json"))
            except OSError:
                continue

            for fname in files:
                fpath = os.path.join(_FILE_DELIVERY_DIR, fname)
                try:
                    with open(fpath, "r") as f:
                        delivery = json.load(f)
                except (json.JSONDecodeError, OSError):
                    # Corrupt — remove
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
                    continue

                session_id = delivery.get("session_id", "")
                file_path = delivery.get("file_path", "")
                caption = delivery.get("caption", "")
                text = delivery.get("text", "")
                retry_count = delivery.get("retry_count", 0)

                if not session_id or (not file_path and not text):
                    # Invalid — remove
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
                    continue
                # Skip file entries where file has been deleted
                if file_path and not os.path.isfile(file_path) and not text:
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
                    continue

                # Try sending via channel manager
                try:
                    if file_path and os.path.isfile(file_path):
                        future = _asyncio.run_coroutine_threadsafe(
                            _channel_manager.send_file(session_id, file_path, caption),
                            _channel_manager._loop,
                        )
                    elif text:
                        future = _asyncio.run_coroutine_threadsafe(
                            _channel_manager.send_message(session_id, text),
                            _channel_manager._loop,
                        )
                    else:
                        os.remove(fpath)
                        continue
                    msg_id = future.result(timeout=30)
                    if msg_id:
                        label = os.path.basename(file_path) if file_path else "text msg"
                        logger.info("Delivery consumer: sent %s to %s (msg %s)",
                                    label, session_id, msg_id)
                        os.remove(fpath)
                    else:
                        raise RuntimeError("send_file returned empty msg_id")
                except Exception as e:
                    retry_count += 1
                    if retry_count >= _FILE_DELIVERY_MAX_RETRIES:
                        logger.error("File delivery failed after %d retries: %s → moving to dead/",
                                     retry_count, fname)
                        os.makedirs(_FILE_DELIVERY_DEAD, exist_ok=True)
                        try:
                            os.rename(fpath, os.path.join(_FILE_DELIVERY_DEAD, fname))
                        except OSError:
                            try:
                                os.remove(fpath)
                            except OSError:
                                pass
                    else:
                        logger.warning("File delivery retry %d/%d for %s: %s",
                                       retry_count, _FILE_DELIVERY_MAX_RETRIES, fname, e)
                        delivery["retry_count"] = retry_count
                        try:
                            with open(fpath, "w") as f:
                                json.dump(delivery, f)
                        except OSError:
                            pass

    thread = Thread(target=_consumer_loop, daemon=True, name="file-delivery-consumer")
    thread.start()
    logger.info("File delivery consumer started (polling %s every 5s)", _FILE_DELIVERY_DIR)


def start_gateway(port: int = 0, token: str = "",
                   daemon: bool = True) -> HTTPServer | None:
    """
    Start the gateway HTTP server.
    Returns the server instance (or None on failure).
    """
    global _start_time, _token, _config

    # ── Ensure .env is loaded (idempotent — setdefault won't clobber) ──
    try:
        from core.env_loader import load_dotenv
        load_dotenv()
    except Exception:
        pass

    port = port or int(os.environ.get("CLEO_GATEWAY_PORT",
                        os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT))))
    token = token or os.environ.get("CLEO_GATEWAY_TOKEN",
                      os.environ.get("SWARM_GATEWAY_TOKEN", ""))

    _start_time = time.time()
    _token = token
    _config = {"port": port, "token": token}

    # Persist token so agent subprocesses can read it for HTTP proxy calls
    try:
        with open(".gateway_token", "w") as f:
            f.write(token)
    except OSError:
        pass

    try:
        server = HTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        logger.error("Cannot start gateway on port %d: %s", port, e)
        return None

    # ── Start background services (both daemon and foreground modes) ──

    # Cron scheduler
    try:
        from core.cron import start_scheduler
        start_scheduler(interval=30)
    except Exception as e:
        logger.warning("Cron scheduler failed to start: %s", e)

    # Channel manager (Telegram/Discord/Feishu/Slack)
    # Always start so channels can be enabled later via Dashboard
    global _channel_manager
    try:
        import yaml
        cfg_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "agents.yaml")
        full_config = {}
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                full_config = yaml.safe_load(f) or {}
        from adapters.channels.manager import start_channel_manager
        _channel_manager = start_channel_manager(full_config)
        logger.info("Channel manager started (hot-reload ready)")
    except Exception as e:
        logger.warning("Channel manager failed to start: %s", e)

    # ── Provider Router (cross-provider LLM failover) ──
    try:
        from core.provider_router import build_provider_router
        router = build_provider_router(full_config)
        if router:
            logger.info("Provider router enabled: %s (strategy=%s)",
                        ", ".join(router.provider_names), router.strategy)
    except Exception as e:
        logger.warning("Provider router failed to init: %s", e)

    # ── WebSocket gateway (async, runs in background thread) ──
    try:
        from core.ws_gateway import start_ws_gateway, _HAS_WEBSOCKETS
        if _HAS_WEBSOCKETS:
            import asyncio

            def _run_ws():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                ws_port = port + 1
                loop.run_until_complete(start_ws_gateway(port=ws_port, token=token))
                loop.run_forever()

            ws_thread = Thread(target=_run_ws, daemon=True, name="ws-gateway")
            ws_thread.start()
            logger.info("WebSocket gateway started on ws://0.0.0.0:%d", port + 1)
        else:
            logger.info("WebSocket gateway disabled (websockets not installed)")
    except Exception as e:
        logger.warning("WebSocket gateway failed to start: %s", e)

    # ── File delivery consumer (polls .file_delivery/ for queued sends) ──
    _start_file_delivery_consumer()

    # ── Serve ──

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

    port = port or int(os.environ.get("CLEO_GATEWAY_PORT",
                       os.environ.get("SWARM_GATEWAY_PORT", str(DEFAULT_PORT))))
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

    token = token or os.environ.get("CLEO_GATEWAY_TOKEN",
                      os.environ.get("SWARM_GATEWAY_TOKEN", ""))
    if not token:
        token = generate_token()
        print(f"  Generated token: {token}")

    port = port or int(os.environ.get("CLEO_GATEWAY_PORT",
                        os.environ.get("SWARM_GATEWAY_PORT",
                                       str(DEFAULT_PORT))))
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
    token = os.environ.get("CLEO_GATEWAY_TOKEN",
              os.environ.get("SWARM_GATEWAY_TOKEN", ""))
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
