"""
core/onboard.py
Interactive onboarding wizard — OpenClaw-inspired.
Uses questionary for arrow-key selection menus.

Two modes:
  - Quick setup (run_quick_setup): first-run ->ready to chat
  - Full wizard (run_onboard):     per-agent config with independent LLM
"""

from __future__ import annotations
import os
import sys

try:
    import questionary
    from questionary import Style
except ImportError:
    print("ERROR: 'questionary' is required.  pip3 install questionary")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
except ImportError:
    print("ERROR: 'rich' package is required.  pip3 install rich")
    sys.exit(1)

import yaml

console = Console()

# ── Purple theme ──────────────────────────────────────────────────────────────

STYLE = Style([
    ("qmark",       "fg:#b388ff bold"),       # purple marker
    ("question",    "bold"),
    ("answer",      "fg:#ce93d8 bold"),        # light purple answer
    ("pointer",     "fg:#b388ff bold"),        # purple arrow
    ("highlighted", ""),                       # no color on whole row
    ("selected",    "fg:#ce93d8"),             # light purple selected
    ("instruction", "fg:#9e9e9e"),             # gray hint
])

# Rich markup colors
C_ACCENT  = "bold magenta"       # main accent
C_OK      = "green"              # success checkmark
C_DIM     = "dim"                # subtle text
C_WARN    = "yellow"             # warning
C_AGENT   = "bold bright_magenta"  # agent names

def _pause(msg: str = "Press Enter to continue..."):
    """Show a message and wait for Enter before continuing."""
    console.print()
    questionary.press_any_key_to_continue(msg, style=STYLE).ask()


# ── Constants ─────────────────────────────────────────────────────────────────

PRESETS = {
    "leo": {
        "label": "Leo — decompose tasks into subtasks",
        "role": (
            "Strategic planner. Decompose the task into clear subtasks.\n"
            "Write one subtask per line, prefixed with TASK:.\n"
            "Do not implement — only plan."
        ),
        "skills": ["planning", "_base"],
    },
    "jerry": {
        "label": "Jerry — implement and execute tasks",
        "role": (
            "Implementation agent. Carry out tasks assigned by the planner.\n"
            "Write clean, working code or content. Always include reasoning."
        ),
        "skills": ["coding", "_base"],
    },
    "alic": {
        "label": "Alic — evaluate and score outputs",
        "role": (
            "Peer reviewer. Evaluate task outputs on correctness, clarity,\n"
            'and completeness. Return JSON: {"score": int, "comment": str}.'
        ),
        "skills": ["review", "_base"],
    },
}

PROVIDERS = {
    "flock": {
        "label": "FLock API",
        "env": "FLOCK_API_KEY",
        "url_env": "FLOCK_BASE_URL",
        "base_url": "https://api.flock.io/v1",
        "model": "qwen3-30b-a3b-instruct-2507",
    },
    "openai": {
        "label": "OpenAI",
        "env": "OPENAI_API_KEY",
        "url_env": "OPENAI_BASE_URL",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
    },
    "minimax": {
        "label": "MiniMax",
        "env": "MINIMAX_API_KEY",
        "url_env": "MINIMAX_BASE_URL",
        "base_url": "https://api.minimax.io/v1",
        "model": "minimax-m2.1",
    },
    "ollama": {
        "label": "Ollama (local)",
        "env": "",
        "url_env": "OLLAMA_URL",
        "base_url": "http://localhost:11434",
        "model": "llama3.1",
    },
}

CONFIG_PATH = "config/agents.yaml"
ENV_PATH = ".env"

# ── ASCII Art Banner ─────────────────────────────────────────────────────────

BANNER = r"""[bold magenta]
    ██████╗██╗     ███████╗ ██████╗
   ██╔════╝██║     ██╔════╝██╔═══██╗
   ██║     ██║     █████╗  ██║   ██║
   ██║     ██║     ██╔══╝  ██║   ██║
   ╚██████╗███████╗███████╗╚██████╔╝
    ╚═════╝╚══════╝╚══════╝ ╚═════╝[/bold magenta]
[dim]           Agent Stack · Configure[/dim]
"""


# ══════════════════════════════════════════════════════════════════════════════
#  QUICK SETUP  — first-run in chat mode
# ══════════════════════════════════════════════════════════════════════════════

def run_quick_setup() -> bool:
    """Minimal onboarding. Returns True on success."""
    try:
        console.print()
        console.print(BANNER)

        # ── Risk acknowledgment (OpenClaw pattern: first-run only) ──
        if not os.path.exists(CONFIG_PATH):
            _show_risk_notice()

        # ── Detect existing config ──
        action = _detect_existing_config()
        if action == "keep":
            return True
        if action == "abort":
            return False
        if action == "sections":
            _wizard_sections()
            return True

        console.print(f"  [{C_ACCENT}]Quick Setup[/{C_ACCENT}]\n")

        # ── Provider ──
        provider = _ask_provider()
        if provider is None:
            return False

        # ── API key ──
        api_key = _ensure_api_key(provider)
        if api_key is None:
            return False

        # ── Model ──
        model = _ask_model(provider, api_key)
        if model is None:
            return False

        # ── Write config ──
        _write_config_quick(provider, model, api_key)

        # ── Skill CLI dependencies ──
        console.print(f"\n  [{C_DIM}]Checking skill CLI dependencies...[/{C_DIM}]")
        _ask_skill_deps()

        # ── Health check ──
        console.print(f"\n  [{C_DIM}]Running health check...[/{C_DIM}]")
        from core.doctor import run_doctor_quick
        run_doctor_quick(console)

        # ── Gateway summary ──
        _show_gateway_summary(provider, model)
        return True

    except KeyboardInterrupt:
        console.print(f"\n  [{C_WARN}]Cancelled.[/{C_WARN}]")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  FULL WIZARD  — cleo configure / /configure
# ══════════════════════════════════════════════════════════════════════════════

def run_onboard():
    """Full interactive wizard — QuickStart, Advanced, or Sectional mode."""
    try:
        console.print()
        console.print(BANNER)

        # ── Detect existing ──
        action = _detect_existing_config()
        if action == "keep":
            console.print(f"  [{C_DIM}]No changes made.[/{C_DIM}]")
            return
        if action == "abort":
            return

        # ── Sectional modify — OpenClaw-style (only change what you need) ──
        if action == "sections":
            _wizard_sections()
            return

        # ── Fresh setup — QuickStart or Advanced ──
        mode = questionary.select(
            "Setup mode:",
            choices=[
                questionary.Choice(
                    "QuickStart (sensible defaults, fast)",
                    value="quick",
                ),
                questionary.Choice(
                    "Advanced (per-agent LLM, gateway, daemon)",
                    value="advanced",
                ),
            ],
            default="quick",
            style=STYLE,
        ).ask()
        if mode is None:
            return

        if mode == "quick":
            _wizard_quick()
        else:
            _wizard_advanced()

    except KeyboardInterrupt:
        console.print(f"\n  [{C_WARN}]Cancelled.[/{C_WARN}]")


def _wizard_quick():
    """QuickStart path — same as run_quick_setup but called from configure."""
    console.print(f"\n  [{C_ACCENT}]Step 1/5 · Model & Auth[/{C_ACCENT}]")

    provider = _ask_provider()
    if provider is None:
        return

    api_key = _ensure_api_key(provider)
    if api_key is None:
        return

    model = _ask_model(provider, api_key)
    if model is None:
        return

    # Write config
    _write_config_quick(provider, model, api_key)

    # Skill CLI dependencies
    console.print(f"\n  [{C_ACCENT}]Step 2/5 · Skill CLI Dependencies[/{C_ACCENT}]")
    _ask_skill_deps()

    # Tools quick setup
    console.print(f"\n  [{C_ACCENT}]Step 3/5 · Tools[/{C_ACCENT}]")
    _ask_tools_quick()

    # Health check
    console.print(f"\n  [{C_ACCENT}]Step 4/5 · Health Check[/{C_ACCENT}]")
    from core.doctor import run_doctor_quick
    run_doctor_quick(console)

    # Gateway summary
    console.print(f"  [{C_ACCENT}]Step 5/5 · Gateway Summary[/{C_ACCENT}]")
    _show_gateway_summary(provider, model)


def _wizard_advanced():
    """Advanced path — full per-agent config + gateway + daemon."""
    # ── Step 1: Team ──
    console.print(f"\n  [{C_ACCENT}]Step 1/5 · Agent Team[/{C_ACCENT}]")

    num_str = questionary.text(
        "How many agents?",
        default="3",
        style=STYLE,
    ).ask()
    if num_str is None:
        return
    try:
        num_agents = max(1, int(num_str))
    except ValueError:
        num_agents = 3

    preset_keys = list(PRESETS.keys())
    agents_cfg = []

    for i in range(num_agents):
        console.print(f"\n  [{C_AGENT}]━━ Agent {i+1}/{num_agents} ━━[/{C_AGENT}]")

        choices = [
            questionary.Choice(PRESETS[k]["label"], value=k)
            for k in preset_keys
        ] + [questionary.Choice("Custom (define your own)", value="custom")]

        default_preset = preset_keys[i] if i < len(preset_keys) else "custom"
        preset = questionary.select(
            "Role:",
            choices=choices,
            default=default_preset,
            style=STYLE,
        ).ask()
        if preset is None:
            return

        if preset == "custom":
            agent_id = questionary.text("Agent ID:", style=STYLE).ask()
            if not agent_id:
                return
            role = questionary.text("Role description:", style=STYLE).ask()
            if not role:
                return
            skills = ["_base"]
        else:
            agent_id = questionary.text(
                "Agent ID:", default=preset, style=STYLE
            ).ask()
            if not agent_id:
                return
            role = PRESETS[preset]["role"]
            skills = list(PRESETS[preset]["skills"])

        console.print(f"  [{C_DIM}]LLM for {agent_id}:[/{C_DIM}]")
        provider = _ask_provider()
        if provider is None:
            return

        api_key = _ensure_api_key(provider)
        if api_key is None:
            return

        model = _ask_model(provider, api_key)
        if model is None:
            return

        agents_cfg.append({
            "id": agent_id, "role": role, "model": model,
            "skills": skills, "provider": provider,
            "api_key": api_key,
        })
        console.print(f"  [{C_OK}]+[/{C_OK}] {agent_id} ->{PROVIDERS[provider]['label']}/{model}")

    # ── Step 2: Memory & Chain ──
    console.print(f"\n  [{C_ACCENT}]Step 2/5 · Memory & Chain[/{C_ACCENT}]")

    memory = _ask_memory()
    if memory is None:
        return

    chain = questionary.confirm(
        "Enable on-chain reputation (ERC-8004)?",
        default=False,
        style=STYLE,
    ).ask()
    if chain is None:
        return

    # ── Step 3: Gateway ──
    console.print(f"\n  [{C_ACCENT}]Step 3/5 · Gateway[/{C_ACCENT}]")
    gateway_port, gateway_token = _ask_gateway()

    # ── Step 4: Daemon ──
    console.print(f"\n  [{C_ACCENT}]Step 4/5 · Background Service[/{C_ACCENT}]")
    _ask_daemon(gateway_port, gateway_token)

    # ── Write config ──
    _write_config_full(agents_cfg, memory, chain)

    # Write gateway config to .env
    if gateway_port:
        _write_env("CLEO_GATEWAY_PORT", str(gateway_port))
    if gateway_token:
        _write_env("CLEO_GATEWAY_TOKEN", gateway_token)

    # ── Step 5: Health Check ──
    console.print(f"\n  [{C_ACCENT}]Step 5/5 · Health Check[/{C_ACCENT}]")
    from core.doctor import run_doctor_quick
    run_doctor_quick(console)

    # ── Summary ──
    _show_gateway_summary_full(agents_cfg, memory, chain, gateway_port)


