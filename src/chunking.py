"""Turn pages into retrievable chunks.

Two chunking strategies over the same extracted pages.

``structure`` — the default. Blocks are grouped under the heading that
introduces them, and each chunk carries its heading as a prefix so that it is
self-describing when it appears alone in a top-k list. Chunk size is capped so
that a chunk plus its provenance header still fits inside the agent's context
budget; a chunk that cannot fit the budget is a chunk the agent cannot use.

``fixed`` — the control. Naive character windows with a fixed overlap. It
ignores paragraph and heading boundaries, which is exactly what makes it a
useful contrast against ``structure``.

Both emit the same :class:`Chunk` shape and the same metadata keys, so the
only thing that differs between them is where the boundaries fall.
"""

from __future__ import annotations

from .models import Chunk, DocumentPage

#: Maximum characters in a chunk body, excluding the heading prefix. Chosen so
#: that a chunk plus its ``[id | source | page N]`` header fits inside the
#: agent's context budget with room for a second chunk.
DEFAULT_MAX_CHARS = 700

#: Fixed-size strategy parameters.
DEFAULT_SIZE = 500
DEFAULT_OVERLAP = 50


def _page_metadata(page: DocumentPage) -> dict:
    """The metadata every chunk of a page carries."""
    meta = dict(page.metadata)
    meta.pop("raw_chars", None)
    meta.pop("is_content_page", None)
    return meta


def _chunk_id(strategy: str, document_id: str, page: int, index: int) -> str:
    return f"{strategy}:{document_id}:p{page:03d}:c{index:04d}"


# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------


def split_sentences(text: str) -> list[str]:
    """Split on sentence terminators, keeping the terminator with its sentence.

    Three cases are deliberately *not* a sentence boundary:

    * a period between two digits - it is a decimal point ("2.4 percentage
      points", "$8.30 a day");
    * a period inside a quoted title, where the closing quote follows the
      period;
    * a period followed by a lowercase letter or a digit with no space
      ("www.worldbank.org", "refer to fig. 1").
    """
    sentences: list[str] = []
    buffer = ""
    for index, char in enumerate(text):
        buffer += char
        if char not in ".!?":
            continue
        if char == ".":
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if previous.isdigit() and following.isdigit():
                continue
            if following in ".,;:!?'\"":
                continue
            if following and not following.isspace():
                continue
        stripped = buffer.strip()
        if stripped:
            sentences.append(stripped)
        buffer = ""
    remainder = buffer.strip()
    if remainder:
        sentences.append(remainder)
    return sentences or [text]


def _split_long_block(text: str, max_size: int) -> list[str]:
    """Split an oversized paragraph on sentence boundaries, then hard-wrap."""
    if len(text) <= max_size:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in split_sentences(text):
        if len(current) + len(sentence) + 1 <= max_size:
            current = f"{current} {sentence}".strip()
            continue
        if current:
            pieces.append(current)
        if len(sentence) <= max_size:
            current = sentence
        else:
            # A single sentence longer than the limit: wrap on word bounds.
            words = sentence.split()
            buffer = ""
            for word in words:
                if len(buffer) + len(word) + 1 > max_size:
                    pieces.append(buffer.strip())
                    buffer = word
                else:
                    buffer = f"{buffer} {word}".strip()
            current = buffer
    if current:
        pieces.append(current)
    return [p for p in pieces if p.strip()]


# ---------------------------------------------------------------------------
# Strategy: structure-aware
# ---------------------------------------------------------------------------


def _group_blocks(page: DocumentPage) -> list[dict]:
    """Group the page's blocks under the heading that introduces them.

    Returns a list of ``{"heading": str, "paragraphs": [str, ...]}`` groups in
    reading order. A sub-heading starts a new group; a topic label starts a
    new group; body blocks accumulate as paragraphs.

    The page's running head is used as the heading of groups that have no
    more specific one, so no text is dropped and every chunk says which
    section it came from.
    """
    groups: list[dict] = []
    current: dict = {"heading": "", "paragraphs": []}
    running_heads: list[str] = []
    block_index = 0
    blocks = page.blocks

    def flush() -> None:
        nonlocal current
        if current["paragraphs"]:
            groups.append(current)
        current = {"heading": "", "paragraphs": []}

    for block in blocks:
        kind = block["kind"]
        text = block["text"]
        if kind == "running_head":
            running_heads.append(text)
            continue
        if kind in ("subheading", "topic_label"):
            flush()
            current["heading"] = text
            # A heading that carries no body of its own (a cover title, a
            # section opener) still holds text that must not be dropped.
            if not any(b["kind"] == "body" for b in blocks[block_index + 1 :]):
                current["paragraphs"].append(text)
            block_index += 1
            continue
        # Body: a block may hold several paragraphs separated by blank lines.
        # They stay together in one unit so that a chunk never splits a
        # multi-paragraph passage across two chunks.
        paragraphs = [para.strip() for para in text.split("\n\n") if para.strip()]
        if paragraphs:
            current["paragraphs"].append("\n\n".join(paragraphs))
        block_index += 1
    flush()

    running_head = " ".join(running_heads)
    for group in groups:
        if not group["heading"]:
            group["heading"] = running_head
    return groups


