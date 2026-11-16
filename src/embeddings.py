"""The embedding provider.

The model is **BAAI/bge-m3** (1024 dimensions), run locally through
sentence-transformers. Two properties matter for the rest of the pipeline:

* **No instruction prefix.** Unlike ``bge-large-en-v1.5``, bge-m3 is trained
  symmetrically, so a query and a passage are encoded identically. Adding a
  "Represent this sentence for searching relevant passages: " prefix would
  put queries in a different region of the embedding space than the passages
  that were indexed, and would quietly cost recall.
* **L2-normalized.** Vectors are unit length, so an inner-product index and a
  cosine index rank identically, and a distance can be read as a similarity.

The backend is pluggable. ``local`` runs the model in-process; ``api`` calls an
OpenAI-compatible ``/v1/embeddings`` endpoint, which is useful when the
provider already serves bge-m3 and a local torch install is not wanted. Both
backends must produce the same 1024-dimensional vectors for the same model,
and the dimension is asserted when the index is built so a silent mismatch
cannot reach the store.
"""

from __future__ import annotations

import os
from typing import Protocol

MODEL_NAME = "BAAI/bge-m3"
MODEL_DIMENSIONS = 1024
MODEL_PROVIDER = "local (sentence-transformers)"

#: bge-m3 supports 8192 tokens, but the chunks here are at most a few hundred
#: tokens. A shorter limit avoids paying the attention cost of a window that
#: can never be filled.
MAX_SEQ_LENGTH = 512

#: How many texts to encode in one batch.
BATCH_SIZE = 16


class EmbeddingError(Exception):
    """Raised when the embedding backend cannot produce vectors."""


class Embedder(Protocol):
    model_name: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order."""
        ...

    def embed_query(self, question: str) -> list[float]:
        """Return a query vector compatible with the stored document vectors."""
        ...


class LocalBgeM3:
    """Local, offline bge-m3 through sentence-transformers."""

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        *,
        normalize: bool = True,
        max_seq_length: int = MAX_SEQ_LENGTH,
        device: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.dimensions = MODEL_DIMENSIONS
        self._normalize = normalize
        self._model = self._load(model_name, max_seq_length, device)

    @staticmethod
    def _load(model_name: str, max_seq_length: int, device: str | None):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on the machine
            raise EmbeddingError(
                "sentence-transformers chưa được cài. Chạy: "
                "pip install sentence-transformers torch --index-url "
                "https://download.pytorch.org/whl/cpu"
            ) from exc
        model = SentenceTransformer(model_name, device=device)
        model.max_seq_length = max_seq_length
        return model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            texts,
            batch_size=BATCH_SIZE,
            normalize_embeddings=self._normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [list(map(float, vector)) for vector in vectors]

    def embed_query(self, question: str) -> list[float]:
        return self.embed_documents([question])[0]


class ApiBgeM3:
    """bge-m3 served by an OpenAI-compatible ``/v1/embeddings`` endpoint.

    The endpoint must return one 1024-dimensional vector per input text, in
    the same order, L2-normalized or not - the values are normalized here so
    that the two backends are interchangeable.
    """

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        dimensions: int = MODEL_DIMENSIONS,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        self._batch_size = batch_size
        self._base_url = base_url or os.environ.get("EMBED_BASE_URL")
        self._api_key = api_key or os.environ.get("EMBED_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not self._base_url:
            raise EmbeddingError(
                "EMBED_BASE_URL chưa được đặt. Cần base URL của endpoint "
                "OpenAI-compatible phục vụ bge-m3."
            )
        from openai import OpenAI

        self._client = OpenAI(base_url=self._base_url, api_key=self._api_key or "not-needed")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            response = self._client.embeddings.create(model=self.model_name, input=batch)
            for item in sorted(response.data, key=lambda d: d.index):
                vectors.append(_normalize(list(map(float, item.embedding))))
        return vectors

    def embed_query(self, question: str) -> list[float]:
        return self.embed_documents([question])[0]


def _normalize(vector: list[float]) -> list[float]:
    norm = sum(value * value for value in vector) ** 0.5
    if not norm:
        return vector
    return [value / norm for value in vector]


def build_embedder(backend: str | None = None, model_name: str | None = None) -> Embedder:
    """Factory for the embedder, selected by ``EMBED_BACKEND``.

    ``local`` (default) runs bge-m3 in-process. ``api`` calls an
    OpenAI-compatible embeddings endpoint. Both produce the same vectors for
    the same model.
    """
    backend = (backend or os.environ.get("EMBED_BACKEND") or "local").strip().lower()
    model_name = model_name or os.environ.get("EMBED_MODEL") or MODEL_NAME
    if backend == "local":
        return LocalBgeM3(model_name)
    if backend == "api":
        return ApiBgeM3(model_name)
    raise EmbeddingError(f"EMBED_BACKEND không hợp lệ: {backend!r} (chọn 'local' hoặc 'api')")