# ══════════════════════════════════════════════════════════════════════════════
#  TOOLS QUICK SETUP
# ══════════════════════════════════════════════════════════════════════════════

def _ask_skill_deps():
    """Scan skill files for CLI dependencies, show status, offer to install missing ones."""
    try:
        from core.skill_deps import (
            scan_skill_deps, get_missing_deps, get_installed_deps,
            pick_best_installer, install_dep, build_install_command,
            check_prerequisites,
        )
    except ImportError:
        console.print(f"  [{C_DIM}]Skill dependency module not available.[/{C_DIM}]")
        return

    all_deps = scan_skill_deps()
    if not all_deps:
        console.print(f"  [{C_DIM}]No skills with CLI dependencies found.[/{C_DIM}]")
        return

    installed = get_installed_deps()
    missing = get_missing_deps()

    # ── Show summary ──
    console.print(f"  [{C_OK}]{len(installed)}[/{C_OK}] skill CLIs already installed, "
                  f"[{C_WARN}]{len(missing)}[/{C_WARN}] missing\n")

    if installed:
        tbl = Table(box=box.SIMPLE, show_header=True, header_style=C_ACCENT,
                    padding=(0, 1))
        tbl.add_column("", width=2)
        tbl.add_column("Skill", style=C_AGENT)
        tbl.add_column("Binary", style=C_DIM)
        for dep in installed:
            emoji = dep.get("emoji", "")
            bins = ", ".join(dep["requires_bins"]) or ", ".join(dep.get("requires_any_bins", []))
            tbl.add_row(emoji, dep["skill"], f"[{C_OK}]{bins}[/{C_OK}]")
        console.print(tbl)

    if not missing:
        console.print(f"\n  [{C_OK}]All skill dependencies satisfied![/{C_OK}]")
        return

    # ── Show what's missing ──
    console.print(f"\n  [{C_ACCENT}]Missing CLI tools:[/{C_ACCENT}]")
    tbl_miss = Table(box=box.SIMPLE, show_header=True, header_style=C_ACCENT,
                     padding=(0, 1))
    tbl_miss.add_column("", width=2)
    tbl_miss.add_column("Skill", style=C_AGENT)
    tbl_miss.add_column("Needs", style=C_WARN)
    tbl_miss.add_column("Install via")
    for dep in missing:
        emoji = dep.get("emoji", "")
        bins = ", ".join(dep["missing_bins"]) if dep["missing_bins"] else ", ".join(dep.get("requires_any_bins", []))
        best = pick_best_installer(dep.get("install", []))
        via = best.get("label", best.get("kind", "?")) if best else "manual"
        tbl_miss.add_row(emoji, dep["skill"], bins, f"[{C_DIM}]{via}[/{C_DIM}]")
    console.print(tbl_miss)

    # ── Check package manager prerequisites ──
    prereqs = check_prerequisites()
    needed_kinds = set()
    for dep in missing:
        for entry in dep.get("install", []):
            mgr = {"brew": "brew", "brew-cask": "brew", "go": "go",
                    "node": "npm", "uv": "uv", "apt": "apt"}.get(entry.get("kind", ""), "")
            if mgr:
                needed_kinds.add(mgr)
    missing_mgrs = [m for m in needed_kinds if not prereqs.get(m, False)]
    if missing_mgrs:
        console.print(f"\n  [{C_WARN}]Package managers not found: {', '.join(missing_mgrs)}[/{C_WARN}]")
        console.print(f"  [{C_DIM}]Skills requiring these will be skipped.[/{C_DIM}]")

    # ── Ask to install ──
    console.print()
    install_mode = questionary.select(
        "Install missing skill CLIs?",
        choices=[
            questionary.Choice("Install all missing",         value="all"),
            questionary.Choice("Let me pick which to install", value="pick"),
            questionary.Choice("Skip for now",                 value="skip"),
        ],
        default="all",
        style=STYLE,
    ).ask()

    if install_mode is None or install_mode == "skip":
        console.print(f"  [{C_DIM}]Skipped — install later with `cleo configure --section skills`[/{C_DIM}]")
        return

    # ── Select which to install ──
    if install_mode == "pick":
        choices = [
            questionary.Choice(
                f"{dep.get('emoji', '')} {dep['skill']} ({', '.join(dep['missing_bins'])})",
                value=i,
                checked=True,
            )
            for i, dep in enumerate(missing)
            if dep.get("install")
        ]
        if not choices:
            console.print(f"  [{C_DIM}]No installable skills found.[/{C_DIM}]")
            return
        selected_idx = questionary.checkbox(
            "Select skills to install:",
            choices=choices,
            style=STYLE,
        ).ask()
        if selected_idx is None:
            return
        to_install = [missing[i] for i in selected_idx]
    else:
        to_install = [d for d in missing if d.get("install")]

    if not to_install:
        return

    # ── Run installs ──
    console.print(f"\n  [{C_ACCENT}]Installing {len(to_install)} skill CLIs...[/{C_ACCENT}]\n")
    ok_count = 0
    fail_count = 0
    for dep in to_install:
        best = pick_best_installer(dep.get("install", []))
        if not best:
            console.print(f"  [{C_WARN}]✗[/{C_WARN}] {dep['skill']} — no installer available")
            fail_count += 1
            continue

        cmd = build_install_command(best)
        if not cmd:
            console.print(f"  [{C_WARN}]✗[/{C_WARN}] {dep['skill']} — cannot build command")
            fail_count += 1
            continue

        emoji = dep.get("emoji", "")
        console.print(f"  {emoji} {dep['skill']}: [dim]$ {cmd}[/dim]")
        success = install_dep(best, quiet=False)
        if success:
            console.print(f"  [{C_OK}]✓[/{C_OK}] {dep['skill']} installed")
            ok_count += 1
        else:
            console.print(f"  [{C_WARN}]✗[/{C_WARN}] {dep['skill']} — install failed")
            fail_count += 1

    console.print()
    if fail_count == 0:
        console.print(f"  [{C_OK}]All {ok_count} CLIs installed successfully![/{C_OK}]")
    else:
        console.print(f"  [{C_OK}]{ok_count} installed[/{C_OK}], "
                      f"[{C_WARN}]{fail_count} failed[/{C_WARN}]")


def _ask_tools_quick():
    """Quick tools setup — ask about web search (most common)."""
    try:
        from core.tools import list_all_tools
        all_tools = list_all_tools()
    except ImportError:
        console.print(f"  [{C_DIM}]Tools module not available.[/{C_DIM}]")
        return

    # Show tool status summary
    available = [t for t in all_tools if t.is_available()]
    unavail = [t for t in all_tools if not t.is_available()]
    console.print(f"  [{C_OK}]{len(available)}[/{C_OK}] tools available, "
                  f"[{C_DIM}]{len(unavail)} need configuration[/{C_DIM}]")

    # Ask about web search (most impactful tool requiring config)
    brave_key = os.environ.get("BRAVE_API_KEY", "")
    if not brave_key:
        enable_search = questionary.confirm(
            "Enable web_search? (requires free Brave Search API key)",
            default=False,
            style=STYLE,
        ).ask()
        if enable_search:
            console.print(f"  [{C_DIM}]Get a free key at https://brave.com/search/api/[/{C_DIM}]")
            key = questionary.text(
                "BRAVE_API_KEY:",
                default="",
                style=STYLE,
            ).ask()
            if key and key.strip():
                _write_env("BRAVE_API_KEY", key.strip())
                os.environ["BRAVE_API_KEY"] = key.strip()
                console.print(f"  [{C_OK}]+[/{C_OK}] web_search enabled")
            else:
                console.print(f"  [{C_DIM}]Skipped — can enable later with `cleo configure --section tools`[/{C_DIM}]")
        else:
            console.print(f"  [{C_DIM}]Skipped — agents can still use other tools (exec, fs, etc.)[/{C_DIM}]")
    else:
        console.print(f"  [{C_OK}]+[/{C_OK}] web_search already enabled")

    # Tool profile
    profile = questionary.select(
        "Default tool access for agents:",
        choices=[
            questionary.Choice("Full (all tools)",    value="full"),
            questionary.Choice("Coding (web + exec + fs)", value="coding"),
            questionary.Choice("Minimal (web only)",  value="minimal"),
        ],
        default="full",
        style=STYLE,
    ).ask()
    if profile:
        # Update config with default tools profile
        try:
            cfg_path = os.path.join("config", "agents.yaml")
            if os.path.exists(cfg_path):
                import yaml
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f) or {}
                cfg.setdefault("tools", {})["default_profile"] = profile
                for agent in cfg.get("agents", []):
                    if "tools" not in agent:
                        agent["tools"] = {"profile": profile}
                with open(cfg_path, "w") as f:
                    f.write("# config/agents.yaml\n\n")
                    yaml.dump(cfg, f, allow_unicode=True,
                              default_flow_style=False, sort_keys=False)
        except Exception:
            pass
        console.print(f"  [{C_OK}]+[/{C_OK}] Tool profile: {profile}")


# ══════════════════════════════════════════════════════════════════════════════
#  SECTIONAL CONFIGURE — OpenClaw-style "only change what you need"
# ══════════════════════════════════════════════════════════════════════════════

# Section definitions: (value, label, description, icon)
_SECTIONS = [
    # (value, label, description)
    ("model",       "Model",       "Change LLM provider, API key, or default model"),
    ("agents",      "Agents",      "Add, remove, or edit individual agents"),
    ("skills",      "Skills",      "Install, manage, and assign agent skills"),
    ("skill_deps",  "Skill CLIs",  "Check & install CLI tools required by skills"),
    ("memory",      "Memory",      "Switch memory backend (mock / chroma / hybrid)"),
    ("resilience",  "Resilience",  "Retry count, circuit breaker, backoff timing"),
    ("compaction",  "Compaction",  "Context window compaction settings"),
    ("gateway",     "Gateway",     "Port, auth token, daemon settings"),
    ("chain",       "Chain",       "On-chain reputation (ERC-8004)"),
    ("tools",       "Tools",       "Built-in tools: web search, exec, cron, media"),
    ("health",      "Health check","Run doctor diagnostics"),
]


