"""Agent management CLI commands."""
from __future__ import annotations

import os

from core.theme import theme as _theme


AGENT_TEMPLATES = {
    "researcher": {
        "role": "Research specialist — finds information, analyzes sources, synthesizes findings",
        "skills": ["_base", "web_search", "summarize"],
        "soul": (
            "# Researcher\n\n"
            "## Identity\n"
            "You are a meticulous researcher. You find accurate, relevant information and present it clearly.\n\n"
            "## Style\n"
            "- Analytical and precise\n"
            "- Always cite sources\n"
            "- Distinguish facts from opinions\n\n"
            "## Values\n"
            "- Accuracy over speed\n"
            "- Cross-reference multiple sources\n"
            "- Flag conflicting information\n\n"
            "## Boundaries\n"
            "- Never fabricate citations\n"
            "- Clearly state when information is uncertain\n"
        ),
    },
    "coder": {
        "role": "Software engineer — writes, reviews, and debugs code",
        "skills": ["_base", "code_write", "code_review"],
        "soul": (
            "# Coder\n\n"
            "## Identity\n"
            "You are a pragmatic software engineer. You write clean, tested, maintainable code.\n\n"
            "## Style\n"
            "- Minimal and focused\n"
            "- Follow existing code patterns\n"
            "- Clear variable/function names\n\n"
            "## Values\n"
            "- Simplicity over cleverness\n"
            "- Understand requirements before coding\n"
            "- Consider edge cases\n\n"
            "## Boundaries\n"
            "- No unnecessary complexity\n"
            "- Handle errors gracefully\n"
            "- Never commit secrets or credentials\n"
        ),
    },
    "debugger": {
        "role": "Debug specialist — diagnoses issues, traces root causes, proposes fixes",
        "skills": ["_base", "code_review", "code_write"],
        "soul": (
            "# Debugger\n\n"
            "## Identity\n"
            "You are a systematic debugger. You find and fix root causes, not just symptoms.\n\n"
            "## Style\n"
            "- Methodical hypothesis-driven investigation\n"
            "- Show your reasoning chain\n"
            "- Propose minimal, targeted fixes\n\n"
            "## Values\n"
            "- Never guess — verify\n"
            "- Reproduce before fixing\n"
            "- Prevent regression\n\n"
            "## Boundaries\n"
            "- Never apply patches blindly\n"
            "- Explain why, not just what\n"
        ),
    },
    "doc_writer": {
        "role": "Documentation writer — creates clear, structured technical docs",
        "skills": ["_base", "summarize"],
        "soul": (
            "# Documentation Writer\n\n"
            "## Identity\n"
            "You write clear, user-friendly documentation. Make complex topics accessible.\n\n"
            "## Style\n"
            "- Simple language, practical examples\n"
            "- Structured with clear headings\n"
            "- Scannable and concise\n\n"
            "## Values\n"
            "- Completeness without verbosity\n"
            "- Target audience awareness\n"
            "- Keep sections focused\n\n"
            "## Boundaries\n"
            "- Never assume reader knowledge\n"
            "- Always include a quick-start path\n"
        ),
    },
}


def cmd_agents_add(name: str, template: str | None = None):
    """Add a new agent to the team interactively."""
    if not os.path.exists("config/agents.yaml"):
        print("No config found. Run `cleo onboard` first.")
        return

    try:
        import questionary
        from core.onboard import (
            STYLE, PRESETS, PROVIDERS, C_OK, C_DIM, C_AGENT,
            _ask_provider, _ensure_api_key, _ask_model,
            _build_agent_entry,
        )
    except ImportError:
        print("ERROR: questionary is required.  pip3 install questionary")
        return

    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        console = None

    import yaml

    with open("config/agents.yaml") as f:
        config = yaml.safe_load(f) or {}

    existing_ids = [a["id"] for a in config.get("agents", [])]
    if name in existing_ids:
        print(f"Agent '{name}' already exists. Choose a different name.")
        return

    print(f"\n  Creating agent: {name}\n")

    if template and template in AGENT_TEMPLATES:
        tmpl = AGENT_TEMPLATES[template]
        role = tmpl["role"]
        skills = list(tmpl["skills"])
        print(f"  Using template: {template}")
        print(f"  Role: {role}\n")
    else:
        preset_choices = [
            questionary.Choice(PRESETS[k]["label"], value=k)
            for k in PRESETS
        ] + [questionary.Choice("Custom (define your own)", value="custom")]

        preset = questionary.select(
            "Role:", choices=preset_choices, style=STYLE,
        ).ask()
        if preset is None:
            return

        if preset == "custom":
            role = questionary.text("Role description:", style=STYLE).ask()
            if not role:
                return
            skills = ["_base"]
        else:
            role = PRESETS[preset]["role"]
            skills = list(PRESETS[preset]["skills"])

    provider = _ask_provider()
    if provider is None:
        return

    api_key = _ensure_api_key(provider)
    if api_key is None:
        return

    model = _ask_model(provider, api_key)
    if model is None:
        return

    entry = _build_agent_entry(name, role, model, skills, provider)
    config.setdefault("agents", []).append(entry)

    from core.config_manager import safe_write_yaml
    safe_write_yaml("config/agents.yaml", config, reason=f"add agent {name}")

    override_dir = os.path.join("skills", "agent_overrides")
    os.makedirs(override_dir, exist_ok=True)
    override_path = os.path.join(override_dir, f"{name}.md")
    if not os.path.exists(override_path):
        with open(override_path, "w") as f:
            f.write(f"# {name} — Skill Overrides\n\n"
                    f"<!-- Add agent-specific instructions here -->\n")

    agent_doc_dir = os.path.join("docs", name)
    os.makedirs(agent_doc_dir, exist_ok=True)
    soul_path = os.path.join(agent_doc_dir, "soul.md")
    if not os.path.exists(soul_path):
        if template and template in AGENT_TEMPLATES:
            content = AGENT_TEMPLATES[template]["soul"]
        else:
            content = (f"# {name}\n\n"
                       f"## Identity\n{role}\n\n"
                       f"## Style\n"
                       f"<!-- How this agent communicates -->\n\n"
                       f"## Values\n"
                       f"<!-- What this agent prioritizes -->\n\n"
                       f"## Boundaries\n"
                       f"<!-- What this agent won't do -->\n")
        with open(soul_path, "w") as f:
            f.write(content)

    from core.onboard import PROVIDERS
    print(f"\n  ✓ Agent '{name}' created → {PROVIDERS[provider]['label']}/{model}")
    print(f"  ✓ {override_path}")
    print(f"  ✓ {soul_path}")
    print(f"  Team: {', '.join(a['id'] for a in config['agents'])}\n")
