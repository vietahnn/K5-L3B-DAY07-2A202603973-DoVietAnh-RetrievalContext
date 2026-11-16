"""The retrieval methods under comparison.

Four methods, all returning the same record shape so that they can be
benchmarked against one another on the same corpus, the same questions and
the same labels:

``dense``
    Embed the query with bge-m3 and take the nearest chunks from the vector
    store. This is the method the lab requires, and it is the baseline.

``bm25``
    Okapi BM25 over the lexical index. It matches on exact terms, which is
    what rescues questions phrased around a specific figure or a proper noun
    ("811 million", "BAAI", "Gini") where a dense embedding can drift.

``hybrid``
    Reciprocal Rank Fusion of the dense and BM25 rankings. RRF is used rather
    than score fusion because BM25 scores are unbounded and cosine scores are
    not; putting them on a common scale would need a normalization that would
    itself become a tunable parameter. RRF only reads the *ranks*, so it needs
    no normalization at all.

``rerank``
    The hybrid candidate list, re-scored by a cross-encoder that reads the
    query and the chunk together. It costs one extra forward pass per
    candidate, so its latency is measured and reported separately.

Every method returns a list of dicts with ``id``, ``text``, ``score``,
``rank``, ``source_id``, ``source``, ``page`` and ``metadata``.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from typing import Any, Protocol

from .textutil import tokenize

#: Okapi BM25 term-frequency saturation parameter.
BM25_K1 = 1.5

#: Okapi BM25 length-normalization parameter.
BM25_B = 0.75

#: The RRF constant. 60 is the value used in the original RRF paper and in
#: most public implementations; it is left configurable so the choice can be
#: justified rather than assumed.
RRF_K = 60


class Retriever(Protocol):
    name: str

    def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        ...


# ---------------------------------------------------------------------------
# Lexical index
# ---------------------------------------------------------------------------


class BM25Index:
    """Okapi BM25 over a fixed list of chunks.

    The index is built once from the chunk texts and then queried many times,
    so the term frequencies and document frequencies are precomputed. Nothing
    here needs a third-party package, which keeps the install small.
    """

    def __init__(self, chunks: list[dict[str, Any]], *, k1: float = BM25_K1, b: float = BM25_B):
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.doc_tokens: list[list[str]] = [tokenize(c.get("text", "")) for c in chunks]
        self.doc_len = [len(tokens) for tokens in self.doc_tokens]
        self.avg_len = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.n_docs = len(chunks)

        frequencies = [Counter(tokens) for tokens in self.doc_tokens]
        df: Counter = Counter()
        for row in frequencies:
            df.update(row.keys())
        self.df = df
        # Lucene's IDF variant, which is positive for every term and so keeps
        # the score comparable across terms.
        self.idf = {
            term: math.log(1.0 + (self.n_docs - count + 0.5) / (count + 0.5))
            for term, count in df.items()
        }
        self.term_freqs = frequencies

    def _score(self, query_tokens: list[str], index: int) -> float:
        score = 0.0
        freqs = self.term_freqs[index]
        doc_len = self.doc_len[index]
        for term in query_tokens:
            tf = freqs.get(term)
            if not tf:
                continue
            idf = self.idf.get(term, 0.0)
            denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / (self.avg_len or 1.0))
            score += idf * (tf * (self.k1 + 1)) / denominator
        return score

    def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        scored: list[tuple[float, int]] = []
        for index in range(self.n_docs):
            if not _matches_filters(self.chunks[index], filters):
                continue
            score = self._score(query_tokens, index)
            if score > 0:
                scored.append((score, index))
        # Ties are broken by chunk id so the ranking is deterministic.
        scored.sort(key=lambda pair: (-pair[0], self.chunks[pair[1]]["id"]))
        return [
            {**self.chunks[index], "score": score, "rank": rank}
            for rank, (score, index) in enumerate(scored[:top_k], start=1)
        ]


def _matches_filters(chunk: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    """True when the chunk satisfies every filter field."""
    if not filters:
        return True
    metadata = chunk.get("metadata") or {}
    for field, value in filters.items():
        if value is None:
            continue
        if metadata.get(field) != value:
            return False
    return True


# ---------------------------------------------------------------------------
# Dense
# ---------------------------------------------------------------------------


class DenseRetriever:
    """Dense retrieval over the ChromaDB vector store."""

    name = "dense"

    def __init__(self, store, embedder, strategy: str = "structure") -> None:
        self.store = store
        self.embedder = embedder
        self.strategy = strategy

    def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        vector = self.embedder.embed_query(query)
        hits = self.store.search(self.strategy, vector, top_k=top_k, filters=filters)
        for hit in hits:
            hit["score"] = hit.get("similarity")
        return hits


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    rankings: list[list[dict[str, Any]]], *, k: int = RRF_K
) -> list[dict[str, Any]]:
    """Fuse several rankings by reciprocal rank.

    ``score(d) = sum over rankings of 1 / (k + rank(d))``. A document that is
    absent from a ranking contributes nothing from that ranking.
    """
    scores: dict[str, float] = {}
    documents: dict[str, dict[str, Any]] = {}

    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            doc_id = doc["id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            documents.setdefault(doc_id, doc)

    fused = [
        {**documents[doc_id], "score": score, "rank": rank}
        for rank, (doc_id, score) in enumerate(
            sorted(scores.items(), key=lambda pair: (-pair[1], pair[0])), start=1
        )
    ]
    return fused


# ---------------------------------------------------------------------------
# Hybrid and rerank
# ---------------------------------------------------------------------------


class HybridRetriever:
    """Reciprocal Rank Fusion of the dense and BM25 rankings.

    Each arm is asked for a deeper candidate list than the final top-k, so
    that a document ranked 4th by both arms can still surface.
    """

    name = "hybrid"

    def __init__(
        self,
        dense: DenseRetriever,
        lexical: BM25Index,
        *,
        candidates: int = 20,
        rrf_k: int = RRF_K,
    ) -> None:
        self.dense = dense
        self.lexical = lexical
        self.candidates = candidates
        self.rrf_k = rrf_k

    def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        dense_hits = self.dense.search(query, filters=filters, top_k=self.candidates)
        lexical_hits = self.lexical.search(query, filters=filters, top_k=self.candidates)
        fused = reciprocal_rank_fusion([dense_hits, lexical_hits], k=self.rrf_k)
        return fused[:top_k]


class RerankRetriever:
    """A cross-encoder re-scores the candidate list produced by another method.

    The reranker reads the query and the chunk together, so it can tell that
    a chunk mentioning "the extreme poverty line" is a better answer to a
    question about the $3.00 line than a chunk that merely repeats the word
    "poverty". That judgement is exactly what a bi-encoder embedding cannot
    express, and it is why this configuration is a genuine improvement
    candidate rather than a re-ranking of the same signal.
    """

    name = "rerank"

    def __init__(
        self,
        base: Retriever,
        *,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        candidates: int = 20,
        device: str | None = None,
    ) -> None:
        self.base = base
        self.candidates = candidates
        self.model_name = model_name
        self._device = device
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:  # pragma: no cover - depends on the machine
                raise RuntimeError(
                    "sentence-transformers chưa được cài; cần cho reranker."
                ) from exc
            self._model = CrossEncoder(self.model_name, device=self._device)
        return self._model

    def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        candidates = self.base.search(query, filters=filters, top_k=self.candidates)
        if not candidates:
            return []
        model = self._load()
        pairs = [(query, doc.get("text", "")) for doc in candidates]
        scores = model.predict(pairs)
        reranked = [
            {**doc, "score": float(score), "rerank_score": float(score)}
            for doc, score in zip(candidates, scores)
        ]
        reranked.sort(key=lambda doc: (-doc["score"], doc["id"]))
        for rank, doc in enumerate(reranked, start=1):
            doc["rank"] = rank
        return reranked[:top_k]


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def timed_search(
    retriever: Retriever, query: str, *, filters: dict[str, Any] | None, top_k: int
) -> tuple[list[dict[str, Any]], float]:
    """Run one search and return its results with the elapsed milliseconds."""
    started = time.perf_counter()
    results = retriever.search(query, filters=filters, top_k=top_k)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return results, elapsed_ms