def _wizard_sections():
    """OpenClaw-style sectional configure — loop until user exits."""
    _label_map = {v: lb for v, lb, _ in _SECTIONS}

    while True:
        console.print()

        # ── Section selector (checkbox) ──
        choices = [
            questionary.Choice(
                f"{label:<14s}({desc})",
                value=value,
                checked=False,
            )
            for value, label, desc in _SECTIONS
        ]
        choices.append(questionary.Choice(
            "[Done] — save & exit",
            value="_exit",
            checked=False,
        ))

        selected = questionary.checkbox(
            "Select sections to configure (or Done to exit):",
            choices=choices,
            style=STYLE,
        ).ask()

        if selected is None:
            console.print(f"\n  [{C_WARN}]Cancelled.[/{C_WARN}]\n")
            return
        if "_exit" in selected or not selected:
            break

        # Load current config
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}

        # ── Run each selected section handler ──
        for i, section in enumerate(selected):
            label = _label_map.get(section, section.title())

            # Section separator
            console.print()
            console.print(f"  [{C_ACCENT}]{'─' * 50}[/{C_ACCENT}]")
            console.print(f"  [{C_ACCENT}]{label}[/{C_ACCENT}]"
                           f"  [{C_DIM}]({i+1}/{len(selected)})[/{C_DIM}]")
            console.print(f"  [{C_ACCENT}]{'─' * 50}[/{C_ACCENT}]")
            console.print()

            handler = _SECTION_HANDLERS.get(section)
            if handler:
                handler(cfg)
            else:
                console.print(f"  [{C_WARN}]No handler for section: {section}[/{C_WARN}]")

        # ── Write updated config after each round ──
        os.makedirs("config", exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            f.write("# config/agents.yaml\n\n")
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        # Auto-generate team skill after config save
        try:
            from core.team_skill import generate_team_skill
            generate_team_skill()
        except Exception:
            pass

        console.print()
        console.print(f"  [{C_OK}]+[/{C_OK}] Config saved -> {CONFIG_PATH}")
        _pause("Press Enter to return to configure menu...")

    console.print(f"\n  [{C_OK}]+[/{C_OK}] Configuration complete.\n")


def _section_model(cfg: dict):
    """Section: Change global LLM provider / API key / default model."""
    current_provider = cfg.get("llm", {}).get("provider", "flock")
    console.print(f"  [{C_DIM}]Current: {current_provider}[/{C_DIM}]\n")

    provider = _ask_provider()
    if provider is None:
        return

    api_key = _ensure_api_key(provider)
    if api_key is None:
        return

    model = _ask_model(provider, api_key)
    if model is None:
        return

    # Update global provider
    cfg.setdefault("llm", {})["provider"] = provider

    # Ask: apply new model to all agents?
    agents = cfg.get("agents", [])
    if agents:
        apply_all = questionary.confirm(
            f"Apply model '{model}' to all {len(agents)} agents?",
            default=True,
            style=STYLE,
        ).ask()
        if apply_all:
            for a in agents:
                a["model"] = model
                a.setdefault("llm", {})["provider"] = provider
            console.print(f"  [{C_OK}]+[/{C_OK}] Updated {len(agents)} agents ->{provider}/{model}")
        else:
            console.print(f"  [{C_DIM}]Global provider set to {provider}. Agent models unchanged.[/{C_DIM}]")

    console.print(f"  [{C_OK}]+[/{C_OK}] Provider: {provider}, Model: {model}")


def _section_agents(cfg: dict):
    """Section: Add, remove, or edit individual agents."""
    agents = cfg.get("agents", [])
    global_provider = cfg.get("llm", {}).get("provider", "flock")

    # Show current agents
    if agents:
        console.print(f"  [{C_DIM}]Current agents:[/{C_DIM}]")
        for a in agents:
            p = a.get("llm", {}).get("provider", global_provider)
            m = a.get("model", "?")
            fb = a.get("fallback_models", [])
            fb_str = f"  [{C_DIM}]fallback: {', '.join(fb)}[/{C_DIM}]" if fb else ""
            console.print(f"    [{C_AGENT}]{a['id']:10}[/{C_AGENT}] {p}/{m}{fb_str}")
    console.print()

    action = questionary.select(
        "Agent action:",
        choices=[
            questionary.Choice("Edit an agent (change model / fallback / role)", value="edit"),
            questionary.Choice("Add a new agent", value="add"),
            questionary.Choice("Remove an agent", value="remove"),
        ],
        style=STYLE,
    ).ask()
    if action is None:
        return

    if action == "edit":
        _section_agents_edit(cfg, agents, global_provider)
    elif action == "add":
        _section_agents_add(cfg, agents, global_provider)
    elif action == "remove":
        _section_agents_remove(cfg, agents)


def _section_agents_edit(cfg: dict, agents: list, global_provider: str):
    """Edit an existing agent."""
    if not agents:
        console.print(f"  [{C_WARN}]No agents to edit.[/{C_WARN}]")
        return

    agent_choices = [
        questionary.Choice(
            f"{a['id']} ({a.get('llm', {}).get('provider', global_provider)}/{a.get('model', '?')})",
            value=i,
        )
        for i, a in enumerate(agents)
    ]
    idx = questionary.select("Select agent:", choices=agent_choices, style=STYLE).ask()
    if idx is None:
        return

    agent = agents[idx]
    console.print(f"\n  Editing [{C_AGENT}]{agent['id']}[/{C_AGENT}]:")

    # What to edit?
    fields = questionary.checkbox(
        "What to change?",
        choices=[
            questionary.Choice("Model", value="model"),
            questionary.Choice("Fallback models", value="fallback"),
            questionary.Choice("Provider (per-agent)", value="provider"),
            questionary.Choice("Role / system prompt", value="role"),
            questionary.Choice("Skills", value="skills"),
        ],
        style=STYLE,
    ).ask()
    if not fields:
        return

    if "provider" in fields or "model" in fields:
        p = agent.get("llm", {}).get("provider", global_provider)
        if "provider" in fields:
            p = _ask_provider()
            if p is None:
                return
            api_key = _ensure_api_key(p)
            if api_key is None:
                return
            agent.setdefault("llm", {})["provider"] = p
        else:
            # Get api_key for model fetching
            env_var = PROVIDERS.get(p, {}).get("env", "")
            api_key = os.environ.get(env_var, "") if env_var else ""

        if "model" in fields:
            model = _ask_model(p, api_key)
            if model:
                agent["model"] = model
                console.print(f"  [{C_OK}]+[/{C_OK}] Model ->{model}")

    if "fallback" in fields:
        current_fb = agent.get("fallback_models", [])
        console.print(f"  [{C_DIM}]Current fallbacks: {', '.join(current_fb) if current_fb else 'none'}[/{C_DIM}]")

        # Fetch available models for selection
        p = agent.get("llm", {}).get("provider", global_provider)
        env_var = PROVIDERS.get(p, {}).get("env", "")
        api_key = os.environ.get(env_var, "") if env_var else ""
        models, _ = _fetch_models(p, api_key)

        if models:
            # Exclude the primary model
            primary = agent.get("model", "")
            available = [m for m in models if m != primary]
            if available:
                fb_choices = [
                    questionary.Choice(m, value=m, checked=(m in current_fb))
                    for m in available
                ]
                new_fb = questionary.checkbox(
                    "Fallback models (priority order):",
                    choices=fb_choices,
                    style=STYLE,
                ).ask()
                if new_fb is not None:
                    agent["fallback_models"] = new_fb
                    console.print(f"  [{C_OK}]+[/{C_OK}] Fallbacks ->{', '.join(new_fb) if new_fb else 'none'}")
        else:
            fb_text = questionary.text(
                "Fallback models (comma-separated):",
                default=", ".join(current_fb),
                style=STYLE,
            ).ask()
            if fb_text is not None:
                agent["fallback_models"] = [m.strip() for m in fb_text.split(",") if m.strip()]

    if "role" in fields:
        current_role = agent.get("role", "")
        console.print(f"  [{C_DIM}]Current role: {current_role[:60]}...[/{C_DIM}]")
        new_role = questionary.text(
            "New role/system prompt:",
            default=current_role,
            style=STYLE,
        ).ask()
        if new_role:
            agent["role"] = new_role
            console.print(f"  [{C_OK}]+[/{C_OK}] Role updated")

    if "skills" in fields:
        current_skills = agent.get("skills", [])
        # Dynamically scan skills/ directory for available skills
        all_skills = ["_base"]
        if os.path.isdir("skills"):
            for fname in sorted(os.listdir("skills")):
                if fname.endswith(".md") and fname != "_team.md":
                    sname = fname.replace(".md", "")
                    if sname not in all_skills:
                        all_skills.append(sname)
        # Ensure current skills are included even if files missing
        for s in current_skills:
            if s not in all_skills:
                all_skills.append(s)
        skill_choices = [
            questionary.Choice(s, value=s, checked=(s in current_skills))
            for s in all_skills
        ]
        new_skills = questionary.checkbox(
            "Skills:",
            choices=skill_choices,
            style=STYLE,
        ).ask()
        if new_skills is not None:
            agent["skills"] = new_skills
            console.print(f"  [{C_OK}]+[/{C_OK}] Skills ->{', '.join(new_skills)}")


def _section_agents_add(cfg: dict, agents: list, global_provider: str):
    """Add a new agent."""
    preset_choices = [
        questionary.Choice(PRESETS[k]["label"], value=k)
        for k in PRESETS
    ] + [questionary.Choice("Custom (define your own)", value="custom")]

    preset = questionary.select("Role:", choices=preset_choices, style=STYLE).ask()
    if preset is None:
        return

    if preset == "custom":
        agent_id = questionary.text("Agent ID:", style=STYLE).ask()
        if not agent_id:
            return
        role = questionary.text("Role description:", style=STYLE).ask()
        if not role:
            return
        skills = ["_base"]
    else:
        agent_id = questionary.text("Agent ID:", default=preset, style=STYLE).ask()
        if not agent_id:
            return
        role = PRESETS[preset]["role"]
        skills = list(PRESETS[preset]["skills"])

    # Check duplicate
    if any(a["id"] == agent_id for a in agents):
        console.print(f"  [{C_WARN}]Agent '{agent_id}' already exists.[/{C_WARN}]")
        return

    # Use same provider/model as first agent, or ask
    use_same = False
    if agents:
        first = agents[0]
        p = first.get("llm", {}).get("provider", global_provider)
        m = first.get("model", "?")
        use_same = questionary.confirm(
            f"Use same provider/model as {first['id']}? ({p}/{m})",
            default=True,
            style=STYLE,
        ).ask()

    if use_same and agents:
        first = agents[0]
        provider = first.get("llm", {}).get("provider", global_provider)
        model = first.get("model", "?")
    else:
        provider = _ask_provider()
        if provider is None:
            return
        api_key = _ensure_api_key(provider)
        if api_key is None:
            return
        model = _ask_model(provider, api_key)
        if model is None:
            return

    entry = _build_agent_entry(agent_id, role, model, skills, provider)
    agents.append(entry)
    cfg["agents"] = agents
    console.print(f"  [{C_OK}]+[/{C_OK}] Added {agent_id} ->{provider}/{model}")


def _section_agents_remove(cfg: dict, agents: list):
    """Remove an agent."""
    if len(agents) <= 1:
        console.print(f"  [{C_WARN}]Cannot remove — need at least 1 agent.[/{C_WARN}]")
        return

    choices = [questionary.Choice(a["id"], value=i) for i, a in enumerate(agents)]
    idx = questionary.select("Remove which agent?", choices=choices, style=STYLE).ask()
    if idx is None:
        return

    removed = agents.pop(idx)
    cfg["agents"] = agents
    console.print(f"  [{C_OK}]+[/{C_OK}] Removed [{C_AGENT}]{removed['id']}[/{C_AGENT}]")


def _section_memory(cfg: dict):
    """Section: Change memory backend."""
    current = cfg.get("memory", {}).get("backend", "mock")
    console.print(f"  [{C_DIM}]Current: {current}[/{C_DIM}]\n")

    has_chroma = _check_chromadb()

    choices = [
        questionary.Choice("Mock (in-memory, no persistence)", value="mock"),
        questionary.Choice(
            "ChromaDB (vector store)" + ("  [ok]" if has_chroma else "  [not installed]"),
            value="chroma",
        ),
        questionary.Choice(
            "Hybrid (Vector + BM25 keyword search)" + ("  [ok]" if has_chroma else "  [BM25 only]"),
            value="hybrid",
        ),
    ]

    backend = questionary.select(
        "Memory backend:",
        choices=choices,
        default=current,
        style=STYLE,
    ).ask()
    if backend is None:
        return

    if backend in ("chroma", "hybrid") and not has_chroma:
        install = questionary.confirm(
            "ChromaDB not installed. Install now?",
            default=True,
            style=STYLE,
        ).ask()
        if install:
            console.print(f"  [{C_DIM}]Installing chromadb...[/{C_DIM}]")
            import subprocess
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "chromadb"],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    console.print(f"  [{C_OK}]+[/{C_OK}] ChromaDB installed")
                else:
                    console.print(f"  [{C_WARN}]Install failed.[/{C_WARN}]")
                    if result.stderr:
                        console.print(f"  [{C_DIM}]{result.stderr.strip()[:200]}[/{C_DIM}]")
                    if backend == "hybrid":
                        console.print(f"  [{C_DIM}]Hybrid will use BM25 only (no vector search).[/{C_DIM}]")
                    else:
                        backend = "mock"
            except subprocess.TimeoutExpired:
                console.print(f"  [{C_WARN}]Install timed out.[/{C_WARN}]")
                if backend != "hybrid":
                    backend = "mock"

    cfg.setdefault("memory", {})["backend"] = backend
    console.print(f"  [{C_OK}]+[/{C_OK}] Memory ->{backend}")


