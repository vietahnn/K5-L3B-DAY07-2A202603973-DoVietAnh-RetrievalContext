"""Persistent vector store backed by ChromaDB.

Why ChromaDB
-------------
* ``PersistentClient`` stores the collection on disk, so the index survives
  between runs - a hard requirement of the lab.
* It performs vector similarity search and supports **metadata filtering**,
  which the filtered configuration needs.
* It is a real vector database, not a hand-rolled NumPy search.

Embedding vectors are always computed by the application and passed in
explicitly (``embeddings=[...]`` on write, ``query_embeddings=[...]`` on
read). ChromaDB ships no bge-m3 embedding function, and computing them
in-process is what makes the same vectors reusable for hybrid fusion and for
logging.

Metric: the collection uses ``hnsw:space="cosine"``. The embedding model
returns L2-normalized vectors, so cosine and inner product rank identically,
but cosine keeps the reported distance readable as ``1 - similarity``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import FILTERABLE_FIELDS, Chunk


class StoreError(Exception):
    """Raised when the vector store cannot satisfy a request."""


class ChromaStore:
    """Persistent ChromaDB store, one collection per chunking strategy."""

    def __init__(self, persist_dir: Path, dimensions: int, *, prefix: str = "day7") -> None:
        self.persist_dir = Path(persist_dir)
        self.dimensions = dimensions
        self.prefix = prefix
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        import chromadb
        from chromadb.config import Settings

        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collections: dict[str, Any] = {}

    def collection_name(self, strategy: str) -> str:
        return f"{self.prefix}_{strategy.lower()}"

    def collection(self, strategy: str):
        name = self.collection_name(strategy)
        if name not in self._collections:
            self._collections[name] = self.client.get_or_create_collection(
                name=name,
                metadata={
                    "hnsw:space": "cosine",
                    "embedding_model": "BAAI/bge-m3",
                    "dimensions": self.dimensions,
                    "strategy": strategy,
                },
            )
        return self._collections[name]

    # -- write path ---------------------------------------------------------

    def upsert(self, strategy: str, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Insert or replace chunks by stable id (no duplicates on re-run)."""
        if len(chunks) != len(vectors):
            raise StoreError(f"số chunk và số vector không khớp: {len(chunks)} vs {len(vectors)}")
        for vector in vectors:
            if len(vector) != self.dimensions:
                raise StoreError(
                    f"vector có {len(vector)} chiều, cần {self.dimensions}. "
                    "Model embedding và vector store phải dùng chung không gian."
                )

        collection = self.collection(strategy)
        # Remove stale chunks from a previous ingest of changed source data.
        existing = collection.get(include=[])["ids"]
        stale = [cid for cid in existing if cid not in {c.chunk_id for c in chunks}]
        if stale:
            collection.delete(ids=stale)

        if not chunks:
            return
        collection.upsert(
            ids=[c.chunk_id for c in chunks],
            documents=[c.text for c in chunks],
            embeddings=[list(map(float, v)) for v in vectors],
            metadatas=[self._metadata(c) for c in chunks],
        )

    @staticmethod
    def _metadata(chunk: Chunk) -> dict[str, Any]:
        """Flatten chunk metadata into Chroma's scalar-only metadata schema."""
        flat: dict[str, Any] = {
            "document_id": chunk.document_id,
            "source": chunk.source,
            "page": int(chunk.page),
        }
        for key, value in (chunk.metadata or {}).items():
            if key in ("document_id", "source", "page"):
                continue
            if value is None:
                continue
            if isinstance(value, bool):
                flat[key] = value
            elif isinstance(value, (int, float, str)):
                flat[key] = value
            else:
                # Chroma metadata values must be scalars; JSON-encode the rest.
                flat[key] = str(value)
        return flat

    # -- read path ----------------------------------------------------------

    def search(
        self,
        strategy: str,
        query_vector: list[float],
        top_k: int = 3,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the top-k chunks for a query, optionally filtered by metadata.

        ``filters`` uses Chroma's ``where`` semantics: a dict of
        ``field -> value`` is an AND over fields. Values must be scalars.
        """
        if len(query_vector) != self.dimensions:
            raise StoreError(
                f"query vector có {len(query_vector)} chiều, cần {self.dimensions}"
            )
        collection = self.collection(strategy)
        where = None
        if filters:
            cleaned = {k: v for k, v in filters.items() if v is not None}
            if cleaned:
                unknown = set(cleaned) - set(FILTERABLE_FIELDS)
                if unknown:
                    raise StoreError(
                        f"filter trên trường không lọc được: {sorted(unknown)}. "
                        f"Cho phép: {list(FILTERABLE_FIELDS)}"
                    )
                # Chroma requires a single operator at the top level; several
                # equality constraints are expressed with $and.
                if len(cleaned) == 1:
                    where = cleaned
                else:
                    where = {"$and": [{key: value} for key, value in sorted(cleaned.items())]}

        result = collection.query(
            query_embeddings=[list(map(float, query_vector))],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        hits: list[dict[str, Any]] = []
        for rank, chunk_id in enumerate(ids):
            distance = float(distances[rank]) if rank < len(distances) else None
            metadata = dict(metadatas[rank] or {}) if rank < len(metadatas) else {}
            hits.append(
                {
                    "id": chunk_id,
                    "rank": rank + 1,
                    "text": documents[rank] if rank < len(documents) else "",
                    "metadata": metadata,
                    "source_id": metadata.get("document_id"),
                    "source": metadata.get("source"),
                    "page": metadata.get("page"),
                    "distance": distance,
                    # cosine space: distance = 1 - similarity
                    "similarity": (1 - distance) if distance is not None else None,
                }
            )
        return hits

    def count(self, strategy: str) -> int:
        return self.collection(strategy).count()

    def reset(self, strategy: str) -> None:
        """Drop a collection entirely."""
        name = self.collection_name(strategy)
        self._collections.pop(name, None)
        try:
            self.client.delete_collection(name)
        except Exception:  # collection did not exist
            pass

    def all_chunks(self, strategy: str) -> list[dict[str, Any]]:
        """Dump every stored chunk (used to rebuild the lexical index)."""
        collection = self.collection(strategy)
        data = collection.get(include=["documents", "metadatas"])
        return [
            {
                "id": cid,
                "text": text,
                "metadata": dict(meta or {}),
                "source_id": (meta or {}).get("document_id"),
                "source": (meta or {}).get("source"),
                "page": (meta or {}).get("page"),
            }
            for cid, text, meta in zip(
                data.get("ids", []),
                data.get("documents", []),
                data.get("metadatas", []),
            )
        ]
