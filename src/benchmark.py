"""Benchmark data contract and the metrics that score it.

Question types
--------------
Two kinds of question live in the same benchmark file, and they are scored
differently.

**Answerable** questions carry a ``gold`` list. Each entry is
``{source_id, page, evidence}`` and each entry is an *alternative* that is
sufficient on its own: the report restates its headline figures in the
Foreword, in Main Messages and again in the Overview chapter, so a question
may legitimately be answered from any of several pages. An entry counts as
matched only when the chunk is from that entry's own page *and* contains
that entry's own quote - pairing the page with its quote is what stops a
quote from one page being credited to a chunk from another.

**No-evidence** questions carry ``answerable: false`` and an
``absence_reason``. They are excluded from the retrieval metrics - a
retriever that fails to find evidence for a question whose answer is not in
the corpus has not made a mistake - but they are the only test of whether
the agent abstains.

Metrics
-------
``Hit@1``
    The top-1 chunk is a hit.
``Evidence Hit@3``
    At least one of the top-3 chunks is a hit.
``Context Evidence Rate``
    The final context sent to the model contains enough evidence to answer.
    This can be higher than Evidence Hit@3, because the context may be built
    from several retrieval rounds.
``Recall@k`` and ``MRR``
    Reported alongside, so that a method that surfaces the right chunk at
    rank 7 is not indistinguishable from one that never surfaces it.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from .textutil import evidence_present


class BenchmarkError(Exception):
    """Raised when the benchmark does not satisfy its own contract."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_questions(path: Path) -> list[dict[str, Any]]:
    """Load a benchmark file and check it against the contract."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("questions", [])
    if not isinstance(data, list) or not data:
        raise BenchmarkError("file câu hỏi phải là JSON array không rồng")
    validate_questions(data)
    return data


def validate_questions(questions: list[dict[str, Any]]) -> None:
    """Check the question list against the Day 7 data contract."""
    seen: set[str] = set()
    for question in questions:
        qid = question.get("id")
        if not isinstance(qid, str) or not qid.strip():
            raise BenchmarkError("question id phải là chuỗi không rồng")
        if qid in seen:
            raise BenchmarkError(f"question id trùng: {qid}")
        seen.add(qid)
        if question.get("split") not in ("dev", "test"):
            raise BenchmarkError(f"{qid}: split phải là 'dev' hoặc 'test'")
        if not isinstance(question.get("question"), str) or not question["question"].strip():
            raise BenchmarkError(f"{qid}: câu hỏi không hợp lệ")

        answerable = question.get("answerable", True)
        if not isinstance(answerable, bool):
            raise BenchmarkError(f"{qid}: answerable phải là boolean")
        if answerable:
            gold = question.get("gold")
            if not isinstance(gold, list) or not gold:
                raise BenchmarkError(f"{qid}: câu có đáp án cần gold không rồng")
            for entry in gold:
                for key in ("source_id", "page", "evidence"):
                    if key not in entry or entry[key] is None:
                        raise BenchmarkError(f"{qid}: gold thiếu {key}")
                if not isinstance(entry["evidence"], str) or not entry["evidence"].strip():
                    raise BenchmarkError(f"{qid}: gold.evidence phải là chuỗi không rồng")
            if not isinstance(question.get("expected_answer"), str) or not question[
                "expected_answer"
            ].strip():
                raise BenchmarkError(f"{qid}: expected_answer phải là chuỗi không rồng")
        else:
            if not isinstance(question.get("absence_reason"), str) or not question[
                "absence_reason"
            ].strip():
                raise BenchmarkError(f"{qid}: câu thiếu evidence cần absence_reason")


def split_questions(
    questions: list[dict[str, Any]], split: str
) -> list[dict[str, Any]]:
    """The questions of one split."""
    if split not in ("dev", "test"):
        raise BenchmarkError(f"split không hợp lệ: {split}")
    return [q for q in questions if q.get("split") == split]


def answerable_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The questions that carry gold evidence."""
    return [q for q in questions if q.get("answerable", True)]


# ---------------------------------------------------------------------------
# Scoring a ranking
# ---------------------------------------------------------------------------