def _section_resilience(cfg: dict):
    """Section: Configure retry, circuit breaker, backoff."""
    res = cfg.get("resilience", {})
    console.print(f"  [{C_DIM}]Current: retry {res.get('max_retries', 3)}x, "
                  f"CB threshold {res.get('circuit_breaker_threshold', 3)}, "
                  f"delay {res.get('base_delay', 1.0)}-{res.get('max_delay', 30.0)}s[/{C_DIM}]")
    console.print()

    # Max retries
    retries_str = questionary.text(
        "Max retries:",
        default=str(res.get("max_retries", 3)),
        style=STYLE,
    ).ask()
    if retries_str is None:
        return
    try:
        max_retries = max(0, int(retries_str))
    except ValueError:
        max_retries = 3

    # Circuit breaker threshold
    cb_str = questionary.text(
        "Circuit breaker threshold (consecutive failures):",
        default=str(res.get("circuit_breaker_threshold", 3)),
        style=STYLE,
    ).ask()
    try:
        cb_threshold = max(1, int(cb_str)) if cb_str else 3
    except ValueError:
        cb_threshold = 3

    # CB cooldown
    cd_str = questionary.text(
        "Circuit breaker cooldown (seconds):",
        default=str(int(res.get("circuit_breaker_cooldown", 120))),
        style=STYLE,
    ).ask()
    try:
        cb_cooldown = max(10, int(cd_str)) if cd_str else 120
    except ValueError:
        cb_cooldown = 120

    # Base delay
    delay_str = questionary.text(
        "Base retry delay (seconds):",
        default=str(res.get("base_delay", 1.0)),
        style=STYLE,
    ).ask()
    try:
        base_delay = max(0.1, float(delay_str)) if delay_str else 1.0
    except ValueError:
        base_delay = 1.0

    cfg["resilience"] = {
        "max_retries": max_retries,
        "base_delay": base_delay,
        "max_delay": res.get("max_delay", 30.0),
        "jitter": res.get("jitter", 0.5),
        "circuit_breaker_threshold": cb_threshold,
        "circuit_breaker_cooldown": cb_cooldown,
    }
    console.print(f"  [{C_OK}]+[/{C_OK}] Resilience: retry {max_retries}x, CB threshold {cb_threshold}, delay {base_delay}s")


def _section_compaction(cfg: dict):
    """Section: Context compaction settings."""
    comp = cfg.get("compaction", {})
    enabled = comp.get("enabled", False)
    console.print(f"  [{C_DIM}]Current: {'enabled' if enabled else 'disabled'}, "
                  f"max_tokens={comp.get('max_context_tokens', 8000)}, "
                  f"keep_recent={comp.get('keep_recent_turns', 4)}[/{C_DIM}]")
    console.print()

    enable = questionary.confirm(
        "Enable context compaction?",
        default=enabled,
        style=STYLE,
    ).ask()
    if enable is None:
        return

    if not enable:
        cfg["compaction"] = {"enabled": False}
        console.print(f"  [{C_OK}]+[/{C_OK}] Compaction disabled")
        return

    # Max context tokens
    max_tok_str = questionary.text(
        "Max context tokens (trigger threshold):",
        default=str(comp.get("max_context_tokens", 8000)),
        style=STYLE,
    ).ask()
    try:
        max_tokens = max(1000, int(max_tok_str)) if max_tok_str else 8000
    except ValueError:
        max_tokens = 8000

    # Summary target
    summary_str = questionary.text(
        "Summary target tokens:",
        default=str(comp.get("summary_target_tokens", 1500)),
        style=STYLE,
    ).ask()
    try:
        summary_tokens = max(200, int(summary_str)) if summary_str else 1500
    except ValueError:
        summary_tokens = 1500

    # Keep recent turns
    keep_str = questionary.text(
        "Keep recent turns (verbatim):",
        default=str(comp.get("keep_recent_turns", 4)),
        style=STYLE,
    ).ask()
    try:
        keep_turns = max(1, int(keep_str)) if keep_str else 4
    except ValueError:
        keep_turns = 4

    cfg["compaction"] = {
        "enabled": True,
        "max_context_tokens": max_tokens,
        "summary_target_tokens": summary_tokens,
        "keep_recent_turns": keep_turns,
    }
    console.print(f"  [{C_OK}]+[/{C_OK}] Compaction: max {max_tokens} tokens, keep {keep_turns} turns")


def _section_gateway(cfg: dict):
    """Section: Gateway port & token."""
    from core.gateway import DEFAULT_PORT, generate_token

    current_port = os.environ.get("CLEO_GATEWAY_PORT", str(DEFAULT_PORT))
    current_token = os.environ.get("CLEO_GATEWAY_TOKEN", "")

    console.print(f"  [{C_DIM}]Current port: {current_port}[/{C_DIM}]")
    if current_token:
        console.print(f"  [{C_DIM}]Token: {current_token}[/{C_DIM}]")
    else:
        console.print(f"  [{C_DIM}]Token: [{C_WARN}]not set[/{C_WARN}][/{C_DIM}]")
    console.print()

    port_str = questionary.text(
        "Gateway port:",
        default=current_port,
        style=STYLE,
    ).ask()
    if port_str is None:
        return
    try:
        port = int(port_str)
    except ValueError:
        port = DEFAULT_PORT

    if current_token:
        regen = questionary.confirm(
            "Regenerate auth token?",
            default=False,
            style=STYLE,
        ).ask()
        if regen is None:
            return
        token = generate_token() if regen else current_token
        if regen:
            console.print(f"  [{C_OK}]+[/{C_OK}] New token: [{C_DIM}]{token}[/{C_DIM}]")
    else:
        token = generate_token()
        console.print(f"  [{C_OK}]+[/{C_OK}] Generated token: [{C_DIM}]{token}[/{C_DIM}]")

    _write_env("CLEO_GATEWAY_PORT", str(port))
    if token:
        _write_env("CLEO_GATEWAY_TOKEN", token)

    console.print(f"  [{C_OK}]+[/{C_OK}] Gateway: http://127.0.0.1:{port}/")
    console.print(f"  [{C_DIM}]Dashboard: http://127.0.0.1:{port}/[/{C_DIM}]")
    console.print(f"  [{C_DIM}]API Base:  http://127.0.0.1:{port}/v1[/{C_DIM}]")

    # Auto-start gateway
    start_gw = questionary.confirm(
        "Start gateway now?",
        default=True,
        style=STYLE,
    ).ask()
    if start_gw:
        try:
            from core.gateway import start_gateway, check_gateway
            import time
            server = start_gateway(port=port, token=token, daemon=True)
            if server:
                # Wait for the daemon thread to be ready
                time.sleep(0.5)
                ok, _ = check_gateway(port)
                if ok:
                    console.print(f"  [{C_OK}]+[/{C_OK}] Gateway running on port {port}")
                else:
                    console.print(f"  [{C_WARN}]Gateway started but not responding yet (may need a moment).[/{C_WARN}]")
                import webbrowser
                url = f"http://127.0.0.1:{port}/?token={token}"
                webbrowser.open(url)
                console.print(f"  [{C_DIM}]Opened dashboard in browser[/{C_DIM}]")
            else:
                console.print(f"  [{C_WARN}]Gateway failed to start (port {port} may be in use).[/{C_WARN}]")
        except Exception as e:
            console.print(f"  [{C_WARN}]Gateway start error: {e}[/{C_WARN}]")


def _section_chain(cfg: dict):
    """Section: On-chain reputation toggle."""
    enabled = cfg.get("chain", {}).get("enabled", False)
    console.print(f"  [{C_DIM}]Current: {'enabled' if enabled else 'disabled'}[/{C_DIM}]\n")

    new_val = questionary.confirm(
        "Enable on-chain reputation (ERC-8004)?",
        default=enabled,
        style=STYLE,
    ).ask()
    if new_val is None:
        return

    if new_val:
        # Check web3 dependency
        try:
            import web3  # noqa: F401
            console.print(f"  [{C_OK}]+[/{C_OK}] web3 installed")
        except ImportError:
            console.print(f"  [{C_WARN}]web3 is required for chain features.[/{C_WARN}]")
            install = questionary.confirm(
                "Install web3 now? (pip install web3)",
                default=True,
                style=STYLE,
            ).ask()
            if install:
                import subprocess
                console.print(f"  [{C_DIM}]Installing web3...[/{C_DIM}]")
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "web3"],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    console.print(f"  [{C_OK}]+[/{C_OK}] web3 installed")
                else:
                    console.print(f"  [{C_WARN}]Install failed.[/{C_WARN}]")
                    if result.stderr:
                        console.print(f"  [{C_DIM}]{result.stderr.strip()[:200]}[/{C_DIM}]")
                    console.print(f"  [{C_DIM}]Chain disabled — install web3 manually and retry.[/{C_DIM}]")
                    cfg.setdefault("chain", {})["enabled"] = False
                    console.print(f"  [{C_OK}]+[/{C_OK}] Chain: disabled")
                    return
            else:
                console.print(f"  [{C_DIM}]Chain disabled — web3 is required.[/{C_DIM}]")
                cfg.setdefault("chain", {})["enabled"] = False
                console.print(f"  [{C_OK}]+[/{C_OK}] Chain: disabled")
                return

        # Require RPC URL
        rpc = os.environ.get("RPC_URL", "")
        if rpc:
            console.print(f"  [{C_DIM}]RPC URL: {rpc}[/{C_DIM}]")
            change = questionary.confirm(
                "Change RPC URL?",
                default=False,
                style=STYLE,
            ).ask()
            if change:
                rpc = ""  # fall through to ask below

        if not rpc:
            rpc_val = questionary.text(
                "RPC URL (required for chain access):",
                default="https://rpc.ankr.com/eth",
                style=STYLE,
            ).ask()
            if not rpc_val or not rpc_val.strip():
                console.print(f"  [{C_WARN}]No RPC URL provided — chain disabled.[/{C_WARN}]")
                cfg.setdefault("chain", {})["enabled"] = False
                console.print(f"  [{C_OK}]+[/{C_OK}] Chain: disabled")
                return
            _write_env("RPC_URL", rpc_val.strip())
            console.print(f"  [{C_OK}]+[/{C_OK}] RPC URL saved")

    cfg.setdefault("chain", {})["enabled"] = new_val
    console.print(f"  [{C_OK}]+[/{C_OK}] Chain: {'enabled' if new_val else 'disabled'}")


