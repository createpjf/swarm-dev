"""
core/cron.py
Built-in scheduler — manages recurring and one-shot jobs.
Inspired by OpenClaw's cron tool pattern.

Supports:
  - One-shot jobs (run at a specific ISO 8601 timestamp)
  - Interval jobs (repeat every N seconds)
  - Cron-expression jobs (standard 5-field cron: min hour dom mon dow)

Jobs are persisted to memory/cron_jobs.json and survive restarts.
Each job can trigger:
  - A task submission (via orchestrator)
  - A shell command (exec)
  - A webhook POST
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from threading import Thread, Event
from typing import Any, Optional

logger = logging.getLogger(__name__)

JOBS_PATH = "memory/cron_jobs.json"
_stop_event = Event()
_scheduler_thread: Optional[Thread] = None


# ══════════════════════════════════════════════════════════════════════════════
#  JOB MODEL
# ══════════════════════════════════════════════════════════════════════════════

def _new_job(
    name: str,
    action: str,           # "task" | "exec" | "webhook"
    payload: str,          # task description / shell command / webhook URL
    schedule_type: str,    # "once" | "interval" | "cron"
    schedule: str,         # ISO timestamp / seconds / cron expression
    agent_id: str = "",
    enabled: bool = True,
) -> dict:
    """Create a new job dict."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "action": action,
        "payload": payload,
        "schedule_type": schedule_type,
        "schedule": schedule,
        "agent_id": agent_id,
        "enabled": enabled,
        "created_at": now,
        "last_run": None,
        "next_run": _compute_next_run(schedule_type, schedule),
        "run_count": 0,
        "last_error": None,
    }


def _compute_next_run(schedule_type: str, schedule: str,
                       after: float | None = None) -> str | None:
    """Compute the next run time as ISO string."""
    now = after or time.time()

    if schedule_type == "once":
        try:
            dt = datetime.fromisoformat(schedule.replace("Z", "+00:00"))
            return dt.isoformat()
        except ValueError:
            return None

    elif schedule_type == "interval":
        try:
            secs = float(schedule)
            dt = datetime.fromtimestamp(now + secs, tz=timezone.utc)
            return dt.isoformat()
        except ValueError:
            return None

    elif schedule_type == "cron":
        return _next_cron_match(schedule, now)

    return None


def _next_cron_match(expr: str, after: float) -> str | None:
    """Simple cron expression matcher (min hour dom mon dow).

    Returns the next matching minute as ISO string.
    Only supports numeric values, *, and */N step syntax.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        return None

    def _matches(field: str, value: int, max_val: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            step = int(field[2:])
            return value % step == 0
        try:
            return int(field) == value
        except ValueError:
            return False

    # Search forward up to 48 hours
    dt = datetime.fromtimestamp(after, tz=timezone.utc)
    # Start from next minute
    dt = dt.replace(second=0, microsecond=0)
    import datetime as dt_mod
    delta = dt_mod.timedelta(minutes=1)

    for _ in range(48 * 60):  # 48 hours of minutes
        dt += delta
        if (_matches(parts[0], dt.minute, 59) and
                _matches(parts[1], dt.hour, 23) and
                _matches(parts[2], dt.day, 31) and
                _matches(parts[3], dt.month, 12) and
                _matches(parts[4], dt.weekday(), 6)):
            return dt.isoformat()

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def _load_jobs() -> list[dict]:
    if not os.path.exists(JOBS_PATH):
        return []
    try:
        with open(JOBS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_jobs(jobs: list[dict]):
    os.makedirs(os.path.dirname(JOBS_PATH), exist_ok=True)
    with open(JOBS_PATH, "w") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False, default=str)


# ══════════════════════════════════════════════════════════════════════════════
#  CRUD API
# ══════════════════════════════════════════════════════════════════════════════

def list_jobs() -> list[dict]:
    """Return all jobs."""
    return _load_jobs()


def get_job(job_id: str) -> dict | None:
    for j in _load_jobs():
        if j["id"] == job_id:
            return j
    return None


def add_job(name: str, action: str, payload: str,
            schedule_type: str, schedule: str,
            agent_id: str = "", enabled: bool = True) -> dict:
    """Create and persist a new job."""
    job = _new_job(name, action, payload, schedule_type, schedule,
                   agent_id, enabled)
    jobs = _load_jobs()
    jobs.append(job)
    _save_jobs(jobs)
    logger.info("Cron job added: %s (%s)", job["id"], name)
    return job


def update_job(job_id: str, **kwargs) -> dict | None:
    """Update a job's fields. Returns updated job or None."""
    jobs = _load_jobs()
    for j in jobs:
        if j["id"] == job_id:
            for k, v in kwargs.items():
                if k in j and k not in ("id", "created_at"):
                    j[k] = v
            # Recompute next_run if schedule changed
            if "schedule" in kwargs or "schedule_type" in kwargs:
                j["next_run"] = _compute_next_run(
                    j["schedule_type"], j["schedule"])
            _save_jobs(jobs)
            return j
    return None


