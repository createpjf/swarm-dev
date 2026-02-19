"""
adapters/memory/mock.py
In-memory dict store — no persistence, no deps. For tests and Level 0.
Matches ChromaDB response shape for drop-in compatibility.
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


class MockMemory:

    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def add(self, collection: str, document: str, metadata: dict):
        coll = self.store.setdefault(collection, [])
        coll.append({"document": document, "metadata": metadata})

    def query(self, collection: str, query: str,
              n_results: int = 3) -> dict:
        """
        Simple substring match. Returns ChromaDB-compatible response shape.
        """
        coll = self.store.get(collection, [])

        # Score by substring match
        results = []
        for entry in coll:
            doc = entry["document"]
            if query.lower() in doc.lower():
                results.append(entry)

        # Limit
        results = results[:n_results]

        return {
            "documents": [[r["document"] for r in results]],
            "metadatas": [[r["metadata"] for r in results]],
        }