def _section_skills(cfg: dict):
    """Section: Install, manage, and assign agent skills."""
    from core.skill_loader import SkillLoader

    actions = [
        questionary.Choice("List installed skills",            value="list"),
        questionary.Choice("Create new skill",                 value="create"),
        questionary.Choice("Install skill from path",          value="install"),
        questionary.Choice("Edit / reassign skill",            value="edit"),
        questionary.Choice("Regenerate team skill (_team.md)", value="regen"),
        questionary.Choice("Remove a skill",                   value="remove"),
    ]

    action = questionary.select(
        "What would you like to do?",
        choices=actions,
        style=STYLE,
    ).ask()

    if action is None:
        return

    loader = SkillLoader()

    # ── List installed skills ──
    if action == "list":
        inventory = loader.list_skills()
        shared = inventory.get("shared", [])
        agents_skills = inventory.get("agents", {})

        if not shared and not agents_skills:
            console.print(f"  [{C_DIM}]No skills installed.[/{C_DIM}]")
            return

        if shared:
            tbl = Table(title="Shared Skills", box=box.SIMPLE,
                        show_header=True, header_style=C_ACCENT)
            tbl.add_column("Name", style=C_AGENT)
            tbl.add_column("File", style=C_DIM)
            tbl.add_column("Description")
            tbl.add_column("Tags", style=C_DIM)
            for s in shared:
                tags = ", ".join(s.get("tags", [])) if s.get("tags") else ""
                tbl.add_row(s["name"], s["file"],
                           s.get("description", ""), tags)
            console.print(tbl)

        for agent_id, skills in agents_skills.items():
            console.print(f"\n  [{C_AGENT}]{agent_id}[/{C_AGENT}] private skills:")
            for s in skills:
                desc = f" — {s['description']}" if s.get("description") else ""
                console.print(f"    • {s['name']} ({s['file']}){desc}")

    # ── Create new skill ──
    elif action == "create":
        name = questionary.text(
            "Skill name (lowercase, no spaces):",
            style=STYLE,
        ).ask()
        if not name:
            return
        name = name.strip().replace(" ", "_").lower()

        desc = questionary.text(
            "Description (one line):",
            default="",
            style=STYLE,
        ).ask() or ""

        tags_raw = questionary.text(
            "Tags (comma-separated, e.g. coding,debug):",
            default="",
            style=STYLE,
        ).ask() or ""
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

        scope = questionary.select(
            "Scope:",
            choices=[
                questionary.Choice("Shared (available to all agents)", value="shared"),
                questionary.Choice("Private (single agent only)", value="private"),
            ],
            style=STYLE,
        ).ask()
        if scope is None:
            return

        if scope == "private":
            agents = cfg.get("agents", [])
            if not agents:
                console.print(f"  [{C_WARN}]No agents configured.[/{C_WARN}]")
                return
            agent_id = questionary.select(
                "Which agent?",
                choices=[a["id"] for a in agents],
                style=STYLE,
            ).ask()
            if not agent_id:
                return
            os.makedirs(os.path.join("skills", "agents", agent_id),
                        exist_ok=True)
            path = os.path.join("skills", "agents", agent_id, f"{name}.md")
        else:
            os.makedirs("skills", exist_ok=True)
            path = os.path.join("skills", f"{name}.md")

        body = questionary.text(
            "Skill instructions (the prompt content):",
            multiline=True,
            style=STYLE,
        ).ask() or ""

        # Build frontmatter (Claude Code compatible)
        lines = ["---"]
        lines.append(f"name: {name}")
        if desc:
            lines.append(f"description: {desc}")
        if tags:
            lines.append(f"tags: [{', '.join(tags)}]")
        lines.append("---")
        lines.append("")
        lines.append(body.strip())
        lines.append("")

        with open(path, "w") as f:
            f.write("\n".join(lines))

        console.print(f"  [{C_OK}]+[/{C_OK}] Created skill: {path}")

        # If shared, ask which agents to assign to
        if scope == "shared":
            agents = cfg.get("agents", [])
            if agents:
                assign = questionary.confirm(
                    "Assign this skill to agents now?",
                    default=True,
                    style=STYLE,
                ).ask()
                if assign:
                    agent_choices = [
                        questionary.Choice(a["id"], value=a["id"], checked=True)
                        for a in agents
                    ]
                    selected = questionary.checkbox(
                        f"Assign '{name}' to:",
                        choices=agent_choices,
                        style=STYLE,
                    ).ask()
                    if selected is not None:
                        for a in agents:
                            current = a.get("skills", [])
                            if a["id"] in selected and name not in current:
                                current.append(name)
                                a["skills"] = current
                        assigned = ", ".join(selected) if selected else "none"
                        console.print(f"  [{C_OK}]+[/{C_OK}] Assigned to: {assigned}")

    # ── Install skill from path ──
    elif action == "install":
        import shutil
        src = questionary.path(
            "Path to .md skill file:",
            style=STYLE,
        ).ask()
        if not src or not os.path.isfile(src):
            console.print(f"  [{C_WARN}]File not found: {src}[/{C_WARN}]")
            return

        # Read and display the skill content
        with open(src) as f:
            content = f.read()
        skill_name = os.path.basename(src).replace(".md", "")

        # Parse frontmatter for display
        desc_line = ""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().splitlines():
                    if line.startswith("name:"):
                        skill_name = line.split(":", 1)[1].strip()
                    elif line.startswith("description:"):
                        desc_line = line.split(":", 1)[1].strip()

        console.print(f"  [{C_DIM}]Name: {skill_name}[/{C_DIM}]")
        if desc_line:
            console.print(f"  [{C_DIM}]Description: {desc_line}[/{C_DIM}]")
        preview = content[:200].replace("\n", " ")
        console.print(f"  [{C_DIM}]Preview: {preview}...[/{C_DIM}]")
        console.print()

        # Choose scope: shared or assign to specific agent
        scope = questionary.select(
            "Install as:",
            choices=[
                questionary.Choice("Shared (available to all agents)", value="shared"),
                questionary.Choice("Private (single agent only)", value="private"),
            ],
            style=STYLE,
        ).ask()
        if scope is None:
            return

        if scope == "private":
            agents = cfg.get("agents", [])
            if not agents:
                console.print(f"  [{C_WARN}]No agents configured. Installing as shared.[/{C_WARN}]")
                scope = "shared"
            else:
                agent_id = questionary.select(
                    "Which agent?",
                    choices=[a["id"] for a in agents],
                    style=STYLE,
                ).ask()
                if not agent_id:
                    return
                dest_dir = os.path.join("skills", "agents", agent_id)
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, os.path.basename(src))
                shutil.copy2(src, dest)
                console.print(f"  [{C_OK}]+[/{C_OK}] Installed: {dest} (private to {agent_id})")

                # Add to agent's skills list
                agent = next(a for a in agents if a["id"] == agent_id)
                current = agent.get("skills", [])
                if skill_name not in current:
                    current.append(skill_name)
                    agent["skills"] = current
                    console.print(f"  [{C_OK}]+[/{C_OK}] Added '{skill_name}' to {agent_id}'s skill list")
                return

        # Shared install
        fname = os.path.basename(src)
        dest = os.path.join("skills", fname)
        os.makedirs("skills", exist_ok=True)
        shutil.copy2(src, dest)
        console.print(f"  [{C_OK}]+[/{C_OK}] Installed: {dest}")

        # Ask which agents to assign to
        agents = cfg.get("agents", [])
        if agents:
            assign = questionary.confirm(
                "Assign this skill to agents now?",
                default=True,
                style=STYLE,
            ).ask()
            if assign:
                agent_choices = [
                    questionary.Choice(a["id"], value=a["id"],
                                       checked=(skill_name in a.get("skills", [])))
                    for a in agents
                ]
                selected_agents = questionary.checkbox(
                    f"Assign '{skill_name}' to:",
                    choices=agent_choices,
                    style=STYLE,
                ).ask()
                if selected_agents is not None:
                    for a in agents:
                        current = a.get("skills", [])
                        if a["id"] in selected_agents:
                            if skill_name not in current:
                                current.append(skill_name)
                                a["skills"] = current
                        else:
                            if skill_name in current:
                                current.remove(skill_name)
                                a["skills"] = current
                    assigned = ", ".join(selected_agents) if selected_agents else "none"
                    console.print(f"  [{C_OK}]+[/{C_OK}] Assigned to: {assigned}")

    # ── Edit / reassign skill ──
    elif action == "edit":
        agents = cfg.get("agents", [])
        if not agents:
            console.print(f"  [{C_WARN}]No agents configured.[/{C_WARN}]")
            return

        # Collect all available shared skills
        all_skills = []
        if os.path.isdir("skills"):
            for fname in sorted(os.listdir("skills")):
                if fname.endswith(".md") and fname != "_team.md":
                    all_skills.append(fname.replace(".md", ""))

        if not all_skills:
            console.print(f"  [{C_DIM}]No shared skills found.[/{C_DIM}]")
            return

        # Pick which skill to edit
        skill_name = questionary.select(
            "Which skill to edit?",
            choices=all_skills,
            style=STYLE,
        ).ask()
        if not skill_name:
            return

        # Show current assignment
        assigned_to = [a["id"] for a in agents if skill_name in a.get("skills", [])]
        if assigned_to:
            console.print(f"  [{C_DIM}]Currently assigned to: {', '.join(assigned_to)}[/{C_DIM}]")
        else:
            console.print(f"  [{C_DIM}]Not assigned to any agent.[/{C_DIM}]")

        # Checkbox for agent assignment
        agent_choices = [
            questionary.Choice(a["id"], value=a["id"],
                               checked=(skill_name in a.get("skills", [])))
            for a in agents
        ]
        selected = questionary.checkbox(
            f"Assign '{skill_name}' to:",
            choices=agent_choices,
            style=STYLE,
        ).ask()
        if selected is not None:
            for a in agents:
                current = a.get("skills", [])
                if a["id"] in selected:
                    if skill_name not in current:
                        current.append(skill_name)
                        a["skills"] = current
                else:
                    if skill_name in current:
                        current.remove(skill_name)
                        a["skills"] = current
            assigned = ", ".join(selected) if selected else "none"
            console.print(f"  [{C_OK}]+[/{C_OK}] '{skill_name}' assigned to: {assigned}")

    # ── Regenerate team skill ──
    elif action == "regen":
        try:
            from core.team_skill import generate_team_skill
            content = generate_team_skill()
            if content:
                console.print(f"  [{C_OK}]+[/{C_OK}] Regenerated skills/_team.md "
                              f"({len(content)} chars)")
            else:
                console.print(f"  [{C_WARN}]No agents found — team skill not generated.[/{C_WARN}]")
        except Exception as e:
            console.print(f"  [{C_WARN}]Error: {e}[/{C_WARN}]")

    # ── Remove a skill ──
    elif action == "remove":
        inventory = loader.list_skills()
        all_files: list[tuple[str, str]] = []  # (display, path)

        for s in inventory.get("shared", []):
            fpath = os.path.join("skills", s["file"])
            all_files.append((f"[shared] {s['name']} ({s['file']})", fpath))

        for agent_id, skills in inventory.get("agents", {}).items():
            for s in skills:
                fpath = os.path.join("skills", "agents", agent_id, s["file"])
                all_files.append(
                    (f"[{agent_id}] {s['name']} ({s['file']})", fpath))

        if not all_files:
            console.print(f"  [{C_DIM}]No skills to remove.[/{C_DIM}]")
            return

        choices = [questionary.Choice(display, value=path)
                   for display, path in all_files]
        to_remove = questionary.select(
            "Select skill to remove:",
            choices=choices,
            style=STYLE,
        ).ask()

        if to_remove:
            confirm = questionary.confirm(
                f"Delete {to_remove}?",
                default=False,
                style=STYLE,
            ).ask()
            if confirm:
                try:
                    os.remove(to_remove)
                    console.print(f"  [{C_OK}]+[/{C_OK}] Removed: {to_remove}")
                except OSError as e:
                    console.print(f"  [{C_WARN}]Error: {e}[/{C_WARN}]")


