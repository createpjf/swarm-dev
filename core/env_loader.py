"""
core/env_loader.py
Minimal .env file loader — no external dependencies.
Loads KEY=VALUE pairs into os.environ at startup.
"""

import os


def load_dotenv(path: str = ".env"):
    """
    Load KEY=VALUE pairs from a .env file into os.environ.
    - Skips blank lines and comments (lines starting with #)
    - Strips surrounding quotes (' or ") from values
    - Uses os.environ.setdefault so real env vars take precedence
    """
    if not os.path.exists(path):
        return

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)
