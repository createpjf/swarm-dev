"""Shell completions CLI commands."""
from __future__ import annotations


def cmd_completions(shell: str = ""):
    """Generate shell completion scripts."""
    from core.completions import generate_bash, generate_zsh, install_completions

    if shell == "bash":
        print(generate_bash())
    elif shell == "zsh":
        print(generate_zsh())
    elif shell == "install":
        ok, msg = install_completions()
        icon = "✓" if ok else "✗"
        print(f"  {icon} {msg}")
    else:
        print("Usage: cleo completions <bash|zsh|install>")
        print("  bash    — output bash completion script")
        print("  zsh     — output zsh completion script")
        print("  install — auto-install to shell rc file")
