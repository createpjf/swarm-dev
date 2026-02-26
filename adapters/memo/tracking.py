"""
adapters/memo/tracking.py — Idempotent export tracker.

Maintains a ``memory/memo_export_tracking.json`` file that maps
Cleo source IDs (episode task_id, case hash, pattern hash, KB slug)
to Memo ``mem_*`` IDs, preventing duplicate exports / uploads.
"""

from __future__ import annotations

import json
import os
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

TRACKING_FILE = os.path.join("memory", "memo_export_tracking.json")


class ExportTracker:
    """Cleo source_id ↔ Memo mem_id mapping with persistence."""

    def __init__(self, path: str = TRACKING_FILE):
        self.path = path
        self._data: dict = self._load()

    # ── persistence ───────────────────────────────────────────────────────

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.debug("[memo-tracking] load failed: %s", e)
        return {"exports": {}, "meta": {"created_at": time.time()}}

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._data["meta"]["updated_at"] = time.time()
        self._data["meta"]["total_exports"] = len(self._data["exports"])
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ── key helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _key(source_type: str, source_id: str) -> str:
        return f"{source_type}:{source_id}"

    # ── query / mutate ────────────────────────────────────────────────────

    def is_exported(self, source_type: str, source_id: str) -> bool:
        return self._key(source_type, source_id) in self._data["exports"]

    def record(self, source_type: str, source_id: str, memo_id: str):
        self._data["exports"][self._key(source_type, source_id)] = {
            "memo_id": memo_id,
            "exported_at": time.time(),
            "source_type": source_type,
        }

    def get_memo_id(self, source_type: str, source_id: str) -> Optional[str]:
        entry = self._data["exports"].get(self._key(source_type, source_id))
        return entry["memo_id"] if entry else None

    def all_memo_ids(self) -> list[str]:
        """Return all exported Memo IDs (useful for skill sync)."""
        return [e["memo_id"] for e in self._data["exports"].values()
                if "memo_id" in e]

    # ── stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        exports = self._data["exports"]
        by_type: dict[str, int] = {}
        for entry in exports.values():
            t = entry.get("source_type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total": len(exports),
            "by_type": by_type,
            "created_at": self._data["meta"].get("created_at"),
            "updated_at": self._data["meta"].get("updated_at"),
        }

    def reset(self):
        """Clear all tracking data (for testing or re-export)."""
        self._data = {"exports": {}, "meta": {"created_at": time.time()}}
        self.save()
