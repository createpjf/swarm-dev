#!/usr/bin/env python3
"""Check that no Python source file exceeds the LOC limit.

Inspired by OpenClaw's 500-line enforcement. Helps keep modules focused
and maintainable.

Exit codes:
  0 — all files within limits
  1 — at least one file exceeds ERROR_LIMIT

Usage:
  python3 scripts/check_loc.py              # default scan
  python3 scripts/check_loc.py --warn 400   # custom warning threshold
"""

from __future__ import annotations

import argparse
import os
import sys

WARN_LIMIT = 500
ERROR_LIMIT = 800
SCAN_DIRS = ["core", "adapters", "cli", "reputation"]
EXTENSIONS = {".py"}
IGNORE_NAMES = {"__pycache__"}


def count_loc(filepath: str) -> int:
    """Count lines of code (including blanks/comments)."""
    with open(filepath, encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def scan(dirs: list[str], warn: int = WARN_LIMIT,
         error: int = ERROR_LIMIT) -> tuple[list[str], list[str]]:
    """Scan directories, return (warnings, errors)."""
    warnings: list[str] = []
    errors: list[str] = []

    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, subdirs, files in os.walk(d):
            # Skip __pycache__ and hidden dirs
            subdirs[:] = [s for s in subdirs
                          if s not in IGNORE_NAMES and not s.startswith(".")]
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1]
                if ext not in EXTENSIONS or fname.startswith("."):
                    continue
                fpath = os.path.join(root, fname)
                loc = count_loc(fpath)
                if loc > error:
                    errors.append(f"{fpath}: {loc} lines (limit: {error})")
                elif loc > warn:
                    warnings.append(f"{fpath}: {loc} lines (warning: {warn})")

    return warnings, errors


def main():
    parser = argparse.ArgumentParser(description="Check LOC limits")
    parser.add_argument("--warn", type=int, default=WARN_LIMIT,
                        help=f"Warning threshold (default: {WARN_LIMIT})")
    parser.add_argument("--error", type=int, default=ERROR_LIMIT,
                        help=f"Error threshold (default: {ERROR_LIMIT})")
    args = parser.parse_args()

    warnings, errors = scan(SCAN_DIRS, warn=args.warn, error=args.error)

    if not warnings and not errors:
        print("All files within LOC limits.")
        return

    for w in warnings:
        print(f"  \u26a0 {w}")
    for e in errors:
        print(f"  \u2717 {e}")

    if errors:
        print(f"\n{len(errors)} file(s) exceed error limit ({args.error} lines)")
        sys.exit(1)
    else:
        print(f"\n{len(warnings)} file(s) above warning threshold (no errors)")


if __name__ == "__main__":
    main()
