"""Read the source PDF into page-level, paragraph-aware units.

How the structure is recovered
------------------------------
The PDF stores text as *blocks* of *spans*, and every span carries a font
size and a font name. That typographic information is the only reliable
signal for "what is a heading and what is body prose" in a designed report
such as an annual report, where a heading is a visual distinction and
nothing else. Rather than hard-coding the point sizes of one particular
document, this module *derives* the thresholds from the document's own font
statistics: the size carrying the most characters is the body size, sizes
materially larger are headings, sizes materially smaller are figure chrome.

That makes the classifier work on a different report without edits, which is
the point of re-implementing the ingestion rather than copying it.

What is deliberately *not* done
------------------------------
Chart chrome, axis labels and folios are dropped from the indexed text, but
they are counted and reported, so a reader can tell how much of the document
was left out and why. Blank pages and pages excluded from the content count
are recorded as error entries with a reason; nothing vanishes silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from .models import DocumentPage
from .textutil import normalize_text

#: A heading must be at least this much larger than the body size to count as
#: a heading rather than as emphasis inside a paragraph.
HEADING_SIZE_RATIO = 1.18

#: A block shorter than this many characters is a label (a topic tag, a figure
#: caption), not prose.
LABEL_MAX_CHARS = 40

#: Prose needs at least this many alphabetic characters to be treated as a
#: body paragraph rather than as a stray fragment.
MIN_PROSE_CHARS = 60

#: Topic labels recognised inside the "Main Messages" chapter. The labels are
#: printed as short bold words that head a block of prose.
TOPIC_LABELS = {"poverty", "prosperity", "planet", "jobs"}

#: Running heads that name a section of the report.
SECTION_ALIASES = {
    "main messages": "Main Messages",
    "overview": "Overview",
    "foreword": "Foreword",
    "acknowledgments": "Acknowledgments",
    "about the team": "About the Team",
}


class IngestionError(Exception):
    """Raised when the PDF cannot be used at all."""


# ---------------------------------------------------------------------------
# Font statistics
# ---------------------------------------------------------------------------


def _block_text(block: dict[str, Any]) -> str:
    """Reconstruct a block's text from its lines and spans.

    ``get_text("dict")`` blocks carry no ``text`` key of their own, so the
    text has to be rebuilt from the spans.
    """
    parts: list[str] = []
    for line in block.get("lines", []):
        line_text = "".join(span.get("text", "") for span in line.get("spans", []))
        if line_text.strip():
            parts.append(line_text)
    return "\n".join(parts)


def _block_spans(block: dict[str, Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            if span.get("text", "").strip():
                spans.append(span)
    return spans


def _block_max_size(block: dict[str, Any]) -> float:
    """The largest font size in the block."""
    return max(
        (round(float(span.get("size", 0.0)), 1) for span in _block_spans(block)),
        default=0.0,
    )


def _block_font(block: dict[str, Any]) -> str:
    """The dominant font of the block, by character count."""
    weights: dict[str, int] = {}
    for span in _block_spans(block):
        font = span.get("font", "")
        weights[font] = weights.get(font, 0) + len(span.get("text", ""))
    return max(weights, key=lambda f: weights[f]) if weights else ""


def _alpha_len(text: str) -> int:
    return sum(1 for char in text if char.isalpha())


def body_font_size(pages_blocks: list[list[dict[str, Any]]]) -> float:
    """Derive the body font size from the document's own statistics.

    The size carrying the most characters is the size the prose is set in.
    Falling back to a default keeps the function total for a document with no
    extractable text.
    """
    weight: dict[float, int] = {}
    for blocks in pages_blocks:
        for block in blocks:
            size = block.get("size", 0.0)
            if not size:
                continue
            weight[size] = weight.get(size, 0) + len(block.get("text", ""))
    if not weight:
        return 10.0
    return max(weight, key=lambda s: weight[s])


def _is_bold(font: str) -> bool:
    lowered = font.casefold()
    return "bold" in lowered or "black" in lowered or "heavy" in lowered


def _classify_block(block: dict[str, Any], body_size: float) -> str | None:
    """Classify a PDF text block, or return ``None`` to drop it.

    ``running_head``  the section title printed at the top of a page
    ``subheading``    a heading inside the body of the chapter
    ``topic_label``   a short bold label that introduces a block of prose
    ``body``          prose, or figure chrome that carries prose
    """
    text = block.get("text", "")
    if not text:
        return None
    size = float(block.get("size", 0.0))
    font = block.get("font", "")
    alpha = _alpha_len(text)
    is_prose = alpha >= MIN_PROSE_CHARS and ("." in text or "?" in text)

    if size >= body_size * 1.6:
        return "running_head"
    if size >= body_size * HEADING_SIZE_RATIO:
        return "subheading"
    # A short, bold label set at body size introduces a topic.
    if alpha <= LABEL_MAX_CHARS and _is_bold(font) and size >= body_size * 0.95:
        return "topic_label"
    # Figure chrome worth keeping when it states something in prose.
    if text.startswith(("Sources:", "Source:", "Note:", "Panel ")) and alpha >= 25:
        return "body"
    if is_prose:
        return "body"
    # Everything else is chart axis labels, legends, folios and page numbers.
    return None


# ---------------------------------------------------------------------------
# Page loading
# ---------------------------------------------------------------------------


def load_pdf(
    path: Path,
    document_id: str,
    source: str,
    *,
    excluded_pages: dict[int, str] | None = None,
) -> tuple[list[DocumentPage], list[dict[str, Any]]]:
    """Extract every physical page of the PDF.

    Returns the page list and a list of error records. The error list covers
    pages with no extractable text, pages excluded from the content count,
    and pages whose blocks were dropped as figure chrome.
    """
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"Không tìm thấy PDF: {path}")
    excluded_pages = dict(excluded_pages or {})

    errors: list[dict[str, Any]] = []
    pages: list[DocumentPage] = []
    current_section: str | None = None
    current_topic: str | None = None
    current_subsection: str | None = None

    with fitz.open(path) as doc:
        total = doc.page_count

        # Two passes: the first reads the raw blocks of every page so the body
        # font size can be derived from the whole document, the second
        # classifies them against that size.
        raw_pages: list[list[dict[str, Any]]] = []
        for index in range(total):
            data = doc[index].get_text("dict")
            raw_blocks: list[dict[str, Any]] = []
            for block in data.get("blocks", []):
                if block.get("type", 0) != 0:
                    continue
                text = normalize_text(_block_text(block))
                if not text:
                    continue
                raw_blocks.append(
                    {
                        "text": text,
                        "size": _block_max_size(block),
                        "font": _block_font(block),
                        "bbox": [round(float(v), 1) for v in block.get("bbox", (0, 0, 0, 0))],
                    }
                )
            raw_pages.append(raw_blocks)

        derived_body_size = body_font_size(raw_pages)

        for index in range(total):
            physical = index + 1
            raw_blocks = raw_pages[index]

            kept: list[dict[str, Any]] = []
            dropped = 0
            for block in raw_blocks:
                kind = _classify_block(block, derived_body_size)
                if kind is None:
                    dropped += 1
                    continue
                kept.append({**block, "kind": kind})
            kept.sort(key=lambda b: (round(b["bbox"][1] / 6), b["bbox"][0]))

            body = "\n\n".join(b["text"] for b in kept if b["kind"] != "running_head")
            raw_len = len(normalize_text(doc[index].get_text()))

            if raw_len == 0:
                errors.append(
                    {
                        "page": physical,
                        "kind": "empty_page",
                        "message": "no extractable text (blank separator page)",
                    }
                )
            if dropped:
                errors.append(
                    {
                        "page": physical,
                        "kind": "figure_chrome_dropped",
                        "message": f"{dropped} text blocks dropped as "
                        "chart labels, legends or folios",
                    }
                )

            for block in kept:
                if block["kind"] == "running_head":
                    resolved = SECTION_ALIASES.get(block["text"].strip().casefold())
                    if resolved:
                        current_section = resolved
                        # Topic labels only scope the Main Messages chapter.
                        if resolved != "Main Messages":
                            current_topic = None
                        current_subsection = None
                elif block["kind"] == "subheading":
                    current_subsection = block["text"].strip()
                elif block["kind"] == "topic_label":
                    label = block["text"].strip().casefold()
                    if label in TOPIC_LABELS and current_section == "Main Messages":
                        current_topic = label.capitalize()

            if physical in excluded_pages:
                errors.append(
                    {
                        "page": physical,
                        "kind": "excluded_from_content_count",
                        "message": excluded_pages[physical],
                    }
                )

            pages.append(
                DocumentPage(
                    document_id=document_id,
                    source=source,
                    page=physical,
                    text=body,
                    metadata={
                        "section": current_section or "Front Matter",
                        "subsection": current_subsection,
                        "topic": current_topic,
                        "raw_chars": raw_len,
                        "body_font_size": derived_body_size,
                    },
                    blocks=kept,
                )
            )

    return pages, errors


def assign_document_metadata(
    pages: list[DocumentPage],
    *,
    title: str,
    document_type_by_section: dict[str, str] | None = None,
    language: str = "en",
    year: int = 2026,
    excluded_pages: dict[int, str] | None = None,
) -> None:
    """Fill the per-page metadata fields that depend on the whole document."""
    document_type_by_section = document_type_by_section or {}
    excluded_pages = dict(excluded_pages or {})
    for page in pages:
        section = page.metadata.get("section") or "Front Matter"
        page.metadata["document_type"] = document_type_by_section.get(section, "body_content")
        page.metadata["title"] = title
        page.metadata["language"] = language
        page.metadata["year"] = year
        page.metadata["is_content_page"] = page.page not in excluded_pages


def content_pages(pages: list[DocumentPage]) -> list[DocumentPage]:
    """The pages that count towards the corpus size."""
    return [p for p in pages if p.metadata.get("is_content_page")]


def build_manifest(
    *,
    document_id: str,
    title: str,
    source: str,
    pages: list[DocumentPage],
    errors: list[dict[str, Any]],
    publisher: str,
    license_name: str,
    license_url: str,
    accessed_at: str,
    doi: str | None = None,
    source_url: str | None = None,
    version: str | None = None,
) -> dict[str, None] | dict[str, Any]:
    """Assemble the data manifest that records what was indexed and why.

    The manifest is the audit trail for the corpus: it states the source, the
    licence, how many pages carry content, which pages were excluded and for
    what reason, and every extraction error encountered.
    """
    from collections import Counter

    counted = content_pages(pages)
    sections = Counter(p.metadata.get("section") or "Front Matter" for p in counted)
    excluded = {
        str(err["page"]): err["message"]
        for err in errors
        if err["kind"] == "excluded_from_content_count"
    }
    return {
        "manifest_version": "1.0",
        "sources": [
            {
                "source_id": document_id,
                "title": title,
                "source_path_or_url": source,
                "publisher": publisher,
                "license_or_permission": license_name,
                "license_url": license_url,
                "accessed_at": accessed_at,
                "doi": doi,
                "source_url": source_url,
                "version_or_snapshot": version,
                "language": "en",
                "physical_page_count": len(pages),
                "content_page_count": len(counted),
                "content_page_numbers": [p.page for p in counted],
                "excluded_pages": excluded,
                "total_normalized_chars": sum(len(p.text) for p in pages),
                "pages_by_section": dict(sections),
                "page_counting_rule": (
                    "A page counts as a content page if it carries substantive prose from "
                    "the report. Cover, half-title, table of contents and blank pages are "
                    "excluded; the excluded pages and the reason for each are listed under "
                    "excluded_pages. Front matter that carries real content (copyright page, "
                    "AI disclosure, foreword, acknowledgments, team bios) IS counted."
                ),
            }
        ],
        "ingestion_errors": errors,
    }
