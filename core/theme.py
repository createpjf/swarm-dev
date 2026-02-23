"""
core/theme.py
Semantic color theme system — replaces hardcoded color strings.

Supports:
  - NO_COLOR=1 → disable all colors
  - FORCE_COLOR=1 → force colors in pipes
  - CLEO_THEME=minimal → alternative theme

Usage:
    from core.theme import theme
    console.print(f"[{theme.accent}]Hello[/{theme.accent}]")
"""

from __future__ import annotations

import os


class Theme:
    """Semantic color definitions for consistent CLI appearance."""

    def __init__(self):
        self._no_color = bool(os.environ.get("NO_COLOR"))
        self._force_color = bool(os.environ.get("FORCE_COLOR"))
        self._theme_name = os.environ.get("CLEO_THEME", "default")

        if self._no_color:
            self._apply_no_color()
        elif self._theme_name == "minimal":
            self._apply_minimal()
        else:
            self._apply_default()

    def _apply_default(self):
        """Default purple/magenta theme."""
        self.accent = "bold magenta"
        self.accent_light = "magenta"
        self.success = "green"
        self.warning = "yellow"
        self.error = "red"
        self.muted = "dim"
        self.agent = "bold bright_magenta"
        self.info = "cyan"
        self.heading = "bold"
        self.link = "underline blue"
        # Questionary style
        self.qmark = "fg:#b388ff bold"
        self.question = "bold"
        self.answer = "fg:#ce93d8 bold"
        self.pointer = "fg:#b388ff bold"
        self.selected = "fg:#ce93d8"
        self.instruction = "fg:#9e9e9e"

    def _apply_minimal(self):
        """Minimal theme — fewer colors, cleaner look."""
        self.accent = "bold"
        self.accent_light = ""
        self.success = "green"
        self.warning = "yellow"
        self.error = "red"
        self.muted = "dim"
        self.agent = "bold"
        self.info = ""
        self.heading = "bold"
        self.link = "underline"
        self.qmark = "bold"
        self.question = "bold"
        self.answer = "bold"
        self.pointer = "bold"
        self.selected = ""
        self.instruction = "fg:#9e9e9e"

    def _apply_no_color(self):
        """No color theme — all empty strings."""
        for attr in ("accent", "accent_light", "success", "warning", "error",
                      "muted", "agent", "info", "heading", "link"):
            setattr(self, attr, "")
        self.qmark = ""
        self.question = ""
        self.answer = ""
        self.pointer = ""
        self.selected = ""
        self.instruction = ""

    def questionary_style(self):
        """Return questionary Style from current theme."""
        try:
            from questionary import Style
            return Style([
                ("qmark", self.qmark),
                ("question", self.question),
                ("answer", self.answer),
                ("pointer", self.pointer),
                ("highlighted", ""),
                ("selected", self.selected),
                ("instruction", self.instruction),
            ])
        except ImportError:
            return None

    @property
    def is_color_enabled(self) -> bool:
        """Check if color output is enabled."""
        if self._no_color:
            return False
        if self._force_color:
            return True
        import sys
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


# Singleton instance
theme = Theme()
