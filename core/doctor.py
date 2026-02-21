"""
core/doctor.py
System health check — validates config, dependencies, LLM connectivity, gateway.

Used by:
  - `cleo doctor` CLI command
  - Post-setup health check in wizard
  - `/doctor` chat command
"""

from __future__ import annotations

import importlib
import os
import sys

import yaml


# ══════════════════════════════════════════════════════════════════════════════
#  PREFLIGHT — fast startup checks with fix suggestions
# ══════════════════════════════════════════════════════════════════════════════

def run_preflight() -> list[str]:
    """
    Quick pre-flight check before entering chat mode.
    Returns list of human-readable issue descriptions (empty = all good).
    Checks: API key configured, LLM reachable, gateway port free.
    """
    from core.i18n import t as _t
    issues: list[str] = []

    # 1. API key configured?
    if not os.path.exists("config/agents.yaml"):
        issues.append(_t("preflight.api_key_empty"))
        return issues  # no point checking further

    with open("config/agents.yaml") as f:
        cfg = yaml.safe_load(f) or {}

    provider = cfg.get("llm", {}).get("provider", "flock")
    key_map = {"flock": "FLOCK_API_KEY", "openai": "OPENAI_API_KEY", "ollama": None}
    env_var = key_map.get(provider)

    if env_var and not os.environ.get(env_var):
        issues.append(_t("preflight.api_key_empty"))

    # 2. LLM endpoint reachable? (fast probe, 3s timeout)
    url_map = {
        "flock": os.environ.get("FLOCK_BASE_URL", "https://api.flock.io/v1"),
        "openai": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "ollama": os.environ.get("OLLAMA_URL", "http://localhost:11434"),
    }
    base_url = url_map.get(provider, "")
    if base_url:
        try:
            import httpx
            probe_url = f"{base_url}/api/tags" if provider == "ollama" else f"{base_url}/models"
            headers = {}
            if env_var:
                key = os.environ.get(env_var, "")
                if key:
                    headers["Authorization"] = f"Bearer {key}"
            resp = httpx.get(probe_url, headers=headers, timeout=3.0)
            if resp.status_code == 401:
                issues.append(_t("preflight.api_key_empty"))
            elif resp.status_code >= 400:
                issues.append(_t("preflight.llm_unreachable", url=base_url))
        except Exception:
            issues.append(_t("preflight.llm_unreachable", url=base_url))

    # 3. Gateway port free?
    import socket
    gw_port = int(os.environ.get("CLEO_GATEWAY_PORT", "19789"))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            result = s.connect_ex(("127.0.0.1", gw_port))
            if result == 0:
                # Port is in use — only warn if it's not our gateway
                try:
                    resp = httpx.get(f"http://127.0.0.1:{gw_port}/health", timeout=1.0)
                    if resp.status_code != 200 or "cleo" not in resp.text.lower():
                        issues.append(_t("preflight.port_in_use", port=gw_port))
                except Exception:
                    issues.append(_t("preflight.port_in_use", port=gw_port))
    except Exception:
        pass

    return issues


# ══════════════════════════════════════════════════════════════════════════════
#  CHECK FUNCTIONS — each returns (ok: bool, label: str, detail: str, hint: str)
#  hint is a human-readable fix suggestion (empty string if ok)
# ══════════════════════════════════════════════════════════════════════════════

def check_config() -> tuple[bool, str, str]:
    """Check if agents.yaml exists and is valid."""
    path = "config/agents.yaml"
    if not os.path.exists(path):
        return False, "Config", "config/agents.yaml not found — run: cleo onboard"
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        agents = cfg.get("agents", [])
        if not agents:
            return False, "Config", "No agents defined in config"
        return True, "Config", f"{len(agents)} agents ({', '.join(a['id'] for a in agents)})"
    except Exception as e:
        return False, "Config", f"Parse error: {e}"


def check_env() -> tuple[bool, str, str]:
    """Check if .env exists."""
    if not os.path.exists(".env"):
        return False, ".env", "Not found — API keys may be missing"
    return True, ".env", "Found"


