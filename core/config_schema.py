"""
core/config_schema.py
Config schema validation & migration for agents.yaml.

Validates structure, applies schema version migrations, and provides
human-readable error messages for invalid configs.

Usage:
    from core.config_schema import validate_config, migrate_config
    errors = validate_config("config/agents.yaml")
    migrate_config("config/agents.yaml")
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import yaml

try:
    from core.theme import theme as _theme
except ImportError:
    class _FallbackTheme:
        success = "green"; error = "red"
    _theme = _FallbackTheme()

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2
CONFIG_PATH = "config/agents.yaml"


# ── Schema definitions ─────────────────────────────────────────────────────

REQUIRED_AGENT_FIELDS = {"id", "role", "model"}
OPTIONAL_AGENT_FIELDS = {
    "skills", "fallback_models", "llm", "tools",
    "max_context_tokens", "compaction",
}
VALID_PROVIDERS = {"flock", "openai", "minimax", "ollama"}
VALID_MEMORY_BACKENDS = {"mock", "chroma", "hybrid"}
VALID_STATUSES = {"pending", "claimed", "review", "completed", "failed",
                  "cancelled", "paused"}


def validate_config(path: str = CONFIG_PATH) -> list[str]:
    """Validate agents.yaml structure.

    Returns list of error messages (empty = valid).
    """
    errors: list[str] = []

    if not os.path.exists(path):
        return ["Config file not found: " + path]

    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return [f"YAML parse error: {e}"]

    if not cfg or not isinstance(cfg, dict):
        return ["Config is empty or not a dictionary"]

    # Check agents
    agents = cfg.get("agents")
    if not agents:
        errors.append("No 'agents' section defined")
    elif not isinstance(agents, list):
        errors.append("'agents' must be a list")
    else:
        agent_ids = set()
        for i, agent in enumerate(agents):
            if not isinstance(agent, dict):
                errors.append(f"Agent #{i} is not a dictionary")
                continue

            # Required fields
            for field in REQUIRED_AGENT_FIELDS:
                if field not in agent:
                    errors.append(f"Agent #{i}: missing required field '{field}'")

            # Duplicate ID check
            aid = agent.get("id", "")
            if aid in agent_ids:
                errors.append(f"Duplicate agent ID: '{aid}'")
            agent_ids.add(aid)

            # ID format
            if aid and not aid.replace("_", "").replace("-", "").isalnum():
                errors.append(f"Agent ID '{aid}' contains invalid characters")

            # Model must be a string
            if "model" in agent and not isinstance(agent["model"], str):
                errors.append(f"Agent '{aid}': 'model' must be a string")

            # Skills must be a list
            if "skills" in agent and not isinstance(agent["skills"], list):
                errors.append(f"Agent '{aid}': 'skills' must be a list")

            # Fallback models must be a list
            if "fallback_models" in agent:
                if not isinstance(agent["fallback_models"], list):
                    errors.append(f"Agent '{aid}': 'fallback_models' must be a list")

    # Check LLM section
    llm = cfg.get("llm", {})
    if llm:
        provider = llm.get("provider", "")
        if provider and provider not in VALID_PROVIDERS:
            errors.append(
                f"Unknown provider '{provider}'. "
                f"Valid: {', '.join(sorted(VALID_PROVIDERS))}"
            )

    # Check memory section
    memory = cfg.get("memory", {})
    if memory:
        backend = memory.get("backend", "")
        if backend and backend not in VALID_MEMORY_BACKENDS:
            errors.append(
                f"Unknown memory backend '{backend}'. "
                f"Valid: {', '.join(sorted(VALID_MEMORY_BACKENDS))}"
            )

    # Check resilience section
    resilience = cfg.get("resilience", {})
    if resilience:
        max_retries = resilience.get("max_retries")
        if max_retries is not None and (not isinstance(max_retries, int) or max_retries < 0):
            errors.append("resilience.max_retries must be a non-negative integer")

        cb = resilience.get("circuit_breaker_threshold")
        if cb is not None and (not isinstance(cb, int) or cb < 1):
            errors.append("resilience.circuit_breaker_threshold must be a positive integer")

    return errors


def migrate_config(path: str = CONFIG_PATH) -> tuple[bool, str]:
    """Migrate config to the current schema version.

    Returns (changed, message).
    """
    if not os.path.exists(path):
        return False, "Config not found"

    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return False, f"YAML parse error: {e}"

    version = cfg.get("schema_version", 1)
    changed = False
    migrations = []

    # Migration v1 → v2: add schema_version, reputation defaults, compaction
    if version < 2:
        cfg["schema_version"] = 2

        # Ensure reputation section exists
        if "reputation" not in cfg:
            agents = cfg.get("agents", [])
            reviewer_ids = [a["id"] for a in agents
                           if "review" in a.get("role", "").lower()]
            cfg["reputation"] = {
                "peer_review_agents": reviewer_ids or (
                    [agents[-1]["id"]] if agents else []),
                "evolution": {
                    "prompt_auto_apply": True,
                    "model_swap_require_confirm": True,
                    "role_vote_threshold": 0.6,
                },
            }
            migrations.append("Added reputation config")

        # Ensure all agents have skills
        for agent in cfg.get("agents", []):
            if "skills" not in agent:
                agent["skills"] = ["_base"]
                migrations.append(f"Added default skills to {agent.get('id', '?')}")

        # Ensure max_idle_cycles
        if "max_idle_cycles" not in cfg:
            cfg["max_idle_cycles"] = 30
            migrations.append("Added max_idle_cycles=30")

        changed = True

    if changed:
        # Backup before migration
        try:
            from core.config_manager import save as config_save
            config_save(path, reason="pre-migration backup")
        except ImportError:
            pass

        with open(path, "w") as f:
            f.write("# config/agents.yaml\n\n")
            yaml.dump(cfg, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)

        msg = f"Migrated v{version} → v{CURRENT_SCHEMA_VERSION}"
        if migrations:
            msg += ": " + "; ".join(migrations)
        logger.info("[config_schema] %s", msg)
        return True, msg

    return False, f"Already at schema v{version}"


def check_and_migrate(path: str = CONFIG_PATH,
                      console=None) -> list[str]:
    """Validate + migrate config. Returns list of issues (empty = OK).

    If console is provided, prints status messages.
    """
    # Migrate first
    migrated, migrate_msg = migrate_config(path)
    if migrated and console:
        console.print(f"  [{_theme.success}]+[/{_theme.success}] {migrate_msg}")

    # Then validate
    errors = validate_config(path)
    if errors and console:
        for err in errors:
            console.print(f"  [{_theme.error}]✗[/{_theme.error}] {err}")

    return errors