def match_entry(chunk: dict[str, Any], entry: dict[str, Any]) -> bool:
    """True when the chunk carries this specific gold evidence.

    The chunk must be from the entry's page and must contain the entry's
    quote. A chunk on the right page that does not carry the quote is not a
    hit: it is on the right page but cannot answer the question.
    """
    page = chunk.get("page")
    metadata = chunk.get("metadata") or {}
    page_value = page if page is not None else metadata.get("page")
    if str(page_value) != str(entry.get("page")):
        return False
    source_id = chunk.get("source_id") or metadata.get("document_id")
    if entry.get("source_id") and source_id and source_id != entry["source_id"]:
        return False
    return evidence_present(chunk.get("text", ""), entry.get("evidence", ""))


def is_hit(chunk: dict[str, Any], question: dict[str, Any]) -> bool:
    """True when the chunk can answer the question on its own."""
    return any(match_entry(chunk, entry) for entry in question.get("gold", []))


def rank_of_first_hit(
    results: list[dict[str, Any]], question: dict[str, Any]
) -> int | None:
    """The 1-based rank of the first hit in the ranking, if any."""
    for index, chunk in enumerate(results, start=1):
        if is_hit(chunk, question):
            return index
    return None


def score_ranking(
    results: list[dict[str, Any]], question: dict[str, Any]
) -> dict[str, Any]:
    """Score one ranking against one question."""
    first = rank_of_first_hit(results, question)
    top3 = results[:3]
    return {
        "hit_at_1": first == 1,
        "evidence_hit_at_3": any(is_hit(chunk, question) for chunk in top3),
        "first_hit_rank": first,
        "reciprocal_rank": (1.0 / first) if first else 0.0,
    }


def score_context(
    context_ids: list[str],
    results: list[dict[str, Any]],
    question: dict[str, Any],
) -> bool:
    """True when the final context contains enough evidence to answer.

    The context is the set of chunk ids that were actually sent to the model.
    A question whose evidence is split across two chunks is satisfied only
    when every required chunk is present.
    """
    by_id = {chunk["id"]: chunk for chunk in results}
    present = [by_id[cid] for cid in context_ids if cid in by_id]
    if not present:
        return False
    combined = " ".join(chunk.get("text", "") for chunk in present)
    for entry in question.get("gold", []):
        if evidence_present(combined, entry.get("evidence", "")):
            return True
    return False


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def summarize(
    traces: list[dict[str, Any]], *, method: str, split: str
) -> dict[str, Any]:
    """Aggregate the per-question traces of one configuration and split."""
    scored = [t for t in traces if t.get("answerable", True)]
    n = len(scored)
    if not n:
        return {
            "method": method,
            "split": split,
            "n_questions": 0,
            "n_answerable": 0,
            "n_no_evidence": len(traces),
        }

    hit1 = sum(1 for t in scored if t.get("hit_at_1"))
    hit3 = sum(1 for t in scored if t.get("evidence_hit_at_3"))
    context_hit = sum(1 for t in scored if t.get("context_evidence"))
    ranks = [t["first_hit_rank"] for t in scored if t.get("first_hit_rank")]
    elapsed = [t["elapsed_ms"] for t in traces if t.get("elapsed_ms") is not None]
    context_chars = [t["context_chars"] for t in traces if t.get("context_chars") is not None]

    return {
        "method": method,
        "split": split,
        "n_questions": len(traces),
        "n_answerable": n,
        "n_no_evidence": len(traces) - n,
        "hit_at_1": hit1,
        "hit_at_1_pct": round(100 * hit1 / n, 1),
        "evidence_hit_at_3": hit3,
        "evidence_hit_at_3_pct": round(100 * hit3 / n, 1),
        "context_evidence_rate": context_hit,
        "context_evidence_rate_pct": round(100 * context_hit / n, 1),
        "recall_at_3_pct": round(100 * hit3 / n, 1),
        "mrr": round(statistics.mean(t["reciprocal_rank"] for t in scored), 4),
        "median_first_hit_rank": statistics.median(ranks) if ranks else None,
        "median_ms": round(statistics.median(elapsed), 1) if elapsed else None,
        "mean_ms": round(statistics.mean(elapsed), 1) if elapsed else None,
        "mean_context_chars": round(statistics.mean(context_chars), 1) if context_chars else None,
        "errors": sum(1 for t in traces if t.get("status") == "error"),
    }


def compare(summarys: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put several summaries side by side, keyed by method."""
    return sorted(summarys, key=lambda s: (-s.get("evidence_hit_at_3_pct", 0), s["method"]))