def check_api_key() -> tuple[bool, str, str]:
    """Check if the primary API key is set."""
    # Read config to find which provider
    if not os.path.exists("config/agents.yaml"):
        return False, "API Key", "No config — cannot determine provider"

    with open("config/agents.yaml") as f:
        cfg = yaml.safe_load(f) or {}

    provider = cfg.get("llm", {}).get("provider", "flock")
    key_map = {
        "flock": "FLOCK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "ollama": None,
    }
    env_var = key_map.get(provider)
    if env_var is None:
        return True, "API Key", f"Not needed ({provider})"

    val = os.environ.get(env_var, "")
    if val:
        masked = val[:6] + "..." + val[-4:] if len(val) > 12 else "***"
        return True, "API Key", f"{env_var} = {masked}"
    return False, "API Key", f"{env_var} not set"


def check_llm_reachable() -> tuple[bool, str, str]:
    """Test LLM endpoint connectivity (light GET to /models)."""
    if not os.path.exists("config/agents.yaml"):
        return False, "LLM", "No config"

    with open("config/agents.yaml") as f:
        cfg = yaml.safe_load(f) or {}

    provider = cfg.get("llm", {}).get("provider", "flock")
    url_map = {
        "flock": ("https://api.flock.io/v1", "FLOCK_API_KEY"),
        "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
        "ollama": ("http://localhost:11434", None),
        "minimax": ("https://api.minimax.io/v1", "MINIMAX_API_KEY"),
    }

    base_url, key_env = url_map.get(provider, ("", None))
    if not base_url:
        return False, "LLM", f"Unknown provider: {provider}"

    # Check custom base URL from env
    url_env_map = {"flock": "FLOCK_BASE_URL", "openai": "OPENAI_BASE_URL", "ollama": "OLLAMA_URL", "minimax": "MINIMAX_BASE_URL"}
    custom_url = os.environ.get(url_env_map.get(provider, ""), "")
    if custom_url:
        base_url = custom_url

    try:
        import httpx
    except ImportError:
        return False, "LLM", "httpx not installed"

    try:
        headers = {}
        if key_env:
            api_key = os.environ.get(key_env, "")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        if provider == "ollama":
            resp = httpx.get(f"{base_url}/api/tags", timeout=5.0)
        elif provider == "minimax":
            # Minimax doesn't expose /models — verify key via lightweight POST
            resp = httpx.post(f"{base_url}/chat/completions", headers=headers,
                              json={"model": "minimax-m2.5", "messages": [{"role": "user", "content": "ping"}],
                                    "max_tokens": 1}, timeout=10.0)
        else:
            resp = httpx.get(f"{base_url}/models", headers=headers, timeout=10.0)

        if resp.status_code == 200:
            if provider == "minimax":
                return True, "LLM", f"{base_url} — Minimax API reachable"
            elif provider == "ollama":
                models = resp.json().get("models", [])
            else:
                models = resp.json().get("data", [])
            return True, "LLM", f"{base_url} — {len(models)} models available"
        elif resp.status_code == 401:
            return False, "LLM", f"{base_url} — Invalid API key (401)"
        else:
            return False, "LLM", f"{base_url} — HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, "LLM", f"Cannot connect to {base_url}"
    except httpx.TimeoutException:
        return False, "LLM", f"Timeout connecting to {base_url}"
    except Exception as e:
        return False, "LLM", str(e)


def check_dependencies() -> tuple[bool, str, str]:
    """Check required and optional Python dependencies."""
    required = ["rich", "questionary", "httpx", "yaml", "filelock"]
    optional = {"chromadb": "Vector memory", "web3": "ERC-8004 chain",
                "lit_python_sdk": "Lit PKP"}

    # Refresh import finder caches so newly pip-installed packages are visible
    importlib.invalidate_caches()

    missing_req = []
    for mod in required:
        mod_name = "pyyaml" if mod == "yaml" else mod
        try:
            importlib.import_module(mod)
        except ImportError:
            missing_req.append(mod_name)

    if missing_req:
        return False, "Dependencies", f"Missing: {', '.join(missing_req)}"

    # Check optional
    opt_status = []
    for mod, label in optional.items():
        try:
            importlib.import_module(mod)
            opt_status.append(f"{label} ok")
        except ImportError:
            opt_status.append(f"{label} missing")

    return True, "Dependencies", f"All required OK  ({', '.join(opt_status)})"


