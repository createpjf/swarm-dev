"""
core/logging_config.py
Structured logging with correlation IDs for cross-agent tracing.
JSON log format for machine-parseable output.
"""

from __future__ import annotations
import json
import logging
import os
import threading
import time
import uuid

# ── Correlation ID (per-task tracing) ─────────────────────────────────────

_correlation_id = threading.local()


def set_correlation_id(cid: str = ""):
    """Set correlation ID for current thread (ties logs to a task)."""
    _correlation_id.value = cid or str(uuid.uuid4())[:8]


def get_correlation_id() -> str:
    return getattr(_correlation_id, "value", "")


# ── Structured JSON Formatter ─────────────────────────────────────────────

class StructuredFormatter(logging.Formatter):
    """
    JSON log formatter for machine-parseable logs.
    Fields: ts, level, logger, msg, agent, cid (correlation ID), extra
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Correlation ID for cross-agent tracing
        cid = get_correlation_id()
        if cid:
            entry["cid"] = cid

        # Agent ID from logger name convention
        if record.name.startswith("agent."):
            entry["agent"] = record.name.split(".", 1)[1]

        # Extra fields
        if hasattr(record, "extra_data"):
            entry["extra"] = record.extra_data

        # Include exception info
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, ensure_ascii=False, default=str)


# ── Setup ─────────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO", structured: bool = False,
                  log_dir: str = ".logs"):
    """
    Configure logging for the swarm.
    Args:
        level: log level (DEBUG/INFO/WARNING/ERROR)
        structured: if True, use JSON format; if False, use human-readable
        log_dir: directory for log files
    """
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    for h in root.handlers[:]:
        root.removeHandler(h)

    if structured:
        formatter = StructuredFormatter()
    else:
        formatter = logging.Formatter(
            "[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )

    # Console handler (human-readable always)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "[%(asctime)s][%(name)s] %(message)s", datefmt="%H:%M:%S"))
    console.setLevel(logging.WARNING)  # Only warnings+ on console
    root.addHandler(console)

    # File handler (structured or not)
    log_path = os.path.join(log_dir, "swarm.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(file_handler)

    return root
