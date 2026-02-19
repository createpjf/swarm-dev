"""
core/team_skill.py
Auto-generate skills/_team.md from config/agents.yaml.

The team skill is a shared document injected into every agent's prompt,
describing the full team roster — roles, models, capabilities, and
communication guidelines. This lets each agent understand its teammates.

Trigger points:
  - Orchestrator.__init__() on every launch
  - After config save (quick setup, full wizard, sectional modify)
  - Manual via configure → Skills → "Regenerate team skill"
"""

from __future__ import annotations

import logging
import os
import time

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = "config/agents.yaml"
TEAM_SKILL_PATH = "skills/_team.md"


def generate_team_skill(
    config_path: str = CONFIG_PATH,
    output_path: str = TEAM_SKILL_PATH,
) -> str:
    """
    Read agents.yaml and generate skills/_team.md.

    Returns the generated markdown content.
    Writes to output_path.
    """
    if not os.path.exists(config_path):
        logger.debug("No config at %s — skipping team skill generation", config_path)
        return ""

    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    agents = cfg.get("agents", [])
    if not agents:
        return ""

    global_provider = cfg.get("llm", {}).get("provider", "?")
    ts = time.strftime("%Y-%m-%d %H:%M")

    lines = [
        "# Team Roster",
        "",
        f"_Auto-generated from agents.yaml on {ts}_",
        "",
        f"Your team has **{len(agents)} agents**. "
        "Each agent runs as an independent process and communicates "
        "via the shared Context Bus and Mailbox system.",
        "",
    ]

    for i, a in enumerate(agents, 1):
        agent_id = a.get("id", "?")
        role = a.get("role", "").replace("\n", " ").strip()
        model = a.get("model", "?")
        provider = a.get("llm", {}).get("provider", global_provider)
        skills = a.get("skills", [])
        fallbacks = a.get("fallback_models", [])
        autonomy = a.get("autonomy_level", 1)

        lines.append(f"## {i}. {agent_id}")
        lines.append(f"- **Role**: {role}")
        lines.append(f"- **Model**: `{model}` ({provider})")
        if skills:
            lines.append(f"- **Skills**: {', '.join(skills)}")
        if fallbacks:
            lines.append(f"- **Fallback models**: {', '.join(fallbacks)}")
        lines.append(f"- **Autonomy level**: {autonomy}")
        lines.append("")

    # Communication guidelines
    lines.extend([
        "## Communication",
        "",
        "- Agents coordinate via the **Context Bus** (shared key-value store) "
        "and **Mailbox** (P2P message passing).",
        "- Address teammates by their **agent ID** when referencing their work.",
        "- The **planner** decomposes tasks; **executors** implement them; "
        "**reviewers** evaluate quality.",
        "- Peer review scores feed into the reputation system, which influences "
        "task assignment priority.",
        "",
    ])

    content = "\n".join(lines)

    # Write to disk
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        f.write(content)

    logger.info("Generated team skill: %s (%d agents)", output_path, len(agents))
    return content
