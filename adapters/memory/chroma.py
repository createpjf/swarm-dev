"""
adapters/memory/chroma.py
ChromaDB vector store adapter.
Falls back to MockMemory if chromadb is not installed.
"""

from __future__ import annotations
import logging
import os

logger = logging.getLogger(__name__)

try:
    import chromadb
    _HAS_CHROMA = True
except (ImportError, Exception):
    # chromadb may fail on Python 3.14+ (pydantic v1 incompatibility)
    _HAS_CHROMA = False


def ChromaAdapter(persist_dir: str = "memory/chroma"):
    """
    Factory function: returns ChromaDB adapter if available,
    otherwise falls back to MockMemory with a warning.
    """
    if _HAS_CHROMA:
        return _ChromaAdapterImpl(persist_dir)
    else:
        logger.warning(
            "chromadb not installed — falling back to MockMemory. "
            "Install with: pip install chromadb"
        )
        from adapters.memory.mock import MockMemory
        return MockMemory()


class _ChromaAdapterImpl:

    def __init__(self, persist_dir: str = "memory/chroma"):
        os.makedirs(persist_dir, exist_ok=True)
        self.client = chromadb.PersistentClient(path=persist_dir)

    def add(self, collection: str, document: str, metadata: dict):
        coll = self.client.get_or_create_collection(collection)
        doc_id = metadata.get("id", str(hash(document)))
        coll.add(
            documents=[document],
            metadatas=[metadata],
            ids=[str(doc_id)],
        )

    def query(self, collection: str, query: str,
              n_results: int = 3) -> dict:
        coll = self.client.get_or_create_collection(collection)
        return coll.query(query_texts=[query], n_results=n_results)
