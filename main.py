#!/usr/bin/env python3
"""
main.py  —  Agent Stack CLI (slim entry point)

All command implementations live in cli/ modules.
This file only handles:
  1. Project root + env loading
  2. Argparse tree definition
  3. Dispatch to cli.dispatch_command()

Usage:
  cleo                       # interactive chat mode (default)
  cleo onboard               # interactive setup wizard
  cleo run "..."             # one-shot task
  cleo status                # show task board
  cleo scores                # show reputation scores
  cleo doctor                # system health check
  cleo gateway [start]       # start HTTP gateway
  cleo agents create <name>  # create an agent
  cleo workflow list          # list workflows
  cleo workflow run <name>    # run a workflow
  cleo chain status          # on-chain identity
  cleo install               # install from GitHub
  cleo uninstall             # remove CLI & daemon
  cleo update                # pull latest from GitHub
  cleo config get/set/unset  # manage configuration

Chat commands:
  /configure  — re-run onboarding wizard
  /config     — show current agent team
  /status     — task board
  /scores     — reputation scores
  /gateway    — gateway status & control
  /doctor     — system health check
  /clear      — clear task history
  /help       — show commands
  exit        — quit
"""

from __future__ import annotations

# Suppress LibreSSL warning on macOS system Python (cosmetic, not functional)
import warnings
warnings.filterwarnings("ignore", message=".*LibreSSL.*", category=Warning)

import argparse
import os
import sys


def _ensure_project_root():
    """Ensure we're running from the project root (needed for file-based state)."""
    root = os.path.dirname(os.path.abspath(__file__))
    if os.getcwd() != root:
        os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)


_ensure_project_root()

# Load .env before anything else
from core.env_loader import load_dotenv
load_dotenv()


# ── Version helper (lightweight, no heavy deps) ─────────────────────────────

def _get_version() -> str:
    """Read version from pyproject.toml, fallback to '0.1.0'."""
    from cli.helpers import get_version
    return get_version()


