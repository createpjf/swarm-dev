"""
adapters/memory/chroma.py
ChromaDB vector store adapter with pluggable embedding provider.
Falls back to MockMemory if chromadb is not installed.
"""

from __future__ import annotations
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import chromadb
    _HAS_CHROMA = True
except (ImportError, Exception):
    # chromadb may fail on Python 3.14+ (pydantic v1 incompatibility)
    _HAS_CHROMA = False


def ChromaAdapter(persist_dir: str = "memory/chroma",
                  embedding_fn=None):
    """
    Factory function: returns ChromaDB adapter if available,
    otherwise falls back to MockMemory with a warning.

    Args:
        persist_dir: ChromaDB persistence directory.
        embedding_fn: Optional ChromaDB-compatible embedding function.
                      Use EmbeddingProvider.as_chromadb_function() to create one.
                      If None, ChromaDB uses its default (all-MiniLM-L6-v2).
    """
    if _HAS_CHROMA:
        return _ChromaAdapterImpl(persist_dir, embedding_fn=embedding_fn)
    else:
        logger.warning(
            "chromadb not installed — falling back to MockMemory. "
            "Install with: pip install chromadb"
        )
        from adapters.memory.mock import MockMemory
        return MockMemory()


class _ChromaAdapterImpl:

    def __init__(self, persist_dir: str = "memory/chroma",
                 embedding_fn=None):
        os.makedirs(persist_dir, exist_ok=True)
        self.client = chromadb.PersistentClient(path=persist_dir)
        self._embedding_fn = embedding_fn

    def _get_collection(self, name: str):
        """Get or create a collection with the configured embedding function."""
        kwargs = {"name": name}
        if self._embedding_fn is not None:
            kwargs["embedding_function"] = self._embedding_fn
        return self.client.get_or_create_collection(**kwargs)

    def add(self, collection: str, document: str, metadata: dict):
        coll = self._get_collection(collection)
        doc_id = metadata.get("id", str(hash(document)))
        coll.add(
            documents=[document],
            metadatas=[metadata],
            ids=[str(doc_id)],
        )

    def query(self, collection: str, query: str,
              n_results: int = 3) -> dict:
        coll = self._get_collection(collection)
        return coll.query(query_texts=[query], n_results=n_results)
