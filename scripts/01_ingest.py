"""Stage 1 - read the source PDF into pages and a data manifest.

Usage:
    python scripts/01_ingest.py --pdf "Poverty, Prosperity and Planet Report 2026 - Overview.pdf"

Writes:
    data/pages.json     every physical page, with its blocks and metadata
    data/manifest.json  source, licence, page counts, exclusions, errors
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _common import ROOT, banner, write_json

from src.ingestion import (
    assign_document_metadata,
    build_manifest,
    content_pages,
    load_pdf,
)

#: The document this lab indexes.
DOCUMENT_ID = "ppp2026-overview"
TITLE = (
    "Poverty, Prosperity, and Planet Report 2026: Jobs at the Center of "
    "Poverty Eradication - Overview booklet"
)
PDF_NAME = "Poverty, Prosperity and Planet Report 2026 - Overview.pdf"
PUBLISHER = "International Bank for Reconstruction and Development / The World Bank"
LICENSE_NAME = "CC BY 3.0 IGO (Creative Commons Attribution 3.0 IGO)"
LICENSE_URL = "http://creativecommons.org/licenses/by/3.0/igo"
DOI = "10.1596/978-1-4648-2342-8"
SOURCE_URL = "https://hdl.handle.net/10986/45652"
VERSION = (
    "Overview booklet, printed 2026 (SKU 33975); no published revision date is "
    "stated in the document, so none is asserted"
)
ACCESSED_AT = "2026-11-16"

#: Pages that carry no substantive prose, with the reason. Recorded verbatim in
#: the manifest so the counting rule is auditable.
EXCLUDED_PAGES: dict[int, str] = {
    1: "cover page (image + title only)",
    2: "blank page",
    5: "inner half-title (duplicate of cover title)",
    7: "table of contents",
    8: "blank page",
    35: "blank page",
}

DOCUMENT_TYPE_BY_SECTION = {
    "Foreword": "foreword",
    "Acknowledgments": "acknowledgments",
    "About the Team": "author_bios",
    "Main Messages": "main_messages",
    "Overview": "chapter_body",
    "Front Matter": "front_matter",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Đọc PDF thành pages + manifest")
    parser.add_argument(
        "--pdf",
        type=Path,
        default=ROOT / PDF_NAME,
        help="đường dẫn tới file PDF nguồn",
    )
    parser.add_argument("--out", type=Path, default=ROOT / "data", help="thư mục output")
    args = parser.parse_args()

    banner("Stage 1 · Ingestion")
    print(f"PDF: {args.pdf}")

    pages, errors = load_pdf(
        args.pdf, DOCUMENT_ID, PDF_NAME, excluded_pages=EXCLUDED_PAGES
    )
    assign_document_metadata(
        pages,
        title=TITLE,
        document_type_by_section=DOCUMENT_TYPE_BY_SECTION,
        excluded_pages=EXCLUDED_PAGES,
    )

    counted = content_pages(pages)
    print(f"physical pages : {len(pages)}")
    print(f"content pages  : {len(counted)}")
    print(f"errors         : {len(errors)}")
    for error in errors:
        print(f"  p{error['page']:>3} {error['kind']}: {error['message']}")

    manifest = build_manifest(
        document_id=DOCUMENT_ID,
        title=TITLE,
        source=PDF_NAME,
        pages=pages,
        errors=errors,
        publisher=PUBLISHER,
        license_name=LICENSE_NAME,
        license_url=LICENSE_URL,
        accessed_at=ACCESSED_AT,
        doi=DOI,
        source_url=SOURCE_URL,
        version=VERSION,
    )

    write_json(args.out / "pages.json", [p.to_dict() for p in pages])
    write_json(args.out / "manifest.json", manifest)
    print(f"\nĐã ghi {args.out / 'pages.json'}")
    print(f"Đã ghi {args.out / 'manifest.json'}")
    print("\nKiểm tra manifest rồi chạy stage 2.")


if __name__ == "__main__":
    main()
