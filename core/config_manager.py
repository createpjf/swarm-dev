"""
core/config_manager.py
Config version control — automatic backup before writes, rollback support.

Watches config/agents.yaml and config/chain_contracts.json.
Keeps last N snapshots so operators can undo bad config changes.
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import shutil
import time

logger = logging.getLogger(__name__)

BACKUP_DIR = "config/.backups"
MAX_BACKUPS = 20  # keep last N versions per file
TRACKED_FILES = [
    "config/agents.yaml",
    "config/chain_contracts.json",
    "config/budget.json",
]


def _file_hash(path: str) -> str:
    """SHA-256 of file contents (empty string if missing)."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except FileNotFoundError:
        return ""


def _backup_subdir(config_path: str) -> str:
    """Return backup subdirectory for a config file."""
    basename = os.path.basename(config_path).replace(".", "_")
    return os.path.join(BACKUP_DIR, basename)


def snapshot(config_path: str, reason: str = "") -> str | None:
    """
    Save a timestamped backup of a config file.

    Args:
        config_path: path to config file (e.g. "config/agents.yaml")
        reason: optional description (stored in manifest)

    Returns:
        backup filename, or None if file unchanged / missing.
    """
    if not os.path.exists(config_path):
        return None

    current_hash = _file_hash(config_path)
    if not current_hash:
        return None

    # Check if latest backup is identical (skip duplicate snapshots)
    backup_dir = _backup_subdir(config_path)
    manifest = _load_manifest(config_path)
    if manifest and manifest[-1].get("hash") == current_hash:
        logger.debug("Config %s unchanged — skipping snapshot", config_path)
        return None

    os.makedirs(backup_dir, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    ext = os.path.splitext(config_path)[1]
    backup_name = f"{ts}_{current_hash}{ext}"
    backup_path = os.path.join(backup_dir, backup_name)

    shutil.copy2(config_path, backup_path)

    # Update manifest
    manifest.append({
        "file": backup_name,
        "hash": current_hash,
        "timestamp": time.time(),
        "reason": reason,
    })

    # Prune old backups
    while len(manifest) > MAX_BACKUPS:
        old = manifest.pop(0)
        old_path = os.path.join(backup_dir, old["file"])
        if os.path.exists(old_path):
            os.remove(old_path)

    _save_manifest(config_path, manifest)
    logger.info("Config snapshot: %s → %s (%s)", config_path, backup_name, reason or "manual")
    return backup_name


def rollback(config_path: str, version: int = -1) -> bool:
    """
    Restore a config file from a previous backup.

    Args:
        config_path: path to config file
        version: index in history (-1 = latest backup, -2 = one before, etc.)

    Returns:
        True if rollback succeeded.
    """
    manifest = _load_manifest(config_path)
    if not manifest:
        logger.warning("No backups found for %s", config_path)
        return False

    try:
        entry = manifest[version]
    except IndexError:
        logger.warning("Version %d out of range (have %d backups)", version, len(manifest))
        return False

    backup_dir = _backup_subdir(config_path)
    backup_path = os.path.join(backup_dir, entry["file"])

    if not os.path.exists(backup_path):
        logger.error("Backup file missing: %s", backup_path)
        return False

    # Snapshot current state before rollback (so rollback is itself reversible)
    snapshot(config_path, reason=f"pre-rollback to {entry['file']}")

    shutil.copy2(backup_path, config_path)
    logger.info("Rolled back %s → %s", config_path, entry["file"])
    return True


def history(config_path: str) -> list[dict]:
    """
    List available backups for a config file.

    Returns list of dicts with: file, hash, timestamp, reason.
    """
    return _load_manifest(config_path)


def snapshot_all(reason: str = "") -> list[str]:
    """Snapshot all tracked config files. Returns list of backup names created."""
    results = []
    for path in TRACKED_FILES:
        name = snapshot(path, reason=reason)
        if name:
            results.append(f"{path} → {name}")
    return results


# ── Safe config write ────────────────────────────────────────────────────────

def safe_write_yaml(config_path: str, data: dict, reason: str = ""):
    """Write YAML config with automatic pre-write snapshot."""
    import yaml
    snapshot(config_path, reason=reason or "pre-write")
    os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)
    logger.info("Config written: %s (%s)", config_path, reason)


def safe_write_json(config_path: str, data: dict, reason: str = ""):
    """Write JSON config with automatic pre-write snapshot."""
    snapshot(config_path, reason=reason or "pre-write")
    os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("Config written: %s (%s)", config_path, reason)


# ── Manifest helpers ─────────────────────────────────────────────────────────

def _manifest_path(config_path: str) -> str:
    return os.path.join(_backup_subdir(config_path), "manifest.json")


def _load_manifest(config_path: str) -> list[dict]:
    path = _manifest_path(config_path)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_manifest(config_path: str, manifest: list[dict]):
    path = _manifest_path(config_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
