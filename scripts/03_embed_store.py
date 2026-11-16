"""Stage 3 - embed the chunks with bge-m3 and store them in the vector store.

Usage:
    python scripts/03_embed_store.py

The first run downloads BAAI/bge-m3 (~2.2GB) into the HuggingFace cache and
takes a few minutes. Later runs reuse the cache.

Writes:
    .chroma/          the persistent ChromaDB index
    data/embed_info.json  the model, dimension, count and timing
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from _common import ROOT, banner, read_json, write_json

from src.embeddings import build_embedder
from src.models import Chunk
from src.store import ChromaStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed chunks và lưu vào Chroma")
    parser.add_argument("--chunks", type=Path, default=ROOT / "data" / "chunks.json")
    parser.add_argument("--index", type=Path, default=ROOT / ".chroma")
    parser.add_argument(
        "--strategy",
        default="structure",
        help="tên collection; phải khớp với chunking đã dùng",
    )
    parser.add_argument(
        "--reset", action="store_true", help="xoá collection trước khi ghi lại"
    )
    args = parser.parse_args()

    banner("Stage 3 · Embedding + vector store")

    chunks = [Chunk.from_dict(c) for c in read_json(args.chunks)]
    print(f"chunks: {len(chunks)}")

    embedder = build_embedder()
    print(f"embedding model: {embedder.model_name} ({embedder.dimensions} dims)")

    started = time.perf_counter()
    vectors = embedder.embed_documents([c.text for c in chunks])
    embed_seconds = time.perf_counter() - started
    print(f"embedded {len(vectors)} chunks trong {embed_seconds:.1f}s")

    store = ChromaStore(args.index, embedder.dimensions)
    if args.reset:
        print(f"reset collection '{args.strategy}'")
        store.reset(args.strategy)

    started = time.perf_counter()
    store.upsert(args.strategy, chunks, vectors)
    store_seconds = time.perf_counter() - started
    print(f"stored {store.count(args.strategy)} chunks trong {store_seconds:.1f}s")

    write_json(
        ROOT / "data" / "embed_info.json",
        {
            "model": embedder.model_name,
            "dimensions": embedder.dimensions,
            "strategy": args.strategy,
            "chunk_count": len(chunks),
            "embed_seconds": round(embed_seconds, 2),
            "store_seconds": round(store_seconds, 2),
            "index_dir": str(args.index),
        },
    )
    print("\nĐã ghi data/embed_info.json")
    print("Chạy stage 4 để benchmark các phương pháp retrieval trên dev.")


if __name__ == "__main__":
    main()
