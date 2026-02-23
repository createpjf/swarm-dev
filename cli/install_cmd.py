"""Install, uninstall, and update CLI commands."""
from __future__ import annotations

import os
import sys

from core.theme import theme as _theme

_DEFAULT_REPO = "https://github.com/createpjf/cleo-dev.git"


def cmd_install(repo: str = "", target: str = "", console=None):
    """Clone from GitHub, set up venv, install deps, link CLI."""
    import subprocess
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            console = None

    def _print(msg):
        if console:
            console.print(msg)
        else:
            print(msg)

    from core.i18n import t as _t
    repo_url = repo or os.environ.get("SWARM_REPO", _DEFAULT_REPO)
    install_dir = target or os.environ.get("SWARM_INSTALL_DIR", "")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not install_dir and os.path.exists(os.path.join(project_root, "pyproject.toml")):
        _print(f"  [{_theme.muted}]{_t('install.already', path=project_root)}[/{_theme.muted}]")
        _print(f"  [{_theme.muted}]Running setup…[/{_theme.muted}]")
        setup_sh = os.path.join(project_root, "setup.sh")
        if os.path.exists(setup_sh):
            result = subprocess.run(["bash", setup_sh], cwd=project_root)
            if result.returncode == 0:
                _print(f"  [{_theme.success}]✓[/{_theme.success}] {_t('install.done')}")
            else:
                _print(f"  [{_theme.error}]✗[/{_theme.error}] {_t('install.failed', err='setup.sh failed')}")
        else:
            venv_pip = os.path.join(project_root, ".venv", "bin", "pip")
            pip_cmd = venv_pip if os.path.exists(venv_pip) else "pip"
            result = subprocess.run(
                [pip_cmd, "install", "-e", ".[dev]"],
                cwd=project_root, capture_output=True, text=True,
            )
            if result.returncode == 0:
                _print(f"  [{_theme.success}]✓[/{_theme.success}] {_t('install.done')}")
            else:
                _print(f"  [{_theme.error}]✗[/{_theme.error}] {_t('install.failed', err=result.stderr[:200])}")
        return

    if not install_dir:
        install_dir = os.path.expanduser("~/cleo-dev")

    _print(f"  [{_theme.muted}]{_t('install.checking')}[/{_theme.muted}]")

    if os.path.exists(install_dir) and os.listdir(install_dir):
        _print(f"  [{_theme.warning}]![/{_theme.warning}] {_t('install.already', path=install_dir)}")
        _print(f"  [{_theme.muted}]Use 'cleo update' to pull latest changes.[/{_theme.muted}]")
        return

    _print(f"  [{_theme.muted}]{_t('install.cloning')}[/{_theme.muted}]  {repo_url}")
    result = subprocess.run(
        ["git", "clone", repo_url, install_dir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _print(f"  [{_theme.error}]✗[/{_theme.error}] {_t('install.failed', err=result.stderr[:200])}")
        return

    _print(f"  [{_theme.muted}]{_t('install.installing')}[/{_theme.muted}]")
    setup_sh = os.path.join(install_dir, "setup.sh")
    if os.path.exists(setup_sh):
        result = subprocess.run(["bash", setup_sh], cwd=install_dir)
    else:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".[dev]"],
            cwd=install_dir, capture_output=True, text=True,
        )

    if result.returncode == 0:
        _print(f"  [{_theme.success}]✓[/{_theme.success}] {_t('install.done')}")
        _print(f"  [{_theme.muted}]Installed to: {install_dir}[/{_theme.muted}]")
        _print(f"  [{_theme.heading}]Quick start:[/{_theme.heading}]  cd {install_dir} && cleo")
    else:
        err = getattr(result, 'stderr', '') or ''
        _print(f"  [{_theme.error}]✗[/{_theme.error}] {_t('install.failed', err=err[:200])}")