def _skills_assign_to_agents(cfg: dict, skill_name: str = "",
                              target_agent: dict | None = None):
    """
    Helper: assign shared skills to an agent using checkbox.
    If skill_name is given, pre-check that skill for all agents.
    If target_agent is given, edit that specific agent's skill list.
    """
    if target_agent is not None:
        # Single-agent assignment: show all shared skills as checkboxes
        current_skills = target_agent.get("skills", [])
        all_skills = ["_base"]
        if os.path.isdir("skills"):
            for fname in sorted(os.listdir("skills")):
                if fname.endswith(".md") and fname != "_team.md":
                    sname = fname.replace(".md", "")
                    if sname not in all_skills:
                        all_skills.append(sname)
        for s in current_skills:
            if s not in all_skills:
                all_skills.append(s)

        skill_choices = [
            questionary.Choice(s, value=s, checked=(s in current_skills))
            for s in all_skills
        ]
        new_skills = questionary.checkbox(
            f"Skills for {target_agent['id']}:",
            choices=skill_choices,
            style=STYLE,
        ).ask()
        if new_skills is not None:
            target_agent["skills"] = new_skills
            console.print(f"  [{C_OK}]+[/{C_OK}] {target_agent['id']} ->"
                          f"{', '.join(new_skills)}")
    else:
        # Assign a specific skill to multiple agents
        agents = cfg.get("agents", [])
        for agent in agents:
            current = agent.get("skills", [])
            if skill_name not in current:
                add = questionary.confirm(
                    f"Add '{skill_name}' to {agent['id']}?",
                    default=True,
                    style=STYLE,
                ).ask()
                if add:
                    current.append(skill_name)
                    agent["skills"] = current
                    console.print(f"  [{C_OK}]+[/{C_OK}] Added to {agent['id']}")


def _section_skill_deps(cfg: dict):
    """Section: Check and install CLI tools required by skills."""
    _ask_skill_deps()


def _section_tools(cfg: dict):
    """Section: Configure built-in tools — OpenClaw-style tool configuration.

    Lets users:
    - Enable/disable tool groups (Web, Automation, Media, Filesystem)
    - Configure required API keys (e.g. BRAVE_API_KEY for web_search)
    - Set default tool profile for new agents
    - Assign tool profiles per agent
    """
    from core.tools import (
        list_all_tools, TOOL_PROFILES, TOOL_GROUPS,
        get_available_tools,
    )

    all_tools = list_all_tools()

    # ── Current status ──
    console.print(f"  [{C_DIM}]Built-in tools status:[/{C_DIM}]")
    for t in all_tools:
        avail = t.is_available()
        icon = f"[{C_OK}]✓[/{C_OK}]" if avail else f"[{C_WARN}]✗[/{C_WARN}]"
        env_note = ""
        if not avail and t.requires_env:
            env_note = f" [{C_DIM}](needs {', '.join(t.requires_env)})[/{C_DIM}]"
        console.print(f"    {icon} {t.name:<14s} [{C_DIM}]{t.group}[/{C_DIM}]{env_note}")
    console.print()

    # ── Action menu ──
    actions = [
        questionary.Choice("Enable web_search (Brave Search API)", value="brave"),
        questionary.Choice("Set default tool profile for agents",  value="profile"),
        questionary.Choice("Configure tools per agent",            value="per_agent"),
        questionary.Choice("Test a tool",                          value="test"),
    ]

    action = questionary.select(
        "What would you like to configure?",
        choices=actions,
        style=STYLE,
    ).ask()
    if action is None:
        return

    # ── Enable Brave Search ──
    if action == "brave":
        current = os.environ.get("BRAVE_API_KEY", "")
        if current:
            console.print(f"  [{C_OK}]+[/{C_OK}] BRAVE_API_KEY is set [{C_DIM}]({current[:8]}...)[/{C_DIM}]")
            change = questionary.confirm(
                "Update the key?", default=False, style=STYLE).ask()
            if not change:
                return

        console.print(f"  [{C_DIM}]Get a free key at https://brave.com/search/api/[/{C_DIM}]")
        key = questionary.text(
            "BRAVE_API_KEY:",
            default="",
            style=STYLE,
        ).ask()
        if key and key.strip():
            _write_env("BRAVE_API_KEY", key.strip())
            os.environ["BRAVE_API_KEY"] = key.strip()
            console.print(f"  [{C_OK}]+[/{C_OK}] web_search enabled (saved to .env)")
        else:
            console.print(f"  [{C_DIM}]Skipped — web_search remains disabled.[/{C_DIM}]")

    # ── Set default tool profile ──
    elif action == "profile":
        profiles_desc = {
            "minimal": "web_search + web_fetch only",
            "coding":  "web + exec + filesystem + process",
            "full":    "all tools (web, automation, media, filesystem)",
        }
        choices = [
            questionary.Choice(
                f"{name:<10s} — {desc}",
                value=name,
            )
            for name, desc in profiles_desc.items()
        ]
        selected = questionary.select(
            "Default tool profile for agents:",
            choices=choices,
            default="full",
            style=STYLE,
        ).ask()
        if selected is None:
            return

        # Write to global config
        cfg.setdefault("tools", {})["default_profile"] = selected
        console.print(f"  [{C_OK}]+[/{C_OK}] Default profile: {selected}")

        # Apply to all agents that don't have per-agent tools config
        agents = cfg.get("agents", [])
        applied = 0
        for agent in agents:
            if "tools" not in agent:
                agent["tools"] = {"profile": selected}
                applied += 1
            elif not agent.get("tools", {}).get("profile"):
                agent.setdefault("tools", {})["profile"] = selected
                applied += 1
        if applied:
            console.print(f"  [{C_DIM}]Applied to {applied} agent(s) without custom tools config.[/{C_DIM}]")

    # ── Per-agent tool configuration ──
    elif action == "per_agent":
        agents = cfg.get("agents", [])
        if not agents:
            console.print(f"  [{C_WARN}]No agents configured yet.[/{C_WARN}]")
            return

        agent_choices = [
            questionary.Choice(
                f"{a['id']:<16s} [{C_DIM}]tools: "
                f"{a.get('tools', {}).get('profile', 'default')}[/{C_DIM}]",
                value=a["id"],
            )
            for a in agents
        ]
        agent_id = questionary.select(
            "Which agent to configure?",
            choices=agent_choices,
            style=STYLE,
        ).ask()
        if agent_id is None:
            return

        agent = next((a for a in agents if a["id"] == agent_id), None)
        if not agent:
            return

        # Tool profile
        profiles_desc = {
            "minimal": "web_search + web_fetch only",
            "coding":  "web + exec + filesystem + process",
            "full":    "all tools",
        }
        current_profile = agent.get("tools", {}).get("profile", "full")
        profile = questionary.select(
            f"Tool profile for {agent_id}:",
            choices=[
                questionary.Choice(f"{n:<10s} — {d}", value=n)
                for n, d in profiles_desc.items()
            ],
            default=current_profile,
            style=STYLE,
        ).ask()
        if profile is None:
            return

        agent.setdefault("tools", {})["profile"] = profile

        # Additional deny list
        deny_groups = questionary.checkbox(
            "Deny access to (optional):",
            choices=[
                questionary.Choice(f"group:web — web_search, web_fetch",       value="group:web"),
                questionary.Choice(f"group:automation — exec, cron, process",  value="group:automation"),
                questionary.Choice(f"group:media — screenshot, notify",        value="group:media"),
                questionary.Choice(f"group:fs — read_file, write_file, list_dir", value="group:fs"),
            ],
            style=STYLE,
        ).ask()
        if deny_groups:
            agent["tools"]["deny"] = deny_groups
            console.print(f"  [{C_DIM}]Denied: {', '.join(deny_groups)}[/{C_DIM}]")

        console.print(f"  [{C_OK}]+[/{C_OK}] {agent_id}: profile={profile}")

    # ── Test a tool ──
    elif action == "test":
        test_choices = [
            questionary.Choice(f"{t.name:<14s} — {t.description[:50]}",
                               value=t.name)
            for t in all_tools if t.is_available()
        ]
        if not test_choices:
            console.print(f"  [{C_WARN}]No tools available. Configure API keys first.[/{C_WARN}]")
            return

        tool_name = questionary.select(
            "Select a tool to test:",
            choices=test_choices,
            style=STYLE,
        ).ask()
        if tool_name is None:
            return

        # Quick test based on tool
        from core.tools import get_tool
        tool = get_tool(tool_name)
        if not tool:
            return

        if tool_name == "web_search":
            query = questionary.text("Search query:", default="hello world",
                                     style=STYLE).ask()
            if query:
                result = tool.execute(query=query, count=3)
                if result.get("ok"):
                    for r in result.get("results", []):
                        console.print(f"  • {r['title']}")
                        console.print(f"    [{C_DIM}]{r['url']}[/{C_DIM}]")
                else:
                    console.print(f"  [{C_WARN}]{result.get('error', 'Failed')}[/{C_WARN}]")

        elif tool_name == "list_dir":
            result = tool.execute(path=".")
            if result.get("ok"):
                for e in result.get("entries", [])[:10]:
                    icon = "📁" if e["type"] == "dir" else "📄"
                    console.print(f"  {icon} {e['name']}")
                console.print(f"  [{C_DIM}]({result.get('total', 0)} items)[/{C_DIM}]")
            else:
                console.print(f"  [{C_WARN}]{result.get('error')}[/{C_WARN}]")

        elif tool_name == "notify":
            result = tool.execute(title="Cleo", message="Tool test successful!")
            if result.get("ok"):
                console.print(f"  [{C_OK}]+[/{C_OK}] Notification sent!")
            else:
                console.print(f"  [{C_WARN}]{result.get('error')}[/{C_WARN}]")

        elif tool_name == "screenshot":
            result = tool.execute()
            if result.get("ok"):
                console.print(f"  [{C_OK}]+[/{C_OK}] Screenshot saved: {result.get('path')}")
            else:
                console.print(f"  [{C_WARN}]{result.get('error')}[/{C_WARN}]")

        elif tool_name == "process":
            result = tool.execute()
            if result.get("ok"):
                procs = result.get("processes", [])[:5]
                for p in procs:
                    console.print(f"  [{C_DIM}]{p}[/{C_DIM}]")
                console.print(f"  [{C_DIM}]({result.get('total', 0)} total)[/{C_DIM}]")
            else:
                console.print(f"  [{C_WARN}]{result.get('error')}[/{C_WARN}]")

        else:
            console.print(f"  [{C_DIM}]Manual test not implemented for {tool_name}. "
                          f"Use the API: POST /v1/exec[/{C_DIM}]")


