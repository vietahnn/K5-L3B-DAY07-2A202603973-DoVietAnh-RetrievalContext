"""Data contracts for the Day 7 pipeline.

Every artifact that leaves the pipeline keeps enough provenance to be traced
back to a physical page of the source PDF. Nothing is dropped silently: a page
that yields no usable text is reported as an error record rather than
disappearing from the corpus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Metadata keys that the vector store can filter on. Chosen from the fields
#: the PDF itself supplies, so a filter is never a guess.
FILTERABLE_FIELDS = (
    "document_id",
    "source",
    "page",
    "section",
    "subsection",
    "topic",
    "document_type",
    "language",
    "year",
)


@dataclass
class DocumentPage:
    """One physical page of the source PDF."""

    document_id: str
    source: str
    page: int
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    blocks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source": self.source,
            "page": self.page,
            "text": self.text,
            "metadata": dict(self.metadata),
            "blocks": list(self.blocks),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DocumentPage":
        return cls(
            document_id=data["document_id"],
            source=data["source"],
            page=int(data["page"]),
            text=data.get("text", ""),
            metadata=dict(data.get("metadata") or {}),
            blocks=list(data.get("blocks") or []),
        )


@dataclass
class Chunk:
    """A retrievable unit of text, traceable to a page."""

    chunk_id: str
    document_id: str
    source: str
    page: int
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.chunk_id,
            "source_id": self.document_id,
            "source": self.source,
            "page": self.page,
            "text": self.text,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Chunk":
        return cls(
            chunk_id=data["id"],
            document_id=data["source_id"],
            source=data["source"],
            page=int(data["page"]),
            text=data.get("text", ""),
            metadata=dict(data.get("metadata") or {}),
        )
