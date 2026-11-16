"""Text normalization shared by ingestion, retrieval and gold matching.

Three different jobs live here, and they are deliberately kept apart:

* :func:`normalize_text` prepares extracted PDF text for indexing. It repairs
  typography (ligatures, dashes, quotes, exotic spaces) and collapses the
  line wrapping inside a paragraph, while keeping paragraph breaks.
* :func:`canon` produces the comparison form used to decide whether a gold
  evidence quote is present in a chunk. It is more aggressive than
  :func:`normalize_text` because it only needs to make two strings comparable,
  not presentable.
* :func:`tokenize` is the lexical analyzer used by the BM25 retriever.
"""

from __future__ import annotations

import re
import unicodedata

#: Typographic characters that PDF text extraction produces and that carry no
#: meaning once the text is plain.
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}

#: Every dash variant collapses to a plain hyphen-minus so that "upper-middle-
#: income" (a line-wrap artifact) matches "upper-middle-income".
_DASHES = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
}

#: Typographic quotes collapse to straight quotes.
_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
}

#: Exotic space characters (NBSP, thin space, ideographic space, ...) all
#: become a plain space.
_SPACES = {
    " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "　": " ", "\t": " ",
}

#: Zero-width and soft-hyphen characters carry no information for a corpus of
#: printed prose.
_INVISIBLE = "­‌‍﻿"


def normalize_text(text: str) -> str:
    """Normalize Unicode and whitespace without discarding paragraph structure.

    Line wrapping inside a paragraph becomes a single space. Blank lines are
    preserved as paragraph separators, so the structure-aware chunker can still
    see where a paragraph ends.
    """
    if not text:
        return ""
    for src, dst in _LIGATURES.items():
        text = text.replace(src, dst)
    for src, dst in _SPACES.items():
        text = text.replace(src, dst)
    for src, dst in _DASHES.items():
        text = text.replace(src, dst)
    for src, dst in _QUOTES.items():
        text = text.replace(src, dst)
    for char in _INVISIBLE:
        text = text.replace(char, "")
    # NFKC runs after the substitutions above so that any combination they
    # introduce is folded too.
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs).strip()


def canon(text: str) -> str:
    """Comparison form used to decide whether a gold quote occurs in a chunk.

    Lower-cases, unifies dashes, drops punctuation and collapses whitespace.
    This absorbs the PDF's line-wrap artifacts without weakening the
    requirement that the words actually be present.
    """
    if not text:
        return ""
    for src, dst in _DASHES.items():
        text = text.replace(src, dst)
    for src, dst in _LIGATURES.items():
        text = text.replace(src, dst)
    for src, dst in _QUOTES.items():
        text = text.replace(src, dst)
    for src, dst in _SPACES.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"[^0-9a-zA-Z\s%$.,:;\"'()\-/]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _squash(text: str) -> str:
    """Comparison form with hyphens and spacing removed.

    A PDF line wrap inside a hyphenated word ("upper-middle-\\nincome")
    becomes "upper-middle- income" once the newline is a space, and that
    never matches the unwrapped "upper-middle-income". Dropping hyphens and
    whitespace on both sides makes the two forms equal.
    """
    return re.sub(r"[-\s]+", "", canon(text))


def evidence_present(chunk_text: str, evidence_quote: str) -> bool:
    """True when the evidence quote occurs in the chunk text.

    Two tolerances are applied, both because a chunk boundary may cut a
    sentence in half: a trailing period is ignored, and a hyphenated word
    split across a line is compared with its hyphen and spacing removed.
    """
    quote = canon(evidence_quote)
    if not quote:
        return False
    haystack = canon(chunk_text)
    if quote in haystack:
        return True
    stripped = quote.rstrip(" .,:;")
    if stripped and stripped in haystack:
        return True
    squashed_quote = _squash(evidence_quote)
    return bool(squashed_quote) and squashed_quote in _squash(chunk_text)


#: Stop words for the lexical retriever. The corpus is English prose, so the
#: list is the standard English function-word list plus a handful of report
#: boilerplate words ("report", "figure") that appear in almost every chunk
#: and therefore carry no discriminative signal.
STOP_WORDS = frozenset("""
a about above after again against all am an and any are aren as at be because been
before being below between both but by can cannot could couldn did didn do does
doesn doing don down during each few for from further had hadn has hasn have haven
having he her here hers herself him himself his how i if in into is isn it its
itself let me more most mustn my myself no nor not of off on once only or other
ought our ours ourselves out over own same shan she should shouldn so some such
than that the their theirs them themselves then there these they this those through
to too under until up very was wasn we were weren what when where which while who
whom why with won would wouldn you your yours yourself yourselves
per cent figure figures report chapter page source note box panel table
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    """Split text into lowercase lexical tokens, dropping stop words.

    Numbers and hyphenated words survive as single tokens so that a figure
    like "52.8" or a term like "upper-middle-income" stays searchable as the
    unit the question actually uses.
    """
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text).casefold()
    return [t for t in _TOKEN_RE.findall(text) if t not in STOP_WORDS and len(t) > 1]