def _section_health(cfg: dict):
    """Section: Run health check."""
    from core.doctor import run_doctor
    console.print()
    results = run_doctor(rich_console=console)
    ok_count = sum(1 for ok, _, _ in results if ok)
    total = len(results)
    if ok_count == total:
        console.print(f"  [{C_OK}]+[/{C_OK}] All {total} checks passed.")
    else:
        console.print(f"  [{C_WARN}]{total - ok_count} of {total} checks need attention.[/{C_WARN}]")


# Handler mapping
_SECTION_HANDLERS = {
    "model":      _section_model,
    "agents":     _section_agents,
    "skills":     _section_skills,
    "skill_deps": _section_skill_deps,
    "tools":      _section_tools,
    "memory":     _section_memory,
    "resilience": _section_resilience,
    "compaction": _section_compaction,
    "gateway":    _section_gateway,
    "chain":      _section_chain,
    "health":     _section_health,
}


# ══════════════════════════════════════════════════════════════════════════════
#  GATEWAY & DAEMON SETUP  (Advanced wizard steps)
# ══════════════════════════════════════════════════════════════════════════════

def _ask_gateway() -> tuple[int, str]:
    """Ask about gateway configuration. Returns (port, token)."""
    from core.gateway import DEFAULT_PORT, generate_token

    enable = questionary.confirm(
        "Enable local HTTP gateway?",
        default=True,
        style=STYLE,
    ).ask()
    if not enable:
        return 0, ""

    port_str = questionary.text(
        "Gateway port:",
        default=str(DEFAULT_PORT),
        style=STYLE,
    ).ask()
    if port_str is None:
        return 0, ""
    try:
        port = int(port_str)
    except ValueError:
        port = DEFAULT_PORT

    token = generate_token()
    console.print(f"  [{C_OK}]+[/{C_OK}] Auth token: [{C_DIM}]{token[:20]}...[/{C_DIM}]")

    return port, token


def _ask_daemon(port: int, token: str):
    """Ask about background service installation."""
    import platform
    system = platform.system()

    if not port:
        console.print(f"  [{C_DIM}]Gateway not enabled — skipping daemon.[/{C_DIM}]")
        return

    svc_type = "LaunchAgent" if system == "Darwin" else "systemd service"
    install = questionary.confirm(
        f"Install as {svc_type} (auto-start on boot)?",
        default=False,
        style=STYLE,
    ).ask()

    if install:
        from core.daemon import install_daemon
        ok, msg = install_daemon(port, token)
        if ok:
            console.print(f"  [{C_OK}]+[/{C_OK}] {msg}")
        else:
            console.print(f"  [{C_WARN}]! {msg}[/{C_WARN}]")
    else:
        console.print(f"  [{C_DIM}]Skipped — run `cleo gateway` to start manually.[/{C_DIM}]")


# ══════════════════════════════════════════════════════════════════════════════
#  GATEWAY SUMMARY — OpenClaw-style post-setup display
# ══════════════════════════════════════════════════════════════════════════════

def _show_gateway_summary(provider: str, model: str):
    """Show gateway-style summary after quick setup."""
    info = PROVIDERS[provider]
    gateway_token = os.environ.get("CLEO_GATEWAY_TOKEN", "")
    gateway_port  = os.environ.get("CLEO_GATEWAY_PORT", "19789")
    token_line = f"  Token      [{C_DIM}]{gateway_token}[/{C_DIM}]\n" if gateway_token else ""

    console.print()
    console.print(Panel(
        f"[{C_ACCENT}]Cleo Gateway — Ready[/{C_ACCENT}]\n\n"
        f"  Provider   [{C_DIM}]{info['label']}[/{C_DIM}]\n"
        f"  Model      [{C_DIM}]{model}[/{C_DIM}]\n"
        f"  Agents     [{C_DIM}]leo, jerry, alic[/{C_DIM}]\n"
        f"  Memory     [{C_DIM}]mock (in-memory)[/{C_DIM}]\n"
        f"  Gateway    [{C_DIM}]http://127.0.0.1:{gateway_port}/[/{C_DIM}]\n"
        f"{token_line}"
        f"  Config     [{C_DIM}]{CONFIG_PATH}[/{C_DIM}]\n\n"
        f"  [{C_OK}]+[/{C_OK}] Type a task to get started!",
        border_style="magenta",
        box=box.ROUNDED,
    ))
    console.print()


def _show_gateway_summary_full(agents_cfg: list[dict], memory: str, chain: bool,
                                gateway_port: int = 0):
    """Show gateway-style summary after full wizard."""
    table = Table(box=box.SIMPLE, border_style="magenta")
    table.add_column("Agent", style=C_AGENT)
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Skills", style=C_DIM)
    for a in agents_cfg:
        table.add_row(
            a["id"],
            PROVIDERS[a["provider"]]["label"],
            a["model"],
            ", ".join(a["skills"]),
        )

    mem_label = "ChromaDB (vector store)" if memory == "chroma" else "Mock (in-memory)"
    chain_label = "ERC-8004 enabled" if chain else "disabled"
    gw_label = f"http://127.0.0.1:{gateway_port}" if gateway_port else "disabled"

    console.print()
    console.print(Panel(
        f"[{C_ACCENT}]Cleo Gateway — Ready[/{C_ACCENT}]",
        border_style="magenta",
        box=box.ROUNDED,
    ))
    console.print(table)
    gateway_token = os.environ.get("CLEO_GATEWAY_TOKEN", "")

    console.print(f"  Memory   [{C_DIM}]{mem_label}[/{C_DIM}]")
    console.print(f"  Chain    [{C_DIM}]{chain_label}[/{C_DIM}]")
    if gateway_port:
        console.print(f"  Gateway  [{C_DIM}]{gw_label}[/{C_DIM}]")
    if gateway_token:
        console.print(f"  Token    [{C_DIM}]{gateway_token}[/{C_DIM}]")
    console.print(f"  Config   [{C_DIM}]{CONFIG_PATH}[/{C_DIM}]")
    console.print(f"\n  [{C_OK}]+[/{C_OK}] Type a task to get started!\n")


