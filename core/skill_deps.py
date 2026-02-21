"""
core/skill_deps.py
Scan skill .md files for CLI dependency metadata, check which binaries
are missing, and offer to install them during onboard.

Frontmatter schema (nested inside metadata.Cleo):
  requires:
    bins: ["memo"]           # all required
    anyBins: ["claude","pi"] # any one suffices
    env: ["API_KEY"]         # env vars needed
    config: ["channels.x"]   # config paths needed
  install:
    - id: brew
      kind: brew | go | node | uv | apt | brew-cask
      formula: "tap/formula"      # brew
      module: "github.com/..."    # go install
      package: "pkg"              # npm / uv / apt
      bins: ["binary"]
      label: "Human-readable label"
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

SKILLS_DIR = "skills"


# ── Frontmatter parser (reuse from skill_loader) ────────────────────────────

def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter metadata dict from a skill .md file."""
    if not content or not content.startswith("---"):
        return {}
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n?', content, re.DOTALL)
    if not match:
        return {}
    try:
        import yaml
        return yaml.safe_load(match.group(1)) or {}
    except Exception:
        return {}


# ── Scan all skills ─────────────────────────────────────────────────────────

def scan_skill_deps(skills_dir: str = SKILLS_DIR) -> list[dict]:
    """
    Scan all skill .md files and return a list of dependency records:
      [{
        "skill": "apple-notes",
        "file": "apple-notes.md",
        "emoji": "📝",
        "os": ["darwin"],          # empty = all platforms
        "requires_bins": ["memo"],
        "requires_any_bins": [],
        "requires_env": [],
        "install": [ {kind, ...} ],
        "missing_bins": ["memo"],  # bins not found on PATH
        "has_any_bin": False,      # for anyBins: at least one present?
        "missing_env": ["KEY"],    # env vars not set
      }]
    """
    results = []
    if not os.path.isdir(skills_dir):
        return results

    current_os = _current_os()

    for fname in sorted(os.listdir(skills_dir)):
        if not fname.endswith(".md") or fname.startswith(".") or fname.startswith("_"):
            continue

        path = os.path.join(skills_dir, fname)
        if not os.path.isfile(path):
            continue

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            continue

        meta = _parse_frontmatter(content)
        cleo = (meta.get("metadata") or {}).get("Cleo", {})
        if not cleo:
            continue

        requires = cleo.get("requires", {})
        req_bins = requires.get("bins", [])
        req_any = requires.get("anyBins", [])
        req_env = requires.get("env", [])
        install = cleo.get("install", [])
        skill_os = cleo.get("os", [])
        emoji = cleo.get("emoji", "")

        # Skip if skill is OS-restricted and we're not on that OS
        if skill_os and current_os not in skill_os:
            continue

        # Check which bins are missing
        missing_bins = [b for b in req_bins if not shutil.which(b)]
        has_any = (not req_any) or any(shutil.which(b) for b in req_any)
        missing_env = [e for e in req_env if not os.environ.get(e)]

        # Only include if there's something installable
        if not install and not req_bins and not req_any:
            continue

        results.append({
            "skill": meta.get("name", fname.replace(".md", "")),
            "file": fname,
            "emoji": emoji,
            "os": skill_os,
            "requires_bins": req_bins,
            "requires_any_bins": req_any,
            "requires_env": req_env,
            "install": install,
            "missing_bins": missing_bins,
            "has_any_bin": has_any,
            "missing_env": missing_env,
        })

    return results


def get_missing_deps(skills_dir: str = SKILLS_DIR) -> list[dict]:
    """Return only skills with missing binary dependencies."""
    all_deps = scan_skill_deps(skills_dir)
    return [d for d in all_deps if d["missing_bins"] or not d["has_any_bin"]]


def get_installed_deps(skills_dir: str = SKILLS_DIR) -> list[dict]:
    """Return only skills whose binary dependencies are all satisfied."""
    all_deps = scan_skill_deps(skills_dir)
    return [d for d in all_deps if not d["missing_bins"] and d["has_any_bin"]]


# ── Install helpers ─────────────────────────────────────────────────────────

def _current_os() -> str:
    """Map platform.system() to skill os values."""
    s = platform.system().lower()
    if s == "darwin":
        return "darwin"
    if s == "linux":
        return "linux"
    if s == "windows":
        return "win32"
    return s