def remove_job(job_id: str) -> bool:
    """Delete a job. Returns True if found."""
    jobs = _load_jobs()
    before = len(jobs)
    jobs = [j for j in jobs if j["id"] != job_id]
    if len(jobs) < before:
        _save_jobs(jobs)
        logger.info("Cron job removed: %s", job_id)
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  JOB EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def _execute_job(job: dict) -> tuple[bool, str]:
    """Execute a single job. Returns (success, message)."""
    action = job["action"]
    payload = job["payload"]

    try:
        if action == "task":
            # Submit as a new task via orchestrator
            from core.orchestrator import Orchestrator
            from core.task_board import TaskBoard
            board = TaskBoard()
            orch = Orchestrator()
            task_id = orch.submit(payload)

            def _run():
                try:
                    orch._launch_all()
                    orch._wait()
                except Exception as e:
                    logger.error("Cron task exec error: %s", e)

            t = Thread(target=_run, daemon=True)
            t.start()
            return True, f"task submitted: {task_id}"

        elif action == "exec":
            # Run shell command
            import subprocess
            result = subprocess.run(
                payload, shell=True, capture_output=True,
                text=True, timeout=300)
            if result.returncode == 0:
                return True, result.stdout[:500] or "(no output)"
            else:
                return False, f"exit {result.returncode}: {result.stderr[:300]}"

        elif action == "webhook":
            # POST to webhook URL
            import urllib.request
            req = urllib.request.Request(
                payload, method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps({
                    "job_id": job["id"],
                    "name": job["name"],
                    "ts": datetime.now(timezone.utc).isoformat(),
                }).encode())
            with urllib.request.urlopen(req, timeout=15) as resp:
                return True, f"webhook {resp.status}"

        else:
            return False, f"unknown action: {action}"

    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER LOOP
# ══════════════════════════════════════════════════════════════════════════════

def _scheduler_tick():
    """Check all jobs and execute any that are due."""
    jobs = _load_jobs()
    now = time.time()
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    changed = False

    for job in jobs:
        if not job.get("enabled", True):
            continue
        next_run = job.get("next_run")
        if not next_run:
            continue

        # Parse next_run
        try:
            next_dt = datetime.fromisoformat(
                next_run.replace("Z", "+00:00"))
            if next_dt.timestamp() > now:
                continue  # not yet due
        except ValueError:
            continue

        # Execute
        logger.info("Cron firing job: %s (%s)", job["id"], job["name"])
        ok, msg = _execute_job(job)

        job["last_run"] = now_iso
        job["run_count"] = job.get("run_count", 0) + 1
        job["last_error"] = None if ok else msg

        # Compute next run
        if job["schedule_type"] == "once":
            job["enabled"] = False  # one-shot done
            job["next_run"] = None
        else:
            job["next_run"] = _compute_next_run(
                job["schedule_type"], job["schedule"], after=now)

        changed = True

    if changed:
        _save_jobs(jobs)


def start_scheduler(interval: int = 30):
    """Start the background scheduler thread.

    Checks for due jobs every `interval` seconds (default 30).
    """
    global _scheduler_thread

    if _scheduler_thread and _scheduler_thread.is_alive():
        return  # already running

    _stop_event.clear()

    def _loop():
        logger.info("Cron scheduler started (tick=%ds)", interval)
        while not _stop_event.is_set():
            try:
                _scheduler_tick()
            except Exception as e:
                logger.error("Cron tick error: %s", e)
            _stop_event.wait(interval)
        logger.info("Cron scheduler stopped")

    _scheduler_thread = Thread(target=_loop, daemon=True, name="cron")
    _scheduler_thread.start()


def stop_scheduler():
    """Stop the background scheduler."""
    _stop_event.set()
