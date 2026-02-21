"""
adapters/memory/hybrid.py
Hybrid retrieval — combines ChromaDB vector search with BM25 keyword search.

OpenClaw-inspired: dual retrieval path with reciprocal rank fusion (RRF)
for better recall on both semantic and keyword queries.

BM25 implementation is self-contained (no external dependency).
"""

from __future__ import annotations
import logging
import math
import os
import re
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


# ── BM25 Implementation ─────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    """Simple word tokenizer — lowercase, split on non-alphanumeric."""
    return re.findall(r'[a-z0-9\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]+',
                      text.lower())


class BM25Index:
    """
    Self-contained BM25 index — no external dependencies.
    Supports incremental document addition and disk persistence.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b  = b
        self.docs: list[str]               = []       # original documents
        self.doc_ids: list[str]             = []       # document IDs
        self.doc_metadata: list[dict]       = []       # metadata per doc
        self.doc_lens: list[int]            = []       # token count per doc
        self.doc_freqs: list[dict[str,int]] = []       # term frequencies per doc
        self.idf: dict[str, float]          = {}       # inverse doc frequency
        self.avg_dl: float                  = 0.0
        self._df: dict[str, int]            = defaultdict(int)  # doc frequency

    def add(self, doc_id: str, document: str, metadata: dict | None = None):
        """Add a document to the BM25 index."""
        tokens = _tokenize(document)
        tf = defaultdict(int)
        for t in tokens:
            tf[t] += 1

        self.docs.append(document)
        self.doc_ids.append(doc_id)
        self.doc_metadata.append(metadata or {})
        self.doc_lens.append(len(tokens))
        self.doc_freqs.append(dict(tf))

        # Update document frequency
        for term in set(tokens):
            self._df[term] += 1

        # Recompute IDF and avg_dl
        n = len(self.docs)
        self.avg_dl = sum(self.doc_lens) / n if n else 0
        self.idf = {}
        for term, df in self._df.items():
            self.idf[term] = math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, n_results: int = 5) -> list[tuple[int, float]]:
        """
        Search the index. Returns list of (doc_index, score) sorted by score desc.
        """
        query_tokens = _tokenize(query)
        if not query_tokens or not self.docs:
            return []

        scores = []
        for i, (doc_tf, dl) in enumerate(zip(self.doc_freqs, self.doc_lens)):
            score = 0.0
            for term in query_tokens:
                if term not in doc_tf:
                    continue
                tf = doc_tf[term]
                idf = self.idf.get(term, 0)
                numerator   = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (
                    1 - self.b + self.b * dl / max(self.avg_dl, 1)
                )
                score += idf * numerator / denominator
            if score > 0:
                scores.append((i, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:n_results]

    # ── Persistence ───────────────────────────────────────────────────────

    def save(self, path: str):
        """Save index to disk as JSON for persistence across restarts."""
        import json
        data = {
            "k1": self.k1, "b": self.b,
            "docs": self.docs,
            "doc_ids": self.doc_ids,
            "doc_metadata": self.doc_metadata,
            "doc_lens": self.doc_lens,
            "doc_freqs": self.doc_freqs,
            "idf": self.idf,
            "avg_dl": self.avg_dl,
            "df": dict(self._df),
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.debug("BM25 index saved to %s (%d docs)", path, len(self.docs))

    @classmethod
    def load(cls, path: str) -> "BM25Index":
        """Load index from disk. Returns empty index if file missing."""
        import json
        try:
            with open(path) as f:
                data = json.load(f)
            idx = cls(k1=data.get("k1", 1.5), b=data.get("b", 0.75))
            idx.docs = data.get("docs", [])
            idx.doc_ids = data.get("doc_ids", [])
            idx.doc_metadata = data.get("doc_metadata", [])
            idx.doc_lens = data.get("doc_lens", [])
            idx.doc_freqs = data.get("doc_freqs", [])
            idx.idf = data.get("idf", {})
            idx.avg_dl = data.get("avg_dl", 0.0)
            idx._df = defaultdict(int, data.get("df", {}))
            logger.debug("BM25 index loaded from %s (%d docs)", path, len(idx.docs))
            return idx
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return cls()


# ── Reciprocal Rank Fusion ───────────────────────────────────────────────────

def reciprocal_rank_fusion(
    *ranked_lists: list[tuple[str, float]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """
    Combine multiple ranked result lists using Reciprocal Rank Fusion.

    Each input is a list of (doc_id, score) tuples.
    Returns merged list of (doc_id, rrf_score) sorted by fused score.

    k=60 is the standard RRF constant.
    """
    fused: dict[str, float] = defaultdict(float)

    for ranked in ranked_lists:
        for rank, (doc_id, _score) in enumerate(ranked, start=1):
            fused[doc_id] += 1.0 / (k + rank)

    result = sorted(fused.items(), key=lambda x: x[1], reverse=True)
    return result


# ── Hybrid Memory Adapter ───────────────────────────────────────────────────

class HybridMemory:
    """
    Combines ChromaDB vector search with BM25 keyword search.
    Uses Reciprocal Rank Fusion to merge results.

    API-compatible with ChromaAdapter (same add/query interface).
    """

    def __init__(self, persist_dir: str = "memory/chroma",
                 alpha: float = 0.5,
                 embedding_fn=None):
        """
        Args:
            persist_dir: ChromaDB persistence directory
            alpha: Weight for vector results (1-alpha for BM25).
                   Not used with RRF, but kept for future weighted fusion.
            embedding_fn: Optional ChromaDB-compatible embedding function.
                          Use EmbeddingProvider.as_chromadb_function() to create one.
                          If None, ChromaDB uses its default (all-MiniLM-L6-v2).
        """
        self.alpha = alpha
        self._embedding_fn = embedding_fn

        # Vector search backend
        try:
            import chromadb
            os.makedirs(persist_dir, exist_ok=True)
            self._chroma = chromadb.PersistentClient(path=persist_dir)
            self._has_chroma = True
        except (ImportError, Exception):
            logger.warning("chromadb not available — hybrid mode uses BM25 only")
            self._chroma = None
            self._has_chroma = False

        # BM25 keyword search (per-collection) — with persistence
        self._bm25_dir = os.path.join(persist_dir, "bm25")
        os.makedirs(self._bm25_dir, exist_ok=True)
        self._bm25_indices: dict[str, BM25Index] = {}
        self._bm25_dirty: set[str] = set()

    def _get_bm25(self, collection: str) -> BM25Index:
        """Get or load BM25 index for a collection."""
        if collection not in self._bm25_indices:
            path = os.path.join(self._bm25_dir, f"{collection}.json")
            self._bm25_indices[collection] = BM25Index.load(path)
        return self._bm25_indices[collection]

    def _save_bm25(self, collection: str):
        """Persist a dirty BM25 index to disk."""
        if collection in self._bm25_indices:
            path = os.path.join(self._bm25_dir, f"{collection}.json")
            self._bm25_indices[collection].save(path)
            self._bm25_dirty.discard(collection)

    def _get_chroma_collection(self, collection: str):
        """Get or create a ChromaDB collection with the configured embedding."""
        kwargs = {"name": collection}
        if self._embedding_fn is not None:
            kwargs["embedding_function"] = self._embedding_fn
        return self._chroma.get_or_create_collection(**kwargs)

    def add(self, collection: str, document: str, metadata: dict):
        """Add a document to both vector and BM25 indices."""
        doc_id = str(metadata.get("id", hash(document)))

        # Vector index
        if self._has_chroma:
            coll = self._get_chroma_collection(collection)
            coll.add(
                documents=[document],
                metadatas=[metadata],
                ids=[doc_id],
            )

        # BM25 index (with persistence)
        bm25 = self._get_bm25(collection)
        bm25.add(doc_id, document, metadata)
        self._bm25_dirty.add(collection)

        # Auto-persist every 10 adds
        if len(bm25.docs) % 10 == 0:
            self._save_bm25(collection)

    def query(self, collection: str, query: str,
              n_results: int = 3) -> dict:
        """
        Hybrid query: run both vector and BM25 search,
        then merge with Reciprocal Rank Fusion.

        Returns ChromaDB-compatible response shape.
        """
        vector_results = []
        bm25_results   = []

        # 1. Vector search
        if self._has_chroma:
            try:
                coll = self._get_chroma_collection(collection)
                vr = coll.query(query_texts=[query], n_results=n_results * 2)
                ids    = vr.get("ids", [[]])[0]
                dists  = vr.get("distances", [[]])[0]
                for doc_id, dist in zip(ids, dists):
                    # ChromaDB returns distances (lower = better)
                    # Convert to similarity score
                    score = 1.0 / (1.0 + dist)
                    vector_results.append((doc_id, score))
            except Exception as e:
                logger.warning("Vector search failed: %s", e)

        # 2. BM25 search
        bm25_idx = self._get_bm25(collection)
        if bm25_idx:
            hits = bm25_idx.search(query, n_results=n_results * 2)
            for doc_idx, score in hits:
                doc_id = bm25_idx.doc_ids[doc_idx]
                bm25_results.append((doc_id, score))

        # 3. Fuse results with RRF
        if vector_results and bm25_results:
            fused = reciprocal_rank_fusion(vector_results, bm25_results)
        elif vector_results:
            fused = vector_results
        elif bm25_results:
            fused = bm25_results
        else:
            return {"documents": [[]], "metadatas": [[]]}

        # 4. Collect documents for top-N results
        fused = fused[:n_results]

        # Build lookup from BM25 index
        doc_lookup = {}
        meta_lookup = {}
        if bm25_idx:
            for i, did in enumerate(bm25_idx.doc_ids):
                doc_lookup[did] = bm25_idx.docs[i]
                meta_lookup[did] = bm25_idx.doc_metadata[i]

        documents = []
        metadatas = []
        for doc_id, _score in fused:
            if doc_id in doc_lookup:
                documents.append(doc_lookup[doc_id])
                metadatas.append(meta_lookup[doc_id])
            else:
                # Try to fetch from ChromaDB
                if self._has_chroma:
                    try:
                        coll = self._get_chroma_collection(collection)
                        result = coll.get(ids=[doc_id])
                        if result["documents"]:
                            documents.append(result["documents"][0])
                            metadatas.append(
                                result["metadatas"][0] if result["metadatas"] else {}
                            )
                            continue
                    except Exception:
                        pass
                documents.append("")
                metadatas.append({})

        return {
            "documents": [documents],
            "metadatas": [metadatas],
        }


# ── Factory ──────────────────────────────────────────────────────────────────

def HybridAdapter(persist_dir: str = "memory/chroma",
                   embedding_fn=None) -> HybridMemory:
    """Factory function for hybrid memory adapter.

    Args:
        persist_dir: ChromaDB persistence directory.
        embedding_fn: Optional ChromaDB-compatible embedding function from
                      EmbeddingProvider.as_chromadb_function(). If None,
                      ChromaDB uses its built-in default model.
    """
    return HybridMemory(persist_dir=persist_dir, embedding_fn=embedding_fn)