# ── Argparse tree ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Build the argparse tree — no command functions imported here."""
    parser = argparse.ArgumentParser(prog="cleo",
                                     description="Multi-agent orchestration CLI")
    parser.add_argument("-V", "--version", action="version",
                        version=f"cleo {_get_version()}")
    parser.add_argument("--json", action="store_true", default=False,
                        help="Output in JSON format (for scripting)")
    sub = parser.add_subparsers(dest="cmd")

    _docs_base = "https://github.com/user/cleo-dev/wiki/cli"

    # ── version (extended info) ──────────────────────────────────────────
    sub.add_parser("version", help="Show extended version info (git, Python, deps)")

    # ── onboard / init / configure ───────────────────────────────────────
    p_onboard = sub.add_parser("onboard", help="Interactive setup wizard",
                               epilog=f"Docs: {_docs_base}#onboard")
    p_onboard.add_argument("--provider", default="",
                           choices=["flock", "openai", "minimax", "ollama", ""],
                           help="LLM provider (non-interactive)")
    p_onboard.add_argument("--api-key", default="",
                           help="API key (non-interactive)")
    p_onboard.add_argument("--model", default="",
                           help="Model name (non-interactive)")
    p_onboard.add_argument("--non-interactive", action="store_true",
                           help="Run without interactive prompts")
    _section_choices = ["", "model", "agents", "skills", "skill_deps",
                        "memory", "resilience", "compaction",
                        "channels", "gateway", "chain", "tools", "health"]
    p_onboard.add_argument("--section", default="", choices=_section_choices,
                           help="Jump to a specific configuration section")
    sub.add_parser("init", help="Interactive setup wizard (alias)")
    p_configure = sub.add_parser("configure", help="Interactive setup wizard (alias)")
    p_configure.add_argument("--section", default="", choices=_section_choices,
                             help="Jump to a specific configuration section")

    # ── config ───────────────────────────────────────────────────────────
    p_cfg = sub.add_parser("config", help="Read/write configuration values")
    p_cfg.add_argument("config_action", metavar="action",
                       choices=["get", "set", "unset"],
                       help="Action: get, set, or unset")
    p_cfg.add_argument("config_path", metavar="path", nargs="?", default="",
                       help="Dot-separated key path (e.g. llm.provider)")
    p_cfg.add_argument("config_value", metavar="value", nargs="?", default="",
                       help="Value to set (for 'set' action)")

    # ── chat ─────────────────────────────────────────────────────────────
    sub.add_parser("chat", help="Interactive chat mode",
                   epilog=f"Docs: {_docs_base}#chat")

    # ── run ──────────────────────────────────────────────────────────────
    p_run = sub.add_parser("run", help="Submit a task and run all agents",
                           epilog=f"Docs: {_docs_base}#run")
    p_run.add_argument("task", help="Task description")

    # ── status / scores ─────────────────────────────────────────────────
    p_status = sub.add_parser("status", help="Show task board",
                              epilog=f"Docs: {_docs_base}#status")
    p_status.add_argument("--agent", "-a", default="",
                          help="Filter by agent ID")
    p_status.add_argument("--status", "-s", dest="status_filter", default="",
                          help="Filter by status (comma-separated)")
    p_status.add_argument("--since", default="",
                          help="Time range (e.g., 1h, 30m, 2d)")
    p_status.add_argument("--search", "-q", default="",
                          help="Search task descriptions")
    sub.add_parser("scores", help="Show reputation scores")

    # ── doctor ───────────────────────────────────────────────────────────
    p_doc = sub.add_parser("doctor", help="System health check",
                           epilog=f"Docs: {_docs_base}#doctor")
    p_doc.add_argument("--repair", action="store_true",
                       help="Auto-fix common issues (missing .env, dirs, stale tasks)")
    p_doc.add_argument("--deep", action="store_true",
                       help="Deep diagnostics (disk, skills, workflows, Python version)")
    p_doc.add_argument("--export", action="store_true",
                       help="Export a pasteable diagnostic report")

    # ── security ──────────────────────────────────────────────────────────
    p_sec = sub.add_parser("security", help="Security tools")
    p_sec.add_argument("action", choices=["audit"], help="Action")
    p_sec.add_argument("--deep", action="store_true",
                       help="Deep audit (file permissions, git history)")
    p_sec.add_argument("--fix", action="store_true",
                       help="Auto-fix security issues")

    # ── export ───────────────────────────────────────────────────────────
    p_export = sub.add_parser("export", help="Export task results")
    p_export.add_argument("task_id", help="Task ID (full or prefix)")
    p_export.add_argument("--format", "-f", choices=["md", "json"], default="md",
                          help="Output format (default: md)")

    # ── cron ─────────────────────────────────────────────────────────────
    p_cron = sub.add_parser("cron", help="Scheduled job management")
    p_cron.add_argument("action", choices=["list", "add", "remove", "run"],
                        help="Cron action")
    p_cron.add_argument("--name", default="", help="Job name (for add)")
    p_cron.add_argument("--action-type", dest="cron_action", default="",
                        choices=["task", "exec", "webhook", ""],
                        help="Job action type (for add)")
    p_cron.add_argument("--payload", default="",
                        help="Task description, shell cmd, or webhook URL")
    p_cron.add_argument("--type", dest="cron_type", default="",
                        choices=["once", "interval", "cron", ""],
                        help="Schedule type: once, interval, or cron")
    p_cron.add_argument("--schedule", default="",
                        help="ISO timestamp, seconds, or cron expression")
    p_cron.add_argument("--id", dest="job_id", default="", help="Job ID")

    # ── gateway ──────────────────────────────────────────────────────────
    p_gw = sub.add_parser("gateway", help="Gateway management")
    p_gw.add_argument("action", nargs="?", default="start",
                      choices=["start", "stop", "restart", "status",
                               "install", "uninstall"],
                      help="Gateway action (default: start)")
    p_gw.add_argument("-p", "--port", type=int, default=0,
                      help="Port (default: 19789 or CLEO_GATEWAY_PORT)")
    p_gw.add_argument("-t", "--token", default="",
                      help="Bearer token (default: auto-generate)")
    p_gw.add_argument("--force", action="store_true",
                      help="Kill existing process on port before starting")

    # ── channels ─────────────────────────────────────────────────────────
    p_ch = sub.add_parser("channels", help="Channel management")
    p_ch.add_argument("action", nargs="?", default="list",
                      choices=["list", "enable", "disable", "status", "test",
                               "pairing"],
                      help="Channel action (default: list)")
    p_ch.add_argument("channel", nargs="?", default=None,
                      help="Channel name, or pairing sub-action (list/generate/approve/revoke)")
    p_ch.add_argument("pairing_arg", nargs="?", default="",
                      help="Pairing argument (channel:user_id for approve/revoke)")

    # ── agents ───────────────────────────────────────────────────────────
    p_agents = sub.add_parser("agents", help="Agent management")
    agents_sub = p_agents.add_subparsers(dest="agents_cmd")
    p_create = agents_sub.add_parser("create", help="Create a new agent")
    p_create.add_argument("name", help="Agent ID/name")
    p_create.add_argument("--template",
                          choices=["researcher", "coder", "debugger", "doc_writer"],
                          default=None, help="Use a built-in role template")
    p_add = agents_sub.add_parser("add", help="Create a new agent (alias for create)")
    p_add.add_argument("name", help="Agent ID/name")
    p_add.add_argument("--template",
                       choices=["researcher", "coder", "debugger", "doc_writer"],
                       default=None, help="Use a built-in role template")

    # ── workflow ─────────────────────────────────────────────────────────
    p_wf = sub.add_parser("workflow", help="Workflow management")
    wf_sub = p_wf.add_subparsers(dest="wf_cmd")
    wf_sub.add_parser("list", help="List available workflows")
    p_wf_run = wf_sub.add_parser("run", help="Run a workflow")
    p_wf_run.add_argument("name", help="Workflow name (e.g., code_review, bug_fix)")
    p_wf_run.add_argument("--input", "-i", default="",
                          help="Task input for the workflow")

    # ── chain ────────────────────────────────────────────────────────────
    p_chain = sub.add_parser("chain", help="On-chain identity management")
    p_chain.add_argument("action",
                         choices=["status", "balance", "init", "register", "health"],
                         help="Chain action")
    p_chain.add_argument("agent_id", nargs="?", default=None,
                         help="Agent ID (required for init/register)")

    # ── install / uninstall / update ─────────────────────────────────────
    p_install = sub.add_parser("install", help="Install cleo from GitHub")
    p_install.add_argument("--repo", default="",
                           help="GitHub repo URL (default: SWARM_REPO env or built-in)")
    p_install.add_argument("--target", default="",
                           help="Install directory (default: ~/cleo-dev)")
    sub.add_parser("uninstall", help="Remove cleo CLI and daemon")
    p_update = sub.add_parser("update", help="Pull latest from GitHub and reinstall")
    p_update.add_argument("--branch", default="",
                          help="Branch to update from (default: current branch)")
    p_update.add_argument("--check", action="store_true",
                          help="Check for updates without pulling (non-destructive)")

    # ── search ───────────────────────────────────────────────────────────
    p_search = sub.add_parser("search", help="Search documents and memory (FTS5)")
    p_search.add_argument("query", nargs="?", help="Search query")
    p_search.add_argument("--collection", "-c", default=None,
                          help="Collection to search (memory/knowledge/workspace/docs)")
    p_search.add_argument("--limit", "-n", type=int, default=10,
                          help="Max results (default: 10)")
    p_search.add_argument("--reindex", action="store_true",
                          help="Rebuild search index from all data sources")

    # ── memory ───────────────────────────────────────────────────────────
    p_mem = sub.add_parser("memory", help="Memory management")
    p_mem.add_argument("action", nargs="?", default="status",
                       choices=["status", "search", "rebuild", "cleanup",
                                "reindex", "graph", "package"],
                       help="Action to perform")
    p_mem.add_argument("query", nargs="?", default=None,
                       help="Search query (for 'search' action)")
    p_mem.add_argument("--agent", default=None,
                       help="Agent ID (for agent-specific operations)")
    p_mem.add_argument("--output", "-o", default=None,
                       help="Output file path (for 'package' action)")
    p_mem.add_argument("--format", dest="fmt", default="json",
                       choices=["json", "dot"],
                       help="Graph export format (for 'graph' action)")

    # ── evolve ───────────────────────────────────────────────────────────
    p_ev = sub.add_parser("evolve", help="Manage evolution actions")
    p_ev.add_argument("agent_id")
    p_ev.add_argument("action", choices=["confirm"])

    # ── logs ─────────────────────────────────────────────────────────────
    p_logs = sub.add_parser("logs", help="View aggregated agent logs")
    p_logs.add_argument("-f", "--follow", action="store_true",
                        help="Follow log output in real-time (tail -f)")
    p_logs.add_argument("--agent", default="",
                        help="Filter by agent ID (e.g., jerry)")
    p_logs.add_argument("--level", default="",
                        choices=["error", "warning", "info", "debug", ""],
                        help="Filter by log level")
    p_logs.add_argument("--since", default="",
                        help="Time range filter (e.g., 1h, 30m, 2d)")
    p_logs.add_argument("-n", "--lines", type=int, default=50,
                        help="Number of lines to show (default: 50)")
    p_logs.add_argument("--export", default="",
                        choices=["json", "jsonl", ""],
                        help="Export logs in structured format (json or jsonl)")

    # ── plugins ──────────────────────────────────────────────────────────
    p_plugins = sub.add_parser("plugins", help="Plugin management")
    plugins_sub = p_plugins.add_subparsers(dest="plugins_cmd")
    plugins_sub.add_parser("list", help="List installed plugins")
    p_pl_install = plugins_sub.add_parser("install", help="Install a plugin")
    p_pl_install.add_argument("source", help="Plugin path or git URL")
    p_pl_remove = plugins_sub.add_parser("remove", help="Remove a plugin")
    p_pl_remove.add_argument("name", help="Plugin name")
    p_pl_enable = plugins_sub.add_parser("enable", help="Enable a plugin")
    p_pl_enable.add_argument("name", help="Plugin name")
    p_pl_disable = plugins_sub.add_parser("disable", help="Disable a plugin")
    p_pl_disable.add_argument("name", help="Plugin name")
    p_pl_info = plugins_sub.add_parser("info", help="Show plugin details")
    p_pl_info.add_argument("name", help="Plugin name")
    p_pl_update = plugins_sub.add_parser("update", help="Update a plugin (git pull)")
    p_pl_update.add_argument("name", help="Plugin name")
    plugins_sub.add_parser("doctor", help="Check plugin health")

    # ── completions ──────────────────────────────────────────────────────
    p_comp = sub.add_parser("completions", help="Generate shell completions")
    p_comp.add_argument("shell", nargs="?", default="",
                        choices=["bash", "zsh", "install", ""],
                        help="Shell type or 'install' to auto-install")

    return parser


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = build_parser()
    args = parser.parse_args()

    # Lazy dispatch — import command modules only when needed
    from cli import dispatch_command
    dispatch_command(args)


if __name__ == "__main__":
    main()