def check_memory_backend() -> tuple[bool, str, str]:
    """Check configured memory backend."""
    if not os.path.exists("config/agents.yaml"):
        return False, "Memory", "No config"

    with open("config/agents.yaml") as f:
        cfg = yaml.safe_load(f) or {}

    backend = cfg.get("memory", {}).get("backend", "mock")

    if backend == "mock":
        return True, "Memory", "Mock (in-memory, no persistence)"

    importlib.invalidate_caches()

    if backend == "chroma":
        try:
            import chromadb  # noqa: F401
            return True, "Memory", "ChromaDB [ok]"
        except (ImportError, Exception):
            return False, "Memory", "ChromaDB configured but not loadable -- pip3 install chromadb"

    if backend == "hybrid":
        try:
            import chromadb  # noqa: F401
            return True, "Memory", "Hybrid (Vector + BM25) [ok]"
        except (ImportError, Exception):
            return True, "Memory", "Hybrid (BM25 only -- install chromadb for vector search)"

    return True, "Memory", f"{backend}"


def check_resilience() -> tuple[bool, str, str]:
    """Check resilience configuration (retry, circuit breaker, failover)."""
    if not os.path.exists("config/agents.yaml"):
        return False, "Resilience", "No config"

    with open("config/agents.yaml") as f:
        cfg = yaml.safe_load(f) or {}

    res = cfg.get("resilience", {})
    if not res:
        return True, "Resilience", "Default settings (3 retries, 3-fail circuit breaker)"

    max_retries = res.get("max_retries", 3)
    cb_thresh   = res.get("circuit_breaker_threshold", 3)

    # Count agents with fallback models
    agents = cfg.get("agents", [])
    with_fallback = sum(1 for a in agents if a.get("fallback_models"))

    return True, "Resilience", (
        f"Retry {max_retries}x, CB threshold {cb_thresh}, "
        f"{with_fallback}/{len(agents)} agents have fallback models"
    )


def check_gateway() -> tuple[bool, str, str]:
    """Check if the gateway is running."""
    from core.gateway import check_gateway as _check, DEFAULT_PORT

    port = int(os.environ.get("CLEO_GATEWAY_PORT", str(DEFAULT_PORT)))
    ok, msg = _check(port)
    if ok:
        return True, "Gateway", f"http://127.0.0.1:{port} — {msg}"
    return False, "Gateway", f"Not running on port {port}"


def check_chain() -> tuple[bool, str, str]:
    """Check on-chain identity config (web3, Lit, ERC-8004, PKP, balances)."""
    if not os.path.exists("config/agents.yaml"):
        return False, "Chain", "No config"

    with open("config/agents.yaml") as f:
        cfg = yaml.safe_load(f) or {}

    chain_cfg = cfg.get("chain", {})
    enabled = chain_cfg.get("enabled", False)
    if not enabled:
        return True, "Chain", "Disabled"

    network = chain_cfg.get("network", "base-sepolia")
    parts = [f"net={network}"]
    warnings = []

    # Official ERC-8004 contract addresses for validation
    OFFICIAL_IDENTITY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
    OFFICIAL_REPUTATION = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"

    # Check web3
    try:
        import web3  # noqa: F401
        parts.append("web3 ok")
    except ImportError:
        return False, "Chain", "Enabled but web3 not installed — pip3 install web3"

    # Check RPC URL
    rpc_env = chain_cfg.get("rpc_url_env", "RPC_URL")
    rpc = os.environ.get(rpc_env, "") or os.environ.get("BASE_RPC_URL", "")
    if rpc:
        parts.append("RPC ok")
    else:
        warnings.append("RPC_URL not set")

    # Check operator key
    key_env = chain_cfg.get("operator_key_env", "CHAIN_PRIVATE_KEY")
    if os.environ.get(key_env):
        parts.append("Key ok")
    else:
        warnings.append("No operator key (read-only mode)")

    # Check Lit SDK
    try:
        import lit_python_sdk  # noqa: F401
        lit_net = chain_cfg.get("lit", {}).get("network", "naga-dev")
        parts.append(f"Lit({lit_net}) ok")
    except ImportError:
        warnings.append("lit-python-sdk not installed")

    # Check ERC-8004 registries + validate addresses for mainnet
    erc_cfg = chain_cfg.get("erc8004", {})
    id_reg = os.environ.get(erc_cfg.get("identity_registry_env", ""), "")
    rep_reg = os.environ.get(erc_cfg.get("reputation_registry_env", ""), "")
    if id_reg:
        if network == "base" and id_reg.lower() != OFFICIAL_IDENTITY.lower():
            warnings.append(f"Identity addr mismatch for mainnet (got {id_reg[:10]}...)")
        else:
            parts.append("Identity ok")
    else:
        warnings.append("ERC8004_IDENTITY_REGISTRY not set")
    if rep_reg:
        if network == "base" and rep_reg.lower() != OFFICIAL_REPUTATION.lower():
            warnings.append(f"Reputation addr mismatch for mainnet (got {rep_reg[:10]}...)")
        else:
            parts.append("Reputation ok")
    else:
        warnings.append("ERC8004_REPUTATION_REGISTRY not set")

    # Check chain_state for registered agents
    try:
        from adapters.chain.chain_state import ChainState
        state = ChainState()
        agents = state.list_agents()
        registered = sum(1 for a in agents.values() if a.get("registered"))
        if agents:
            parts.append(f"{registered}/{len(agents)} registered")
    except Exception:
        pass

    # Check Safe
    safe_addr = os.environ.get(chain_cfg.get("safe", {}).get("address_env", ""), "")
    if safe_addr:
        parts.append("Safe ok")

    # x402
    if chain_cfg.get("x402", {}).get("enabled"):
        parts.append("x402 ok")

    detail = "  ".join(parts)
    if warnings:
        detail += "  [" + ", ".join(warnings) + "]"

    ok = len(warnings) <= 1  # Allow minor warnings
    return ok, "Chain", detail


