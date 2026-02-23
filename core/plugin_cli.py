"""
core/plugin_cli.py
CLI commands for plugin management.

Commands:
  cleo plugins list                — list installed plugins
  cleo plugins install <path|url>  — install a plugin
  cleo plugins remove <name>       — remove a plugin
  cleo plugins enable <name>       — enable a disabled plugin
  cleo plugins disable <name>      — disable a plugin
  cleo plugins info <name>         — show plugin details
"""

from __future__ import annotations

import json
import os
import shutil
import yaml

try:
    from core.theme import theme as _theme
except ImportError:
    class _FallbackTheme:
        success = "green"; error = "red"; warning = "yellow"
        muted = "dim"; heading = "bold"; info = "cyan"
    _theme = _FallbackTheme()

PLUGINS_DIR = "plugins"


def cmd_plugins_list(console=None):
    """List all installed plugins with status."""
    plugins = _scan_plugins()

    if not plugins:
        _print(console, f"  [{_theme.muted}]No plugins installed.[/{_theme.muted}]")
        _print(console, f"  [{_theme.muted}]Install with: cleo plugins install <path>[/{_theme.muted}]")
        return

    if console:
        from rich.table import Table
        tbl = Table(box=None, padding=(0, 1), show_header=True)
        tbl.add_column("Status", width=3)
        tbl.add_column("Plugin", style="bold", min_width=15)
        tbl.add_column("Version", style="dim", min_width=8)
        tbl.add_column("Hooks", min_width=15)
        tbl.add_column("Tools", min_width=15)

        for p in plugins:
            status = f"[{_theme.success}]✓[/{_theme.success}]" if p["enabled"] else f"[{_theme.muted}]○[/{_theme.muted}]"
            hooks = ", ".join(p.get("hooks", [])) or "—"
            tools = ", ".join(p.get("tools", [])) or "—"
            tbl.add_row(status, p["name"], p.get("version", "?"),
                        hooks, tools)
        console.print(tbl)
    else:
        for p in plugins:
            status = "✓" if p["enabled"] else "○"
            print(f"  {status} {p['name']:20} v{p.get('version', '?')}")

    _print(console, "")


def cmd_plugins_install(source: str, console=None):
    """Install a plugin from a local directory or git URL."""
    if not source:
        _print(console, f"  [{_theme.error}]Usage: cleo plugins install <path|git-url>[/{_theme.error}]")
        return False

    os.makedirs(PLUGINS_DIR, exist_ok=True)

    # Git URL
    if source.startswith(("http://", "https://", "git@")):
        return _install_from_git(source, console)

    # Local directory
    if os.path.isdir(source):
        return _install_from_dir(source, console)

    _print(console, f"  [{_theme.error}]Source not found: {source}[/{_theme.error}]")
    return False


def cmd_plugins_remove(name: str, console=None):
    """Remove a plugin by name."""
    plugin_dir = os.path.join(PLUGINS_DIR, name)
    if not os.path.isdir(plugin_dir):
        _print(console, f"  [{_theme.error}]Plugin not found: {name}[/{_theme.error}]")
        return False

    try:
        shutil.rmtree(plugin_dir)
        _print(console, f"  [{_theme.success}]✓[/{_theme.success}] Removed plugin: {name}")
        return True
    except OSError as e:
        _print(console, f"  [{_theme.error}]Failed to remove: {e}[/{_theme.error}]")
        return False


def cmd_plugins_enable(name: str, console=None):
    """Enable a disabled plugin."""
    return _set_enabled(name, True, console)


def cmd_plugins_disable(name: str, console=None):
    """Disable a plugin (keep files, skip loading)."""
    return _set_enabled(name, False, console)


def cmd_plugins_info(name: str, console=None):
    """Show detailed info about a plugin."""
    plugin_dir = os.path.join(PLUGINS_DIR, name)
    manifest = _load_manifest(plugin_dir)
    if not manifest:
        _print(console, f"  [{_theme.error}]Plugin not found or invalid: {name}[/{_theme.error}]")
        return

    if console:
        from rich.panel import Panel
        from rich import box

        lines = [
            f"[{_theme.heading}]{manifest.get('name', name)}[/{_theme.heading}] v{manifest.get('version', '?')}",
            "",
        ]
        if manifest.get("description"):
            lines.append(f"  {manifest['description']}")
            lines.append("")
        if manifest.get("author"):
            lines.append(f"  Author: {manifest['author']}")

        hooks = manifest.get("hooks", [])
        if hooks:
            lines.append(f"  Hooks:  {', '.join(hooks)}")

        tools = manifest.get("tools", [])
        if tools:
            lines.append(f"  Tools:  {', '.join(t.get('name', '?') for t in tools)}")

        config = manifest.get("config", {})
        if config:
            lines.append(f"  Config: {json.dumps(config, indent=2)}")

        enabled = not os.path.exists(os.path.join(plugin_dir, ".disabled"))
        status = f"[{_theme.success}]enabled[/{_theme.success}]" if enabled else f"[{_theme.muted}]disabled[/{_theme.muted}]"
        lines.append(f"  Status: {status}")

        console.print(Panel("\n".join(lines), title=f"Plugin: {name}",
                           border_style="dim", box=box.ROUNDED))
    else:
        print(f"  Plugin: {manifest.get('name', name)}")
        print(f"  Version: {manifest.get('version', '?')}")
        print(f"  Hooks: {manifest.get('hooks', [])}")
        print(f"  Tools: {manifest.get('tools', [])}")