def cmd_uninstall(console=None):
    """Remove cleo CLI symlink and daemon service. Source code stays."""
    import subprocess
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            console = None

    def _print(msg):
        if console:
            console.print(msg)
        else:
            print(msg)

    from core.i18n import t as _t

    try:
        import questionary
        from core.onboard import STYLE
        ok = questionary.confirm(
            _t("uninstall.confirm"), default=False, style=STYLE,
        ).ask()
        if not ok:
            _print(f"  [{_theme.muted}]{_t('uninstall.cancelled')}[/{_theme.muted}]")
            return
    except ImportError:
        answer = input(f"  {_t('uninstall.confirm')} [y/N] ").strip().lower()
        if answer != "y":
            _print(f"  [{_theme.muted}]{_t('uninstall.cancelled')}[/{_theme.muted}]")
            return

    removed = []

    try:
        from core.daemon import uninstall_daemon
        ok, msg = uninstall_daemon()
        if ok:
            removed.append("daemon")
            _print(f"  [{_theme.success}]✓[/{_theme.success}] {msg}")
    except Exception:
        pass

    target = "/usr/local/bin/cleo"
    if os.path.islink(target):
        try:
            os.remove(target)
            removed.append("CLI symlink")
            _print(f"  [{_theme.success}]✓[/{_theme.success}] Removed {target}")
        except PermissionError:
            result = subprocess.run(["sudo", "rm", "-f", target],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                removed.append("CLI symlink")
                _print(f"  [{_theme.success}]✓[/{_theme.success}] Removed {target}")
            else:
                _print(f"  [{_theme.warning}]![/{_theme.warning}] Could not remove {target}")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_pip = os.path.join(project_root, ".venv", "bin", "pip")
    pip_cmd = venv_pip if os.path.exists(venv_pip) else "pip"
    result = subprocess.run(
        [pip_cmd, "uninstall", "-y", "cleo-agent-stack"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        removed.append("pip package")
        _print(f"  [{_theme.success}]✓[/{_theme.success}] Uninstalled pip package")

    if removed:
        _print(f"\n  [{_theme.success}]✓[/{_theme.success}] {_t('uninstall.done')} ({', '.join(removed)})")
        _print(f"  [{_theme.muted}]Source code remains at: {project_root}[/{_theme.muted}]")
        _print(f"  [{_theme.muted}]To fully remove: rm -rf {project_root}[/{_theme.muted}]")
    else:
        _print(f"  [{_theme.muted}]Nothing to uninstall.[/{_theme.muted}]")


def cmd_update(branch: str = "", check_only: bool = False, console=None):
    """Pull latest code from GitHub and reinstall dependencies."""
    import subprocess
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            console = None

    def _print(msg):
        if console:
            console.print(msg)
        else:
            print(msg)

    from core.i18n import t as _t
    from cli.helpers import get_version

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            tomllib = None

    if tomllib:
        try:
            with open(os.path.join(project_root, "pyproject.toml"), "rb") as f:
                pyproject = tomllib.load(f)
            version = pyproject.get("project", {}).get("version", "?")
            _print(f"  [{_theme.muted}]{_t('update.version', version=version)}[/{_theme.muted}]")
        except Exception:
            pass

    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_root, capture_output=True, text=True,
    )
    current_branch = result.stdout.strip() if result.returncode == 0 else "main"
    target_branch = branch or current_branch
    _print(f"  [{_theme.muted}]{_t('update.branch', branch=target_branch)}[/{_theme.muted}]")

    result = subprocess.run(
        ["git", "remote"],
        cwd=project_root, capture_output=True, text=True,
    )
    remote = result.stdout.strip().split("\n")[0] if result.returncode == 0 else "origin"

    _print(f"  [{_theme.muted}]{_t('update.checking')}[/{_theme.muted}]")

    _print(f"  [{_theme.muted}]{_t('update.fetching', remote=remote)}[/{_theme.muted}]")
    result = subprocess.run(
        ["git", "fetch", remote, target_branch],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        _print(f"  [{_theme.error}]✗[/{_theme.error}] {_t('update.failed', err=result.stderr[:200])}")
        return

    result = subprocess.run(
        ["git", "log", f"HEAD..{remote}/{target_branch}", "--oneline"],
        cwd=project_root, capture_output=True, text=True,
    )
    commits = result.stdout.strip()
    if not commits:
        _print(f"  [{_theme.success}]✓[/{_theme.success}] {_t('update.up_to_date')}")
        return

    commit_count = len(commits.split("\n"))
    _print(f"  [{_theme.info}]{commit_count} new commit(s) available[/{_theme.info}]")

    result = subprocess.run(
        ["git", "diff", "--stat", f"HEAD..{remote}/{target_branch}"],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.stdout.strip():
        for line in result.stdout.strip().split("\n")[-3:]:
            _print(f"  [{_theme.muted}]{line.strip()}[/{_theme.muted}]")

    # --check mode: just report availability, don't pull
    if check_only:
        _print(f"  [{_theme.muted}]Run 'cleo update' to apply these changes.[/{_theme.muted}]")
        return

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root, capture_output=True, text=True,
    )
    has_local_changes = bool(result.stdout.strip())
    if has_local_changes:
        _print(f"  [{_theme.warning}]![/{_theme.warning}] Stashing local changes…")
        subprocess.run(
            ["git", "stash", "push", "-m", "cleo-update-auto-stash"],
            cwd=project_root, capture_output=True, text=True,
        )

    result = subprocess.run(
        ["git", "pull", remote, target_branch],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        _print(f"  [{_theme.error}]✗[/{_theme.error}] {_t('update.failed', err=result.stderr[:200])}")
        if has_local_changes:
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=project_root, capture_output=True, text=True,
            )
        return

    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~" + str(commit_count), "HEAD"],
        cwd=project_root, capture_output=True, text=True,
    )
    changed_files = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0

    _print(f"  [{_theme.muted}]{_t('update.deps')}[/{_theme.muted}]")
    venv_pip = os.path.join(project_root, ".venv", "bin", "pip")
    pip_cmd = venv_pip if os.path.exists(venv_pip) else "pip"
    subprocess.run(
        [pip_cmd, "install", "-e", ".[dev]", "-q"],
        cwd=project_root, capture_output=True, text=True,
    )

    if has_local_changes:
        _print(f"  [{_theme.muted}]Restoring local changes…[/{_theme.muted}]")
        result = subprocess.run(
            ["git", "stash", "pop"],
            cwd=project_root, capture_output=True, text=True,
        )
        if result.returncode != 0:
            _print(f"  [{_theme.warning}]![/{_theme.warning}] Stash pop had conflicts — check 'git stash list'")

    if tomllib:
        try:
            with open(os.path.join(project_root, "pyproject.toml"), "rb") as f:
                pyproject = tomllib.load(f)
            new_version = pyproject.get("project", {}).get("version", "?")
            _print(f"  [{_theme.muted}]{_t('update.version', version=new_version)}[/{_theme.muted}]")
        except Exception:
            pass

    summary = _t("update.changes", n=changed_files)
    _print(f"  [{_theme.success}]✓[/{_theme.success}] {_t('update.updated', summary=summary)}\n")