def _has_cmd(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def build_install_command(entry: dict) -> str | None:
    """
    Build a shell command string for a single install entry.
    Returns None if the install kind is not supported or prerequisites missing.
    """
    kind = entry.get("kind", "")

    if kind == "brew":
        formula = entry.get("formula", "")
        if not formula:
            return None
        return f"brew install {formula}"

    if kind == "brew-cask":
        formula = entry.get("formula", "")
        if not formula:
            return None
        return f"brew install --cask {formula}"

    if kind == "go":
        module = entry.get("module", "")
        if not module:
            return None
        return f"go install {module}"

    if kind == "node":
        package = entry.get("package", "")
        if not package:
            return None
        return f"npm i -g {package}"

    if kind == "uv":
        package = entry.get("package", "")
        if not package:
            return None
        return f"uv tool install {package}"

    if kind == "apt":
        package = entry.get("package", "")
        if not package:
            return None
        return f"sudo apt install -y {package}"

    return None


def pick_best_installer(install_entries: list[dict]) -> dict | None:
    """
    From a list of install options, pick the one most likely to work
    on the current system.  Priority: brew > go > node > uv > apt.
    """
    # Preference order
    pref = ["brew", "brew-cask", "go", "node", "uv", "apt"]
    # Filter to installers whose package manager is available
    available = []
    for entry in install_entries:
        kind = entry.get("kind", "")
        mgr = {"brew": "brew", "brew-cask": "brew", "go": "go",
               "node": "npm", "uv": "uv", "apt": "apt"}.get(kind, "")
        if mgr and _has_cmd(mgr):
            available.append(entry)

    if not available:
        return install_entries[0] if install_entries else None

    # Sort by preference
    def _rank(e):
        k = e.get("kind", "")
        return pref.index(k) if k in pref else 99

    available.sort(key=_rank)
    return available[0]


def install_dep(entry: dict, quiet: bool = False) -> bool:
    """
    Run the install command for a single install entry.
    Returns True on success.
    """
    cmd = build_install_command(entry)
    if not cmd:
        return False

    if not quiet:
        print(f"  $ {cmd}")

    try:
        result = subprocess.run(
            cmd, shell=True,
            stdout=subprocess.PIPE if quiet else None,
            stderr=subprocess.PIPE if quiet else None,
            timeout=300,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        if not quiet:
            print(f"  Error: {e}")
        return False


def install_skill_deps(dep_record: dict, quiet: bool = False) -> bool:
    """
    Install all missing dependencies for a single skill.
    Returns True if all deps satisfied after install.
    """
    install_entries = dep_record.get("install", [])
    if not install_entries:
        return False

    best = pick_best_installer(install_entries)
    if not best:
        return False

    return install_dep(best, quiet=quiet)


# ── Bulk operations ─────────────────────────────────────────────────────────

def check_prerequisites() -> dict[str, bool]:
    """Check which package managers are available."""
    return {
        "brew": _has_cmd("brew"),
        "go": _has_cmd("go"),
        "npm": _has_cmd("npm"),
        "uv": _has_cmd("uv"),
        "apt": _has_cmd("apt"),
    }


def sync_exec_approvals(skills_dir: str = SKILLS_DIR):
    """
    Scan all skills and auto-approve their binaries in exec_approvals.json.
    Call this at startup or after installing new skill CLIs so that agents
    with the exec tool can run the binaries without being blocked.
    """
    import re as _re
    all_deps = scan_skill_deps(skills_dir)
    bins_to_approve = set()

    for dep in all_deps:
        # Collect all bins that are actually installed on PATH
        for b in dep.get("requires_bins", []):
            if shutil.which(b):
                bins_to_approve.add(b)
        for b in dep.get("requires_any_bins", []):
            if shutil.which(b):
                bins_to_approve.add(b)

    if not bins_to_approve:
        return

    try:
        from core.exec_tool import add_approval
        for b in sorted(bins_to_approve):
            pattern = rf"^{_re.escape(b)}\b"
            add_approval(pattern)
        logger.info("sync_exec_approvals: approved %d skill binaries", len(bins_to_approve))
    except Exception as e:
        logger.warning("sync_exec_approvals failed: %s", e)
