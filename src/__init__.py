"""Day 7 pipeline: ingestion, retrieval, and the agent that uses them.

Stages
------
``ingestion``   PDF -> pages, with the document's structure recovered
``chunking``    pages -> retrievable chunks
``embeddings``  bge-m3 vectors for chunks and queries
``store``       persistent ChromaDB vector store
``retrievers``  dense, BM25, hybrid (RRF) and rerank
``benchmark``   the question contract and the retrieval metrics
``agent``       the model-driven search loop over ``starter/agentic.py``
"""

from .models import Chunk, DocumentPage  # noqa: F401

__all__ = ["Chunk", "DocumentPage"]
