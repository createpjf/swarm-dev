"""
adapters/memory/embedding.py — Pluggable embedding provider abstraction.

Supports multiple embedding backends:
  - chromadb_default: ChromaDB's built-in (all-MiniLM-L6-v2, ~384 dims)
  - openai: OpenAI text-embedding-3-small/large (1536/3072 dims)
  - local: sentence-transformers on CPU (no API key needed)

Usage:
    from adapters.memory.embedding import get_embedding_provider

    provider = get_embedding_provider(config)
    vectors = provider.embed(["hello world", "test query"])

ChromaDB integration:
    provider.as_chromadb_function()  → chromadb.EmbeddingFunction compatible
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import List, Optional

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier."""
        ...

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Embedding vector dimensionality."""
        ...

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for a list of texts.

        Args:
            texts: List of strings to embed.

        Returns:
            List of float vectors, one per input text.
        """
        ...

    def as_chromadb_function(self):
        """Return a ChromaDB-compatible embedding function.

        ChromaDB expects a callable with __call__(self, input: Documents)
        that returns Embeddings (list of float lists).
        """
        provider = self

        class _ChromaDBEmbeddingFunction:
            def __call__(self, input: List[str]) -> List[List[float]]:
                return provider.embed(input)

        return _ChromaDBEmbeddingFunction()


# ── Implementations ──────────────────────────────────────────────────────────


class ChromaDBDefaultProvider(EmbeddingProvider):
    """Use ChromaDB's built-in default embedding model (all-MiniLM-L6-v2)."""

    @property
    def name(self) -> str:
        return "chromadb_default"

    @property
    def dimensions(self) -> int:
        return 384

    def embed(self, texts: List[str]) -> List[List[float]]:
        # ChromaDB handles this internally — this provider is a no-op marker
        raise NotImplementedError(
            "ChromaDBDefaultProvider.embed() should not be called directly. "
            "Use ChromaDB's default embedding by not specifying an embedding_function."
        )

    def as_chromadb_function(self):
        # Return None to let ChromaDB use its default
        return None


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI text-embedding-3-small/large via httpx (no openai SDK needed)."""

    def __init__(self, model: str = "text-embedding-3-small",
                 api_key: str = "",
                 base_url: str = "",
                 batch_size: int = 100):
        self._model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = (base_url
                          or os.environ.get("OPENAI_BASE_URL", "")
                          or "https://api.openai.com/v1")
        self._batch_size = batch_size

        # Dimension lookup
        self._dim_map = {
            "text-embedding-3-small": 1536,
            "text-embedding-3-large": 3072,
            "text-embedding-ada-002": 1536,
        }

        if not self._api_key:
            raise ValueError(
                "OpenAI embedding provider requires OPENAI_API_KEY env var")

    @property
    def name(self) -> str:
        return f"openai:{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dim_map.get(self._model, 1536)

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Batch-embed texts via OpenAI API."""
        import httpx

        all_embeddings: List[List[float]] = []
        url = f"{self._base_url.rstrip('/')}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Process in batches
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i:i + self._batch_size]
            # Ensure non-empty strings (OpenAI rejects empty)
            batch = [t if t.strip() else " " for t in batch]

            try:
                resp = httpx.post(
                    url,
                    headers=headers,
                    json={"input": batch, "model": self._model},
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
                # Sort by index to ensure order matches input
                embeddings = sorted(data["data"], key=lambda x: x["index"])
                all_embeddings.extend([e["embedding"] for e in embeddings])
            except Exception as e:
                logger.error("OpenAI embedding failed (batch %d): %s",
                             i // self._batch_size, e)
                # Return zero vectors as fallback for this batch
                all_embeddings.extend(
                    [[0.0] * self.dimensions for _ in batch])

        return all_embeddings


class FlockEmbeddingProvider(EmbeddingProvider):
    """Flock AI embedding provider (OpenAI-compatible API)."""

    def __init__(self, model: str = "text-embedding-3-small",
                 api_key: str = "",
                 base_url: str = "",
                 batch_size: int = 100):
        self._api_key = api_key or os.environ.get("FLOCK_API_KEY", "")
        self._base_url = (base_url
                          or os.environ.get("FLOCK_BASE_URL", "")
                          or "https://api.flock.io/v1")
        # Delegate to OpenAI-compatible implementation
        self._inner = OpenAIEmbeddingProvider(
            model=model,
            api_key=self._api_key,
            base_url=self._base_url,
            batch_size=batch_size,
        )

    @property
    def name(self) -> str:
        return f"flock:{self._inner._model}"

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    def embed(self, texts: List[str]) -> List[List[float]]:
        return self._inner.embed(texts)


class LocalEmbeddingProvider(EmbeddingProvider):
    """Local sentence-transformers model (no API key needed)."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self._model_name = model_name
        self._model = None

        # Dimension lookup for common models
        self._dim_map = {
            "all-MiniLM-L6-v2": 384,
            "all-MiniLM-L12-v2": 384,
            "all-mpnet-base-v2": 768,
            "paraphrase-multilingual-MiniLM-L12-v2": 384,
        }

    @property
    def name(self) -> str:
        return f"local:{self._model_name}"

    @property
    def dimensions(self) -> int:
        return self._dim_map.get(self._model_name, 384)

    def _load_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self._model_name)
                logger.info("Loaded local embedding model: %s",
                            self._model_name)
            except ImportError:
                raise ImportError(
                    "sentence-transformers not installed. "
                    "Install with: pip install sentence-transformers")

    def embed(self, texts: List[str]) -> List[List[float]]:
        self._load_model()
        embeddings = self._model.encode(texts, show_progress_bar=False)
        return [e.tolist() for e in embeddings]


# ── Factory ──────────────────────────────────────────────────────────────────


def get_embedding_provider(config: Optional[dict] = None) -> EmbeddingProvider:
    """Create an embedding provider from configuration.

    Config format (in agents.yaml):
        memory:
          embedding:
            provider: openai          # openai | flock | local | chromadb_default
            model: text-embedding-3-small
            api_key_env: OPENAI_API_KEY
            base_url_env: OPENAI_BASE_URL

    Falls back to chromadb_default if no config or provider unavailable.
    """
    if not config:
        return ChromaDBDefaultProvider()

    emb_config = config.get("memory", {}).get("embedding", {})
    if not emb_config:
        return ChromaDBDefaultProvider()

    provider_name = emb_config.get("provider", "chromadb_default")
    model = emb_config.get("model", "text-embedding-3-small")

    # Resolve API key from env
    api_key_env = emb_config.get("api_key_env", "")
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    base_url_env = emb_config.get("base_url_env", "")
    base_url = os.environ.get(base_url_env, "") if base_url_env else ""

    try:
        if provider_name == "openai":
            return OpenAIEmbeddingProvider(
                model=model, api_key=api_key, base_url=base_url)
        elif provider_name == "flock":
            return FlockEmbeddingProvider(
                model=model, api_key=api_key, base_url=base_url)
        elif provider_name == "local":
            return LocalEmbeddingProvider(model_name=model)
        elif provider_name == "chromadb_default":
            return ChromaDBDefaultProvider()
        else:
            logger.warning("Unknown embedding provider '%s', "
                           "falling back to chromadb_default", provider_name)
            return ChromaDBDefaultProvider()
    except Exception as e:
        logger.warning("Failed to create embedding provider '%s': %s. "
                       "Falling back to chromadb_default.", provider_name, e)
        return ChromaDBDefaultProvider()