# ── Internal ────────────────────────────────────────────────────────────────

def _scan_plugins() -> list[dict]:
    """Scan plugins directory for installed plugins."""
    plugins = []
    if not os.path.isdir(PLUGINS_DIR):
        return plugins

    for name in sorted(os.listdir(PLUGINS_DIR)):
        plugin_dir = os.path.join(PLUGINS_DIR, name)
        if not os.path.isdir(plugin_dir):
            continue
        if name.startswith(".") or name.startswith("_"):
            continue

        manifest = _load_manifest(plugin_dir)
        if manifest:
            enabled = not os.path.exists(os.path.join(plugin_dir, ".disabled"))
            plugins.append({
                "name": manifest.get("name", name),
                "version": manifest.get("version", "0.0.0"),
                "hooks": manifest.get("hooks", []),
                "tools": [t.get("name", "?") for t in manifest.get("tools", [])],
                "enabled": enabled,
                "path": plugin_dir,
            })

    return plugins


def _load_manifest(plugin_dir: str) -> dict | None:
    """Load plugin manifest (YAML or JSON)."""
    for fname in ("manifest.yaml", "manifest.yml", "manifest.json"):
        path = os.path.join(plugin_dir, fname)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    if fname.endswith(".json"):
                        return json.load(f)
                    else:
                        return yaml.safe_load(f)
            except Exception:
                return None
    return None


def _install_from_dir(source: str, console=None) -> bool:
    """Install plugin from local directory."""
    manifest = _load_manifest(source)
    if not manifest:
        _print(console, f"  [{_theme.error}]No valid manifest.yaml found in {source}[/{_theme.error}]")
        return False

    name = manifest.get("name", os.path.basename(source))
    target = os.path.join(PLUGINS_DIR, name)

    if os.path.exists(target):
        _print(console, f"  [{_theme.warning}]Plugin '{name}' already exists. Overwriting.[/{_theme.warning}]")
        shutil.rmtree(target)

    try:
        shutil.copytree(source, target)
        _print(console, f"  [{_theme.success}]✓[/{_theme.success}] Installed: {name} v{manifest.get('version', '?')}")
        hooks = manifest.get("hooks", [])
        tools = manifest.get("tools", [])
        if hooks:
            _print(console, f"    Hooks: {', '.join(hooks)}")
        if tools:
            _print(console, f"    Tools: {', '.join(t.get('name', '?') for t in tools)}")
        return True
    except OSError as e:
        _print(console, f"  [{_theme.error}]Install failed: {e}[/{_theme.error}]")
        return False


def _install_from_git(url: str, console=None) -> bool:
    """Install plugin from git URL."""
    import subprocess
    import tempfile

    _print(console, f"  [{_theme.muted}]Cloning {url}...[/{_theme.muted}]")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", url, tmp],
                capture_output=True, text=True, timeout=60,
                check=True,
            )
            return _install_from_dir(tmp, console)
        except subprocess.CalledProcessError as e:
            _print(console, f"  [{_theme.error}]Git clone failed: {e.stderr[:100]}[/{_theme.error}]")
            return False
        except FileNotFoundError:
            _print(console, f"  [{_theme.error}]git not found. Install git first.[/{_theme.error}]")
            return False


def _set_enabled(name: str, enabled: bool, console=None) -> bool:
    """Set plugin enabled/disabled state."""
    plugin_dir = os.path.join(PLUGINS_DIR, name)
    if not os.path.isdir(plugin_dir):
        _print(console, f"  [{_theme.error}]Plugin not found: {name}[/{_theme.error}]")
        return False

    flag = os.path.join(plugin_dir, ".disabled")
    if enabled:
        if os.path.exists(flag):
            os.remove(flag)
        _print(console, f"  [{_theme.success}]✓[/{_theme.success}] Enabled: {name}")
    else:
        with open(flag, "w") as f:
            f.write("")
        _print(console, f"  [{_theme.muted}]○[/{_theme.muted}] Disabled: {name}")
    return True