# ══════════════════════════════════════════════════════════════════════════════
#  MODEL FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_models(provider: str, api_key: str) -> tuple[list[str], str]:
    """
    Fetch available models from the provider's /v1/models endpoint.
    Returns (list_of_model_ids, error_message).
    On success error_message is empty; on failure list is empty.
    """
    import httpx

    info = PROVIDERS[provider]
    base_url = os.environ.get(info["url_env"], "") if info["url_env"] else ""
    if not base_url:
        base_url = info.get("base_url", "")
    if not base_url:
        return [], "No base URL configured"

    # Determine the actual API key to use
    env_var = info["env"]
    actual_key = api_key or (os.environ.get(env_var, "") if env_var else "")

    if provider == "ollama":
        # Ollama uses /api/tags
        try:
            resp = httpx.get(f"{base_url}/api/tags", timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            return [m["name"] for m in data.get("models", [])], ""
        except httpx.ConnectError:
            return [], "Cannot connect to Ollama — is it running?"
        except httpx.HTTPStatusError as e:
            return [], f"Ollama returned {e.response.status_code} — is it running?"
        except Exception as e:
            return [], f"Ollama: {e}"

    # OpenAI-compatible /v1/models
    if not actual_key:
        return [], "No API key available"

    headers = {"Authorization": f"Bearer {actual_key}"}

    try:
        resp = httpx.get(f"{base_url}/models", headers=headers, timeout=15.0)
        if resp.status_code == 401:
            return [], "Invalid API key (401 Unauthorized)"
        resp.raise_for_status()
        data = resp.json()
        return [m["id"] for m in data.get("data", [])], ""
    except httpx.ConnectError:
        return [], f"Cannot connect to {base_url}"
    except httpx.TimeoutException:
        return [], f"Request timed out ({base_url})"
    except Exception as e:
        return [], str(e)


def _ask_model(provider: str, api_key: str) -> str | None:
    """Let user select a model: fetch from API or manual input."""
    default_model = PROVIDERS[provider]["model"]

    # Try to fetch models from the API
    console.print(f"  [{C_DIM}]Fetching models...[/{C_DIM}]", end="")
    models, err = _fetch_models(provider, api_key)

    if models:
        console.print(f"\r  [{C_OK}]+[/{C_OK}] {len(models)} models available    ")

        # Build choices: fetched models + manual input
        choices = [questionary.Choice(m, value=m) for m in models]
        choices.append(questionary.Separator())
        choices.append(questionary.Choice("Enter manually...", value="__manual__"))

        # Set default to the provider's default if it's in the list
        default_val = default_model if default_model in models else models[0]

        selection = questionary.select(
            "Model:",
            choices=choices,
            default=default_val,
            style=STYLE,
        ).ask()
        if selection is None:
            return None
        if selection != "__manual__":
            return selection

    else:
        if err:
            console.print(f"\r  [{C_WARN}]! {err}[/{C_WARN}]" + " " * 20)
        else:
            console.print(f"\r  [{C_DIM}]Could not fetch models[/{C_DIM}]" + " " * 20)

    # Manual input fallback
    model = questionary.text(
        "Model name:",
        default=default_model,
        style=STYLE,
    ).ask()
    return model


# ══════════════════════════════════════════════════════════════════════════════
#  MEMORY CHECK
# ══════════════════════════════════════════════════════════════════════════════

def _check_chromadb() -> bool:
    """Check if chromadb is installed and loadable."""
    import importlib
    importlib.invalidate_caches()
    try:
        import chromadb  # noqa: F401
        return True
    except (ImportError, Exception):
        return False


def _ask_memory() -> str | None:
    """Ask for memory backend, check chromadb availability."""
    has_chroma = _check_chromadb()

    chroma_tag = "  [ok]" if has_chroma else "  [not installed]"

    choice = questionary.select(
        "Memory backend:",
        choices=[
            questionary.Choice("Mock (in-memory, no persistence)", value="mock"),
            questionary.Choice(f"ChromaDB (vector store){chroma_tag}", value="chroma"),
            questionary.Choice(f"Hybrid (Vector + BM25 keyword search){chroma_tag}", value="hybrid"),
        ],
        default="mock",
        style=STYLE,
    ).ask()
    if choice is None:
        return None

    if choice in ("chroma", "hybrid") and not has_chroma:
        install = questionary.confirm(
            "ChromaDB is not installed. Install now? (pip3 install chromadb)",
            default=True,
            style=STYLE,
        ).ask()
        if install:
            console.print(f"  [{C_DIM}]Installing chromadb...[/{C_DIM}]")
            import subprocess
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "chromadb"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                console.print(f"  [{C_OK}]+[/{C_OK}] ChromaDB installed")
            else:
                console.print(f"  [{C_WARN}]Install failed.[/{C_WARN}]")
                if choice == "hybrid":
                    console.print(f"  [{C_DIM}]Hybrid will use BM25 only (no vector search).[/{C_DIM}]")
                    return choice  # hybrid still works with BM25 only
                console.print(f"  [{C_DIM}]Falling back to mock.[/{C_DIM}]")
                return "mock"
        else:
            if choice == "hybrid":
                console.print(f"  [{C_DIM}]Hybrid will use BM25 only.[/{C_DIM}]")
                return choice
            console.print(f"  [{C_DIM}]Using mock memory instead.[/{C_DIM}]")
            return "mock"

    return choice


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _show_risk_notice():
    """OpenClaw pattern: show risk acknowledgment on first run."""
    console.print(Panel(
        f"[{C_ACCENT}]Welcome to Cleo Agent Stack[/{C_ACCENT}]\n\n"
        f"  Cleo is a multi-agent orchestration system.\n"
        f"  Agents will call LLM APIs on your behalf and\n"
        f"  may incur usage costs depending on your provider.\n\n"
        f"  [{C_DIM}]• API calls are billed by your LLM provider[/{C_DIM}]\n"
        f"  [{C_DIM}]• Use Ollama (free, local) to avoid costs[/{C_DIM}]\n"
        f"  [{C_DIM}]• Set a budget: POST /v1/budget or /budget[/{C_DIM}]",
        border_style="magenta",
        box=box.ROUNDED,
        title=f"[{C_WARN}]Notice[/{C_WARN}]",
    ))
    proceed = questionary.confirm(
        "I understand. Continue setup?",
        default=True,
        style=STYLE,
    ).ask()
    if not proceed:
        console.print(f"  [{C_DIM}]Setup cancelled.[/{C_DIM}]")
        raise KeyboardInterrupt  # exits cleanly


def _detect_existing_config() -> str:
    """
    Check for existing config. OpenClaw pattern: Keep / Modify / Reset.
    Returns: 'keep', 'modify', 'reset', 'abort'
    """
    if not os.path.exists(CONFIG_PATH):
        return "modify"

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}

    agents = cfg.get("agents", [])
    if not agents:
        return "modify"

    # ── Show current config summary (OpenClaw-style) ──
    console.print(Panel(
        f"[{C_ACCENT}]Existing config detected[/{C_ACCENT}]",
        border_style="magenta",
        box=box.ROUNDED,
    ))

    global_provider = cfg.get("llm", {}).get("provider", "?")
    memory_backend = cfg.get("memory", {}).get("backend", "mock")
    chain_enabled = cfg.get("chain", {}).get("enabled", False)
    resilience = cfg.get("resilience", {})
    compaction = cfg.get("compaction", {})

    # Build a summary block
    lines = []
    for a in agents:
        llm = a.get("llm", {})
        p = llm.get("provider", global_provider)
        fb = a.get("fallback_models", [])
        fb_str = f" ->{', '.join(fb)}" if fb else ""
        lines.append(f"    [{C_AGENT}]{a['id']:10}[/{C_AGENT}] [{C_DIM}]{p}/{a.get('model', '?')}{fb_str}[/{C_DIM}]")

    # Gateway token
    gateway_token = os.environ.get("CLEO_GATEWAY_TOKEN", "")
    gateway_port  = os.environ.get("CLEO_GATEWAY_PORT", "19789")

    console.print()
    console.print(f"  [{C_DIM}]provider:[/{C_DIM}]      {global_provider}")
    console.print(f"  [{C_DIM}]memory:[/{C_DIM}]        {memory_backend}")
    console.print(f"  [{C_DIM}]chain:[/{C_DIM}]         {'enabled' if chain_enabled else 'disabled'}")
    if resilience:
        console.print(f"  [{C_DIM}]resilience:[/{C_DIM}]    retry {resilience.get('max_retries', 3)}x, "
                      f"CB threshold {resilience.get('circuit_breaker_threshold', 3)}")
    if compaction and compaction.get("enabled"):
        console.print(f"  [{C_DIM}]compaction:[/{C_DIM}]    {compaction.get('max_context_tokens', 8000)} tokens max")
    console.print(f"  [{C_DIM}]gateway:[/{C_DIM}]       http://127.0.0.1:{gateway_port}/")
    if gateway_token:
        console.print(f"  [{C_DIM}]token:[/{C_DIM}]         {gateway_token}")
    else:
        console.print(f"  [{C_DIM}]token:[/{C_DIM}]         [{C_WARN}]not set[/{C_WARN}]")
    console.print()
    console.print(f"  [{C_DIM}]agents:[/{C_DIM}]")
    for line in lines:
        console.print(line)
    console.print()
    console.print()

    choice = questionary.select(
        "What would you like to do?",
        choices=[
            questionary.Choice("Modify sections (choose what to change)", value="sections"),
            questionary.Choice("Keep current config", value="keep"),
            questionary.Choice("Reset (delete and start fresh)", value="reset"),
        ],
        default="sections",
        style=STYLE,
    ).ask()

    if choice is None or choice == "keep":
        return "keep"
    elif choice == "sections":
        return "sections"
    elif choice == "reset":
        os.remove(CONFIG_PATH)
        if os.path.exists(ENV_PATH):
            remove_env = questionary.confirm(
                "Also remove .env?", default=False, style=STYLE
            ).ask()
            if remove_env:
                os.remove(ENV_PATH)
        console.print(f"  [{C_DIM}]Config cleared.[/{C_DIM}]\n")
        return "modify"
    else:
        return "modify"


def _detect_ollama_running() -> bool:
    """Probe localhost:11434 to check if Ollama is running."""
    try:
        import httpx
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def _detect_provider_from_env() -> str | None:
    """Auto-detect provider from existing environment variables or running services."""
    for name, info in PROVIDERS.items():
        env_var = info["env"]
        if env_var and os.environ.get(env_var):
            return name
    # If no API key set but Ollama is running locally, suggest it
    if _detect_ollama_running():
        return "ollama"
    return None


def _ask_provider() -> str | None:
    """Interactive provider selection with arrow-key menu."""
    choices = []
    detected = _detect_provider_from_env()
    ollama_running = _detect_ollama_running()

    for key, info in PROVIDERS.items():
        label = info["label"]
        env_var = info["env"]
        if env_var and os.environ.get(env_var):
            label += "  [key detected]"
        elif key == "ollama" and ollama_running:
            label += "  [running locally]"
        choices.append(questionary.Choice(label, value=key))

    default_val = detected if detected else "flock"

    provider = questionary.select(
        "LLM Provider:",
        choices=choices,
        default=default_val,
        style=STYLE,
    ).ask()

    return provider


def _ensure_api_key(provider: str) -> str | None:
    """
    Make sure API key is available. Returns the key value (or empty to keep existing).
    Also writes the key to os.environ and .env immediately so subsequent
    steps (model fetching, etc.) can use it right away.
    """
    info = PROVIDERS[provider]
    env_var = info["env"]

    if not env_var:  # ollama
        return ""

    existing = os.environ.get(env_var, "")
    if existing:
        masked = existing[:6] + "..." + existing[-4:] if len(existing) > 12 else "***"
        console.print(f"  [{C_OK}]+[/{C_OK}] {env_var} = {masked}")

        action = questionary.select(
            "API Key:",
            choices=[
                questionary.Choice(f"Keep current ({masked})", value="keep"),
                questionary.Choice("Enter new key", value="new"),
            ],
            default="keep",
            style=STYLE,
        ).ask()
        if action is None:
            return None
        if action == "keep":
            return ""

        key = questionary.password("New API Key:", style=STYLE).ask()
        if key:
            # Write immediately so model fetching can use it
            _write_env(env_var, key)
        return key

    console.print(f"  [{C_DIM}]({env_var} not found in environment)[/{C_DIM}]")
    key = questionary.password("API Key:", style=STYLE).ask()
    if key:
        # Write immediately so model fetching can use it
        _write_env(env_var, key)
    return key


def _write_env(env_var: str, value: str):
    """Write a key to .env — update if exists, append if new."""
    if not env_var or not value:
        return

    lines: list[str] = []
    found = False

    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                raw = line.strip()
                if raw and not raw.startswith("#") and "=" in raw:
                    key = raw.partition("=")[0].strip()
                    if key == env_var:
                        lines.append(f"{env_var}={value}\n")
                        found = True
                        continue
                lines.append(line)

    if not found:
        lines.append(f"{env_var}={value}\n")

    tmp_path = ENV_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        f.writelines(lines)
    os.replace(tmp_path, ENV_PATH)

    os.environ[env_var] = value


def _build_agent_entry(agent_id: str, role: str, model: str,
                       skills: list[str], provider: str,
                       api_key_env: str = "",
                       base_url_env: str = "") -> dict:
    """Build one agent entry for agents.yaml."""
    info = PROVIDERS[provider]
    entry = {
        "id": agent_id,
        "role": role,
        "model": model,
        "skills": skills,
        "memory": {"short_term_turns": 20, "long_term": True, "recall_top_k": 3},
        "autonomy_level": 1,
        "llm": {"provider": provider},
    }
    # Use agent-specific env var if provided, else provider default
    key_env = api_key_env or info["env"]
    url_env = base_url_env or info["url_env"]
    if key_env:
        entry["llm"]["api_key_env"] = key_env
    if url_env:
        entry["llm"]["base_url_env"] = url_env
    return entry


def _write_config_quick(provider: str, model: str, api_key: str):
    """Write quick-setup config (3-agent default team)."""
    _write_env(PROVIDERS[provider]["env"], api_key)

    config = {
        "llm": {"provider": provider},
        "memory": {"backend": "mock"},
        "chain": {"enabled": False},
        "reputation": {
            "peer_review_agents": ["alic"],
            "evolution": {
                "prompt_auto_apply": True,
                "model_swap_require_confirm": True,
                "role_vote_threshold": 0.6,
            },
        },
        "max_idle_cycles": 30,
        "agents": [
            _build_agent_entry(name, p["role"], model, list(p["skills"]), provider)
            for name, p in PRESETS.items()
        ],
    }

    os.makedirs("config", exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        f.write("# config/agents.yaml\n\n")
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # Auto-generate team skill after config save
    try:
        from core.team_skill import generate_team_skill
        generate_team_skill()
    except Exception:
        pass


def _write_config_full(agents_cfg: list[dict], memory: str, chain: bool):
    """Write full-wizard config with per-agent LLM settings."""
    # Write all API keys to .env
    seen_envs: set[str] = set()
    for a in agents_cfg:
        provider = a["provider"]
        api_key = a.get("api_key", "")
        env_var = PROVIDERS[provider]["env"]
        if env_var and env_var not in seen_envs:
            _write_env(env_var, api_key)
            seen_envs.add(env_var)

    # Determine global provider from first agent
    global_provider = agents_cfg[0]["provider"] if agents_cfg else "flock"
    reviewer_ids = [a["id"] for a in agents_cfg if "review" in a.get("role", "").lower()]

    config = {
        "llm": {"provider": global_provider},
        "memory": {"backend": memory},
        "chain": {"enabled": chain},
        "reputation": {
            "peer_review_agents": reviewer_ids or [agents_cfg[-1]["id"]],
            "evolution": {
                "prompt_auto_apply": True,
                "model_swap_require_confirm": True,
                "role_vote_threshold": 0.6,
            },
        },
        "max_idle_cycles": 30,
        "agents": [
            _build_agent_entry(
                a["id"], a["role"], a["model"], a["skills"], a["provider"],
            )
            for a in agents_cfg
        ],
    }

    os.makedirs("config", exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        f.write("# config/agents.yaml\n\n")
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # Auto-generate team skill after config save
    try:
        from core.team_skill import generate_team_skill
        generate_team_skill()
    except Exception:
        pass
