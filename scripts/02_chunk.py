"""Stage 2 - turn the pages into retrievable chunks.

Usage:
    python scripts/02_chunk.py --strategy structure
    python scripts/02_chunk.py --strategy fixed --size 500 --overlap 50

Writes:
    data/chunks.json   the chunk list, in the Day 7 chunk schema
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from _common import ROOT, banner, read_json, write_json

from src.chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP, DEFAULT_SIZE, build_chunks
from src.models import DocumentPage


def main() -> None:
    parser = argparse.ArgumentParser(description="Chia pages thành chunks")
    parser.add_argument("--pages", type=Path, default=ROOT / "data" / "pages.json")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "chunks.json")
    parser.add_argument(
        "--strategy",
        choices=("structure", "fixed"),
        default="structure",
        help="structure = theo heading; fixed = cửa sổ ký tự cố định",
    )
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    args = parser.parse_args()

    banner("Stage 2 · Chunking")
    raw = read_json(args.pages)
    pages = [DocumentPage.from_dict(p) for p in raw]
    print(f"pages: {len(pages)}")

    chunks = build_chunks(
        pages,
        args.strategy,
        max_chars=args.max_chars,
        size=args.size,
        overlap=args.overlap,
    )

    lengths = [len(c.text) for c in chunks]
    pages_covered = sorted({c.page for c in chunks})
    print(f"strategy       : {args.strategy}")
    print(f"chunks         : {len(chunks)}")
    if lengths:
        print(f"chars mean/min/max: {statistics.mean(lengths):.0f} / {min(lengths)} / {max(lengths)}")
    print(f"pages covered  : {len(pages_covered)}")

    write_json(args.out, [c.to_dict() for c in chunks])
    print(f"\nĐã ghi {args.out}")
    print("Chạy stage 3 để embed và lưu vào vector store.")


if __name__ == "__main__":
    main()