def cmd_plugins_update(name: str, console=None):
    """Update a plugin (git pull if it's a git repo)."""
    import subprocess
    plugin_dir = os.path.join(PLUGINS_DIR, name)
    if not os.path.isdir(plugin_dir):
        _print(console, f"  [{_theme.error}]Plugin not found: {name}[/{_theme.error}]")
        return False

    git_dir = os.path.join(plugin_dir, ".git")
    if os.path.isdir(git_dir):
        _print(console, f"  [{_theme.muted}]Updating {name} (git pull)...[/{_theme.muted}]")
        result = subprocess.run(
            ["git", "-C", plugin_dir, "pull"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            _print(console, f"  [{_theme.success}]✓[/{_theme.success}] Updated: {name}")
            # Update integrity hash
            _save_integrity(name, plugin_dir)
            return True
        else:
            _print(console, f"  [{_theme.error}]Update failed: {result.stderr[:100]}[/{_theme.error}]")
            return False
    else:
        _print(console, f"  [{_theme.warning}]{name} is not a git repo — reinstall to update.[/{_theme.warning}]")
        return False


def cmd_plugins_doctor(console=None):
    """Health check for all installed plugins."""
    plugins = _scan_plugins()
    if not plugins:
        _print(console, f"  [{_theme.muted}]No plugins installed.[/{_theme.muted}]")
        return

    ok_count = 0
    total = len(plugins)

    for p in plugins:
        name = p["name"]
        path = p.get("path", os.path.join(PLUGINS_DIR, name))
        issues = []

        # 1. Check manifest
        manifest = _load_manifest(path)
        if not manifest:
            issues.append("missing or invalid manifest")

        # 2. Check required manifest fields
        if manifest:
            for field in ("name", "version"):
                if not manifest.get(field):
                    issues.append(f"missing '{field}' in manifest")

        # 3. Check hooks are importable
        if manifest and manifest.get("hooks"):
            try:
                import importlib
                import importlib.util
                init_path = os.path.join(path, "__init__.py")
                if os.path.exists(init_path):
                    spec = importlib.util.spec_from_file_location(
                        f"plugins.{name}", init_path)
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        for hook in manifest["hooks"]:
                            handler_name = f"on_{hook.replace(':', '_')}"
                            if not hasattr(mod, handler_name):
                                issues.append(f"handler '{handler_name}' not found")
                else:
                    issues.append("missing __init__.py")
            except Exception as e:
                issues.append(f"import error: {e}")

        # 4. Check integrity
        integrity_ok = _verify_integrity(name, path)

        # Report
        if not issues and integrity_ok:
            _print(console, f"  [{_theme.success}]✓[/{_theme.success}] {name}: healthy")
            ok_count += 1
        elif not issues and not integrity_ok:
            _print(console, f"  [{_theme.warning}]![/{_theme.warning}] {name}: modified since install")
            ok_count += 1  # not fatal
        else:
            _print(console, f"  [{_theme.error}]✗[/{_theme.error}] {name}:")
            for issue in issues:
                _print(console, f"      - {issue}")

    _print(console, "")
    if ok_count == total:
        _print(console, f"  [{_theme.success}]All {total} plugin(s) healthy.[/{_theme.success}]")
    else:
        _print(console, f"  [{_theme.warning}]{total - ok_count} of {total} plugin(s) have issues.[/{_theme.warning}]")


# ── Integrity hashing ──────────────────────────────────────────────────────

_INTEGRITY_FILE = os.path.join(PLUGINS_DIR, ".integrity.json")


def _compute_plugin_hash(plugin_dir: str) -> str:
    """Compute SHA-256 of all plugin files (excluding __pycache__)."""
    import hashlib
    h = hashlib.sha256()
    for root, dirs, files in os.walk(plugin_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__" and not d.startswith(".")]
        for fname in sorted(files):
            if fname.endswith((".pyc", ".pyo")):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "rb") as f:
                    h.update(f.read())
            except Exception:
                pass
    return h.hexdigest()[:16]


def _save_integrity(name: str, plugin_dir: str):
    """Record plugin hash after install/update."""
    integrity = {}
    if os.path.exists(_INTEGRITY_FILE):
        try:
            with open(_INTEGRITY_FILE) as f:
                integrity = json.load(f)
        except Exception:
            pass
    integrity[name] = _compute_plugin_hash(plugin_dir)
    os.makedirs(os.path.dirname(_INTEGRITY_FILE) or ".", exist_ok=True)
    with open(_INTEGRITY_FILE, "w") as f:
        json.dump(integrity, f, indent=2)


def _verify_integrity(name: str, plugin_dir: str) -> bool:
    """Check if plugin files match the recorded hash."""
    if not os.path.exists(_INTEGRITY_FILE):
        return True  # no baseline = assume ok
    try:
        with open(_INTEGRITY_FILE) as f:
            integrity = json.load(f)
    except Exception:
        return True
    if name not in integrity:
        return True
    return integrity[name] == _compute_plugin_hash(plugin_dir)


def _print(console, text: str):
    """Print to Rich console if available, else plain print."""
    if console:
        console.print(text)
    else:
        # Strip Rich markup for plain output
        import re
        clean = re.sub(r"\[/?[^\]]*\]", "", text)
        print(clean)