# ══════════════════════════════════════════════════════════════════════════════
#  RUNNER
# ══════════════════════════════════════════════════════════════════════════════

ALL_CHECKS = [
    check_config,
    check_env,
    check_api_key,
    check_llm_reachable,
    check_dependencies,
    check_memory_backend,
    check_resilience,
    check_gateway,
    check_chain,
]


def run_doctor(checks: list | None = None, rich_console=None) -> list[tuple[bool, str, str]]:
    """
    Run all health checks.
    Returns list of (ok, label, detail).
    If rich_console is provided, prints results with rich formatting.
    """
    checks = checks or ALL_CHECKS
    results = []

    for check_fn in checks:
        try:
            ok, label, detail = check_fn()
        except Exception as e:
            ok, label, detail = False, check_fn.__name__, f"Error: {e}"
        results.append((ok, label, detail))

    if rich_console:
        _print_rich(rich_console, results)

    return results


def run_doctor_quick(rich_console=None) -> list[tuple[bool, str, str]]:
    """
    Quick post-setup health check (skip gateway/chain).
    """
    quick_checks = [
        check_config,
        check_env,
        check_api_key,
        check_llm_reachable,
        check_dependencies,
        check_memory_backend,
    ]
    return run_doctor(quick_checks, rich_console)


# ══════════════════════════════════════════════════════════════════════════════
#  REPAIR MODE — OpenClaw-inspired auto-fix (cleo doctor --repair)
# ══════════════════════════════════════════════════════════════════════════════