def structure_chunks(
    page: DocumentPage, max_chars: int = DEFAULT_MAX_CHARS
) -> list[Chunk]:
    """Group paragraphs under their heading, respecting a size limit.

    Oversized paragraphs are split on sentence boundaries; a chunk's text is
    prefixed with its heading so the chunk carries its own context.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    groups = _group_blocks(page)
    if not groups:
        return []

    chunks: list[Chunk] = []
    index = 0
    for group in groups:
        heading = group["heading"]
        # The heading itself eats into the budget of the chunk it introduces.
        budget = max_chars - (len(heading) + 2 if heading else 0)
        if budget < 50:
            budget = max_chars

        current_parts: list[str] = []
        current_len = 0
        pending: list[str] = []
        for para in group["paragraphs"]:
            for piece in _split_long_block(para, budget):
                pending.append(piece)
                current_len += len(piece) + 1
                if current_len >= budget:
                    current_parts.extend(pending)
                    pending = []
                    current_len = 0
        if pending:
            current_parts.extend(pending)

        for part in current_parts:
            body = part.strip()
            if not body:
                continue
            text = f"{heading}\n{body}" if heading else body
            meta = _page_metadata(page)
            meta.update(
                {
                    "chunking": "structure",
                    "max_chars": max_chars,
                    "heading": heading or None,
                }
            )
            chunks.append(
                Chunk(
                    chunk_id=_chunk_id("S", page.document_id, page.page, index),
                    document_id=page.document_id,
                    source=page.source,
                    page=page.page,
                    text=text,
                    metadata=meta,
                )
            )
            index += 1
    return chunks


# ---------------------------------------------------------------------------
# Strategy: fixed-size
# ---------------------------------------------------------------------------


def fixed_chunks(
    page: DocumentPage, size: int = DEFAULT_SIZE, overlap: int = DEFAULT_OVERLAP
) -> list[Chunk]:
    """Split a page's text into overlapping fixed-width character windows."""
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must satisfy 0 <= overlap < size")

    text = page.text.strip()
    if not text:
        return []

    chunks: list[Chunk] = []
    step = size - overlap
    start = 0
    index = 0
    while start < len(text):
        window = text[start : start + size].strip()
        # A trailing window shorter than half the size carries almost no
        # context, so it is merged into the previous chunk instead of being
        # emitted as a near-empty fragment.
        if len(window) < size // 2 and chunks:
            previous = chunks[-1]
            previous.text = f"{previous.text} {window}".strip()
            previous.metadata["char_end"] = min(start + size, len(text))
            previous.metadata["trailing_merged"] = True
        elif window:
            meta = _page_metadata(page)
            meta.update(
                {
                    "chunking": "fixed",
                    "chunk_size": size,
                    "overlap": overlap,
                    "char_start": start,
                    "char_end": min(start + size, len(text)),
                }
            )
            chunks.append(
                Chunk(
                    chunk_id=_chunk_id("F", page.document_id, page.page, index),
                    document_id=page.document_id,
                    source=page.source,
                    page=page.page,
                    text=window,
                    metadata=meta,
                )
            )
            index += 1
        if start + size >= len(text):
            break
        start += step
    return chunks


def build_chunks(
    pages: list[DocumentPage],
    strategy: str = "structure",
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    size: int = DEFAULT_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Build the chunk list for one chunking strategy."""
    chunks: list[Chunk] = []
    for page in pages:
        if strategy == "structure":
            chunks.extend(structure_chunks(page, max_chars))
        elif strategy == "fixed":
            chunks.extend(fixed_chunks(page, size, overlap))
        else:
            raise ValueError(f"unknown chunking strategy: {strategy}")
    return chunks