def run_doctor_repair(rich_console=None) -> list[tuple[bool, str, str]]:
    """
    Run all checks, then auto-fix what we can.
    Repairs: missing .env, missing dirs, missing optional deps, stale tasks.
    """
    results = run_doctor(rich_console=rich_console)
    if rich_console:
        from core.i18n import t as _t
        rich_console.print(f"\n  [bold magenta]── Auto-Repair ──[/bold magenta]\n")

    repaired = 0

    # Fix 1: Create .env if missing
    if not os.path.exists(".env"):
        if os.path.exists(".env.example"):
            import shutil
            shutil.copy(".env.example", ".env")
            if rich_console:
                rich_console.print("  [green]+[/green] Created .env from .env.example")
            repaired += 1
        else:
            with open(".env", "w") as f:
                f.write("# Cleo Agent Stack — Environment\n# Add your API key below:\n# FLOCK_API_KEY=\n")
            if rich_console:
                rich_console.print("  [green]+[/green] Created empty .env")
            repaired += 1

    # Fix 2: Create required directories
    for d in ["config", ".logs", "memory", "workflows", "skills"]:
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            if rich_console:
                rich_console.print(f"  [green]+[/green] Created directory: {d}/")
            repaired += 1

    # Fix 3: Recover stale tasks (stuck in claimed/review)
    if os.path.exists(".task_board.json"):
        try:
            import json
            with open(".task_board.json") as f:
                data = json.load(f)
            stale_count = 0
            now = __import__("time").time()
            for tid, t in data.items():
                if t.get("status") == "claimed" and (now - t.get("claimed_at", now)) > 300:
                    t["status"] = "pending"
                    t["agent_id"] = None
                    stale_count += 1
                elif t.get("status") == "review" and (now - t.get("claimed_at", now)) > 180:
                    t["status"] = "pending"
                    t["agent_id"] = None
                    stale_count += 1
            if stale_count:
                with open(".task_board.json", "w") as f:
                    json.dump(data, f, indent=2)
                if rich_console:
                    rich_console.print(f"  [green]+[/green] Recovered {stale_count} stale task(s)")
                repaired += 1
        except Exception:
            pass

    # Fix 4: Auto-install missing optional packages
    fixable = _detect_fixable(results)
    if fixable and rich_console:
        _offer_auto_fix(rich_console, fixable)
        repaired += len(fixable)

    if rich_console:
        if repaired:
            rich_console.print(f"\n  [green]+[/green] Repaired {repaired} issue(s)\n")
        else:
            rich_console.print(f"  [dim]Nothing to repair.[/dim]\n")

    return results


# ══════════════════════════════════════════════════════════════════════════════
#  DEEP MODE — OpenClaw-inspired deep diagnostics (cleo doctor --deep)
# ══════════════════════════════════════════════════════════════════════════════

def run_doctor_deep(rich_console=None) -> list[tuple[bool, str, str]]:
    """
    Deep diagnostics: all checks + connectivity test + file integrity + disk usage.
    """
    results = run_doctor(rich_console=rich_console)

    if rich_console:
        rich_console.print(f"\n  [bold magenta]── Deep Diagnostics ──[/bold magenta]\n")

    deep_results = []

    # Deep 1: Disk usage of Cleo data files
    data_files = [".task_board.json", ".context_bus.json", "memory/usage.json",
                  "memory/reputation_cache.json"]
    total_size = 0
    for fp in data_files:
        if os.path.exists(fp):
            sz = os.path.getsize(fp)
            total_size += sz
    ok = total_size < 50 * 1024 * 1024  # < 50MB is healthy
    size_mb = total_size / (1024 * 1024)
    detail = f"{size_mb:.1f} MB total data files"
    deep_results.append((ok, "Disk Usage", detail))

    # Deep 2: Check skills directory integrity
    skill_count = 0
    broken_skills = []
    if os.path.isdir("skills"):
        for fname in os.listdir("skills"):
            if fname.endswith(".md"):
                skill_count += 1
                path = os.path.join("skills", fname)
                try:
                    with open(path) as f:
                        content = f.read()
                    if not content.strip():
                        broken_skills.append(fname)
                except Exception:
                    broken_skills.append(fname)
    if broken_skills:
        deep_results.append((False, "Skills", f"{len(broken_skills)} empty/broken: {', '.join(broken_skills[:3])}"))
    else:
        deep_results.append((True, "Skills", f"{skill_count} skill files OK"))

    # Deep 3: Check workflow YAML validity
    wf_count = 0
    broken_wf = []
    if os.path.isdir("workflows"):
        for fname in os.listdir("workflows"):
            if fname.endswith((".yaml", ".yml")):
                wf_count += 1
                try:
                    import yaml
                    with open(os.path.join("workflows", fname)) as f:
                        wf = yaml.safe_load(f)
                    if not wf or "steps" not in wf:
                        broken_wf.append(fname)
                except Exception:
                    broken_wf.append(fname)
    if broken_wf:
        deep_results.append((False, "Workflows", f"{len(broken_wf)} invalid: {', '.join(broken_wf[:3])}"))
    else:
        deep_results.append((True, "Workflows", f"{wf_count} workflow files OK"))

    # Deep 4: Python version check
    v = sys.version_info
    py_ok = v.major >= 3 and v.minor >= 10
    deep_results.append((py_ok, "Python", f"{v.major}.{v.minor}.{v.micro}" + (" (3.10+ required)" if not py_ok else "")))

    # Print deep results
    if rich_console:
        for ok, label, detail in deep_results:
            icon = "[green]+[/green]" if ok else "[red]x[/red]"
            rich_console.print(f"  {icon} [bold]{label:14}[/bold] [dim]{detail}[/dim]")
        rich_console.print()

    return results + deep_results


def _print_rich(console, results: list[tuple[bool, str, str]]):
    """Pretty-print doctor results with rich, then offer auto-fix for fixable issues."""
    from rich.panel import Panel
    from rich import box
    from core.i18n import t as _t

    lines = []
    ok_count = sum(1 for ok, _, _ in results if ok)
    total = len(results)

    for ok, label, detail in results:
        icon = "[green]+[/green]" if ok else "[red]x[/red]"
        lines.append(f"  {icon} [bold]{label:14}[/bold] [dim]{detail}[/dim]")

    body = "\n".join(lines)
    if ok_count == total:
        status = f"[green]{_t('doctor.all_ok')}[/green]"
    else:
        status = f"[yellow]{_t('doctor.some_fail', ok=ok_count, total=total)}[/yellow]"

    console.print()
    console.print(Panel(
        f"[bold magenta]{_t('doctor.title')}[/bold magenta]\n\n{body}\n\n  {status}",
        border_style="magenta",
        box=box.ROUNDED,
    ))
    console.print()

    # ── Auto-fix: offer to install missing optional packages ─────────
    fixable = _detect_fixable(results)
    if fixable:
        _offer_auto_fix(console, fixable)


# Map of package labels to pip package names for auto-install
_FIXABLE_PACKAGES = {
    "chromadb": "chromadb",
    "web3": "web3",
    "lit_python_sdk": "lit-python-sdk",
}


def _detect_fixable(results: list[tuple[bool, str, str]]) -> list[str]:
    """Detect which optional packages could be auto-installed."""
    fixable = []
    for ok, label, detail in results:
        detail_lower = detail.lower()
        # Check failed results for missing packages
        if not ok:
            if "chromadb" in detail_lower and ("not installed" in detail_lower or "not loadable" in detail_lower):
                fixable.append("chromadb")
            elif "web3" in detail_lower and "not installed" in detail_lower:
                fixable.append("web3")
            elif "lit" in detail_lower and "not installed" in detail_lower:
                fixable.append("lit_python_sdk")
        # Also check OK results with optional deps marked as missing
        if label == "Dependencies" and "missing" in detail_lower:
            if "vector memory missing" in detail_lower and "chromadb" not in fixable:
                fixable.append("chromadb")
            if "erc-8004 chain missing" in detail_lower and "web3" not in fixable:
                fixable.append("web3")
            if "lit pkp missing" in detail_lower and "lit_python_sdk" not in fixable:
                fixable.append("lit_python_sdk")
        if label == "Memory" and "install chromadb" in detail_lower:
            if "chromadb" not in fixable:
                fixable.append("chromadb")
    return fixable


def _offer_auto_fix(console, fixable: list[str]):
    """Offer to install missing packages via questionary."""
    from core.i18n import t as _t
    try:
        import questionary
        from core.onboard import STYLE
    except ImportError:
        return

    # Build checkbox choices
    choices = []
    for pkg_key in fixable:
        pip_name = _FIXABLE_PACKAGES.get(pkg_key, pkg_key)
        choices.append(questionary.Choice(pip_name, value=pip_name, checked=True))

    selected = questionary.checkbox(
        _t("doctor.fix_prompt"),
        choices=choices,
        style=STYLE,
    ).ask()

    if not selected:
        return

    import subprocess
    for pkg in selected:
        console.print(f"  [dim]{_t('doctor.installing', pkg=pkg)}[/dim]")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                console.print(f"  [green]+[/green] {_t('doctor.installed', pkg=pkg)}")
            else:
                console.print(f"  [red]x[/red] {_t('doctor.install_fail', pkg=pkg)}")
                if result.stderr:
                    console.print(f"    [dim]{result.stderr.strip()[:100]}[/dim]")
        except Exception as e:
            console.print(f"  [red]x[/red] {_t('doctor.install_fail', pkg=pkg)}: {e}")

    console.print()
